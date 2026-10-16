"""Offline regression tests for app-confirmed button routes and guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.button import (
    NarwalConsumableResetButton,
    NarwalResumeBaseSearchButton,
    _reset_protocol,
    async_setup_entry,
)
from custom_components.narwal.narwal_client.client import NarwalClient
from custom_components.narwal.narwal_client.cloud_api import CloudAccount, NarwalCloudApi
from custom_components.narwal.narwal_client.const import (
    BASE_STATION_NOT_FOUND_ERROR,
    CommandResult,
)
from custom_components.narwal.narwal_client.error_codes import (
    ROBOT_ERROR_MESSAGES,
    error_message,
)
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    ConsumableInfo,
    NarwalState,
)
from custom_components.narwal.sensor import NarwalRobotErrorSensor


def _item(type_id: int, *, category: int = 2, reset_supported: bool = True) -> ConsumableInfo:
    return ConsumableInfo(
        key=f"type_{type_id}",
        name="Consumable",
        type_id=type_id,
        category=category,
        total_duration=100,
        usage_duration=10,
        reset_supported=reset_supported,
    )


def _coordinator(state: NarwalState) -> SimpleNamespace:
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


def test_robot_error_sensor_combines_multiple_active_errors() -> None:
    state = NarwalState()
    state.update_from_base_status({"1": [{"1": 0x02100018}, {"1": 0x02100017}]})
    sensor = NarwalRobotErrorSensor(_coordinator(state))
    assert sensor.native_value == "机器人找不到基站，请放入基站并对准充电片（另有1项不同故障）"
    assert [error["code"] for error in sensor.extra_state_attributes["errors"]] == [
        "0x02100018",
        "0x02100017",
    ]
    state.update_from_base_status({})
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"errors": []}


def test_base_search_retry_matches_app_and_requires_single_error() -> None:
    state = NarwalState()
    coordinator = _coordinator(state)
    client = coordinator.client
    client.resume = AsyncMock(return_value=CommandResponse(result_code=CommandResult.SUCCESS))
    button = NarwalResumeBaseSearchButton(coordinator)
    sensor = NarwalRobotErrorSensor(coordinator)
    assert not button.available and sensor.native_value is None
    with pytest.raises(HomeAssistantError):
        asyncio.run(button.async_press())
    client.resume.assert_not_awaited()

    state.update_from_base_status({"1": {"1": BASE_STATION_NOT_FOUND_ERROR}, "11": 1})
    assert button.available
    assert sensor.native_value == "机器人找不到基站，请放入基站并对准充电片"
    asyncio.run(button.async_press())
    client.resume.assert_awaited_once_with(timeout=10.0)
    coordinator.async_request_refresh.assert_awaited_once()
    state.update_from_base_status(
        {"1": [{"1": BASE_STATION_NOT_FOUND_ERROR}, {"1": 0x02100017}], "11": 1}
    )
    assert not button.available
    state.update_from_base_status({"1": {"1": BASE_STATION_NOT_FOUND_ERROR}, "11": 2})
    assert not button.available


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
    client.get_consumable_info = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(client.reset_consumable(category="replace", protocol_value=3))
    client.send_command.assert_awaited_once_with(
        "consumable/reset_consumable_info", b"\x0a\x02\x10\x03"
    )
    client.get_consumable_info.assert_awaited_once()

    client.send_command.reset_mock()
    asyncio.run(client.reset_consumable(category="maintain", protocol_value=1))
    client.send_command.assert_awaited_once_with(
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


@pytest.mark.parametrize(
    ("type_id", "category", "expected"),
    [
        (3, 2, ("replace", 3)),
        (101, 1, ("maintain", 1)),
        (110, 2, None),
        (999, 2, None),
        (3, 0, None),
    ],
)
def test_reset_mapping_is_unambiguous(
    type_id: int, category: int, expected: tuple[str, int] | None
) -> None:
    assert _reset_protocol(_item(type_id, category=category)) == expected


def test_buttons_created_without_active_maintenance_alert() -> None:
    state = NarwalState(consumables={item.key: item for item in (_item(3), _item(110))})
    state.mqtt_consumables_supported = True
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


def test_failed_robot_reset_does_not_refresh() -> None:
    state = NarwalState(consumables={"type_3": _item(3)})
    state.mqtt_consumables_supported = True
    coordinator = _coordinator(state)
    coordinator.client.reset_consumable.return_value = CommandResponse(result_code=2)
    button = NarwalConsumableResetButton(coordinator, state.consumables["type_3"], "replace", 3)

    with pytest.raises(HomeAssistantError):
        asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_not_awaited()
