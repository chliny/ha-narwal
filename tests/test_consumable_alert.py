"""Regression tests for the app-style consumable reminder binary sensor."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.narwal.binary_sensor import NarwalConsumableReminderSensor
from custom_components.narwal.narwal_client.client import NarwalClient, NarwalCommandTimeoutError
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    ConsumableInfo,
    NarwalState,
)


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


def test_depleted_consumable_turns_on() -> None:
    """A consumable the app reports as used up raises the reminder."""
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


def test_robot_reported_reminder_turns_on_before_depletion() -> None:
    """The robot's replace/maintain flags raise the reminder before lifetime hits zero."""
    replace = NarwalState(consumables={"side_brush": _consumable()})
    replace.mqtt_consumables_supported = replace.mqtt_consumables_available = True
    replace.consumable_replace_items = {3}

    replace_sensor = _sensor(replace)
    assert replace_sensor.is_on
    assert replace_sensor.attention_items[0]["reasons"] == ["replace"]

    maintain = NarwalState(
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
    maintain.mqtt_consumables_supported = maintain.mqtt_consumables_available = True
    maintain.consumable_maintain_items = {2}

    maintain_sensor = _sensor(maintain)
    assert maintain_sensor.is_on
    assert maintain_sensor.attention_items[0]["reasons"] == ["maintain"]


def test_reminder_reports_unknown_until_data_is_current() -> None:
    """A clean-looking snapshot is not reported as healthy while data is unusable."""
    state = NarwalState(consumables={"side_brush": _consumable()})
    entity = _sensor(state)

    assert entity.available
    assert entity.is_on is None  # The robot status has not been confirmed yet.
    assert entity.attention_items == []
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    assert entity.is_on is False

    # A damaged cloud lifetime next to a clean robot report must not read as OK.
    state.consumables["side_brush"] = _consumable(total=0, used=123)
    assert entity.is_on is None
    # A consumable without a lifetime (0/0) is legitimately not measured.
    state.consumables["side_brush"] = _consumable(total=0, used=0)
    assert entity.is_on is False


def test_reminder_is_unavailable_without_current_cloud_data() -> None:
    """An outage hides the reminder instead of claiming everything is fine."""
    state = NarwalState()
    coordinator = _coordinator(state)
    coordinator.data = None
    entity = NarwalConsumableReminderSensor(coordinator)

    assert not entity.available
    assert not entity.is_on

    outage = _sensor(NarwalState(consumables={"side_brush": _consumable(used=100)}))
    outage.coordinator.cloud_metadata_available = False
    assert not outage.available


def test_robot_query_failure_does_not_hide_a_known_cloud_warning() -> None:
    """A failed robot query invalidates the previous all-clear, not the cloud warning."""
    client = NarwalClient(device_id="device", product_id="product")
    client.state.consumables = {"side_brush": _consumable()}
    client.state.mqtt_consumables_supported = client.state.mqtt_consumables_available = True
    reminder = _sensor(client.state)
    assert reminder.available and reminder.is_on is False  # The robot reported no reminder.

    client.send_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[NarwalCommandTimeoutError("timeout"), CommandResponse(data={"1": {"2": [3]}})]
    )
    with pytest.raises(NarwalCommandTimeoutError):
        asyncio.run(client.get_consumable_info())
    assert reminder.available and reminder.is_on is None

    # A definite cloud warning stays visible while the robot status is unknown.
    client.state.consumables["side_brush"] = _consumable(used=100)
    assert reminder.is_on is True

    client.state.consumables["side_brush"] = _consumable()
    asyncio.run(client.get_consumable_info())
    assert reminder.available and reminder.is_on
