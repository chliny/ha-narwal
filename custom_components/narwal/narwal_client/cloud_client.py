"""Async Narwal cloud MQTT5 transport."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from aiohttp import ClientSession
from paho.mqtt import MQTTException

from .client import (
    NarwalAuthenticationError,
    NarwalClient,
    NarwalCommandError,
    NarwalConnectionError,
)
from .cloud_api import (
    CloudDevice,
    NarwalCloudApi,
    NarwalCloudAuthError,
    NarwalCloudError,
)
from .const import COMMAND_RESPONSE_TIMEOUT, TOPIC_CMD_ACTIVE_ROBOT
from .models import CommandResponse

_LOGGER = logging.getLogger(__name__)

_CLOUD_REACHABILITY_TIMEOUT = 150.0
_ACTIVE_PUBLISH_TIMEOUTS = (2, 2, 4, 4, 8, 8, 16, 16, 32, 60)
_ACTIVE_PUBLISH_DEFAULT_DURATION_MS = 60_000
_TASK_RECORD_CACHE_LIMIT = 60

_COMMON_THEMES = (
    "status/robot_base_status",
    "status/working_status",
    "report/clean_report",
    "report/explorer_report",
    "report/maintain_floor_report",
    "status/point_navi_plan_traj",
    "map/display_map",
    "status/time_line_status",
    "develop/debug/robot_to_app",
    "status/streaming_info",
    "status/streaming_active_event",
    "general/topic",
)


def _varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _read_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _string_field(number: int, value: str) -> bytes:
    return _bytes_field(number, value.encode())


def _varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _protobuf_fields(data: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 7
        value: int | bytes
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value = data[offset:end]
            offset = end
        elif wire_type == 1:
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 5:
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(number, []).append(value)
    return fields


def _service_envelope(user_uuid: str, payload: bytes) -> bytes:
    header = _string_field(1, user_uuid) + _string_field(2, user_uuid)
    # The business endpoint is the MQTT topic. The binary body contains only
    # the protocol header followed by the endpoint's raw protobuf payload.
    return b"\x01" + _varint(len(header)) + header + payload


def _decode_envelope(data: bytes) -> tuple[str, bytes]:
    if not data or data[0] != 1:
        return "", data
    header_length, offset = _read_varint(data, 1)
    offset += header_length
    if offset > len(data):
        raise ValueError("truncated Narwal envelope")
    payload = data[offset:]
    # Only general/topic multiplexes an inner URL. Direct service responses
    # and broadcasts carry the endpoint protobuf immediately after the header.
    try:
        fields = _protobuf_fields(payload)
        raw_url = fields.get(1, [b""])[0]
        raw_payload = fields.get(2, [b""])[0]
    except ValueError:
        return "", payload
    if isinstance(raw_url, bytes) and raw_url.startswith(b"/"):
        return (
            raw_url.decode(errors="replace"),
            bytes(raw_payload) if isinstance(raw_payload, bytes) else b"",
        )
    return "", payload


def _correlation_data(message_id: str) -> bytes:
    # AppMessageMark: messageId=1, timestamp=4 (microseconds since epoch).
    return _string_field(1, message_id) + _varint_field(4, time.time_ns() // 1_000)


def _correlation_id(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        value = _protobuf_fields(bytes(data)).get(1, [b""])[0]
    except ValueError:
        return ""
    return bytes(value).decode(errors="replace") if isinstance(value, bytes) else ""


def _connect_start_timestamp() -> str:
    """Return the app-compatible MQTT session start time in milliseconds."""
    return str(int(time.time() * 1_000))


async def _async_configure_tls(client: mqtt.Client) -> None:
    """Configure MQTT TLS without loading CA certificates on the event loop."""
    await asyncio.to_thread(client.tls_set, cert_reqs=ssl.CERT_REQUIRED)


def _task_record_id(record: dict[str, Any]) -> str | None:
    """Return the stable identifier shared by task-record response variants."""
    value = (
        record.get("clean_record_id")
        or record.get("record_id")
        or record.get("report_id")
        or record.get("id")
    )
    return str(value) if value is not None else None


def _merge_task_records(
    fresh: list[dict[str, Any]],
    cached: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Merge the newest page with the bounded multi-page cache."""
    merged: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    for record in (*fresh, *cached):
        record_id = _task_record_id(record)
        if record_id is not None:
            if record_id in known_ids:
                continue
            known_ids.add(record_id)
        merged.append(record)
        if len(merged) >= limit:
            break
    return merged


