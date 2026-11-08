"""Offline regression tests for app-confirmed button routes and guards."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.binary_sensor import NarwalConsumableReminderSensor
from custom_components.narwal.button import (
    NarwalConsumableResetButton,
    NarwalResumeBaseSearchButton,
    async_setup_entry,
)
from custom_components.narwal.narwal_client.client import (
    NarwalClient,
    NarwalCommandError,
    NarwalCommandNotSentError,
    NarwalCommandTimeoutError,
    NarwalConnectionError,
)
from custom_components.narwal.narwal_client.cloud_api import (
    CloudAccount,
    CloudDevice,
    NarwalCloudApi,
    NarwalCloudError,
)
from custom_components.narwal.narwal_client.cloud_client import NarwalCloudClient, _service_envelope
from custom_components.narwal.narwal_client.const import (
    BASE_STATION_NOT_FOUND_ERROR,
    CommandResult,
)
from custom_components.narwal.narwal_client.error_codes import (
    ROBOT_ERROR_MESSAGES,
    error_message,
    load_error_catalog,
)
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    ConsumableInfo,
    NarwalState,
)
from custom_components.narwal.sensor import NarwalConsumableSensor, NarwalRobotErrorSensor
from custom_components.narwal.vacuum import NarwalVacuum


def _item(
    type_id: int,
    *,
    category: int = 2,
    reset_supported: bool = True,
    name: str = "Consumable",
) -> ConsumableInfo:
    return ConsumableInfo(
        key=f"type_{type_id}",
        name=name,
        type_id=type_id,
        category=category,
        total_duration=100,
        usage_duration=10,
        reset_supported=reset_supported,
    )


def _coordinator(state: NarwalState) -> SimpleNamespace:
    # Existing entity scenarios start with a successful cloud snapshot.
    state.cloud_consumables_available = True
    return SimpleNamespace(
        config_entry=SimpleNamespace(
            data={"device_id": "test-device", "model": "Narwal Test"}, title="Robot"
        ),
        client=SimpleNamespace(
            state=state,
            robot_awake=True,
            reset_consumable=AsyncMock(
                return_value=CommandResponse(result_code=CommandResult.SUCCESS)
            ),
            reset_cloud_consumable=AsyncMock(),
        ),
        data=state,
        cloud_metadata_available=True,
        last_update_success=True,
        supports_single_dry_mop=False,
        supports_dust_gathering=False,
        async_add_listener=Mock(return_value=Mock()),
        async_update_listeners=Mock(),
        async_request_refresh=AsyncMock(),
    )


def test_base_search_error_decodes_from_wire_and_clears_on_snapshot() -> None:
    client = NarwalClient(device_id="device", product_id="product")
    payload = client._encode_bytes_field(
        1, client._encode_varint_field(1, BASE_STATION_NOT_FOUND_ERROR)
    )
    state = client.state
    state.update_from_base_status(client._decode_protobuf(payload))
    assert state.error_codes == (BASE_STATION_NOT_FOUND_ERROR,)
    coordinator = _coordinator(state)
    sensor = NarwalRobotErrorSensor(coordinator)
    assert sensor.native_value == "机器人找不到基站，请放入基站并对准充电片"
    assert sensor.extra_state_attributes["errors"] == [
        {"code": "0x02100018", "message": "机器人找不到基站，请放入基站并对准充电片"}
    ]
    state.update_from_base_status({"1": [{"1": 0x02100017}]})
    assert sensor.native_value == "机器人回基站失败，请检查"  # a distinct app error
    state.update_from_base_status({})
    assert state.error_codes == ()


def test_j4_error_catalog_has_readable_messages_and_unknown_fallback() -> None:
    assert len(ROBOT_ERROR_MESSAGES) == 185
    assert error_message(0x02100018) == "机器人找不到基站，请放入基站并对准充电片"
    assert error_message(0x02100017) == "机器人回基站失败，请检查"
    assert error_message(0xDEADBEEF) == "未知机器人故障（0xDEADBEEF）"


def test_robot_error_entity_uses_system_language_and_preserves_unknown_codes() -> None:
    """The entity shows a usable localized fault, not a raw catalog key."""
    state = NarwalState()
    state.update_from_base_status({"1": [{"1": 0x02100050}, {"1": 0xDEADBEEF}]})
    sensor = NarwalRobotErrorSensor(_coordinator(state), load_error_catalog())
    sensor.hass = SimpleNamespace(config=SimpleNamespace(language="fr"))

    assert "station de base" in sensor.native_value.lower()
    assert sensor.extra_state_attributes["errors"][0]["message"] == sensor.native_value
    assert sensor.extra_state_attributes["errors"][1] == {
        "code": "0xDEADBEEF",
        "message": "0xDEADBEEF",
    }


def test_robot_error_sensor_combines_multiple_active_errors() -> None:
    state = NarwalState()
    state.update_from_base_status({"1": [{"1": 0x02100018}, {"1": 0x02100017}]})
    sensor = NarwalRobotErrorSensor(_coordinator(state))
    # The state shows one vendor message; the attribute lists every active code.
    assert sensor.native_value == "机器人找不到基站，请放入基站并对准充电片"
    assert [error["code"] for error in sensor.extra_state_attributes["errors"]] == [
        "0x02100018",
        "0x02100017",
    ]
    state.update_from_base_status({})
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"errors": []}


def test_start_and_resume_button_follow_paused_task_not_error_or_dock() -> None:
    """Start resumes paused tasks; a docked idle robot starts a new clean."""
    state = NarwalState()
    coordinator = _coordinator(state)
    coordinator.client.start = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    coordinator.client.resume = AsyncMock(
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    button = NarwalResumeBaseSearchButton(coordinator)
    vacuum = NarwalVacuum(coordinator)

    state.update_from_base_status(
        {"1": {"1": BASE_STATION_NOT_FOUND_ERROR}, "3": {"1": 1}, "11": 2}
    )
    assert not button.available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(button.async_press())
    asyncio.run(vacuum.async_start())
    coordinator.client.start.assert_awaited_once()
    coordinator.client.resume.assert_not_awaited()

    state.update_from_base_status({"1": {"1": 0x02100017}, "3": {"1": 5, "2": 1}, "11": 1})
    assert button.available
    assert vacuum.activity == VacuumActivity.PAUSED
    asyncio.run(vacuum.async_start())
    coordinator.client.resume.assert_awaited_once_with(timeout=10.0)
    coordinator.client.start.assert_awaited_once()

    state.update_from_base_status(
        {
            "1": [{"1": BASE_STATION_NOT_FOUND_ERROR}, {"1": 0x02100017}],
            "3": {"1": 14, "2": 1},
            "11": 2,
        }
    )
    assert button.available
    assert vacuum.activity == VacuumActivity.PAUSED
    asyncio.run(button.async_press())
    assert coordinator.client.resume.await_count == 2
    coordinator.client.start.assert_awaited_once()

    state.update_from_base_status({"3": {"1": 14}, "11": 2})
    assert not button.available
    assert coordinator.async_request_refresh.await_count == 3


def test_base_search_retry_exact_command_topic_and_payload() -> None:
    client = NarwalClient(device_id="device", product_id="product")
    client.send_command = AsyncMock(return_value=CommandResponse(result_code=CommandResult.SUCCESS))
    asyncio.run(client.resume(timeout=10.0))
    client.send_command.assert_awaited_once_with("task/resume", timeout=10.0)


def test_reset_request_routes_to_exact_robot_topic_and_protobuf() -> None:
    client = NarwalClient(device_id="device", product_id="product")
    client.send_command = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResponse(result_code=CommandResult.SUCCESS)
    )
    asyncio.run(client.reset_consumable(category="replace", protocol_value=3))
    assert client.send_command.await_args_list == [
        call("consumable/reset_consumable_info", b"\x0a\x02\x10\x03"),
        call("consumable/get_consumable_info"),
    ]

    client.send_command.reset_mock()
    asyncio.run(client.reset_consumable(category="maintain", protocol_value=1))
    assert client.send_command.await_args_list[0] == call(
        "consumable/reset_consumable_info", b"\x0a\x02\x08\x01"
    )

    client.send_command.reset_mock()
    with pytest.raises(ValueError):
        asyncio.run(client.reset_consumable(category="unknown", protocol_value=3))
    with pytest.raises(ValueError):
        asyncio.run(client.reset_consumable(category="replace", protocol_value=21))
    client.send_command.assert_not_awaited()


def test_cloud_reset_uses_app_v1_path_and_exact_payload() -> None:
    api = NarwalCloudApi(Mock(), phone="phone", password="")
    api.account = CloudAccount(token="token", uuid="user")
    api._request = AsyncMock(return_value={})  # type: ignore[method-assign]

    asyncio.run(api.reset_cloud_consumable(device_id="device", product_id="product", type_id=110))
    api._request.assert_awaited_once_with(
        "POST",
        "/consumables-management-app-server/v1/consumables/reset",
        json={
            "product_id": "product",
            "device_id": "device",
            "type": [110],
            "language": "zh_CN",
        },
        device_id="device",
        product_id="product",
    )
    with pytest.raises(ValueError):
        asyncio.run(api.reset_cloud_consumable(device_id="device", product_id="product", type_id=3))
    assert api._request.await_count == 1


def _reset_buttons(added: list) -> dict[str, NarwalConsumableResetButton]:
    return {
        entity.unique_id: entity
        for entity in added
        if isinstance(entity, NarwalConsumableResetButton)
    }


def test_cloud_dust_box_and_wash_ribs_resets_use_app_enums() -> None:
    """The App resets these maintain items via MQTT maintain enums 1 and 4."""
    state = NarwalState(
        consumables={
            item.key: item
            for item in (
                _item(101, category=1, name="尘盒"),
                _item(104, category=1, name="基站清洁组件"),
            )
        }
    )
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1, 4}
    coordinator = _coordinator(state)
    added: list = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))

    buttons = _reset_buttons(added)
    assert set(buttons) == {
        "narwal_test_device_reset_consumable_type_101",
        "narwal_test_device_reset_consumable_type_104",
    }
    asyncio.run(buttons["narwal_test_device_reset_consumable_type_101"].async_press())
    asyncio.run(buttons["narwal_test_device_reset_consumable_type_104"].async_press())
    assert [call.kwargs for call in coordinator.client.reset_consumable.await_args_list] == [
        {"category": "maintain", "protocol_value": 1},
        {"category": "maintain", "protocol_value": 4},
    ]


def test_missing_item_type_does_not_create_a_guessed_reset_button() -> None:
    """The cloud type id is global, so a missing category must fail closed."""
    state = NarwalState(consumables={"type_101": _item(101, category=0)})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1}
    coordinator = _coordinator(state)
    added: list = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))

    # The missing cloud category cannot authorize a reset; the independently
    # reported robot maintenance item still has its own button.
    assert set(_reset_buttons(added)) == {"narwal_test_device_reset_consumable_maintain_1"}


def test_reset_buttons_exist_before_robot_answers_consumable_query() -> None:
    """Entity registration must not depend on the robot being awake at startup."""
    state = NarwalState(consumables={"type_3": _item(3)})
    coordinator = _coordinator(state)  # mqtt_consumables_supported is still False
    added: list = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))

    buttons = _reset_buttons(added)
    assert set(buttons) == {"narwal_test_device_reset_consumable_type_3"}
    # The entity exists but cannot be pressed until the robot confirms support.
    assert not buttons["narwal_test_device_reset_consumable_type_3"].available


def test_packed_robot_response_enables_reported_maintenance_reset() -> None:
    """A robot maintenance reminder creates a usable reset button."""
    client = NarwalCloudClient(
        Mock(), phone="phone", password="password", device_id="device", product_id="product"
    )
    coordinator = _coordinator(client.state)
    added: list = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())
    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))
    assert not _reset_buttons(added)

    # ConsumableInfoPayload.maintainItems = [1], packed as on the robot.
    client._publish_request = AsyncMock(  # type: ignore[method-assign]
        return_value=_service_envelope("uuid", b"\x0a\x03\x0a\x01\x01")
    )
    asyncio.run(client.get_consumable_info())
    coordinator.async_add_listener.call_args.args[0]()
    button = _reset_buttons(added)["narwal_test_device_reset_consumable_maintain_1"]
    assert button.available
    asyncio.run(button.async_press())
    coordinator.client.reset_consumable.assert_awaited_once_with(
        category="maintain", protocol_value=1
    )

    # The robot query can fail while the overall coordinator still reports OK.
    client._publish_request.side_effect = TimeoutError()
    coordinator.cloud_metadata_available = False
    with pytest.raises(NarwalCommandTimeoutError):
        asyncio.run(client.get_consumable_info())
    assert not button.available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(button.async_press())
    coordinator.client.reset_consumable.assert_awaited_once()

    # A fresh cloud declaration independently authorizes the same operation.
    coordinator.cloud_metadata_available = True
    client.state.consumables["type_101"] = _item(101, category=1, reset_supported=False)
    assert button.available
    client.state.consumables.clear()
    assert not button.available

    # A successful empty robot reply clears the reminder; a later report restores it.
    client._publish_request.side_effect = None
    client._publish_request.return_value = _service_envelope("uuid", b"\x0a\x00")
    asyncio.run(client.get_consumable_info())
    assert not button.available
    client._publish_request.return_value = _service_envelope("uuid", b"\x0a\x03\x0a\x01\x01")
    asyncio.run(client.get_consumable_info())
    assert button.available

    # A timeout is ambiguous even if later robot and cloud reads succeed.
    with (
        patch.object(client, "send_command", AsyncMock(side_effect=NarwalCommandTimeoutError())),
        pytest.raises(NarwalCommandTimeoutError),
    ):
        asyncio.run(client.reset_consumable(category="maintain", protocol_value=1))
    asyncio.run(client.get_consumable_info())
    client.state.consumables["type_101"] = _item(101, category=1, reset_supported=False)
    assert not button.available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(button.async_press())
    client.state.consumables["type_101"] = _item(101, category=1)
    cloud_button = NarwalConsumableResetButton(
        coordinator,
        key="reset_consumable_type_101",
        category="maintain",
        protocol_value=1,
        translation_key="reset_consumable",
        consumable_key="type_101",
        consumable_type_id=101,
    )
    assert not cloud_button.available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(cloud_button.async_press())


def test_robot_maintain_items_add_resets_absent_from_cloud_list() -> None:
    """Robot-reported maintain items back the App resets the cloud omits."""
    state = NarwalState(consumables={})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1, 4}
    coordinator = _coordinator(state)
    added: list = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))

    buttons = _reset_buttons(added)
    assert set(buttons) == {
        "narwal_test_device_reset_consumable_maintain_1",
        "narwal_test_device_reset_consumable_maintain_4",
    }
    asyncio.run(buttons["narwal_test_device_reset_consumable_maintain_1"].async_press())
    asyncio.run(buttons["narwal_test_device_reset_consumable_maintain_4"].async_press())
    assert [call.kwargs for call in coordinator.client.reset_consumable.await_args_list] == [
        {"category": "maintain", "protocol_value": 1},
        {"category": "maintain", "protocol_value": 4},
    ]

    # The cloud list can arrive later than the robot status. The old fallback
    # must stop accepting presses when the cloud-backed button is registered.
    state.consumables["type_101"] = _item(101, category=1)
    coordinator.async_add_listener.call_args.args[0]()
    cloud_button = _reset_buttons(added)["narwal_test_device_reset_consumable_type_101"]
    assert cloud_button.available
    assert not buttons["narwal_test_device_reset_consumable_maintain_1"].available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(buttons["narwal_test_device_reset_consumable_maintain_1"].async_press())
    assert coordinator.client.reset_consumable.await_count == 2

    # If the cloud withdraws the item again, its old button cannot silently
    # become a robot-only fallback under a different entity identity.
    state.consumables.clear()
    assert not cloud_button.available
    assert buttons["narwal_test_device_reset_consumable_maintain_1"].available


def test_declared_maintenance_can_reset_without_active_reminder() -> None:
    """Declared maintenance resets remain usable after a reminder is cleared."""
    state = NarwalState(
        consumables={
            item.key: item
            for item in (
                _item(101, category=1, reset_supported=False),
                _item(104, category=1, reset_supported=False),
            )
        }
    )
    coordinator = _coordinator(state)
    added: list = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))

    buttons = _reset_buttons(added)
    dust = buttons["narwal_test_device_reset_consumable_maintain_1"]
    base = buttons["narwal_test_device_reset_consumable_maintain_4"]
    assert not dust.available and not base.available  # Registered before MQTT answers.
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    assert dust.available and base.available  # No reminder, but both items are declared.
    asyncio.run(dust.async_press())
    coordinator.client.reset_consumable.assert_awaited_once_with(
        category="maintain", protocol_value=1
    )
    state.consumables.clear()
    assert not dust.available and not base.available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(base.async_press())


def test_buttons_created_without_active_maintenance_alert() -> None:
    state = NarwalState(consumables={item.key: item for item in (_item(3), _item(110))})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    coordinator = _coordinator(state)
    added: list[NarwalConsumableResetButton] = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.extend(entities)))

    assert {entity.unique_id for entity in added} == {
        "narwal_test_device_reset_consumable_type_3",
        "narwal_test_device_reset_consumable_type_110",
        "narwal_test_device_resume_base_search",
    }
    assert state.consumable_replace_items == set()

    robot_button = next(
        entity for entity in added if getattr(entity, "_category", None) == "replace"
    )
    cloud_button = next(entity for entity in added if getattr(entity, "_category", None) == "cloud")
    asyncio.run(robot_button.async_press())
    coordinator.client.reset_consumable.assert_awaited_once_with(
        category="replace", protocol_value=3
    )
    asyncio.run(cloud_button.async_press())
    coordinator.client.reset_cloud_consumable.assert_awaited_once_with(type_id=110)
    assert coordinator.async_request_refresh.await_count == 2

    state.consumables[robot_button._consumable_key] = _item(3, reset_supported=False)
    assert not robot_button.available
    with pytest.raises(HomeAssistantError):
        asyncio.run(robot_button.async_press())
    assert coordinator.client.reset_consumable.await_count == 1

    coordinator.cloud_metadata_available = False
    assert not cloud_button.available


def test_partial_consumables_failure_disables_cloud_authorized_resets() -> None:
    """A stale consumable list must not authorize resets after a partial outage."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice(
            device_id="device",
            product_id="product",
            name="Robot",
            firmware_version="1",
            serial_number="serial",
        )
        items = [
            _item(3),
            _item(110),
            _item(101, category=1),
            _item(104, category=1, reset_supported=False),
        ]
        client._api = SimpleNamespace(
            consumables=AsyncMock(side_effect=[items, NarwalCloudError("unavailable"), items, []]),
            notifications=AsyncMock(side_effect=[[], ["new notification"], [], []]),
            task_records=AsyncMock(return_value=[]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
        )
        client.reset_consumable = AsyncMock(
            return_value=CommandResponse(result_code=CommandResult.SUCCESS)
        )
        client.reset_cloud_consumable = AsyncMock()
        state = client.state
        client._robot_awake = True
        client._last_broadcast_time = time.monotonic()
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        coordinator = _coordinator(state)
        coordinator.client = client
        added: list = []
        entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())

        await client.get_cloud_metadata()
        lifetime = NarwalConsumableSensor(coordinator, state.consumables["type_3"])
        reminder_sensor = NarwalConsumableReminderSensor(coordinator)
        assert lifetime.available and reminder_sensor.available
        await async_setup_entry(Mock(), entry, lambda entities: added.extend(entities))
        buttons = _reset_buttons(added)
        dependent = [
            buttons["narwal_test_device_reset_consumable_type_3"],
            buttons["narwal_test_device_reset_consumable_type_110"],
            buttons["narwal_test_device_reset_consumable_maintain_4"],
            buttons["narwal_test_device_reset_consumable_type_101"],
        ]
        assert all(button.available for button in dependent)

        await client.get_cloud_metadata()  # Other metadata still succeeds.
        assert not lifetime.available and not reminder_sensor.available
        coordinator.async_add_listener.call_args.args[0]()
        reminder = _reset_buttons(added)["narwal_test_device_reset_consumable_maintain_1"]
        for button in dependent:
            assert not button.available
            with pytest.raises(HomeAssistantError, match="not currently available"):
                await button.async_press()
        client.reset_consumable.assert_not_awaited()
        client.reset_cloud_consumable.assert_not_awaited()
        assert reminder.available
        await reminder.async_press()
        client.reset_consumable.assert_awaited_once_with(category="maintain", protocol_value=1)

        await client.get_cloud_metadata()
        assert lifetime.available and reminder_sensor.available
        assert all(button.available for button in dependent)
        assert not reminder.available  # The valid cloud-backed button takes precedence again.
        await dependent[0].async_press()
        await dependent[1].async_press()
        await dependent[2].async_press()
        client.reset_cloud_consumable.assert_awaited_once_with(type_id=110)
        assert client.reset_consumable.await_count == 3

        await client.get_cloud_metadata()
        assert all(not button.available for button in dependent)  # A successful empty list.

        assert reminder.available
        for button in dependent:
            with pytest.raises(HomeAssistantError, match="not currently available"):
                await button.async_press()
        assert client.reset_consumable.await_count == 3
        client.reset_cloud_consumable.assert_awaited_once()

    asyncio.run(run())


