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


def test_cloud_recovery_primes_history_without_replaying_it() -> None:
    state = NarwalState()
    coordinator = _coordinator(state, cloud_available=False)
    entity = NarwalNotificationEvent(coordinator)

    historical = [_notification("old-1", 1), _notification("old-2", 2)]
    state.notifications = historical
    state.notifications_loaded = True
    coordinator.cloud_metadata_available = True

    with (
        patch.object(entity, "_trigger_notification") as trigger,
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()

    trigger.assert_not_called()


def test_new_notification_triggers_after_recovery_baseline() -> None:
    state = NarwalState()
    coordinator = _coordinator(state, cloud_available=False)
    entity = NarwalNotificationEvent(coordinator)

    state.notifications = [_notification("old-1", 1)]
    state.notifications_loaded = True
    coordinator.cloud_metadata_available = True
    with (
        patch.object(entity, "_trigger_notification"),
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()

    new_notification = _notification("new-1", 3)
    state.notifications = [new_notification, state.notifications[0]]
    with (
        patch.object(entity, "_trigger_notification") as trigger,
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()

    trigger.assert_called_once_with(new_notification)


def test_other_cloud_success_does_not_prime_notification_history() -> None:
    state = NarwalState()
    coordinator = _coordinator(state, cloud_available=True)
    entity = NarwalNotificationEvent(coordinator)
    with (
        patch.object(entity, "_trigger_notification") as trigger,
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()  # Another endpoint succeeded only.
        assert not entity._primed
        state.notifications = [_notification("old", 1)]
        state.notifications_loaded = True
        entity._handle_coordinator_update()  # First real notification success.
        trigger.assert_not_called()
        assert entity._primed
        state.notifications = [_notification("new", 2), state.notifications[0]]
        entity._handle_coordinator_update()
        assert trigger.call_args.args[0].message_id == "new"


def test_empty_successful_sync_allows_later_notification() -> None:
    state = NarwalState(notifications_loaded=True)
    coordinator = _coordinator(state, cloud_available=True)
    entity = NarwalNotificationEvent(coordinator)

    new_notification = _notification("new-1", 1)
    state.notifications = [new_notification]
    with (
        patch.object(entity, "_trigger_notification") as trigger,
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()

    trigger.assert_called_once_with(new_notification)
