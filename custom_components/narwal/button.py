"""Action buttons for the Narwal base station."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .const import DOMAIN
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResponse, ConsumableInfo, NarwalClient, NarwalConnectionError
from .narwal_client.const import (
    CONSUMABLE_CLOUD_RESET_TYPES,
    CONSUMABLE_MAINTAIN_TYPE_MAP,
    CONSUMABLE_REPLACE_TYPE_MAP,
)

_LOGGER = logging.getLogger(__name__)

# Reset actions the App offers from its consumable list but that the cloud
# consumable endpoint does not always return; values are translation keys in
# ``strings.json``. The actions come from the App's ``consumables_management``
# resources (``CONSUMABLE_MAINTAIN_ITEM_*``); other maintain enums stay
# unexposed.
ROBOT_MAINTAIN_RESET_KEYS: dict[int, str] = {
    1: "reset_maintain_dust_bin",
    4: "reset_maintain_wash_ribs",
}


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
    """Return the App reset action for a consumable the cloud categorized.

    The cloud type id space is global, not split per category (the App's
    ``NRConsumableType`` uses type 1 for both maintain and replace dust
    filters), so a type id alone must not decide the category.
    """
    type_id = consumable.type_id
    if consumable.category == 1:
        maintain = CONSUMABLE_MAINTAIN_TYPE_MAP.get(type_id)
        return ("maintain", maintain) if maintain is not None else None
    if consumable.category == 2:
        replace = CONSUMABLE_REPLACE_TYPE_MAP.get(type_id)
        return ("replace", replace) if replace is not None else None
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Narwal base-station buttons."""
    del hass
    coordinator = entry.runtime_data
    known: set[str] = set()
    logged_candidates = False

    def _add_supported() -> None:
        nonlocal logged_candidates
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
        if not logged_candidates and state.consumables:
            logged_candidates = True
            _LOGGER.debug(
                "Consumable reset candidates (key, type, category, reset): %s",
                {
                    item.key: (item.type_id, item.category, item.reset_supported)
                    for item in state.consumables.values()
                },
            )
        if state.clean_plans and "start_clean_plan" not in known:
            known.add("start_clean_plan")
            async_add_entities([NarwalStartCleanPlanButton(coordinator)])
        reset_entities: list[NarwalConsumableResetButton] = []
        # Every cloud item that maps to an App reset action, whether or not the
        # App currently enables its reset button, so the robot-side fallback
        # below never duplicates an item the cloud already reports.
        covered: set[tuple[str, int]] = set()
        for consumable in state.consumables.values():
            protocol = _reset_protocol(consumable)
            if protocol is not None:
                covered.add(protocol)
            if not consumable.reset_supported:
                continue
            key = f"reset_consumable_{consumable.key}"
            if key in known:
                continue
            if consumable.type_id in CONSUMABLE_CLOUD_RESET_TYPES:
                if not coordinator.cloud_metadata_available:
                    continue
                action = ("cloud", consumable.type_id)
            elif protocol is not None:
                action = protocol
            else:
                _LOGGER.debug(
                    "No confirmed reset route for cloud consumable %s"
                    " (type=%s, category=%s, reset_supported=%s)",
                    consumable.key,
                    consumable.type_id,
                    consumable.category,
                    consumable.reset_supported,
                )
                continue
            known.add(key)
            reset_entities.append(
                NarwalConsumableResetButton(
                    coordinator,
                    key=key,
                    category=action[0],
                    protocol_value=action[1],
                    translation_key="reset_consumable",
                    translation_placeholders={"name": consumable.name},
                    consumable_key=consumable.key,
                    consumable_type_id=consumable.type_id,
                )
            )
        for value, translation_key in ROBOT_MAINTAIN_RESET_KEYS.items():
            protocol = ("maintain", value)
            key = f"reset_consumable_maintain_{value}"
            if protocol in covered or key in known:
                continue
            if not (state.mqtt_consumables_supported and value in state.consumable_maintain_items):
                continue
            known.add(key)
            reset_entities.append(
                NarwalConsumableResetButton(
                    coordinator,
                    key=key,
                    category="maintain",
                    protocol_value=value,
                    translation_key=translation_key,
                )
            )
        if reset_entities:
            _LOGGER.debug(
                "Adding Narwal consumable reset buttons: %s",
                [entity.unique_id for entity in reset_entities],
            )
            async_add_entities(reset_entities)

    async_add_entities([NarwalResumeBaseSearchButton(coordinator)])
    _add_supported()
    entry.async_on_unload(coordinator.async_add_listener(_add_supported))


