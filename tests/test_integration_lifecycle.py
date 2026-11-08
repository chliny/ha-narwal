"""Regression tests for config-entry setup and teardown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.narwal import async_setup_entry
from custom_components.narwal.const import (
    CONF_AREA_CODE,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PRODUCT_KEY,
)


def _entry() -> SimpleNamespace:
    """Return a minimal cloud config entry."""
    return SimpleNamespace(
        data={
            CONF_PHONE: "13800000000",
            CONF_PASSWORD: "secret",
            CONF_AREA_CODE: "86",
            CONF_COUNTRY_CODE: "CN",
            CONF_PRODUCT_KEY: "product",
            "device_id": "device",
        },
        runtime_data=None,
    )


def test_failed_platform_setup_shuts_down_without_hiding_error() -> None:
    """A failure after MQTT setup rolls back the coordinator and propagates."""

    async def run_test() -> None:
        entry = _entry()
        failure = RuntimeError("platform failed")
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(side_effect=failure)
            )
        )
        coordinator = SimpleNamespace(
            async_setup=AsyncMock(),
            async_shutdown=AsyncMock(side_effect=RuntimeError("cleanup failed")),
        )

        with (
            patch("custom_components.narwal.NarwalCoordinator", return_value=coordinator),
            pytest.raises(RuntimeError, match="platform failed") as raised,
        ):
            await async_setup_entry(hass, entry)

        assert raised.value is failure
        coordinator.async_shutdown.assert_awaited_once_with()

    asyncio.run(run_test())
