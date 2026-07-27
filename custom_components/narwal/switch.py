"""Switch entities for Narwal station tasks."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import WorkingStatus


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal station switches."""
    del hass
    async_add_entities([NarwalWashAndDryMopSwitch(entry.runtime_data)])


class NarwalWashAndDryMopSwitch(NarwalEntity, SwitchEntity):
    """Start or stop the station wash-and-dry mop cycle."""

    _attr_translation_key = "wash_and_dry_mop"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("switch", "wash_and_dry_mop")

    @property
    def is_on(self) -> bool:
        """Return whether the station is washing and drying mop pads."""
        state = self.coordinator.client.state
        return state.working_status == WorkingStatus.WASH_AND_DRY_MOP

    async def async_turn_on(self, **kwargs: object) -> None:
        """Start the station wash-and-dry cycle."""
        response = await self._async_await_command(
            self.coordinator.client.wash_and_dry_mop(),
            "start wash and dry mop",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        """End the station wash-and-dry cycle."""
        response = await self._async_await_command(
            self.coordinator.client.end_wash_and_dry_mop(),
            "end wash and dry mop",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()
