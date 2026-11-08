"""Focused regression tests for Home Assistant entity behavior."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

# The lightweight development environment omits optional HA stream wheels.
# They are not exercised by these still-image camera unit tests.
if importlib.util.find_spec("numpy") is None:
    sys.modules["numpy"] = types.ModuleType("numpy")
if importlib.util.find_spec("turbojpeg") is None:
    turbojpeg = types.ModuleType("turbojpeg")
    turbojpeg.TurboJPEG = Mock  # type: ignore[attr-defined]
    sys.modules["turbojpeg"] = turbojpeg

from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.button import NarwalResumeBaseSearchButton, NarwalStartCleanPlanButton
from custom_components.narwal.camera import NarwalMapCamera
from custom_components.narwal.const import FAN_SPEED_MAP
from custom_components.narwal.entity import NarwalEntity
from custom_components.narwal.event import NarwalNotificationEvent
from custom_components.narwal.narwal_client import NarwalCommandError
from custom_components.narwal.narwal_client.const import CommandResult
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    ConsumableInfo,
    MapData,
    NarwalState,
)
from custom_components.narwal.number import NarwalVolumeNumber
from custom_components.narwal.select import (
    MOP_HUMIDITY_OPTIONS,
    NarwalCleanPlanSelect,
    NarwalCleanStrategySelect,
    NarwalMapSelect,
    NarwalMopHumiditySelect,
)
from custom_components.narwal.sensor import (
    SENSOR_DESCRIPTIONS,
    NarwalConsumableSensor,
    NarwalDustBagHealthSensor,
    NarwalLatestNotificationSensor,
    NarwalSensor,
    NarwalTaskRecordSensor,
)
from custom_components.narwal.switch import CONFIG_SWITCHES, NarwalConfigSwitch
from custom_components.narwal.update import NarwalFirmwareUpdateEntity
from custom_components.narwal.vacuum import NarwalVacuum


def _coordinator(state: NarwalState | None = None) -> SimpleNamespace:
    """Build the coordinator surface needed by entity unit tests."""
    state = state or NarwalState()
    entry = SimpleNamespace(
        data={"device_id": "test-device", "model": "Narwal Test"},
        title="Test robot",
    )
    client = SimpleNamespace(
        state=state,
        robot_awake=True,
        set_fan_speed=AsyncMock(return_value=CommandResponse(result_code=CommandResult.SUCCESS)),
        resume=AsyncMock(return_value=CommandResponse(result_code=CommandResult.SUCCESS)),
        set_mop_humidity=AsyncMock(return_value=CommandResponse(result_code=CommandResult.SUCCESS)),
    )
    return SimpleNamespace(
        config_entry=entry,
        client=client,
        data=state,
        last_update_success=True,
        cloud_metadata_available=True,
        supports_fan_speed=True,
        async_request_refresh=AsyncMock(),
        async_set_updated_data=Mock(),
    )


def test_battery_sensor_distinguishes_unknown_from_real_zero() -> None:
    """A robot-reported 0% battery is valid, while the default stays unknown."""
    state = NarwalState()
    description = next(item for item in SENSOR_DESCRIPTIONS if item.key == "battery")
    sensor = NarwalSensor(_coordinator(state), description)

    assert sensor.native_value is None

    state.update_from_base_status({"2": 0})
    assert sensor.native_value == 0

    state.battery_level = 42
    assert sensor.native_value == 42


def test_resume_button_rejection_does_not_report_success() -> None:
    state = NarwalState()
    state.update_from_base_status({"3": {"1": 5, "2": 1}, "11": 1})
    coordinator = _coordinator(state)
    coordinator.client.resume = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.GENERAL_FAILED)
    )
    button = NarwalResumeBaseSearchButton(coordinator)
    with pytest.raises(HomeAssistantError, match="result code"):
        asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_not_awaited()


def test_cloud_entities_remain_available_while_robot_is_asleep() -> None:
    """Cloud metadata availability is independent of robot reachability."""
    consumable = ConsumableInfo(
        key="filter",
        name="Filter",
        type_id=1,
        category=2,
        total_duration=100,
        usage_duration=20,
    )
    state = NarwalState(
        consumables={consumable.key: consumable},
        cloud_consumables_available=True,
        task_records_supported=True,
        dust_bag_health_supported=True,
        firmware_update_supported=True,
    )
    coordinator = _coordinator(state)
    coordinator.last_update_success = False
    coordinator.client.robot_awake = False

    cloud_entities = (
        NarwalLatestNotificationSensor(coordinator),
        NarwalTaskRecordSensor(coordinator),
        NarwalDustBagHealthSensor(coordinator),
        NarwalConsumableSensor(coordinator, consumable),
        NarwalNotificationEvent(coordinator),
    )

    assert all(entity.available for entity in cloud_entities)
    # The firmware endpoint already had independent cloud-only availability.
    assert NarwalFirmwareUpdateEntity(coordinator).available

    coordinator.cloud_metadata_available = False
    assert not any(entity.available for entity in cloud_entities)


def test_fan_speed_optimism_yields_to_later_robot_state() -> None:
    """An app-side status update must replace HA's optimistic fan value."""

    async def run_test() -> None:
        coordinator = _coordinator()
        entity = NarwalVacuum(coordinator)
        with patch.object(entity, "async_write_ha_state"):
            await entity.async_set_fan_speed("strong")

        assert coordinator.client.state.raw_base_status["26"] == int(FAN_SPEED_MAP["strong"])
        assert entity.fan_speed == "strong"

        coordinator.client.state.raw_base_status = {"26": int(FAN_SPEED_MAP["normal"])}
        assert entity.fan_speed == "normal"

    asyncio.run(run_test())