class NarwalResumeBaseSearchButton(NarwalEntity, ButtonEntity):
    """Resume a paused task without starting a new clean or recall."""

    _attr_translation_key = "resume_base_search"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("button", "resume_base_search")

    @property
    def available(self) -> bool:
        state = self.coordinator.data
        return bool(super().available and state is not None and state.has_paused_task)

    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError(
                "Task resume is not currently available",
                translation_domain=DOMAIN,
                translation_key="resume_unavailable",
            )
        response = await self._async_await_command(
            self.coordinator.client.resume(timeout=10.0), "resume task"
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal resume task failed with result code {response.result_code}",
                translation_domain=DOMAIN,
                translation_key="resume_failed",
                translation_placeholders={"result_code": str(response.result_code)},
            )
        await self.coordinator.async_request_refresh()


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
                f"Narwal action failed with result code {response.result_code}",
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={"result_code": str(response.result_code)},
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
            raise HomeAssistantError(
                "No saved Narwal cleaning plan is available",
                translation_domain=DOMAIN,
                translation_key="clean_plan_unavailable",
            )
        response = await self._async_await_command(
            self.coordinator.client.start_clean_plan(plan_id),
            "start clean plan",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}",
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={"result_code": str(response.result_code)},
            )
        await self.coordinator.async_request_refresh()


class NarwalConsumableResetButton(NarwalEntity, ButtonEntity):
    """Explicitly reset one app-declared consumable counter."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:restore"

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        *,
        key: str,
        category: str,
        protocol_value: int,
        translation_key: str,
        translation_placeholders: Mapping[str, str] | None = None,
        consumable_key: str | None = None,
        consumable_type_id: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._consumable_key = consumable_key
        self._consumable_type_id = consumable_type_id
        self._category = category
        self._protocol_value = protocol_value
        self._attr_translation_key = translation_key
        if translation_placeholders is not None:
            self._attr_translation_placeholders = translation_placeholders
        self._set_entity_identity("button", key)

    @property
    def available(self) -> bool:
        """Only allow a reset while its app-declared capability is current."""
        state = self.coordinator.data
        if state is None:
            return False
        item = (
            state.consumables.get(self._consumable_key)
            if self._consumable_key is not None
            else None
        )
        if self._category == "cloud":
            return bool(
                item
                and item.reset_supported
                and item.type_id == self._consumable_type_id
                and item.type_id in CONSUMABLE_CLOUD_RESET_TYPES
                and self.coordinator.cloud_metadata_available
            )
        if not super().available or not state.mqtt_consumables_supported:
            return False
        if self._consumable_key is not None:
            return bool(
                item
                and item.reset_supported
                and item.type_id == self._consumable_type_id
                and _reset_protocol(item) == (self._category, self._protocol_value)
            )
        # A robot-only fallback must stop accepting presses once the cloud
        # reports the same item, including when the App disables its reset.
        if any(
            _reset_protocol(cloud_item) == (self._category, self._protocol_value)
            for cloud_item in state.consumables.values()
        ):
            return False
        return self._protocol_value in (
            state.consumable_maintain_items
            if self._category == "maintain"
            else state.consumable_replace_items
        )

    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError(
                "Consumable reset is not currently available",
                translation_domain=DOMAIN,
                translation_key="consumable_reset_unavailable",
            )
        if self._category == "cloud":
            await self._async_await_cloud_reset()
        else:
            response = await self._async_await_command(
                self.coordinator.client.reset_consumable(
                    category=self._category,
                    protocol_value=self._protocol_value,
                ),
                "reset consumable",
            )
            if not response.success:
                raise HomeAssistantError(
                    f"Narwal action failed with result code {response.result_code}"
                )
        await self.coordinator.async_request_refresh()

    async def _async_await_cloud_reset(self) -> None:
        """Translate cloud failures without exposing device identifiers."""
        try:
            await self.coordinator.client.reset_cloud_consumable(type_id=self._protocol_value)
        except NarwalConnectionError as err:
            raise HomeAssistantError(
                "Narwal cloud consumable reset failed",
                translation_domain=DOMAIN,
                translation_key="cloud_consumable_reset_failed",
            ) from err
