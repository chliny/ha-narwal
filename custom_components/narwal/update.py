"""Read-only firmware update entity for Narwal."""

from __future__ import annotations

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the read-only Narwal firmware update entity."""
    async_add_entities([NarwalFirmwareUpdateEntity(entry.runtime_data)])


class NarwalFirmwareUpdateEntity(NarwalEntity, UpdateEntity):
    """Expose firmware availability without offering OTA installation."""

    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the firmware update entity."""
        super().__init__(coordinator)
        self._set_entity_identity("update", "firmware")

    @property
    def available(self) -> bool:
        """Return whether the cloud update-description endpoint succeeded."""
        state = self.coordinator.data
        return bool(state and state.firmware_update_supported)

    @property
    def installed_version(self) -> str | None:
        """Return the installed robot firmware version."""
        state = self.coordinator.data
        if not state:
            return None
        return state.firmware_version or None

    @property
    def latest_version(self) -> str | None:
        """Return the latest firmware version reported by the cloud."""
        state = self.coordinator.data
        if not state:
            return None
        return state.firmware_target or state.firmware_version or None

    @property
    def release_summary(self) -> str | None:
        """Return the bounded firmware description supplied by the cloud."""
        state = self.coordinator.data
        if not state:
            return None
        description = str(state.firmware_update_info.get("describe") or "").strip()
        return description[:500] or None
