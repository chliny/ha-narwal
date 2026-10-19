"""Tests for unrestricted and room-specific Narwal cleaning payloads."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import blackboxprotobuf

from custom_components.narwal.narwal_client.client import NarwalClient
from custom_components.narwal.narwal_client.const import (
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_START_CLEAN_PLAN,
    CleanMode,
    CleanTaskType,
    CommandResult,
)
from custom_components.narwal.narwal_client.models import CommandResponse


def _decode(payload: bytes) -> dict:
    """Decode a protobuf payload using inferred field definitions."""
    decoded, _ = blackboxprotobuf.decode_message(payload)
    return decoded


def _client() -> NarwalClient:
    """Create a transport-free client for clean-command tests."""
    return NarwalClient(device_id="test-device", product_id="test-product")


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


def test_start_rejection_never_changes_to_a_different_clean_mode() -> None:
    """A rejected smart start cannot silently restart in sweep+mop mode."""
    client = _client()
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=rejected,
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is rejected
    send_command.assert_awaited_once()
    assert send_command.await_args.args[0] == TOPIC_CMD_START_CLEAN


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
