"""Tests for unrestricted and room-specific Narwal cleaning payloads."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import blackboxprotobuf
import pytest

from custom_components.narwal.narwal_client.client import NarwalClient, NarwalCommandError
from custom_components.narwal.narwal_client.const import (
    EASY_CLEAN_PRODUCT_KEYS,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_START_CLEAN_PLAN,
    CleanMode,
    CleanTaskType,
    CommandResult,
)
from custom_components.narwal.narwal_client.models import CommandResponse, MapData, RoomInfo

# DeviceModel.ax3 product key; the App converts its CleanTask to StartEasyClean.
J3_PRODUCT_KEY = "cUlfJN5JYP"


def _decode(payload: bytes) -> dict:
    """Decode a protobuf payload using inferred field definitions."""
    decoded, _ = blackboxprotobuf.decode_message(payload)
    return decoded


def _client(product_id: str = "test-product") -> NarwalClient:
    """Create a transport-free client for clean-command tests."""
    client = NarwalClient(device_id="test-device", product_id=product_id)
    # Tests exercise the fallback order without the real retry pause.
    client._START_RETRY_DELAY = 0.0
    client.get_map = AsyncMock(return_value=client.state.map_data)
    return client


def test_v2_default_payload_targets_whole_map_without_room_id() -> None:
    """The default v2 payload must not restrict cleaning to cached rooms."""
    client = _client()

    clean_item = _decode(client._build_clean_payload_v2())["1"]["2"]

    assert clean_item["1"]["1"] == client._ZONE_TYPE_WHOLE_MAP
    assert "2" not in clean_item["1"]


def test_v2_default_payload_keeps_shortcut_task_type() -> None:
    """Room and default whole-map tasks keep the App's shortcut task type."""
    client = _client()

    assert _decode(client._build_clean_payload_v2())["1"]["5"] == CleanTaskType.SHORTCUT
    assert _decode(client._build_clean_payload_v2([5]))["1"]["5"] == CleanTaskType.SHORTCUT


def test_v2_clean_params_follow_app_protobuf_fields() -> None:
    """CleanParam fields match App mode, fan and mop-humidity definitions."""
    client = _client()
    params = _decode(client._build_clean_payload_v2())["1"]["2"]["2"]

    assert params == {"1": 3, "2": 3, "4": 2}
    assert "3" not in params  # mopStrengthLevel is not a pass count.
    assert "7" not in params  # sweepMopSyncTime is not humidity.


def test_v2_payload_keeps_reported_fan_and_mop_settings() -> None:
    """Starting a task preserves controls already selected on the robot."""
    client = _client()
    client.state.raw_base_status.update({"26": 1, "29": 0})

    params = _decode(client._build_clean_payload_v2())["1"]["2"]["2"]

    assert params == {"1": 3, "2": 1, "4": 0}


def test_v2_payload_keeps_extended_app_enum_settings() -> None:
    """App-supported settings remain intact even when not in HA's control list."""
    client = _client()
    client.state.raw_base_status.update({"26": 5, "29": 3})

    params = _decode(client._build_clean_payload_v2())["1"]["2"]["2"]

    assert params == {"1": 3, "2": 5, "4": 3}


def test_v2_payload_uses_active_map_id() -> None:
    """The App's CleanTask.mapId tracks the selected map, not map one."""
    client = _client()
    client.state.current_map_id = 42

    assert _decode(client._build_clean_payload_v2())["1"]["1"] == 42
    assert _decode(client._build_clean_payload_v2([5]))["1"]["1"] == 42


def test_v2_room_payload_targets_only_requested_rooms() -> None:
    """Explicit room cleaning keeps using room zones and room IDs."""
    client = _client()

    clean_items = _decode(client._build_clean_payload_v2([5, 7]))["1"]["2"]

    assert [item["1"]["1"] for item in clean_items] == [
        client._ZONE_TYPE_ROOM,
        client._ZONE_TYPE_ROOM,
    ]
    assert [item["1"]["2"] for item in clean_items] == [5, 7]


