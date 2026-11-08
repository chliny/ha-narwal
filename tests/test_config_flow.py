"""Regression tests for Narwal config-flow state boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.narwal.config_flow import NarwalConfigFlow
from custom_components.narwal.const import (
    CONF_AREA_CODE,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_PHONE,
    DEFAULT_AREA_CODE,
    DEFAULT_COUNTRY_CODE,
)
from custom_components.narwal.narwal_client import CloudDevice, NarwalCloudError


def _device(device_id: str = "device") -> CloudDevice:
    """Return a minimal discovered robot."""
    return CloudDevice(
        device_id=device_id,
        product_id="product",
        name="Robot",
        firmware_version="1.0.0",
        serial_number="serial",
    )


def test_stale_device_selection_returns_form_error() -> None:
    """An old or forged selection must not raise StopIteration from a coroutine."""
    flow = NarwalConfigFlow()
    flow._cloud_devices = [_device()]

    result = asyncio.run(flow.async_step_cloud_device({"device_id": "no-longer-present"}))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cloud_device"
    assert result["errors"] == {"base": "device_not_found"}


def test_reauth_updates_credentials_and_requests_reload() -> None:
    """Successful reauthentication updates the existing entry and reloads it."""

    async def run_test() -> None:
        flow = NarwalConfigFlow()
        flow.hass = SimpleNamespace()
        flow._cloud_input = {
            CONF_PHONE: "old-phone",
            CONF_PASSWORD: "old-password",
            CONF_AREA_CODE: None,
            CONF_COUNTRY_CODE: "",
            "device_id": "device",
        }
        api = SimpleNamespace(
            login=AsyncMock(),
            devices=AsyncMock(return_value=[_device()]),
            broker=AsyncMock(return_value="mqtts://broker.example"),
        )
        entry = object()
        session = object()
        expected = {"type": "abort", "reason": "reauth_successful"}

        with (
            patch(
                "custom_components.narwal.config_flow.async_get_clientsession",
                return_value=session,
            ),
            patch(
                "custom_components.narwal.config_flow.NarwalCloudApi",
                return_value=api,
            ) as api_type,
            patch.object(flow, "_get_reauth_entry", return_value=entry),
            patch.object(
                flow,
                "async_update_reload_and_abort",
                new=Mock(return_value=expected),
            ) as update_reload,
        ):
            result = await flow.async_step_reauth_confirm(
                {
                    CONF_PHONE: "+8613800000000",
                    CONF_PASSWORD: "new-password",
                }
            )

        assert result is expected
        # The stored area code is applied and the phone is normalized for login.
        api_type.assert_called_once_with(
            session,
            phone="13800000000",
            password="new-password",
            area_code=DEFAULT_AREA_CODE,
            country_code=DEFAULT_COUNTRY_CODE,
        )
        api.login.assert_awaited_once_with()
        api.devices.assert_awaited_once_with()
        api.broker.assert_awaited_once_with()
        update_reload.assert_called_once_with(
            entry,
            data_updates={
                CONF_PHONE: "13800000000",
                CONF_PASSWORD: "new-password",
                CONF_AREA_CODE: DEFAULT_AREA_CODE,
                CONF_COUNTRY_CODE: DEFAULT_COUNTRY_CODE,
                "device_id": "device",
            },
        )

    asyncio.run(run_test())


def test_reauth_rejects_unavailable_broker() -> None:
    """Reauthentication must validate all services needed by entry setup."""

    async def run_test() -> None:
        flow = NarwalConfigFlow()
        flow.hass = SimpleNamespace()
        flow._cloud_input = {
            CONF_PHONE: "old-phone",
            CONF_PASSWORD: "old-password",
            CONF_AREA_CODE: DEFAULT_AREA_CODE,
            CONF_COUNTRY_CODE: DEFAULT_COUNTRY_CODE,
            "device_id": "device",
        }
        api = SimpleNamespace(
            login=AsyncMock(),
            devices=AsyncMock(return_value=[_device()]),
            broker=AsyncMock(side_effect=NarwalCloudError("broker unavailable")),
        )

        with (
            patch(
                "custom_components.narwal.config_flow.async_get_clientsession",
                return_value=object(),
            ),
            patch(
                "custom_components.narwal.config_flow.NarwalCloudApi",
                return_value=api,
            ),
            patch.object(flow, "async_update_reload_and_abort") as update_reload,
        ):
            result = await flow.async_step_reauth_confirm(
                {
                    CONF_PHONE: "new-phone",
                    CONF_PASSWORD: "new-password",
                }
            )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": "cannot_connect"}
        api.login.assert_awaited_once_with()
        api.devices.assert_awaited_once_with()
        api.broker.assert_awaited_once_with()
        update_reload.assert_not_called()

    asyncio.run(run_test())
