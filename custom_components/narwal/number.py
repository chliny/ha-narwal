"""Number entities for Narwal settings."""

from __future__ import annotations

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up supported Narwal number entities."""
    del hass
    coordinator = entry.runtime_data
    added = False

    def _add_supported() -> None:
        nonlocal added
        if coordinator.supports_volume and not added:
            added = True
            async_add_entities([NarwalVolumeNumber(coordinator)])

    _add_supported()
    entry.async_on_unload(coordinator.async_add_listener(_add_supported))


class NarwalVolumeNumber(NarwalEntity, NumberEntity):
    """Robot speaker volume."""

    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("number", "volume")

    @property
    def native_value(self) -> float | None:
        """Return a finite volume percentage reported by the robot."""
        raw = self.coordinator.device_config.get("1")
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str, bytes, bytearray)):
            return None
        try:
            value = float(raw)
        except TypeError, ValueError:
            return None
        return value if math.isfinite(value) and 0 <= value <= 100 else None

    async def async_set_native_value(self, value: float) -> None:
        response = await self._async_await_command(
            self.coordinator.client.set_volume(round(value)),
            "set volume",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        self.coordinator.device_config["1"] = round(value)
        self.async_write_ha_state()
