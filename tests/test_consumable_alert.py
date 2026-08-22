"""Regression tests for the app-style consumable reminder binary sensor."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.narwal.binary_sensor import NarwalConsumableReminderSensor
from custom_components.narwal.narwal_client.models import ConsumableInfo, NarwalState


def _coordinator(state: NarwalState | None) -> SimpleNamespace:
    """Build the coordinator surface needed by the reminder entity."""
    entry = SimpleNamespace(
        data={"device_id": "test-device", "model": "Narwal Test"},
        title="Test robot",
    )
    client = SimpleNamespace(
        state=state or NarwalState(), firmware_version="1.0.0", robot_awake=True
    )
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
    assert not entity.is_on
    assert entity.attention_items == []


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
    state.mqtt_consumables_supported = True
    state.consumable_replace_items = {3}
    entity = _sensor(state)

    assert entity.is_on
    assert entity.attention_items[0]["reasons"] == ["replace"]


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
    state.mqtt_consumables_supported = True
    state.consumable_maintain_items = {2}
    entity = _sensor(state)

    assert entity.is_on
    assert entity.attention_items[0]["reasons"] == ["maintain"]


def test_unmapped_type_ids_never_false_positive() -> None:
    state = NarwalState(consumables={"mystery": _consumable(type_id=999)})
    state.mqtt_consumables_supported = True
    state.consumable_replace_items = {3, 4, 5}
    entity = _sensor(state)

    assert not entity.is_on


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


def test_identity_follows_integration_convention() -> None:
    entity = _sensor(NarwalState())

    assert entity.unique_id == "narwal_test_device_consumable_reminder"
    assert entity.entity_id == "binary_sensor.narwal_test_device_consumable_reminder"