class NarwalCloudClient(NarwalClient):
    """Narwal client using the app's cloud MQTT5 transport."""

    def __init__(
        self,
        session: ClientSession,
        *,
        phone: str,
        password: str,
        device_id: str,
        product_id: str,
        area_code: str = "86",
        country_code: str = "CN",
    ) -> None:
        super().__init__(
            device_id=device_id,
            product_id=product_id,
        )
        self._api = NarwalCloudApi(
            session,
            phone=phone,
            password=password,
            area_code=area_code,
            country_code=country_code,
        )
        self._mqtt: mqtt.Client | None = None
        self._mqtt_ready = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[bytes]]] = {}
        self._disconnect_waiter = asyncio.Event()
        self._connect_error: Exception | None = None
        self._subscribe_mid: int | None = None
        self._response_topics: set[str] = set()
        self._activation_task: asyncio.Task[None] | None = None
        self.cloud_device: CloudDevice | None = None

    @property
    def connected(self) -> bool:
        """Return whether the cloud broker accepted the MQTT connection."""
        return self._mqtt is not None and self._mqtt_ready.is_set()

    @property
    def robot_awake(self) -> bool:
        """Return whether the robot recently sent a response or broadcast."""
        return (
            self._robot_awake
            and self._last_broadcast_time > 0
            and self.last_broadcast_age <= _CLOUD_REACHABILITY_TIMEOUT
        )

    async def get_cloud_metadata(self, *, task_record_pages: int = 1) -> None:
        """Refresh consumable lifetimes and device notifications."""
        device = self.cloud_device
        if device is None:
            raise NarwalConnectionError("Cloud device metadata is unavailable")
        task_records_map_id = self.state.current_map_id
        if task_records_map_id != self.state.task_records_map_id:
            # Populate multiple pages immediately after setup or a map switch.
            task_record_pages = max(task_record_pages, 3)
        (
            consumables,
            notifications,
            task_records,
            dust_bag_health,
            dust_bag_health_current,
        ) = await asyncio.gather(
            self._api.consumables(
                device_id=device.device_id,
                product_id=device.product_id,
            ),
            self._api.notifications(
                device_id=device.device_id,
                product_id=device.product_id,
            ),
            self._api.task_records(
                device_id=device.device_id,
                product_id=device.product_id,
                map_id=task_records_map_id,
                pages=task_record_pages,
                record_limit=_TASK_RECORD_CACHE_LIMIT,
            ),
            self._api.dust_bag_health(
                device_id=device.device_id,
                product_id=device.product_id,
            ),
            self._api.dust_bag_health_current(
                device_id=device.device_id,
                product_id=device.product_id,
            ),
            return_exceptions=True,
        )
        errors: list[BaseException] = []
        if isinstance(consumables, BaseException):
            errors.append(consumables)
            _LOGGER.debug(
                "Narwal consumable refresh failed: %s",
                consumables,
            )
        else:
            self.state.consumables = {item.key: item for item in consumables}
        if isinstance(notifications, BaseException):
            errors.append(notifications)
            _LOGGER.debug(
                "Narwal notification refresh failed: %s",
                notifications,
            )
        else:
            self.state.notifications = notifications
        if isinstance(task_records, BaseException):
            errors.append(task_records)
            _LOGGER.debug("Narwal task record refresh failed: %s", task_records)
        else:
            self.state.task_records_supported = True
            cached_records = (
                self.state.task_records
                if (
                    task_record_pages == 1 and self.state.task_records_map_id == task_records_map_id
                )
                else []
            )
            self.state.task_records = _merge_task_records(
                task_records,
                cached_records,
                limit=_TASK_RECORD_CACHE_LIMIT,
            )
            self.state.task_records_map_id = task_records_map_id
            self.state.latest_task_record = (
                self.state.task_records[0] if self.state.task_records else None
            )
            self.state.task_record_details = await self._get_task_record_details(
                device,
                self.state.task_records[:5],
                self.state.task_record_details,
            )
        if isinstance(dust_bag_health, BaseException):
            errors.append(dust_bag_health)
            _LOGGER.debug("Narwal dust-bag health refresh failed: %s", dust_bag_health)
        else:
            self.state.dust_bag_health_supported = True
            self.state.dust_bag_health = dust_bag_health
        if isinstance(dust_bag_health_current, BaseException):
            errors.append(dust_bag_health_current)
            _LOGGER.debug(
                "Narwal current dust-bag health refresh failed: %s",
                dust_bag_health_current,
            )
        else:
            self.state.dust_bag_health_supported = True
            self.state.dust_bag_health_current = dust_bag_health_current or {}
        if any(isinstance(error, NarwalCloudAuthError) for error in errors):
            raise NarwalAuthenticationError("Narwal cloud credentials were rejected")
        if len(errors) == 5:
            raise NarwalConnectionError("Cannot refresh Narwal cloud metadata") from errors[0]

    async def get_firmware_update(self) -> None:
        """Refresh firmware availability without exposing an OTA action."""
        device = self.cloud_device
        firmware_version = self.state.firmware_version or (
            device.firmware_version if device else ""
        )
        if device is None or not firmware_version:
            return
        try:
            info = await self._api.firmware_update(
                device_id=device.device_id,
                product_id=device.product_id,
                firmware_version=firmware_version,
            )
        except NarwalCloudAuthError as err:
            raise NarwalAuthenticationError("Narwal cloud credentials were rejected") from err
        except NarwalCloudError as err:
            raise NarwalConnectionError("Cannot refresh Narwal firmware availability") from err
        latest_version = str(info.get("latest_version") or firmware_version).strip()
        self.state.firmware_target = latest_version
        self.state.firmware_update_info = info
        self.state.firmware_update_supported = True

    async def get_voice_packs(self, app_language_code: str = "zh-CN") -> None:
        """Refresh the official voice-pack catalog for the active language."""
        device = self.cloud_device
        language = self.state.current_voice.get("language", "")
        if device is None or not language:
            return
        packs = await self._api.voice_packs(
            device_id=device.device_id,
            product_id=device.product_id,
            device_language_code=language,
            app_language_code=app_language_code,
        )
        self.state.voice_packs = packs
        self.state.voice_packs_supported = bool(packs)

    async def _get_task_record_details(
        self,
        device: CloudDevice,
        records: list[dict[str, object]],
        cached: dict[str, dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        """Fetch a bounded set of task details for HA attributes."""
        ids = [
            str(value)
            for record in records
            if (
                value := (
                    record.get("clean_record_id")
                    or record.get("record_id")
                    or record.get("report_id")
                    or record.get("id")
                )
            )
            is not None
        ]
        missing_ids = [record_id for record_id in ids if record_id not in cached]
        results = await asyncio.gather(
            *(
                self._api.task_record_detail(
                    device_id=device.device_id,
                    product_id=device.product_id,
                    record_id=record_id,
                )
                for record_id in missing_ids
            ),
            return_exceptions=True,
        )
        fetched = {
            record_id: detail
            for record_id, detail in zip(missing_ids, results, strict=False)
            if isinstance(detail, dict)
        }
        return {
            record_id: detail
            for record_id in ids
            if (detail := fetched.get(record_id) or cached.get(record_id)) is not None
        }

    async def connect(self) -> None:
        """Login, discover the broker, and open MQTT5 over TLS."""
        if self.connected:
            return
        await self._async_discard_stale_mqtt()
        self._loop = asyncio.get_running_loop()
        self._mqtt_ready.clear()
        self._disconnect_waiter.clear()
        self._connect_error = None
        self._response_topics.clear()
        try:
            account = await self._api.login()
            devices = await self._api.devices()
            self.cloud_device = next(
                (item for item in devices if item.device_id == self.device_id),
                None,
            )
            if self.cloud_device is None:
                raise NarwalConnectionError("Configured robot is no longer bound to this account")
            broker_url = await self._api.broker()
        except NarwalCloudAuthError as err:
            raise NarwalAuthenticationError("Narwal cloud credentials were rejected") from err
        except NarwalCloudError as err:
            raise NarwalConnectionError("Cannot connect to the Narwal cloud") from err

        parsed = urlparse(broker_url)
        host = parsed.hostname or broker_url.removeprefix("mqtts://")
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"app_{account.uuid}_{uuid.uuid4()}",
            protocol=mqtt.MQTTv5,
        )
        client.username_pw_set(account.uuid, account.token)
        await _async_configure_tls(client)
        client.on_connect = self._on_mqtt_connect
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_message = self._on_mqtt_message
        client.on_subscribe = self._on_mqtt_subscribe
        self._mqtt = client

        properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.ReceiveMaximum = 128
        properties.MaximumPacketSize = 1_048_576
        properties.UserProperty = [("startTimestamp", _connect_start_timestamp())]
        try:
            await asyncio.to_thread(
                client.connect,
                host=host,
                port=8883,
                keepalive=15,
                clean_start=True,
                properties=properties,
            )
            client.loop_start()
            await asyncio.wait_for(self._mqtt_ready.wait(), timeout=20)
            if self._connect_error is not None:
                raise self._connect_error
        except (
            OSError,
            TimeoutError,
            MQTTException,
            NarwalConnectionError,
        ) as err:
            client.loop_stop()
            self._mqtt = None
            self._mqtt_ready.clear()
            if isinstance(err, NarwalConnectionError):
                raise
            raise NarwalConnectionError("Cannot connect to the Narwal MQTT broker") from err

        # The app starts a response-confirmed active-publish loop immediately.
        await self.subscribe_to_topics()

    def _on_mqtt_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        loop = self._loop
        if loop is None:
            return
        if int(reason_code.value) != 0:
            loop.call_soon_threadsafe(
                self._fail_connect,
                NarwalConnectionError(f"MQTT rejected connection ({reason_code.value})"),
            )
            return
        prefix = f"{self.topic_prefix}/{self.device_id}"
        topics = [(f"{prefix}/{topic}", mqtt.SubscribeOptions(qos=0)) for topic in _COMMON_THEMES]
        result, mid = client.subscribe(topics)
        if result != mqtt.MQTT_ERR_SUCCESS:
            loop.call_soon_threadsafe(
                self._fail_connect,
                NarwalConnectionError(f"MQTT subscribe failed ({result})"),
            )
            return
        self._subscribe_mid = mid

    def _on_mqtt_subscribe(
        self,
        _client: mqtt.Client,
        _userdata: object,
        mid: int,
        reason_codes: list[mqtt.ReasonCode],
        _properties: mqtt.Properties | None,
    ) -> None:
        """Mark the transport ready only after all subscriptions are accepted."""
        loop = self._loop
        if loop is None or mid != self._subscribe_mid:
            return
        rejected = [int(code.value) for code in reason_codes if int(code.value) >= 128]
        if rejected:
            loop.call_soon_threadsafe(
                self._fail_connect,
                NarwalConnectionError(f"MQTT subscriptions rejected ({rejected[0]})"),
            )
            return
        loop.call_soon_threadsafe(self._mqtt_ready.set)

    def _fail_connect(self, error: Exception) -> None:
        self._connect_error = error
        for _response_topic, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._mqtt_ready.set()

    def _on_mqtt_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _disconnect_flags: mqtt.DisconnectFlags,
        _reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._mark_disconnected)

    def _mark_disconnected(self) -> None:
        self._mqtt_ready.clear()
        self._subscribe_mid = None
        self._response_topics.clear()
        self._robot_awake = False
        activation_task = self._activation_task
        self._activation_task = None
        if activation_task is not None:
            activation_task.cancel()
        self._disconnect_waiter.set()
        error = NarwalConnectionError("Narwal cloud MQTT connection closed")
        for _response_topic, future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    def _on_mqtt_message(
        self,
        _client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        loop = self._loop
        if loop is None:
            return
        raw = bytes(message.payload)
        if message.topic.endswith("/response"):
            correlation = _correlation_id(getattr(message.properties, "CorrelationData", None))
            loop.call_soon_threadsafe(
                self._deliver_response,
                correlation,
                message.topic,
                raw,
            )
            return
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._handle_cloud_broadcast(message.topic, raw))
        )

    def _deliver_response(
        self,
        correlation: str,
        response_topic: str,
        raw: bytes,
    ) -> None:
        self._mark_robot_reachable()
        pending = self._pending.get(correlation)
        future = pending[1] if pending is not None and pending[0] == response_topic else None
        if future is None:
            topic_futures = [
                pending_future
                for pending_topic, pending_future in self._pending.values()
                if pending_topic == response_topic
            ]
            if len(topic_futures) == 1:
                future = topic_futures[0]
        if future is not None and not future.done():
            future.set_result(raw)

    def _mark_robot_reachable(self) -> None:
        """Record a robot-originated message independently of broker state."""
        was_reachable = self.robot_awake
        self._last_broadcast_time = time.monotonic()
        self._robot_awake = True
        if not was_reachable and self.on_state_update is not None:
            self.on_state_update(self.state)

    async def _handle_cloud_broadcast(self, topic: str, raw: bytes) -> None:
        was_reachable = self.robot_awake
        try:
            inner_url, payload = _decode_envelope(raw)
        except ValueError:
            _LOGGER.debug("Discarding malformed Narwal cloud broadcast")
            return
        prefix = f"{self.topic_prefix}/{self.device_id}/"
        short_topic = topic.removeprefix(prefix)
        if short_topic == "general/topic" and inner_url:
            short_topic = inner_url.lstrip("/")
        handled = await self._handle_broadcast(short_topic, payload)
        if not handled and not was_reachable and self.on_state_update is not None:
            self.on_state_update(self.state)

    @classmethod
    def _build_cloud_active_payload(cls) -> bytes:
        durations = (
            _varint_field(1, 2_000)
            + _varint_field(2, 2_000)
            + _varint_field(3, 2_000)
            + _varint_field(8, 0)
        )
        return (
            _bytes_field(1, durations)
            + _varint_field(2, _ACTIVE_PUBLISH_DEFAULT_DURATION_MS)
            + _varint_field(3, 0)
        )

    @staticmethod
    def _active_renewal_delay(raw: bytes) -> float:
        """Return half the publication lease, matching the Android app."""
        try:
            _inner_url, payload = _decode_envelope(raw)
            fields = _protobuf_fields(payload)
            duration = fields.get(2, [_ACTIVE_PUBLISH_DEFAULT_DURATION_MS])[0]
            if not isinstance(duration, int) or duration <= 0:
                raise ValueError("invalid active-publish duration")
        except ValueError:
            duration = _ACTIVE_PUBLISH_DEFAULT_DURATION_MS
        return max(1.0, duration / 2_000)

    async def _activation_loop(self) -> None:
        """Confirm and renew robot status publication using the app backoff."""
        backoff_index = 0
        while self.connected and self._should_reconnect:
            timeout = _ACTIVE_PUBLISH_TIMEOUTS[
                min(backoff_index, len(_ACTIVE_PUBLISH_TIMEOUTS) - 1)
            ]
            try:
                raw = await asyncio.wait_for(
                    self._publish_request(
                        TOPIC_CMD_ACTIVE_ROBOT,
                        self._build_cloud_active_payload(),
                        expect_response=True,
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                backoff_index += 1
                _LOGGER.debug(
                    "Narwal active-publish request timed out after %ds",
                    timeout,
                )
                continue
            except NarwalConnectionError:
                return
            if raw is None:
                backoff_index += 1
                continue
            backoff_index = 0
            await asyncio.sleep(self._active_renewal_delay(raw))

    async def _publish_request(
        self,
        short_topic: str,
        payload: bytes,
        *,
        expect_response: bool,
    ) -> bytes | None:
        client = self._mqtt
        account = self._api.account
        if client is None or account is None or not self.connected:
            raise NarwalConnectionError("Not connected to the Narwal cloud")
        request_topic = f"{self.topic_prefix}/{self.device_id}/{short_topic.lstrip('/')}"
        response_topic = f"{request_topic}/response"
        if response_topic not in self._response_topics:
            result, _mid = client.subscribe(
                response_topic,
                options=mqtt.SubscribeOptions(qos=0),
            )
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise NarwalConnectionError(f"MQTT response subscription failed ({result})")
            self._response_topics.add(response_topic)
        message_id = str(uuid.uuid4())
        properties = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
        properties.ResponseTopic = response_topic
        properties.CorrelationData = _correlation_data(message_id)
        future: asyncio.Future[bytes] | None = None
        if expect_response:
            future = asyncio.get_running_loop().create_future()
            self._pending[message_id] = (response_topic, future)
        info = client.publish(
            request_topic,
            _service_envelope(account.uuid, payload),
            qos=0,
            properties=properties,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self._pending.pop(message_id, None)
            raise NarwalConnectionError(f"MQTT publish failed ({info.rc})")
        if future is None:
            return None
        try:
            return await future
        finally:
            self._pending.pop(message_id, None)

    async def send_command(
        self,
        short_topic: str,
        payload: bytes = b"",
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
    ) -> CommandResponse:
        """Send a protobuf service request through cloud MQTT."""
        async with self._command_lock:
            try:
                raw = await asyncio.wait_for(
                    self._publish_request(
                        short_topic,
                        payload,
                        expect_response=True,
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                raise NarwalCommandError(
                    f"No response for command '{short_topic}' within {timeout}s"
                ) from None
        if raw is None:
            raise NarwalCommandError(f"Empty response for command '{short_topic}'")
        try:
            _inner_url, response_payload = _decode_envelope(raw)
            decoded = self._decode_protobuf(response_payload)
        except Exception:
            response_payload = b""
            decoded = {}
        raw_field1 = decoded.get("1", 0)
        try:
            result_code = int(raw_field1)
        except (ValueError, TypeError):
            result_code = 1
        return CommandResponse(
            result_code=result_code,
            data=decoded,
            raw_payload=response_payload,
        )

    async def subscribe_to_topics(self, duration: int = 600) -> None:
        """Ensure the response-confirmed cloud publication loop is running."""
        del duration
        if self._activation_task is None or self._activation_task.done():
            self._activation_task = asyncio.create_task(
                self._activation_loop(),
                name="narwal-cloud-active-publish",
            )

    async def start_listening(self) -> None:
        """Keep the MQTT transport active and reconnect when needed."""
        while self._should_reconnect:
            if not self.connected:
                try:
                    await self.connect()
                except NarwalAuthenticationError:
                    raise
                except NarwalConnectionError:
                    await asyncio.sleep(5)
                    continue
            await self.subscribe_to_topics()
            await self._disconnect_waiter.wait()
            self._disconnect_waiter.clear()
            await self._async_discard_stale_mqtt()

    async def _async_discard_stale_mqtt(self) -> None:
        """Stop and discard an MQTT network loop after a lost connection."""
        client = self._mqtt
        if client is None or self._mqtt_ready.is_set():
            return
        self._mqtt = None
        await asyncio.to_thread(client.loop_stop)

    async def disconnect(self) -> None:
        """Close MQTT without blocking Home Assistant's event loop."""
        self._should_reconnect = False
        activation_task = self._activation_task
        self._activation_task = None
        if activation_task is not None:
            activation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await activation_task
        client = self._mqtt
        self._mqtt = None
        self._mqtt_ready.clear()
        if client is not None:
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
