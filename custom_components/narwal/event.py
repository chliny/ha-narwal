"""Event entity for Narwal cloud device notifications."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import NarwalNotification


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cloud notification synchronization."""
    del hass
    async_add_entities([NarwalNotificationEvent(entry.runtime_data)])


class NarwalNotificationEvent(NarwalEntity, EventEntity):
    """Emit newly synchronized app messages as Home Assistant events."""

    _attr_translation_key = "notification"
    _attr_event_types = ["notification", "consumable"]
    _attr_icon = "mdi:robot-vacuum-alert"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("event", "notification")
        state = coordinator.data
        self._seen_ids = {
            item.message_id
            for item in (state.notifications if state is not None else [])
        }

    def _handle_coordinator_update(self) -> None:
        """Emit each newly fetched cloud notification once per runtime."""
        state = self.coordinator.data
        if state is not None:
            current_ids = {item.message_id for item in state.notifications}
            unseen = [
                item
                for item in state.notifications
                if item.message_id not in self._seen_ids
            ]
            for item in sorted(unseen, key=lambda value: value.created_at):
                self._trigger_notification(item)
                self._seen_ids.add(item.message_id)
            self._seen_ids.intersection_update(current_ids)
        super()._handle_coordinator_update()

    def _trigger_notification(self, item: NarwalNotification) -> None:
        self._trigger_event(
            item.category,
            {
                "message_id": item.message_id,
                "title": item.title,
                "content": item.content,
                "created_at": item.created_at,
                "is_read": item.is_read,
            },
        )
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Cloud messages remain available while the robot sleeps."""
        return self.coordinator.last_update_success
