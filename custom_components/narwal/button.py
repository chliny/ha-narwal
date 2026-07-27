"""Action buttons for the Narwal base station."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResponse, NarwalClient


@dataclass(frozen=True, kw_only=True)
class NarwalButtonEntityDescription(ButtonEntityDescription):
    """Describe a Narwal action button."""

    action: Callable[[NarwalClient], Awaitable[CommandResponse]]


BUTTON_DESCRIPTIONS = (
    NarwalButtonEntityDescription(
        key="dry_mop",
        translation_key="dry_mop",
        icon="mdi:weather-windy",
        action=lambda client: client.dry_mop(),
    ),
    NarwalButtonEntityDescription(
        key="empty_dustbin",
        translation_key="empty_dustbin",
        icon="mdi:delete-empty",
        action=lambda client: client.empty_dustbin(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal base-station buttons."""
    del hass
    coordinator = entry.runtime_data
    descriptions = [BUTTON_DESCRIPTIONS[-1]]
    if coordinator.supports_single_dry_mop:
        descriptions.insert(0, BUTTON_DESCRIPTIONS[0])
    async_add_entities(
        [NarwalActionButton(coordinator, description) for description in descriptions]
    )


class NarwalActionButton(NarwalEntity, ButtonEntity):
    """Run an explicit base-station action."""

    entity_description: NarwalButtonEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalButtonEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._set_entity_identity("button", description.key)

    async def async_press(self) -> None:
        """Run the selected base-station action."""
        response = await self._async_await_command(
            self.entity_description.action(self.coordinator.client),
            self.entity_description.key.replace("_", " "),
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()