def test_malformed_cloud_consumables_keep_cache_but_disable_entities() -> None:
    """A damaged external response must not look like a successful empty list."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        api = NarwalCloudApi(Mock(), phone="phone", password="password")
        api.account = CloudAccount(token="token", uuid="user")
        api._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "consumable_list": [
                        {
                            "type": 3,
                            "item_type": 2,
                            "name": "Brush",
                            "total_duration": 100,
                            "usage_duration": 20,
                            "reset_btn_switch": 1,
                        }
                    ]
                },
                {"consumable_list": "invalid"},
                {"consumable_list": [None]},
                {
                    "consumable_list": [
                        {"type": 3, "total_duration": 100, "usage_duration": "invalid"}
                    ]
                },
                {"consumable_list": []},
            ]
        )
        api.notifications = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.task_records = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health_current = AsyncMock(return_value={})  # type: ignore[method-assign]
        client._api = api
        coordinator = _coordinator(client.state)
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        await client.get_cloud_metadata()
        item = state.consumables["type_3"]
        lifetime = NarwalConsumableSensor(coordinator, item)
        added: list = []
        entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())
        await async_setup_entry(Mock(), entry, lambda entities: added.extend(entities))
        button = _reset_buttons(added)["narwal_test_device_reset_consumable_type_3"]
        assert lifetime.available and lifetime.native_value == 80 and button.available

        for _ in range(3):
            await client.get_cloud_metadata()  # Other metadata endpoints still succeed.
            assert state.consumables["type_3"] is item
            assert not lifetime.available and not button.available
            with pytest.raises(HomeAssistantError, match="not currently available"):
                await button.async_press()
        coordinator.client.reset_consumable.assert_not_awaited()
        await client.get_cloud_metadata()  # A valid empty list really removes the item.
        assert not state.consumables and not lifetime.available and not button.available

    asyncio.run(run())


def test_missing_usage_duration_keeps_freshly_reset_consumable_usable() -> None:
    """The cloud omits a zero usage duration right after replacement/reset."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        api = NarwalCloudApi(Mock(), phone="phone", password="password")
        api.account = CloudAccount(token="token", uuid="user")
        missing: dict[str, object] = {
            "type": 3,
            "item_type": 2,
            "name": "Brush",
            "total_duration": 300,
        }
        api._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"consumable_list": [missing, {**missing, "usage_duration": None}]}
        )
        api.notifications = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.task_records = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health_current = AsyncMock(return_value={})  # type: ignore[method-assign]
        client._api = api
        coordinator = _coordinator(client.state)
        await client.get_cloud_metadata()

        assert client.state.cloud_consumables_available
        item = client.state.consumables["type_3"]
        lifetime = NarwalConsumableSensor(coordinator, item)
        assert lifetime.available and lifetime.native_value == 100

    asyncio.run(run())


