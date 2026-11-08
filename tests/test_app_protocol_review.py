"""Offline regression tests for App protocol parity fixes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.narwal.narwal_client.cloud_api import (
    CloudAccount,
    CloudDevice,
    NarwalCloudApi,
    NarwalCloudError,
)
from custom_components.narwal.narwal_client.cloud_client import NarwalCloudClient


class _Response:
    def __init__(self, code: int, result: object) -> None:
        self.code, self.result = code, result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        pass

    async def json(self, **_kwargs):
        return {"code": self.code, "result": self.result}


@pytest.mark.parametrize("code", [0, 200])
@pytest.mark.parametrize("kind", ["firmware", "voice"])
def test_app_plain_success_codes_are_accepted(code: int, kind: str) -> None:
    """The cloud reports plain success as 0 or 200, not a tagged success value."""
    result = {} if kind == "firmware" else []
    session = SimpleNamespace(request=Mock(return_value=_Response(code, result)))
    api = NarwalCloudApi(session, phone="test", password="test")
    api.account = CloudAccount(token="token", uuid="user")
    command = (
        api.firmware_update(device_id="device", product_id="product", firmware_version="1")
        if kind == "firmware"
        else api.voice_packs(device_id="device", product_id="product", device_language_code="zh")
    )

    parsed = asyncio.run(command)

    assert isinstance(parsed, dict if kind == "firmware" else list)


def test_app_failure_code_is_rejected() -> None:
    """A non-success business code is a failed request, not empty success data."""
    session = SimpleNamespace(request=Mock(return_value=_Response(400, {})))
    api = NarwalCloudApi(session, phone="test", password="test")
    api.account = CloudAccount(token="token", uuid="user")

    with pytest.raises(NarwalCloudError):
        asyncio.run(
            api.firmware_update(device_id="device", product_id="product", firmware_version="1")
        )


def test_mqtt_connect_uses_refreshed_discovery_account() -> None:
    """MQTT must be authenticated with the account refreshed during connect."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="test", password="test", device_id="device", product_id="product"
        )
        old = CloudAccount(token="old-token", uuid="old-user")
        new = CloudAccount(token="new-token", uuid="new-user")
        api = SimpleNamespace(account=old)
        api.login = AsyncMock(return_value=old)
        api.devices = AsyncMock(
            return_value=[
                CloudDevice(
                    device_id="device",
                    product_id="product",
                    name="Robot",
                    firmware_version="1",
                    serial_number="serial",
                )
            ]
        )

        async def broker():
            api.account = new
            return "broker.example"

        api.broker = AsyncMock(side_effect=broker)
        client._api = api
        client.subscribe_to_topics = AsyncMock()
        mqtt_client = Mock()
        mqtt_client.loop_start.side_effect = client._mqtt_ready.set

        async def call_inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

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
                side_effect=call_inline,
            ),
        ):
            await client.connect()
        # The MQTT session must use the account refreshed by broker discovery,
        # not the stale credentials from the login that preceded it.
        mqtt_client.username_pw_set.assert_called_once_with("new-user", "new-token")

    asyncio.run(run())
