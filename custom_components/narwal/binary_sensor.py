"""Binary sensor entities for Narwal vacuum."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import ConsumableInfo
from .narwal_client.const import (
    CONSUMABLE_MAINTAIN_TYPE_MAP,
    CONSUMABLE_REPLACE_TYPE_MAP,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Narwal binary sensor entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NarwalDockedSensor(coordinator),
            NarwalConsumableReminderSensor(coordinator),
        ]
    )


class NarwalDockedSensor(NarwalEntity, BinarySensorEntity):
    """Binary sensor that reports whether the vacuum is on the dock."""

    _attr_translation_key = "docked"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the docked sensor."""
        super().__init__(coordinator)
        self._set_entity_identity("binary_sensor", "docked")

    @property
    def is_on(self) -> bool | None:
        """Return True if the vacuum is on the dock."""
        state = self.coordinator.data
        if state is None:
            return None
        return state.is_docked


class NarwalConsumableReminderSensor(NarwalEntity, BinarySensorEntity):
    """Mirror the app's launch-time consumable popup as a problem entity.

    The Narwal app re-raises a consumable reminder on every launch. The same
    judgement is available without waiting for push messages: the robot
    reports maintain/replace items through ``consumable/get_consumable_info``
    and the cloud v3 endpoint reports lifetimes that reach zero when used up.
    """

    _attr_translation_key = "consumable_reminder"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the consumable reminder sensor."""
        super().__init__(coordinator)
        self._set_entity_identity("binary_sensor", "consumable_reminder")

    @property
    def is_on(self) -> bool | None:
        """Return True while any consumable needs attention."""
        return bool(self.attention_items)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """List every consumable the app would remind about."""
        return {"items": self.attention_items}

    @property
    def available(self) -> bool:
        """Require cloud lifetime data so an empty fetch cannot read as OK."""
        state = self.coordinator.data
        return bool(
            self.coordinator.cloud_metadata_available and state is not None and state.consumables
        )

    @property
    def attention_items(self) -> list[dict[str, object]]:
        """Return one entry per flagged consumable with its reasons."""
        state = self.coordinator.data
        if state is None or not state.consumables:
            return []
        items: list[dict[str, object]] = []
        for consumable in sorted(state.consumables.values(), key=lambda item: item.name):
            reasons = self._reasons(consumable)
            if reasons:
                items.append(
                    {
                        "name": consumable.name,
                        "reasons": reasons,
                        "remaining_percentage": consumable.remaining_percentage,
                    }
                )
        return items

    def _reasons(self, consumable: ConsumableInfo) -> list[str]:
        """Classify why a consumable needs attention using confirmed data."""
        state = self.coordinator.data
        reasons: list[str] = []
        if consumable.remaining_percentage == 0:
            reasons.append("depleted")
        if state is not None and state.mqtt_consumables_supported:
            maintain = consumable.category == 1
            mapping = CONSUMABLE_MAINTAIN_TYPE_MAP if maintain else CONSUMABLE_REPLACE_TYPE_MAP
            protocol_value = mapping.get(consumable.type_id)
            reported = (
                state.consumable_maintain_items if maintain else state.consumable_replace_items
            )
            if protocol_value is not None and protocol_value in reported:
                reasons.append("maintain" if maintain else "replace")
        return reasons