def test_mqtt_disconnect_invalidates_reminder_and_ignores_delayed_reply() -> None:
    """Disconnect cannot leave a pre-disconnect reminder marked as current."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        state = client.state
        state.consumables = {"type_3": _item(3)}
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        coordinator = _coordinator(state)
        reminder = NarwalConsumableReminderSensor(coordinator)
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_maintain_1",
            category="maintain",
            protocol_value=1,
            translation_key="reset_maintain_dust_bin",
        )
        assert button.available
        started, release = asyncio.Event(), asyncio.Event()
        reads = 0

        async def send(*args: object, **kwargs: object) -> CommandResponse:
            nonlocal reads
            reads += 1
            if reads == 1:
                started.set()
                await release.wait()
            return CommandResponse(data={"1": {"1": [1]}})

        client.send_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        client.on_connection_lost = Mock()
        client._mqtt_ready.set()
        old_read = asyncio.create_task(client.get_consumable_info())
        await started.wait()
        client._mark_disconnected()
        client.on_connection_lost.assert_called_once()
        assert not button.available and reminder.is_on is None
        release.set()
        await old_read
        assert not button.available and reminder.is_on is None
        await client.get_consumable_info()
        assert button.available

    asyncio.run(run())


def test_confirmed_reset_keeps_button_disabled_until_its_read_returns() -> None:
    """The window between the ACK and the confirming read must not allow a second reset."""

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        state.consumables = {"type_101": _item(101, category=1)}
        coordinator = _coordinator(state)
        coordinator.client.reset_consumable = client.reset_consumable
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_type_101",
            category="maintain",
            protocol_value=1,
            translation_key="reset_consumable",
            consumable_key="type_101",
            consumable_type_id=101,
        )
        assert button.available
        read_started, release_read = asyncio.Event(), asyncio.Event()

        async def send(topic: str, *args: object, **kwargs: object) -> CommandResponse:
            if topic == "consumable/get_consumable_info":
                read_started.set()
                await release_read.wait()
                return CommandResponse(data={"1": {"1": []}})
            return CommandResponse(result_code=CommandResult.SUCCESS)

        client.send_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        press = asyncio.create_task(button.async_press())
        await read_started.wait()
        assert not button.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()
        release_read.set()
        await press
        assert client.send_command.await_count == 2
        assert not state.consumable_maintain_items

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["suback_timeout", "disconnect_before_publish"])
def test_reset_awaiting_suback_never_publishes_and_can_be_retried(failure: str) -> None:
    """The command is not sent until the response subscription is acknowledged."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client._api.account = CloudAccount(token="token", uuid="user")
        mqtt_client = client._mqtt = Mock()
        mqtt_client.subscribe.return_value = (0, 10)
        client._mqtt_ready.set()
        client._accept_mqtt_messages = True
        with patch(
            "custom_components.narwal.narwal_client.cloud_client.COMMAND_RESPONSE_TIMEOUT", 0.01
        ):
            task = asyncio.create_task(
                client.reset_consumable(category="maintain", protocol_value=1)
            )
            if failure == "disconnect_before_publish":
                while not client._subscriptions:
                    await asyncio.sleep(0)
                client._mark_disconnected(mqtt_client)
            with pytest.raises(NarwalConnectionError):
                await task
            # A retry reaches the transport again instead of staying blocked.
            with pytest.raises(NarwalConnectionError):
                await client.reset_consumable(category="maintain", protocol_value=1)
        mqtt_client.publish.assert_not_called()

    asyncio.run(run())