def test_room_clean_missing_result_code_raises_ha_error() -> None:
    """An incomplete robot reply must not escape as a TypeError."""

    async def run_test() -> None:
        coordinator = _coordinator()
        coordinator.client.start_rooms = AsyncMock(return_value=CommandResponse())
        entity = NarwalVacuum(coordinator)
        with pytest.raises(HomeAssistantError, match="room clean"):
            await entity.async_clean_segments(["5"])

    asyncio.run(run_test())


def test_mop_humidity_optimism_yields_to_later_robot_state() -> None:
    """An app-side status update must replace HA's optimistic mop value."""

    async def run_test() -> None:
        coordinator = _coordinator()
        entity = NarwalMopHumiditySelect(coordinator)
        with patch.object(entity, "async_write_ha_state"):
            await entity.async_select_option("wet")

        assert coordinator.client.state.raw_base_status["29"] == int(MOP_HUMIDITY_OPTIONS["wet"])
        assert entity.current_option == "wet"

        coordinator.client.state.raw_base_status = {"29": int(MOP_HUMIDITY_OPTIONS["dry"])}
        assert entity.current_option == "dry"

    asyncio.run(run_test())


def test_config_entities_reject_malformed_numeric_values() -> None:
    """Malformed protocol values must not be truncated into valid HA states."""
    coordinator = _coordinator()
    coordinator.device_config = {
        "1": "nan",
        "5": 0.5,
        "18": {"1": object()},
    }

    volume = NarwalVolumeNumber(coordinator)
    high_altitude = NarwalConfigSwitch(coordinator, CONFIG_SWITCHES[0])
    strategy = NarwalCleanStrategySelect(coordinator)

    assert volume.native_value is None
    assert high_altitude.is_on is None
    assert strategy.current_option is None

    coordinator.device_config = {
        "1": "42",
        "5": "1",
        "18": {"1": "2"},
    }
    assert volume.native_value == 42
    assert high_altitude.is_on is True
    assert strategy.current_option == "room"


def test_clean_plan_ids_must_be_integral() -> None:
    """Fractional and boolean cloud IDs must never alias a real plan ID."""
    state = NarwalState(
        clean_plans=[
            {"id": 1.5, "name": "Fractional"},
            {"id": True, "name": "Boolean"},
            {"id": "7", "name": "Valid"},
        ]
    )
    coordinator = _coordinator(state)
    coordinator.selected_clean_plan_id = None
    entity = NarwalCleanPlanSelect(coordinator, {})

    assert entity.options == ["Valid"]
    assert entity.current_option == "Valid"
    assert coordinator.selected_clean_plan_id == 7


def test_config_switch_uses_reported_get_and_set_fields() -> None:
    """Toggling a switch writes the app's config field and reflects it locally."""

    async def run_test() -> None:
        coordinator = _coordinator()
        coordinator.device_config = {"7": 0}
        coordinator.client.set_boolean_config = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        description = next(item for item in CONFIG_SWITCHES if item.key == "child_lock")
        entity = NarwalConfigSwitch(coordinator, description)

        assert entity.is_on is False
        with patch.object(entity, "async_write_ha_state"):
            await entity.async_turn_on()
            coordinator.client.set_boolean_config.assert_awaited_once_with(8, True)
            assert entity.is_on is True
            coordinator.client.set_boolean_config.reset_mock()
            await entity.async_turn_off()
            coordinator.client.set_boolean_config.assert_awaited_once_with(8, False)
            assert entity.is_on is False

    asyncio.run(run_test())


