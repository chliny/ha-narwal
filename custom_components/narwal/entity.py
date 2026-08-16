"""Base entity for Narwal vacuum integration."""

from __future__ import annotations

import re
from collections.abc import Awaitable

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_MODEL,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import NarwalCoordinator
from .narwal_client import (
    CommandResponse,
    NarwalCommandError,
    NarwalConnectionError,
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _identity_part(value: str) -> str:
    """Normalize a stable identity component for HA registries."""
    normalized = _CAMEL_BOUNDARY.sub("_", value.strip()).lower()
    return _NON_ALNUM.sub("_", normalized).strip("_")


class NarwalEntity(CoordinatorEntity[NarwalCoordinator]):
    """Base class for Narwal entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry_data = coordinator.config_entry.data
        device_id = entry_data["device_id"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            model=entry_data[CONF_MODEL],
            sw_version=coordinator.client.state.firmware_version or None,
            name=coordinator.config_entry.title,
        )

    def _set_entity_identity(self, platform: str, name: str) -> None:
        """Set stable unique_id and deterministic entity_id together."""
        device_id = _identity_part(str(self.coordinator.config_entry.data["device_id"]))
        identity = f"{DOMAIN}_{device_id}_{_identity_part(name)}"
        self._attr_unique_id = identity
        self.entity_id = f"{platform}.{identity}"

    async def _async_await_command(
        self,
        command: Awaitable[CommandResponse],
        action: str,
    ) -> CommandResponse:
        """Convert transport failures into a safe Home Assistant service error."""
        try:
            return await command
        except (NarwalCommandError, NarwalConnectionError) as err:
            raise HomeAssistantError(
                f"Narwal {action} failed because the robot did not respond; "
                "verify that it is online in the Narwal app"
            ) from err

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self.coordinator.last_update_success and self.coordinator.client.robot_awake
