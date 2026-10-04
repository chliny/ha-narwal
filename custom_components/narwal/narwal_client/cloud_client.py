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

from . import const
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
from .const import (
    COMMAND_RESPONSE_TIMEOUT,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_GET_BATTERY_INFO,
    TOPIC_CMD_GET_CLEAN_PROGRESS,
    TOPIC_CMD_GET_CONSUMABLE_INFO,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_FEATURE_LIST,
    CommandResult,
)
from .models import CommandResponse

_LOGGER = logging.getLogger(__name__)

_CLOUD_REACHABILITY_TIMEOUT = 150.0
_ACTIVE_PUBLISH_TIMEOUTS = (2, 2, 4, 4, 8, 8, 16, 16, 32, 60)
_ACTIVE_PUBLISH_DEFAULT_DURATION_MS = 60_000
_TASK_RECORD_CACHE_LIMIT = 60
_DEFAULT_MQTT_PORT = 8883
_MQTT_AUTH_FAILURE_REASONS = {0x86, 0x87}

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
    "status/download_status",
    "upgrade/upgrade_status",
    "general/topic",
)


def _parse_broker_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse a TLS MQTT endpoint returned by the cloud API."""
    if not isinstance(endpoint, str) or not (endpoint := endpoint.strip()):
        raise ValueError("empty MQTT broker endpoint")

    if "://" in endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme.casefold() != "mqtts":
            raise ValueError("MQTT broker endpoint must use TLS")
    else:
        parsed = urlparse(f"mqtts://{endpoint}")

    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.hostname)
    ):
        raise ValueError("invalid MQTT broker endpoint")
    if parsed.netloc.endswith(":"):
        raise ValueError("empty MQTT broker port")

    try:
        parsed_port = parsed.port
    except ValueError as err:
        raise ValueError("invalid MQTT broker port") from err
    if parsed_port is not None and not 1 <= parsed_port <= 65_535:
        raise ValueError("invalid MQTT broker port")
    return parsed.hostname, parsed_port or _DEFAULT_MQTT_PORT


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
        if shift == 63 and byte > 1:
            raise ValueError("protobuf varint exceeds 64 bits")
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
        if not 1 <= number < (1 << 29):
            raise ValueError("invalid protobuf field number")
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
            if offset + 8 > len(data):
                raise ValueError("truncated protobuf fixed64")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated protobuf fixed32")
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


def _decode_envelope(data: bytes) -> bytes:
    """Unframe a service message, retaining support for raw protobuf bodies."""
    if not data or data[0] != 1:
        _protobuf_fields(data)
        return data
    header_length, offset = _read_varint(data, 1)
    end = offset + header_length
    if end > len(data):
        raise ValueError("truncated Narwal envelope")
    _protobuf_fields(data[offset:end])
    payload = data[end:]
    _protobuf_fields(payload)
    return payload


def _decode_topic(payload: bytes) -> tuple[str, bytes]:
    """Decode mqtt.msg.Topic only for the general/topic outer topic."""
    fields = _protobuf_fields(payload)
    urls = fields.get(1, [])
    bodies = fields.get(2, [])
    if (
        len(urls) != 1
        or not isinstance(urls[0], bytes)
        or not urls[0].startswith(b"/")
        or len(bodies) != 1
        or not isinstance(bodies[0], bytes)
    ):
        raise ValueError("invalid Narwal multiplex topic")
    _protobuf_fields(bodies[0])
    return urls[0].decode("utf-8"), bodies[0]


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
        self._subscriptions: dict[int, asyncio.Future[None]] = {}
        self._response_topics: set[str] = set()
        self._activation_task: asyncio.Task[None] | None = None
        self._broadcast_tasks: set[asyncio.Task[None]] = set()
        self._accept_mqtt_messages = False
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
        if any(isinstance(result, NarwalCloudAuthError) for result in results):
            raise NarwalAuthenticationError("Narwal cloud credentials were rejected")
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

        try:
            host, port = _parse_broker_endpoint(broker_url)
        except ValueError as err:
            raise NarwalConnectionError("Narwal returned an invalid MQTT broker endpoint") from err
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
        self._accept_mqtt_messages = True

        properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.ReceiveMaximum = 128
        properties.MaximumPacketSize = 1_048_576
        properties.UserProperty = [("startTimestamp", _connect_start_timestamp())]
        try:
            await asyncio.to_thread(
                client.connect,
                host=host,
                port=port,
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
            self._accept_mqtt_messages = False
            self._loop = None
            await asyncio.to_thread(client.loop_stop)
            await self._async_cancel_broadcast_tasks()
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
        if loop is None or client is not self._mqtt:
            return
        reason_value = int(reason_code.value)
        if reason_value != 0:
            error_type = (
                NarwalAuthenticationError
                if reason_value in _MQTT_AUTH_FAILURE_REASONS
                else NarwalConnectionError
            )
            loop.call_soon_threadsafe(
                self._fail_connect,
                error_type(f"MQTT rejected connection ({reason_value})"),
                client,
            )
            return
        prefix = f"{self.topic_prefix}/{self.device_id}"
        topics = [(f"{prefix}/{topic}", mqtt.SubscribeOptions(qos=0)) for topic in _COMMON_THEMES]
        result, mid = client.subscribe(topics)
        if client is not self._mqtt:
            return
        if result != mqtt.MQTT_ERR_SUCCESS:
            loop.call_soon_threadsafe(
                self._fail_connect,
                NarwalConnectionError(f"MQTT subscribe failed ({result})"),
                client,
            )
            return
        self._subscribe_mid = mid

    def _on_mqtt_subscribe(
        self,
        client: mqtt.Client,
        _userdata: object,
        mid: int,
        reason_codes: list[mqtt.ReasonCode],
        _properties: mqtt.Properties | None,
    ) -> None:
        """Mark the transport ready only after all subscriptions are accepted."""
        loop = self._loop
        if loop is None or client is not self._mqtt:
            return
        rejected = [int(code.value) for code in reason_codes if int(code.value) >= 128]
        error = (
            NarwalConnectionError("MQTT subscriptions rejected or missing SUBACK reasons")
            if rejected or not reason_codes
            else None
        )
        loop.call_soon_threadsafe(self._mark_subscribed, client, mid, error)

    def _mark_subscribed(
        self, source_client: mqtt.Client, mid: int, error: Exception | None = None
    ) -> None:
        """Mark only the current client's matching subscription as ready."""
        if source_client is not self._mqtt:
            return
        if mid == self._subscribe_mid:
            self._subscribe_mid = None
            if error is not None:
                self._fail_connect(error, source_client)
            else:
                self._mqtt_ready.set()
        elif (future := self._subscriptions.get(mid)) is not None and not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(None)

    def _fail_connect(
        self,
        error: Exception,
        source_client: mqtt.Client | None = None,
    ) -> None:
        if source_client is not None and source_client is not self._mqtt:
            return
        self._connect_error = error
        for future in self._subscriptions.values():
            if not future.done():
                future.set_exception(error)
        self._subscriptions.clear()
        for _response_topic, response_future in self._pending.values():
            if not response_future.done():
                response_future.set_exception(error)
        self._mqtt_ready.set()

    def _on_mqtt_disconnect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _disconnect_flags: mqtt.DisconnectFlags,
        _reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if self._loop is not None and client is self._mqtt:
            self._loop.call_soon_threadsafe(self._mark_disconnected, client)

    def _mark_disconnected(self, source_client: mqtt.Client | None = None) -> None:
        if source_client is not None and source_client is not self._mqtt:
            return
        self._accept_mqtt_messages = False
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
        for future in self._subscriptions.values():
            if not future.done():
                future.set_exception(error)
        self._subscriptions.clear()
        for _response_topic, response_future in self._pending.values():
            if not response_future.done():
                response_future.set_exception(error)

    def _on_mqtt_message(
        self,
        client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        loop = self._loop
        if loop is None or not self._accept_mqtt_messages or client is not self._mqtt:
            return
        raw = bytes(message.payload)
        if message.topic.endswith("/response"):
            correlation = _correlation_id(getattr(message.properties, "CorrelationData", None))
            loop.call_soon_threadsafe(
                self._deliver_response,
                correlation,
                message.topic,
                raw,
                client,
            )
            return
        loop.call_soon_threadsafe(
            self._schedule_cloud_broadcast,
            message.topic,
            raw,
            client,
        )

    def _schedule_cloud_broadcast(
        self,
        topic: str,
        raw: bytes,
        source_client: mqtt.Client | None = None,
    ) -> None:
        """Create and retain a broadcast task until its result is consumed."""
        if not self._accept_mqtt_messages or (
            source_client is not None and source_client is not self._mqtt
        ):
            return

        async def handle_current_broadcast() -> None:
            if self._accept_mqtt_messages and (
                source_client is None or source_client is self._mqtt
            ):
                await self._handle_cloud_broadcast(topic, raw)

        task = asyncio.create_task(
            handle_current_broadcast(),
            name="narwal-cloud-broadcast",
        )
        self._broadcast_tasks.add(task)
        task.add_done_callback(self._broadcast_task_done)

    def _broadcast_task_done(self, task: asyncio.Task[None]) -> None:
        """Consume an unexpected task exception so it cannot escape the loop."""
        self._broadcast_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.debug(
                "Narwal cloud broadcast handler failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _deliver_response(
        self,
        correlation: str,
        response_topic: str,
        raw: bytes,
        source_client: mqtt.Client | None = None,
    ) -> None:
        if not self._accept_mqtt_messages or (
            source_client is not None and source_client is not self._mqtt
        ):
            return
        pending = self._pending.get(correlation)
        future = pending[1] if pending is not None and pending[0] == response_topic else None
        prefix = f"{self.topic_prefix}/{self.device_id}/"
        if (
            not correlation
            or future is None
            or future.done()
            or not response_topic.startswith(prefix)
        ):
            return
        short_topic = response_topic[len(prefix) :].removesuffix("/response")
        try:
            response = self._decode_response(short_topic, raw)
        except NarwalCommandError as err:
            future.set_exception(err)
            return
        # A valid failure result is still evidence that the robot responded.
        if (
            (
                short_topic
                in {value for name, value in vars(const).items() if name.startswith("TOPIC_CMD_")}
                and type(response.data.get("1")) is int
                and response.result_code in set(CommandResult)
            )
            or (short_topic == TOPIC_CMD_ACTIVE_ROBOT and bool(response.data.keys() & {"1", "2"}))
            or (
                short_topic == TOPIC_CMD_GET_BATTERY_INFO
                and any(
                    type(response.data.get(str(field))) is int for field in (1, 2, 4, 5, 6, 7, 8, 9)
                )
            )
            or (
                short_topic == TOPIC_CMD_GET_CONSUMABLE_INFO
                and isinstance(response.data.get("1"), dict)
            )
            or (
                short_topic == TOPIC_CMD_GET_DEVICE_INFO
                and all(
                    isinstance(response.data.get(field), bytes) and response.data[field]
                    for field in ("1", "2")
                )
            )
            or (
                short_topic == TOPIC_CMD_GET_FEATURE_LIST
                and all(type(value) is int for value in response.data.values())
                and response.data.get("1", 0) in (0, 1)
            )
        ):
            self._mark_robot_reachable()
        future.set_result(raw)

    def _mark_robot_reachable(self) -> None:
        """Record a robot-originated message independently of broker state."""
        was_reachable = self.robot_awake
        self._last_broadcast_time = time.monotonic()
        self._robot_awake = True
        if not was_reachable and self.on_state_update is not None:
            self.on_state_update(self.state)

    async def _handle_cloud_broadcast(self, topic: str, raw: bytes) -> None:
        prefix = f"{self.topic_prefix}/{self.device_id}/"
        if not topic.startswith(prefix):
            return
        short_topic = topic[len(prefix) :]
        try:
            payload = _decode_envelope(raw)
            if short_topic == "general/topic":
                inner_url, payload = _decode_topic(payload)
                short_topic = inner_url.removeprefix("/")
        except ValueError:
            _LOGGER.debug("Discarding malformed Narwal cloud broadcast")
            return
        try:
            await self._handle_broadcast(short_topic, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug(
                "Discarding invalid Narwal cloud broadcast for topic %s",
                short_topic,
                exc_info=True,
            )

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
            payload = _decode_envelope(raw)
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
            except NarwalCommandError:
                backoff_index += 1
                await asyncio.sleep(timeout)
                continue
            if raw is None:
                backoff_index += 1
                await asyncio.sleep(timeout)
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
            # Concurrent requests own separate MIDs, even for the same topic:
            # cancelling one request must not cancel another's SUBACK wait.
            result, mid = client.subscribe(
                response_topic,
                options=mqtt.SubscribeOptions(qos=0),
            )
            if result != mqtt.MQTT_ERR_SUCCESS or mid is None:
                raise NarwalConnectionError(f"MQTT response subscription failed ({result})")
            subscription = asyncio.get_running_loop().create_future()
            self._subscriptions[mid] = subscription
            try:
                await asyncio.wait_for(subscription, timeout=COMMAND_RESPONSE_TIMEOUT)
                if client is not self._mqtt or not self.connected or not self._accept_mqtt_messages:
                    raise NarwalConnectionError(
                        "Narwal MQTT connection changed during subscription"
                    )
                self._response_topics.add(response_topic)
            finally:
                if self._subscriptions.get(mid) is subscription:
                    self._subscriptions.pop(mid)
                subscription.cancel()
                if not subscription.cancelled():
                    subscription.exception()
        message_id = str(uuid.uuid4())
        properties = mqtt.Properties(mqtt.PacketTypes.PUBLISH)
        properties.ResponseTopic = response_topic
        properties.CorrelationData = _correlation_data(message_id)
        future: asyncio.Future[bytes] | None = None
        if expect_response:
            future = asyncio.get_running_loop().create_future()
            self._pending[message_id] = (response_topic, future)
        try:
            info = client.publish(
                request_topic,
                _service_envelope(account.uuid, payload),
                qos=0,
                properties=properties,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise NarwalConnectionError(f"MQTT publish failed ({info.rc})")
            return await future if future is not None else None
        finally:
            self._pending.pop(message_id, None)
            if future is not None:
                future.cancel()
                if not future.cancelled():
                    future.exception()

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
        return self._decode_response(short_topic, raw)

    def _decode_response(self, short_topic: str, raw: bytes) -> CommandResponse:
        """Decode endpoint results without treating payload fields as result codes."""
        try:
            response_payload = _decode_envelope(raw)
            decoded = self._decode_protobuf(response_payload)
        except Exception as err:
            raise NarwalCommandError(f"Malformed response for command '{short_topic}'") from err
        short_topic = short_topic.lstrip("/")
        if not isinstance(decoded, dict) or (not decoded and short_topic != TOPIC_CMD_ACTIVE_ROBOT):
            raise NarwalCommandError(f"Empty response for command '{short_topic}'")
        # ActiveRobotPublish.Response contains topicsDuration=1 (message) and
        # defaultDuration=2 (uint), not ServiceResult. An empty body is defaults.
        if short_topic == TOPIC_CMD_ACTIVE_ROBOT and (
            ("1" in decoded and not isinstance(decoded["1"], dict))
            or ("2" in decoded and (type(decoded["2"]) is not int or decoded["2"] < 0))
            or (
                isinstance(decoded.get("1"), dict)
                and any(type(value) is not int or value < 0 for value in decoded["1"].values())
            )
        ):
            raise NarwalCommandError(f"Malformed response for command '{short_topic}'")

        raw_field1 = decoded.get("1")
        result_code: int | None = 0
        if short_topic in {
            TOPIC_CMD_ACTIVE_ROBOT,
            TOPIC_CMD_GET_BATTERY_INFO,
            TOPIC_CMD_GET_CONSUMABLE_INFO,
            TOPIC_CMD_GET_CLEAN_PROGRESS,
            TOPIC_CMD_GET_DEVICE_INFO,
            TOPIC_CMD_GET_FEATURE_LIST,
        }:
            result_code = None
        elif type(raw_field1) is int:
            result_code = raw_field1
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
        self._mark_disconnected(client)
        self._accept_mqtt_messages = False
        self._loop = None
        self._mqtt = None
        await asyncio.to_thread(client.loop_stop)
        await self._async_cancel_broadcast_tasks()

    async def _async_cancel_broadcast_tasks(self) -> None:
        """Cancel and await every retained MQTT broadcast task."""
        while broadcast_tasks := tuple(self._broadcast_tasks):
            for task in broadcast_tasks:
                task.cancel()
            await asyncio.gather(*broadcast_tasks, return_exceptions=True)

    async def disconnect(self) -> None:
        """Close MQTT without blocking Home Assistant's event loop."""
        self._should_reconnect = False
        # Close admission before yielding so callbacks already queued by the
        # Paho thread cannot create work after the first task snapshot.
        self._accept_mqtt_messages = False
        self._loop = None
        activation_task = self._activation_task
        self._activation_task = None
        if activation_task is not None:
            activation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await activation_task
        self._mark_disconnected()
        await self._async_cancel_broadcast_tasks()
        client = self._mqtt
        self._mqtt = None
        try:
            if client is not None:
                try:
                    client.disconnect()
                finally:
                    await asyncio.to_thread(client.loop_stop)
        finally:
            # A callback may already have crossed the thread boundary before
            # admission closed. Drain once more after the network loop stops.
            await self._async_cancel_broadcast_tasks()