def test_start_clean_plan_rejects_fractional_fallback_id() -> None:
    """A malformed fallback ID must fail locally instead of starting another plan."""

    async def run_test() -> None:
        state = NarwalState(clean_plans=[{"id": 1.5, "name": "Malformed"}])
        coordinator = _coordinator(state)
        coordinator.selected_clean_plan_id = None
        entity = NarwalStartCleanPlanButton(coordinator)

        try:
            await entity.async_press()
        except HomeAssistantError as err:
            assert str(err) == "No saved Narwal cleaning plan is available"
        else:
            raise AssertionError("A fractional clean-plan ID was accepted")

    asyncio.run(run_test())


def test_map_switch_refresh_failure_does_not_misreport_switch_failure() -> None:
    """Once accepted, a map switch succeeds even if its immediate refresh fails."""

    async def run_test() -> None:
        state = NarwalState(
            saved_maps=[
                {"id": 1, "name": "Downstairs"},
                {"id": 2, "name": "Upstairs"},
            ],
            saved_maps_supported=True,
            current_map_id=1,
        )
        coordinator = _coordinator(state)

        async def switch_map(map_id: int) -> CommandResponse:
            state.current_map_id = map_id
            state.map_data = None
            return CommandResponse(result_code=CommandResult.SUCCESS)

        coordinator.client.switch_map = AsyncMock(side_effect=switch_map)
        coordinator.client.get_map = AsyncMock(
            side_effect=NarwalCommandError("map refresh timed out")
        )
        coordinator.client.get_all_maps = AsyncMock()
        entity = NarwalMapSelect(coordinator)

        await entity.async_select_option("Upstairs")

        coordinator.client.switch_map.assert_awaited_once_with(2)
        coordinator.client.get_map.assert_awaited_once_with()
        coordinator.client.get_all_maps.assert_not_awaited()
        assert state.current_map_id == 2
        coordinator.async_set_updated_data.assert_called_once_with(state)

    asyncio.run(run_test())


def _camera_hass(tasks: list[asyncio.Task[None]]) -> SimpleNamespace:
    """Build the minimal hass surface the camera needs to render a frame."""

    def create_task(coro, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    async def run_executor(function, *args):
        return function(*args)

    return SimpleNamespace(async_create_task=create_task, async_add_executor_job=run_executor)


def _valid_map(map_id: int = 7) -> MapData:
    """A small but real map payload the renderer can turn into a PNG."""
    return MapData.from_response(
        {
            "1": 0,
            "2": {
                "1": map_id,
                "3": 60,
                "4": 2,
                "5": 2,
                "17": zlib.compress(b"\x0a\x04\x00\x00\x00\x00"),
            },
        }
    )


def test_camera_serves_first_frame_from_map_present_at_entity_add() -> None:
    """The map fetched during setup is served without waiting for another tick."""

    async def run_test() -> None:
        state = NarwalState()
        state.current_map_id = 7
        state.map_data = _valid_map()
        entity = NarwalMapCamera(_coordinator(state))
        tasks: list[asyncio.Task[None]] = []
        entity.hass = _camera_hass(tasks)
        with (
            patch.object(NarwalEntity, "async_added_to_hass", new=AsyncMock()),
            patch.object(entity, "async_write_ha_state"),
        ):
            await entity.async_added_to_hass()
            await tasks[0]

        image = await entity.async_camera_image()
        assert image is not None and image.startswith(b"\x89PNG")
        assert entity.content_type == "image/png"
        assert entity.available

    asyncio.run(run_test())


def test_camera_stops_serving_previous_floor_after_map_is_cleared() -> None:
    """A failed post-switch download must not expose the previous floor."""

    async def run_test() -> None:
        state = NarwalState()
        state.current_map_id = 7
        state.map_data = _valid_map()
        entity = NarwalMapCamera(_coordinator(state))
        tasks: list[asyncio.Task[None]] = []
        entity.hass = _camera_hass(tasks)
        with (
            patch.object(NarwalEntity, "async_added_to_hass", new=AsyncMock()),
            patch.object(entity, "async_write_ha_state"),
        ):
            await entity.async_added_to_hass()
            await tasks[0]
        assert await entity.async_camera_image() is not None

        # The App switched floors; the new download failed and cleared the map.
        state.current_map_id = 2
        state.map_data = None
        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()

        assert await entity.async_camera_image() is None
        assert not entity.available

    asyncio.run(run_test())
