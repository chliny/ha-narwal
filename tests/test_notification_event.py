"""Regression tests for Narwal notification event delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.narwal.event import NarwalNotificationEvent
from custom_components.narwal.narwal_client.models import NarwalNotification, NarwalState


def _coordinator(state: NarwalState, *, cloud_available: bool) -> SimpleNamespace:
    """Build the coordinator surface needed by the notification entity."""
    entry = SimpleNamespace(
        data={"device_id": "test-device", "model": "Narwal Test"},
        title="Test robot",
    )
    client = SimpleNamespace(state=state, robot_awake=True)
    return SimpleNamespace(
        config_entry=entry,
        client=client,
        data=state,
        cloud_metadata_available=cloud_available,
    )


def _notification(message_id: str, created_at: int) -> NarwalNotification:
    return NarwalNotification(
        message_id=message_id,
        title="Robot notification",
        content=message_id,
        created_at=created_at,
    )


def _update(entity: NarwalNotificationEvent) -> dict[str, object]:
    """Deliver one coordinator update and return the entity's latest event data."""
    with patch.object(entity, "async_write_ha_state"):
        entity._handle_coordinator_update()
    return entity.state_attributes


def test_recovered_cloud_history_is_not_replayed_as_events() -> None:
    """After a failed startup sync, the first successful fetch only sets a baseline."""
    state = NarwalState()  # notifications_loaded is still False: no reliable baseline
    coordinator = _coordinator(state, cloud_available=False)
    entity = NarwalNotificationEvent(coordinator)

    state.notifications = [_notification("old-1", 1), _notification("old-2", 2)]
    state.notifications_loaded = True
    coordinator.cloud_metadata_available = True

    attributes = _update(entity)

    assert entity.state is None
    assert attributes == {"event_type": None}


def test_new_cloud_notification_is_emitted_once_as_an_event() -> None:
    """A newly synchronized cloud message becomes exactly one HA event."""
    state = NarwalState(notifications_loaded=True)
    coordinator = _coordinator(state, cloud_available=True)
    entity = NarwalNotificationEvent(coordinator)

    state.notifications = [_notification("new-1", 3)]
    attributes = _update(entity)

    assert attributes == {
        "event_type": "notification",
        "message_id": "new-1",
        "title": "Robot notification",
        "content": "new-1",
        "created_at": 3,
        "is_read": False,
    }
    emitted_state = entity.state
    assert emitted_state is not None

    # The same notification is not replayed on the next coordinator refresh.
    assert _update(entity) == attributes
    assert entity.state == emitted_state

    # Consumable reminders keep their distinct event type for user automations.
    reminder = NarwalNotification(
        message_id="new-2", title="滤网", content="请更换滤网", created_at=4
    )
    state.notifications = [reminder, *state.notifications]
    attributes = _update(entity)
    assert attributes["event_type"] == "consumable"
    assert attributes["message_id"] == "new-2"
    assert entity.state != emitted_state