def test_reset_that_was_never_published_can_be_retried() -> None:
    """A transport failure before publishing did not reset anything."""

    disconnected = NarwalCloudClient(
        Mock(), phone="phone", password="password", device_id="device", product_id="product"
    )
    assert not disconnected.connected
    for _ in range(2):
        # The second attempt only reaches the transport when the first one is
        # not mistaken for an unknown outcome.
        with pytest.raises(NarwalConnectionError):
            asyncio.run(disconnected.reset_consumable(category="maintain", protocol_value=1))

    subscription_failed = NarwalCloudClient(
        Mock(), phone="phone", password="password", device_id="device", product_id="product"
    )
    subscription_failed._mqtt = Mock()
    subscription_failed._mqtt_ready.set()
    subscription_failed._api.account = CloudAccount(token="token", uuid="user")
    subscription_failed._mqtt.subscribe.return_value = (1, None)
    for _ in range(2):
        with pytest.raises(NarwalConnectionError):
            asyncio.run(subscription_failed.reset_consumable(category="maintain", protocol_value=1))

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        coordinator = _coordinator(state)
        coordinator.client.reset_consumable = client.reset_consumable
        client.send_command = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                NarwalCommandNotSentError("MQTT response subscription failed"),
                CommandResponse(data={"1": {"1": [1]}}),
                CommandResponse(result_code=CommandResult.SUCCESS),
                CommandResponse(data={"1": {"1": []}}),
            ]
        )
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_maintain_1",
            category="maintain",
            protocol_value=1,
            translation_key="reset_maintain_dust_bin",
        )
        assert button.available
        with pytest.raises(HomeAssistantError, match="connection is unavailable"):
            await button.async_press()
        # The next status poll confirms the reminder, so the user may press again.
        await client.get_consumable_info()
        assert button.available
        await button.async_press()
        assert client.send_command.await_count == 4
        assert not state.consumable_maintain_items

    asyncio.run(run())