def test_start_prefers_freo_mind_mode() -> None:
    """A fresh start asks the robot to run 鲸灵托管 (Freo Mind)."""
    client = _client()
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=success,
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is success
    send_command.assert_awaited_once()
    assert send_command.await_args.args[0] == TOPIC_CMD_START_CLEAN
    clean_task = _decode(send_command.await_args.kwargs["payload"])["1"]
    assert clean_task["2"]["1"] == {"1": client._ZONE_TYPE_WHOLE_MAP}
    assert clean_task["2"]["2"]["1"] == CleanMode.SMART
    assert clean_task["2"]["2"]["9"] == 1  # CleanParam.enableSmartMode
    assert clean_task["3"]["1"] == 1  # TaskOption.enableSmartMode
    # The app leaves taskType unspecified for smart mode.
    assert "5" not in clean_task


def test_start_reports_failure_only_after_every_variant() -> None:
    """All start variants are rejected before the frontend sees a failure."""
    client = _client()
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)
    attempts = client._start_attempts()

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=rejected,
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is rejected
    assert send_command.await_count == len(attempts)
    assert [call.args[0] for call in send_command.await_args_list] == [
        topic for _label, topic, _payload in attempts
    ]


def test_start_raises_after_silent_variants() -> None:
    """A robot that never answers still produces one clear error."""
    client = _client()

    with (
        patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            side_effect=NarwalCommandError("No response for command"),
        ) as send_command,
        pytest.raises(NarwalCommandError),
    ):
        asyncio.run(client.start())

    assert send_command.await_count == len(client._start_attempts())


def test_start_retries_next_variant_when_robot_stays_silent() -> None:
    """A silent Freo Mind attempt falls through to explicit whole-house easy clean."""
    client = _client()
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        side_effect=[NarwalCommandError("No response for command"), success],
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is success
    assert send_command.await_count == 2
    assert send_command.await_args_list[0].args[0] == TOPIC_CMD_START_CLEAN
    assert send_command.await_args_list[1].args[0] == TOPIC_CMD_EASY_CLEAN


def test_start_reaches_sweep_and_mop_after_smart_rejections() -> None:
    """Devices that reject Freo Mind everywhere still start with the old task."""
    client = _client()
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        side_effect=[rejected, rejected, success],
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is success
    assert send_command.await_count == 3
    fallback = send_command.await_args_list[2]
    assert fallback.args[0] == TOPIC_CMD_START_CLEAN
    clean_task = _decode(fallback.kwargs["payload"])["1"]
    assert clean_task["2"]["1"] == {"1": client._ZONE_TYPE_WHOLE_MAP}
    assert clean_task["2"]["2"]["1"] == 3
    assert clean_task["5"] == CleanTaskType.SHORTCUT


def test_start_orders_every_freo_mind_variant_first() -> None:
    """Freo Mind is exhausted on all endpoints before sweep+mop is tried."""
    client = _client()
    attempts = client._start_attempts()

    assert [label.split(" on ")[0] for label, _topic, _payload in attempts] == [
        "Freo Mind",
        "Freo Mind",
        "sweep and mop",
        "sweep and mop",
    ]
    assert [topic for _label, topic, _payload in attempts] == [
        TOPIC_CMD_START_CLEAN,
        TOPIC_CMD_EASY_CLEAN,
        TOPIC_CMD_START_CLEAN,
        TOPIC_CMD_EASY_CLEAN,
    ]


def test_j3_starts_freo_mind_on_the_easy_clean_endpoint() -> None:
    """Model ax3 (J3) uses StartEasyClean before StartClean, as the App does."""
    assert {J3_PRODUCT_KEY} == EASY_CLEAN_PRODUCT_KEYS
    client = _client(product_id=J3_PRODUCT_KEY)
    attempts = client._start_attempts()

    assert [topic for _label, topic, _payload in attempts] == [
        TOPIC_CMD_EASY_CLEAN,
        TOPIC_CMD_START_CLEAN,
        TOPIC_CMD_EASY_CLEAN,
        TOPIC_CMD_START_CLEAN,
    ]
    assert [label.split(" on ")[0] for label, _topic, _payload in attempts] == [
        "Freo Mind",
        "Freo Mind",
        "sweep and mop",
        "sweep and mop",
    ]
    first_payload = _decode(attempts[0][2])
    assert first_payload == {"1": CleanMode.SMART, "2": 1, "3": {"2": 3}}


