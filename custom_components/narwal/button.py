"""Action buttons for the Narwal base station."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResponse, ConsumableInfo, NarwalClient
from .narwal_client.const import (
    CONSUMABLE_MAINTAIN_TYPE_MAP,
    CONSUMABLE_REPLACE_TYPE_MAP,
)


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


def _reset_protocol(consumable: ConsumableInfo) -> tuple[str, int] | None:
    mapping = (
        CONSUMABLE_MAINTAIN_TYPE_MAP if consumable.category == 1 else CONSUMABLE_REPLACE_TYPE_MAP
    )
    value = mapping.get(consumable.type_id)
    if value is None:
        return None
    return ("maintain" if consumable.category == 1 else "replace", value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Narwal base-station buttons."""
    del hass
    coordinator = entry.runtime_data
    known: set[str] = set()

    def _add_supported() -> None:
        descriptions = []
        if coordinator.supports_single_dry_mop:
            descriptions.append(BUTTON_DESCRIPTIONS[0])
        if coordinator.supports_dust_gathering:
            descriptions.append(BUTTON_DESCRIPTIONS[1])
        new = [description for description in descriptions if description.key not in known]
        known.update(description.key for description in new)
        if new:
            async_add_entities(
                [NarwalActionButton(coordinator, description) for description in new]
            )
        state = coordinator.data
        if state is None:
            return
        if state.clean_plans and "start_clean_plan" not in known:
            known.add("start_clean_plan")
            async_add_entities([NarwalStartCleanPlanButton(coordinator)])
        reset_entities = []
        for consumable in state.consumables.values():
            key = f"reset_consumable_{consumable.key}"
            protocol = _reset_protocol(consumable)
            protocol_reported = bool(
                protocol
                and protocol[1]
                in (
                    state.consumable_maintain_items
                    if protocol[0] == "maintain"
                    else state.consumable_replace_items
                )
            )
            if (
                key not in known
                and consumable.reset_supported
                and protocol is not None
                and protocol_reported
            ):
                known.add(key)
                reset_entities.append(
                    NarwalConsumableResetButton(coordinator, consumable, *protocol)
                )
        if reset_entities:
            async_add_entities(reset_entities)

    _add_supported()
    entry.async_on_unload(coordinator.async_add_listener(_add_supported))


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


class NarwalStartCleanPlanButton(NarwalEntity, ButtonEntity):
    """Start the plan selected by the clean-plan select entity."""

    _attr_translation_key = "start_clean_plan"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("button", "start_clean_plan")

    async def async_press(self) -> None:
        plan_id = self.coordinator.selected_clean_plan_id
        if plan_id is None:
            state = self.coordinator.data
            if state and state.clean_plans:
                raw_plan_id = state.clean_plans[0].get("id")
                if isinstance(raw_plan_id, int) and not isinstance(raw_plan_id, bool):
                    plan_id = raw_plan_id
                elif isinstance(raw_plan_id, (str, bytes, bytearray)):
                    try:
                        plan_id = int(raw_plan_id)
                    except TypeError, ValueError:
                        plan_id = None
        if plan_id is None:
            raise HomeAssistantError("No saved Narwal cleaning plan is available")
        response = await self._async_await_command(
            self.coordinator.client.start_clean_plan(plan_id),
            "start clean plan",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()


class NarwalConsumableResetButton(NarwalEntity, ButtonEntity):
    """Explicitly reset one app-declared consumable counter."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:restore"

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        consumable: ConsumableInfo,
        category: str,
        protocol_value: int,
    ) -> None:
        super().__init__(coordinator)
        self._consumable_key = consumable.key
        self._category = category
        self._protocol_value = protocol_value
        self._attr_name = f"Reset {consumable.name}"
        self._set_entity_identity("button", f"reset_consumable_{consumable.key}")

    async def async_press(self) -> None:
        response = await self._async_await_command(
            self.coordinator.client.reset_consumable(
                category=self._category,
                protocol_value=self._protocol_value,
            ),
            f"reset consumable {self._consumable_key}",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()