def test_inflight_robot_read_cannot_overwrite_reset_or_allow_parallel_reset() -> None:
    """A delayed pre-reset reply cannot restore an old robot reminder."""

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        read_started, release_read = asyncio.Event(), asyncio.Event()
        reset_started, release_reset = asyncio.Event(), asyncio.Event()
        reads = 0

        async def send(topic: str, *args: object, **kwargs: object) -> CommandResponse:
            nonlocal reads
            if topic == "consumable/get_consumable_info":
                reads += 1
                if reads == 1:
                    read_started.set()
                    await release_read.wait()
                    return CommandResponse(data={"1": {"1": [1]}})
                return CommandResponse(data={"1": {"1": []}})
            reset_started.set()
            await release_reset.wait()
            return CommandResponse(result_code=CommandResult.SUCCESS)

        client.send_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        old_read = asyncio.create_task(client.get_consumable_info())
        await read_started.wait()
        reset = asyncio.create_task(client.reset_consumable(category="maintain", protocol_value=1))
        await reset_started.wait()
        with pytest.raises(NarwalCommandError):
            await client.reset_consumable(category="maintain", protocol_value=1)
        release_reset.set()
        await reset
        assert not state.mqtt_consumables_available and not state.consumable_maintain_items
        release_read.set()
        await old_read
        assert not state.mqtt_consumables_available and not state.consumable_maintain_items

    asyncio.run(run())


