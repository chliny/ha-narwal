"""Safety and connection regression tests for the cloud MQTT client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.narwal.narwal_client.client import (
    NarwalAuthenticationError,
    NarwalCommandNotSentError,
    NarwalConnectionError,
)
from custom_components.narwal.narwal_client.cloud_api import (
    CloudAccount,
    CloudDevice,
    NarwalCloudAuthError,
)
from custom_components.narwal.narwal_client.cloud_client import NarwalCloudClient


def _client() -> NarwalCloudClient:
    return NarwalCloudClient(
        Mock(),
        phone="phone",
        password="password",
        device_id="device",
        product_id="product",
    )


def _api() -> SimpleNamespace:
    """Return the cloud discovery surface used by ``connect``."""
    account = CloudAccount(token="token", uuid="uuid")
    return SimpleNamespace(
        account=account,
        login=AsyncMock(return_value=account),
        devices=AsyncMock(
            return_value=[
                CloudDevice(
                    device_id="device",
                    product_id="product",
                    name="Robot",
                    firmware_version="1.0",
                    serial_number="serial",
                )
            ]
        ),
        broker=AsyncMock(return_value="broker.example:9443"),
    )


async def _call_inline(function: object, *args: object, **kwargs: object) -> object:
    return function(*args, **kwargs)  # type: ignore[operator]


def test_connect_uses_broker_port_and_subscribes_app_topics() -> None:
    """The cloud broker endpoint and app subscriptions drive a real connection."""

    async def run() -> None:
        client = _client()
        client._api = _api()
        # The post-connect active-publish loop is unrelated to the initial
        # broker subscription handshake; keep that later loop dormant.
        client.subscribe_to_topics = AsyncMock()  # type: ignore[method-assign]
        mqtt_client = Mock()
        mqtt_client.subscribe.return_value = (0, 37)

        def fake_connect(**_kwargs: object) -> None:
            mqtt_client.on_connect(mqtt_client, None, Mock(), SimpleNamespace(value=0), None)

        subscription_sent = asyncio.Event()
        mqtt_client.connect.side_effect = fake_connect
        mqtt_client.loop_start.side_effect = subscription_sent.set

        with (
            patch(
                "custom_components.narwal.narwal_client.cloud_client.mqtt.Client",
                return_value=mqtt_client,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client._async_configure_tls",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client.asyncio.to_thread",
                side_effect=_call_inline,
            ),
        ):
            connecting = asyncio.create_task(client.connect())
            try:
                await asyncio.wait_for(subscription_sent.wait(), timeout=2)
                await asyncio.sleep(0)
                assert not connecting.done()
                assert not client.connected
                topics = mqtt_client.subscribe.call_args.args[0]
                reasons = [SimpleNamespace(value=0) for _ in topics]
                mqtt_client.on_subscribe(mqtt_client, None, 37, reasons, None)
                await asyncio.wait_for(connecting, timeout=2)
            finally:
                if not connecting.done():
                    connecting.cancel()
                    await asyncio.gather(connecting, return_exceptions=True)

        assert client.connected
        assert mqtt_client.connect.call_args.kwargs["host"] == "broker.example"
        assert mqtt_client.connect.call_args.kwargs["port"] == 9443
        topics = {topic for topic, _options in mqtt_client.subscribe.call_args.args[0]}
        assert "/product/device/status/download_status" in topics
        assert "/product/device/upgrade/upgrade_status" in topics

    asyncio.run(run())


@pytest.mark.parametrize(
    ("reason", "expected_error"),
    [
        (0x86, NarwalAuthenticationError),
        (0x87, NarwalAuthenticationError),
        (0x80, NarwalConnectionError),
    ],
)
def test_connect_reports_mqtt_rejection(reason: int, expected_error: type[Exception]) -> None:
    """Broker auth rejections trigger reauthentication; transient ones do not."""

    async def run() -> None:
        client = _client()
        client._api = _api()
        mqtt_client = Mock()

        def fake_connect(**_kwargs: object) -> None:
            mqtt_client.on_connect(mqtt_client, None, Mock(), SimpleNamespace(value=reason), None)

        mqtt_client.connect.side_effect = fake_connect

        with (
            patch(
                "custom_components.narwal.narwal_client.cloud_client.mqtt.Client",
                return_value=mqtt_client,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client._async_configure_tls",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client.asyncio.to_thread",
                side_effect=_call_inline,
            ),
            pytest.raises(expected_error) as raised,
        ):
            await client.connect()

        assert type(raised.value) is expected_error

    asyncio.run(run())


def test_connect_failure_surfaces_error_and_stops_network_loop() -> None:
    """A TCP failure is reported as a connection error and leaks no MQTT loop."""

    async def run() -> None:
        client = _client()
        client._api = _api()
        mqtt_client = Mock()
        mqtt_client.connect.side_effect = OSError("connect failed")

        with (
            patch(
                "custom_components.narwal.narwal_client.cloud_client.mqtt.Client",
                return_value=mqtt_client,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client._async_configure_tls",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client.asyncio.to_thread",
                side_effect=_call_inline,
            ),
            pytest.raises(NarwalConnectionError),
        ):
            await client.connect()

        mqtt_client.loop_stop.assert_called_once_with()

    asyncio.run(run())


def test_connect_rejects_mqtt_subscription_without_accepted_suback() -> None:
    """Broker connection is not ready when its initial SUBACK rejects a topic."""

    async def run() -> None:
        client = _client()
        client._api = _api()
        mqtt_client = Mock()
        mqtt_client.subscribe.return_value = (0, 37)

        def fake_connect(**_kwargs: object) -> None:
            mqtt_client.on_connect(mqtt_client, None, Mock(), SimpleNamespace(value=0), None)

        def start_network_loop() -> None:
            topics = mqtt_client.subscribe.call_args.args[0]
            reasons = [SimpleNamespace(value=0) for _ in topics]
            reasons[-1] = SimpleNamespace(value=128)
            mqtt_client.on_subscribe(mqtt_client, None, 37, reasons, None)

        mqtt_client.connect.side_effect = fake_connect
        mqtt_client.loop_start.side_effect = start_network_loop

        with (
            patch(
                "custom_components.narwal.narwal_client.cloud_client.mqtt.Client",
                return_value=mqtt_client,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client._async_configure_tls",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client.asyncio.to_thread",
                side_effect=_call_inline,
            ),
            pytest.raises(NarwalConnectionError, match="subscriptions rejected"),
        ):
            await client.connect()

        mqtt_client.subscribe.assert_called_once()
        mqtt_client.loop_stop.assert_called_once_with()
        assert not client.connected

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
def test_reset_waiting_for_response_subscription_can_be_retried(failure: str) -> None:
    """An unconfirmed response subscription cannot publish an irreversible reset."""

    async def run() -> None:
        client = _client()
        client._api = _api()
        client.subscribe_to_topics = AsyncMock()  # type: ignore[method-assign]
        response_subscription = asyncio.Event()
        mqtt_clients: list[Mock] = []

        def make_mqtt(*_args: object, **_kwargs: object) -> Mock:
            mqtt_client = Mock()
            mqtt_clients.append(mqtt_client)
            next_mid = iter(range(1, 10))

            def subscribe(topics: object, **_kwargs: object) -> tuple[int, int]:
                mid = next(next_mid)
                if isinstance(topics, list):
                    reasons = [SimpleNamespace(value=0) for _ in topics]
                    asyncio.get_running_loop().call_soon(
                        mqtt_client.on_subscribe, mqtt_client, None, mid, reasons, None
                    )
                else:
                    response_subscription.set()
                return (0, mid)

            def publish(topic: str, _payload: bytes, **kwargs: object) -> SimpleNamespace:
                properties = kwargs["properties"]
                message = SimpleNamespace(
                    topic=f"{topic}/response",
                    payload=b"\x08\x02",  # Robot explicitly rejected the reset.
                    properties=SimpleNamespace(CorrelationData=properties.CorrelationData),
                )
                asyncio.get_running_loop().call_soon(
                    mqtt_client.on_message, mqtt_client, None, message
                )
                return SimpleNamespace(rc=0)

            mqtt_client.subscribe.side_effect = subscribe
            mqtt_client.publish.side_effect = publish
            mqtt_client.connect.side_effect = lambda **_kwargs: mqtt_client.on_connect(
                mqtt_client, None, Mock(), SimpleNamespace(value=0), None
            )
            return mqtt_client

        with (
            patch(
                "custom_components.narwal.narwal_client.cloud_client.mqtt.Client",
                side_effect=make_mqtt,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client._async_configure_tls",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client.asyncio.to_thread",
                side_effect=_call_inline,
            ),
            patch(
                "custom_components.narwal.narwal_client.cloud_client.COMMAND_RESPONSE_TIMEOUT", 0.1
            ),
        ):
            await client.connect()
            first_mqtt = mqtt_clients[-1]
            first = asyncio.create_task(
                client.reset_consumable(category="maintain", protocol_value=1)
            )
            await asyncio.wait_for(response_subscription.wait(), timeout=2)
            assert first_mqtt.subscribe.call_args.args[0] == (
                "/product/device/consumable/reset_consumable_info/response"
            )
            assert not first.done()
            first_mqtt.publish.assert_not_called()
            if failure == "disconnect":
                first_mqtt.on_disconnect(first_mqtt, None, Mock(), SimpleNamespace(value=0), None)
            with pytest.raises(NarwalCommandNotSentError):
                await first
            first_mqtt.publish.assert_not_called()

            if failure == "disconnect":
                await client.connect()
            retry_mqtt = mqtt_clients[-1]
            response_subscription.clear()
            retry = asyncio.create_task(
                client.reset_consumable(category="maintain", protocol_value=1)
            )
            await asyncio.wait_for(response_subscription.wait(), timeout=2)
            retry_mqtt.publish.assert_not_called()
            mid = retry_mqtt.subscribe.call_count
            retry_mqtt.on_subscribe(retry_mqtt, None, mid, [SimpleNamespace(value=0)], None)
            result = await asyncio.wait_for(retry, timeout=2)
            assert result.result_code == 2
            retry_mqtt.publish.assert_called_once()
            await client.disconnect()

    asyncio.run(run())


def test_cloud_metadata_auth_failure_is_raised_for_reauth() -> None:
    """An expired token during metadata refresh must reach the coordinator."""

    async def run() -> None:
        client = _client()
        client.cloud_device = CloudDevice(
            device_id="device",
            product_id="product",
            name="Robot",
            firmware_version="1.0",
            serial_number="serial",
        )
        client._api = SimpleNamespace(
            consumables=AsyncMock(return_value=[]),
            notifications=AsyncMock(return_value=[]),
            task_records=AsyncMock(return_value=[{"clean_record_id": "record"}]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
            task_record_detail=AsyncMock(side_effect=NarwalCloudAuthError("expired")),
        )

        with pytest.raises(NarwalAuthenticationError):
            await client.get_cloud_metadata()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("topic", "raw", "awake"),
    [
        ("/product/other/status/download_status", b"\x08\x01", False),
        ("/product/device/status/download_status", b"\x09\x00", False),
        ("/product/device/status/download_status", b"\x08\x01", True),
        (
            "/product/device/general/topic",
            b"\x0a\x17/status/download_status\x12\x02\x08\x01",
            True,
        ),
    ],
)
def test_cloud_broadcast_topic_and_decoding_gate_liveness(
    topic: str, raw: bytes, awake: bool
) -> None:
    """Only a valid broadcast for this robot proves the robot is reachable."""
    client = _client()
    client.on_state_update = Mock()

    asyncio.run(client._handle_cloud_broadcast(topic, raw))

    assert client.robot_awake is awake
    assert client.on_state_update.called is awake