@pytest.mark.parametrize("product_id", [J3_PRODUCT_KEY, "EHf6cRNRGT"])
@pytest.mark.parametrize("room_count", [0, 3, 7])
def test_every_default_start_variant_explicitly_targets_whole_home(
    product_id: str, room_count: int
) -> None:
    """Split rooms or a stale three-room cache cannot constrain default start."""
    client = _client(product_id)
    client.state.current_map_id = 42
    client.state.map_data = MapData(rooms=[RoomInfo(room_id=i) for i in range(1, room_count + 1)])

    for _label, topic, payload in client._start_attempts():
        decoded = _decode(payload)
        assert topic != TOPIC_CMD_START_CLEAN_PLAN
        if topic == TOPIC_CMD_EASY_CLEAN:
            assert decoded["2"] == 42
            # CleanAreaOption: field 2 is cleanZoneType, NOT zoneId.
            assert decoded["3"] == {"2": 3}  # WHOLE_HOUSE, no room IDs
        else:
            assert decoded["1"]["1"] == 42
            assert decoded["1"]["2"]["1"] == {"1": 3}  # WHOLE_MAP


@pytest.mark.parametrize("product_id", [J3_PRODUCT_KEY, "EHf6cRNRGT"])
@pytest.mark.parametrize("room_count", [3, 7])
def test_start_targets_whole_home_without_downloading_map(product_id: str, room_count: int) -> None:
    """Starting is independent of map downloads and cached room counts."""
    client = _client(product_id)
    client.state.current_map_id = 42
    cached_map = MapData(rooms=[RoomInfo(room_id=i) for i in range(1, room_count + 1)])
    client.state.map_data = cached_map

    with (
        patch.object(
            client,
            "get_map",
            new_callable=AsyncMock,
            side_effect=AssertionError("start must not download the map"),
        ) as get_map,
        patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            return_value=CommandResponse(result_code=CommandResult.SUCCESS),
        ) as send_command,
    ):
        result = asyncio.run(client.start())

    assert result.success
    get_map.assert_not_awaited()
    assert client.state.map_data is cached_map
    send_command.assert_awaited_once()
    decoded = _decode(send_command.await_args.kwargs["payload"])
    if product_id == J3_PRODUCT_KEY:
        assert decoded["2"] == 42
        assert decoded["3"] == {"2": 3}
    else:
        assert decoded["1"]["1"] == 42
        assert decoded["1"]["2"]["1"] == {"1": 3}


def test_general_failure_uses_protocol_name_and_compatibility_alias() -> None:
    """Result code 2 is a general failure while keeping the old API alias."""
    response = CommandResponse(result_code=CommandResult(2))

    assert CommandResult(2) is CommandResult.GENERAL_FAILED
    assert CommandResult(2).name == "GENERAL_FAILED"
    assert CommandResult.NOT_APPLICABLE is CommandResult.GENERAL_FAILED
    assert response.general_failed
    assert response.not_applicable


def test_clean_task_and_saved_plan_use_distinct_protocol_topics() -> None:
    """StartClean accepts a CleanTask; StartWithPlan accepts a plan ID."""
    assert TOPIC_CMD_START_CLEAN == "clean/start_clean"
    assert TOPIC_CMD_START_CLEAN_PLAN == "clean/plan/start"
    client = _client()
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client, "send_command", new_callable=AsyncMock, return_value=success
    ) as send_command:
        result = asyncio.run(client.start_clean_plan(45))

    assert result is success
    send_command.assert_awaited_once()
    assert send_command.await_args.args[0] == TOPIC_CMD_START_CLEAN_PLAN
    assert send_command.await_args.args[1] == b"\x08\x2d"  # planId=45, varint


def test_start_rooms_does_not_retry_after_general_failure() -> None:
    """A rejected v2 room task must not be resent using a legacy schema."""
    client = _client()
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=rejected,
    ) as send_command:
        result = asyncio.run(client.start_rooms([5, 7]))

    assert result is rejected
    send_command.assert_awaited_once()
    assert send_command.await_args.args[0] == TOPIC_CMD_START_CLEAN
    clean_items = _decode(send_command.await_args.kwargs["payload"])["1"]["2"]
    assert [item["1"]["2"] for item in clean_items] == [5, 7]


def test_start_does_not_fallback_after_conflict() -> None:
    """A conflict is authoritative and must not trigger another start command."""
    client = _client()
    conflict = CommandResponse(result_code=CommandResult.CONFLICT)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=conflict,
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is conflict
    send_command.assert_awaited_once()
    clean_task = _decode(send_command.await_args.kwargs["payload"])["1"]
    assert clean_task["2"]["2"]["1"] == CleanMode.SMART