def test_inflight_cloud_read_cannot_overwrite_reset_result() -> None:
    """The latest post-reset cloud snapshot wins over an older delayed read."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        old = _item(110)
        new = _item(110, reset_supported=False)
        read_started, release_read = asyncio.Event(), asyncio.Event()
        reads = 0

        async def read(**kwargs: object) -> list[ConsumableInfo]:
            nonlocal reads
            reads += 1
            if reads == 1:
                read_started.set()
                await release_read.wait()
                return [old]
            return [new]

        reset_started, release_reset = asyncio.Event(), asyncio.Event()

        async def reset_request(**kwargs: object) -> None:
            reset_started.set()
            await release_reset.wait()

        client._api = SimpleNamespace(
            consumables=AsyncMock(side_effect=read),
            reset_cloud_consumable=AsyncMock(side_effect=reset_request),
            notifications=AsyncMock(return_value=[]),
            task_records=AsyncMock(return_value=[]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
        )
        old_read = asyncio.create_task(client.get_cloud_metadata())
        await read_started.wait()
        reset = asyncio.create_task(client.reset_cloud_consumable(type_id=110))
        await reset_started.wait()
        with pytest.raises(NarwalConnectionError):
            await client.reset_cloud_consumable(type_id=110)
        release_reset.set()
        await reset
        client._api.reset_cloud_consumable.assert_awaited_once()
        await client.get_cloud_metadata()
        assert client.state.consumables[old.key] is new
        release_read.set()
        await old_read
        assert client.state.cloud_consumables_available
        assert client.state.consumables[old.key] is new

    asyncio.run(run())


def test_successful_robot_reset_is_not_reported_failed_when_followup_query_times_out() -> None:
    """A confirmed write must not invite a retry merely because its read failed."""
    client = NarwalClient(device_id="device", product_id="product")
    state = client.state
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1}
    coordinator = _coordinator(state)
    coordinator.cloud_metadata_available = False
    coordinator.client.reset_consumable = client.reset_consumable
    client.send_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            CommandResponse(result_code=CommandResult.SUCCESS),
            NarwalCommandTimeoutError(),
        ]
    )
    button = NarwalConsumableResetButton(
        coordinator,
        key="reset_consumable_maintain_1",
        category="maintain",
        protocol_value=1,
        translation_key="reset_maintain_dust_bin",
    )
    assert button.available
    coordinator.async_request_refresh.side_effect = RuntimeError("refresh failed")
    asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_awaited_once()
    assert not button.available
    assert client.send_command.await_count == 2


def test_ambiguous_cloud_reset_disables_cached_entities_until_refreshed() -> None:
    """A cloud reset timeout might mean the reset was already applied."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        item = _item(110)
        state = client.state
        state.consumables = {item.key: item}
        coordinator = _coordinator(state)
        coordinator.client.reset_cloud_consumable = client.reset_cloud_consumable
        client._api = SimpleNamespace(
            reset_cloud_consumable=AsyncMock(side_effect=NarwalCloudError("timeout")),
            consumables=AsyncMock(return_value=[item]),
            notifications=AsyncMock(return_value=[]),
            task_records=AsyncMock(return_value=[]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
        )
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_type_110",
            category="cloud",
            protocol_value=110,
            translation_key="reset_consumable",
            consumable_key=item.key,
            consumable_type_id=110,
        )
        lifetime = NarwalConsumableSensor(coordinator, item)
        assert button.available and lifetime.available
        with pytest.raises(HomeAssistantError, match="cloud consumable reset failed"):
            await button.async_press()
        assert not button.available and not lifetime.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()
        client._api.reset_cloud_consumable.assert_awaited_once()
        await client.get_cloud_metadata()
        assert not button.available and lifetime.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()
        client._api.reset_cloud_consumable.assert_awaited_once()

    asyncio.run(run())


def test_failed_robot_reset_does_not_refresh() -> None:
    state = NarwalState(consumables={"type_3": _item(3)})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    coordinator = _coordinator(state)
    coordinator.client.reset_consumable.return_value = CommandResponse(result_code=2)
    button = NarwalConsumableResetButton(
        coordinator,
        key="reset_consumable_type_3",
        category="replace",
        protocol_value=3,
        translation_key="reset_consumable",
        consumable_key="type_3",
        consumable_type_id=3,
    )

    with pytest.raises(HomeAssistantError):
        asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_not_awaited()
