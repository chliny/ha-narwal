"""Regression tests for the app-style consumable reminder binary sensor."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.narwal.binary_sensor import NarwalConsumableReminderSensor
from custom_components.narwal.coordinator import NarwalCoordinator
from custom_components.narwal.narwal_client.client import NarwalClient, NarwalCommandTimeoutError
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    ConsumableInfo,
    NarwalState,
)
from custom_components.narwal.sensor import NarwalConsumableSensor


def _coordinator(state: NarwalState | None) -> SimpleNamespace:
    """Build the coordinator surface needed by consumable entities."""
    entry = SimpleNamespace(
        data={"device_id": "test-device", "model": "Narwal Test"},
        title="Test robot",
    )
    client = SimpleNamespace(
        state=state or NarwalState(), firmware_version="1.0.0", robot_awake=True
    )
    if state is not None:
        state.cloud_consumables_available = True
    return SimpleNamespace(
        config_entry=entry,
        client=client,
        data=state,
        last_update_success=True,
        cloud_metadata_available=True,
    )


def _sensor(state: NarwalState | None) -> NarwalConsumableReminderSensor:
    return NarwalConsumableReminderSensor(_coordinator(state))


def _consumable(
    key: str = "side_brush",
    category: int = 2,
    type_id: int = 3,
    total: int = 100,
    used: int = 45,
) -> ConsumableInfo:
    return ConsumableInfo(
        key=key,
        name="Side brush",
        type_id=type_id,
        category=category,
        total_duration=total,
        usage_duration=used,
    )


def test_healthy_consumables_stay_off() -> None:
    state = NarwalState(consumables={"side_brush": _consumable()})
    entity = _sensor(state)

    assert entity.available
    assert entity.is_on is None  # The robot status has not been confirmed yet.
    assert entity.attention_items == []
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    assert entity.is_on is False


def test_depleted_consumable_turns_on() -> None:
    state = NarwalState(consumables={"side_brush": _consumable(used=100)})
    entity = _sensor(state)

    assert entity.is_on
    assert entity.attention_items == [
        {
            "name": "Side brush",
            "reasons": ["depleted"],
            "remaining_percentage": 0,
        }
    ]


def test_robot_replace_flag_turns_on_before_depletion() -> None:
    state = NarwalState(consumables={"side_brush": _consumable()})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_replace_items = {3}
    entity = _sensor(state)

    assert entity.is_on
    assert entity.attention_items[0]["reasons"] == ["replace"]

    # Robot status became unknown; the still-current cloud lifetime remains readable.
    lifetime = NarwalConsumableSensor(entity.coordinator, state.consumables["side_brush"])
    state.mqtt_consumables_available = False
    assert entity.available and entity.is_on is None
    assert lifetime.available
    assert "mqtt_reported" not in lifetime.extra_state_attributes
    state.mqtt_consumables_available = True
    assert entity.available and entity.is_on
    assert lifetime.extra_state_attributes["mqtt_reported"] is True


def test_robot_maintain_flag_uses_maintain_mapping() -> None:
    state = NarwalState(
        consumables={
            "filter": ConsumableInfo(
                key="filter",
                name="Filter",
                type_id=1,
                category=1,
                total_duration=100,
                usage_duration=10,
            )
        }
    )
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {2}
    entity = _sensor(state)

    assert entity.is_on
    assert entity.attention_items[0]["reasons"] == ["maintain"]


def test_unmapped_type_ids_never_false_positive() -> None:
    state = NarwalState(consumables={"mystery": _consumable(type_id=999)})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_replace_items = {3, 4, 5}
    entity = _sensor(state)

    assert not entity.is_on


def test_unknown_lifetime_is_not_reported_as_healthy() -> None:
    """A damaged cloud lifetime next to a clean robot report must not read as OK."""
    state = NarwalState(consumables={"side_brush": _consumable(total=0, used=123)})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    entity = _sensor(state)

    assert entity.available
    assert entity.is_on is None
    # A consumable without a lifetime (0/0) is legitimately not measured.
    state.consumables["side_brush"] = _consumable(total=0, used=0)
    assert entity.is_on is False


def test_unknown_cloud_state_is_not_reported_as_ok() -> None:
    state = NarwalState()
    coordinator = _coordinator(state)
    coordinator.data = None
    entity = NarwalConsumableReminderSensor(coordinator)

    assert not entity.available
    assert not entity.is_on


def test_cloud_outage_hides_reminder_instead_of_all_clear() -> None:
    state = NarwalState(consumables={"side_brush": _consumable(used=100)})
    coordinator = _coordinator(state)
    coordinator.cloud_metadata_available = False
    entity = NarwalConsumableReminderSensor(coordinator)

    assert not entity.available


def test_first_robot_query_failure_does_not_report_all_clear() -> None:
    """A robot whose capability is still unknown must not be treated as healthy."""
    client = NarwalClient(device_id="device", product_id="product")
    client.state.consumables = {"side_brush": _consumable()}
    reminder = _sensor(client.state)
    client.send_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[NarwalCommandTimeoutError("timeout"), CommandResponse(data={"1": {"2": [3]}})]
    )
    with pytest.raises(NarwalCommandTimeoutError):
        asyncio.run(client.get_consumable_info())
    assert reminder.available and reminder.is_on is None
    client.state.consumables["side_brush"] = _consumable(used=100)
    assert reminder.is_on is True  # A definite cloud warning is not hidden.
    client.state.consumables["side_brush"] = _consumable()
    asyncio.run(client.get_consumable_info())
    assert reminder.available and reminder.is_on


def test_robot_status_outage_invalidates_reminder_and_rechecks_on_recovery() -> None:
    """A temporarily unreachable robot must not leave an old reminder current."""

    async def run() -> None:
        robot = NarwalClient(device_id="device", product_id="product")
        state = robot.state
        state.consumables = {"side_brush": _consumable()}
        state.cloud_consumables_available = True
        robot.send_command = AsyncMock(  # type: ignore[method-assign]
            return_value=CommandResponse(data={"1": {"2": [3]}})
        )
        await robot.get_consumable_info()
        coordinator = object.__new__(NarwalCoordinator)
        coordinator.client = SimpleNamespace(
            state=state,
            connected=True,
            get_status=AsyncMock(side_effect=[NarwalCommandTimeoutError("timeout"), None]),
            get_dry_mop_remaining_time=AsyncMock(),
            get_clean_timeline=AsyncMock(),
            get_consumable_info=robot.get_consumable_info,
            invalidate_robot_consumables=robot.invalidate_robot_consumables,
        )
        coordinator.config_entry = SimpleNamespace(
            data={"device_id": "test-device", "model": "Narwal Test"}, title="Test robot"
        )
        coordinator.data = state
        coordinator.cloud_metadata_available = True
        coordinator._consecutive_failures = 0
        coordinator._max_failures = 3
        coordinator._feature_discovery_complete = True
        coordinator._config_discovery_complete = True
        coordinator._extended_refresh_count = 0
        coordinator._fast_poll_remaining = 0
        coordinator._async_refresh_cloud_metadata = AsyncMock()
        coordinator._async_maybe_refresh_firmware_update = AsyncMock()
        coordinator._map_refresh_due = Mock(return_value=False)
        reminder = NarwalConsumableReminderSensor(coordinator)
        assert reminder.available and reminder.is_on
        await coordinator._async_update_data()
        assert reminder.available and reminder.is_on is None
        await coordinator._async_update_data()
        assert reminder.available and reminder.is_on
        assert robot.send_command.await_count == 2

    asyncio.run(run())


def test_identity_follows_integration_convention() -> None:
    entity = _sensor(NarwalState())

    assert entity.unique_id == "narwal_test_device_consumable_reminder"
    assert entity.entity_id == "binary_sensor.narwal_test_device_consumable_reminder"
