"""Select entities for Narwal cleaning preferences."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import MopHumidity

MOP_HUMIDITY_OPTIONS: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal cleaning preference selects."""
    del hass
    async_add_entities([NarwalMopHumiditySelect(entry.runtime_data)])


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity):
    """Set how wet the robot keeps its mop pads."""

    _attr_translation_key = "mop_humidity"
    _attr_icon = "mdi:water-percent"
    _attr_options = list(MOP_HUMIDITY_OPTIONS)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "mop_humidity")
        self._current_option: str | None = None

    @property
    def current_option(self) -> str | None:
        """Return the last humidity selected through Home Assistant."""
        return self._current_option

    async def async_select_option(self, option: str) -> None:
        """Set mop humidity."""
        level = MOP_HUMIDITY_OPTIONS.get(option)
        if level is None:
            raise HomeAssistantError(f"Unsupported mop humidity option: {option}")
        response = await self._async_await_command(
            self.coordinator.client.set_mop_humidity(level),
            "set mop humidity",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        self._current_option = option
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
