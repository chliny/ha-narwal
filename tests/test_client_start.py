"""Tests for unrestricted and room-specific Narwal cleaning payloads."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import blackboxprotobuf
import pytest

from custom_components.narwal.narwal_client.client import NarwalClient, NarwalCommandError
from custom_components.narwal.narwal_client.const import (
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_START_CLEAN_PLAN,
    CleanMode,
    CleanTaskType,
    CommandResult,
)
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    MapData,
    RoomInfo,
    WorkingStatus,
)

# DeviceModel.ax3 product key; the App converts its CleanTask to StartEasyClean.
J3_PRODUCT_KEY = "cUlfJN5JYP"


def _decode(payload: bytes) -> dict:
    """Decode a protobuf payload using inferred field definitions."""
    decoded, _ = blackboxprotobuf.decode_message(payload)
    return decoded


def _client(product_id: str = "test-product") -> NarwalClient:
    """Create a transport-free client for clean-command tests."""
    client = NarwalClient(device_id="test-device", product_id=product_id)
    client.state.current_map_id = 1
    # Tests exercise the fallback order without the real retry pause.
    client._START_RETRY_DELAY = 0.0
    client.get_map = AsyncMock(return_value=client.state.map_data)
    return client


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
    assert clean_task["2"]["1"] == {"1": 3}  # WHOLE_MAP zone type
    assert clean_task["2"]["2"]["1"] == CleanMode.SMART
    assert clean_task["2"]["2"]["9"] == 1  # CleanParam.enableSmartMode
    assert clean_task["3"]["1"] == 1  # TaskOption.enableSmartMode
    # The app leaves taskType unspecified for smart mode.
    assert "5" not in clean_task


def test_start_reports_failure_only_after_every_variant() -> None:
    """All start variants are rejected before the frontend sees a failure."""
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
    # Freo Mind and sweep-and-mop are both attempted on the model's endpoint.
    assert send_command.await_count == 2
    assert [call.args[0] for call in send_command.await_args_list] == [
        TOPIC_CMD_START_CLEAN,
        TOPIC_CMD_START_CLEAN,
    ]


def test_start_surfaces_final_timeout_after_earlier_rejections() -> None:
    """The last timeout cannot be represented as the previous device rejection."""
    client = _client()
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)
    timeout = NarwalCommandError("final attempt unanswered")
    with (
        patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            side_effect=[rejected, timeout],
        ) as send,
        pytest.raises(NarwalCommandError, match="final attempt unanswered"),
    ):
        asyncio.run(client.start())
    assert [call.args[0] for call in send.await_args_list] == [
        TOPIC_CMD_START_CLEAN,
        TOPIC_CMD_START_CLEAN,
    ]


def test_unanswered_start_does_not_trust_old_cleaning_status() -> None:
    """A stale cleaning state cannot turn an unanswered start into success."""
    client = _client()
    client.state.working_status = WorkingStatus.SWEEP
    timeout = NarwalCommandError("No response for command")
    with (
        patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            side_effect=[timeout, CommandResponse(result_code=CommandResult.SUCCESS)],
        ) as send_command,
        pytest.raises(NarwalCommandError, match="No response"),
    ):
        asyncio.run(client.start())
    # A status query can repeat the previous session's task; it cannot certify
    # that this start was accepted. No second clean variant may be dispatched.
    send_command.assert_awaited_once()


def test_start_reaches_sweep_and_mop_after_smart_rejections() -> None:
    """A rejected Freo Mind task is retried in sweep-and-mop mode."""
    client = _client()
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        side_effect=[rejected, success],
    ) as send_command:
        result = asyncio.run(client.start())

    assert result is success
    assert send_command.await_count == 2
    fallback = send_command.await_args_list[1]
    assert fallback.args[0] == TOPIC_CMD_START_CLEAN
    clean_task = _decode(fallback.kwargs["payload"])["1"]
    assert clean_task["2"]["1"] == {"1": 3}  # WHOLE_MAP zone type
    assert clean_task["2"]["2"]["1"] == CleanMode.SWEEP_MOP_SYNC
    assert clean_task["5"] == CleanTaskType.SHORTCUT


def test_default_start_uses_the_current_map_builtin_plan() -> None:
    """Normal HA start prefers the built-in Freo Mind plan for this map."""
    client = _client()
    client.state.clean_plans = [
        {"id": 7, "map_id": 1, "clean_mode": CleanMode.SMART, "is_custom": False}
    ]
    success = CommandResponse(result_code=CommandResult.SUCCESS)
    with patch.object(client, "send_command", new_callable=AsyncMock, return_value=success) as send:
        assert asyncio.run(client.start()) is success
    send.assert_awaited_once()
    assert send.await_args.args[0] == TOPIC_CMD_START_CLEAN_PLAN
    assert _decode(send.await_args.kwargs["payload"]) == {"1": 7}


def test_default_start_skips_saved_plan_for_another_map() -> None:
    """A plan recorded for a different map is not used by default start."""
    client = _client()
    client.state.clean_plans = [
        {"id": 7, "map_id": 43, "clean_mode": CleanMode.SMART, "is_custom": False}
    ]
    success = CommandResponse(result_code=CommandResult.SUCCESS)
    with patch.object(client, "send_command", new_callable=AsyncMock, return_value=success) as send:
        assert asyncio.run(client.start()) is success
    send.assert_awaited_once()
    assert send.await_args.args[0] == TOPIC_CMD_START_CLEAN
    assert _decode(send.await_args.kwargs["payload"])["1"]["2"]["1"] == {"1": 3}


@pytest.mark.parametrize(
    "plan",
    [
        {"id": 7, "map_id": 1, "clean_mode": CleanMode.SMART, "is_custom": True},
        {"id": 8, "map_id": 1, "clean_mode": CleanMode.SWEEP_MOP_SYNC, "is_custom": False},
    ],
    ids=["custom-plan", "non-smart-plan"],
)
def test_default_start_does_not_select_partial_or_non_smart_plan(plan: dict) -> None:
    """Default start remains whole-home when same-map saved plans are ineligible."""
    client = _client()
    client.state.clean_plans = [plan]
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(client, "send_command", new_callable=AsyncMock, return_value=success) as send:
        assert asyncio.run(client.start()) is success

    send.assert_awaited_once()
    assert send.await_args.args[0] == TOPIC_CMD_START_CLEAN
    task = _decode(send.await_args.kwargs["payload"])["1"]
    assert task["2"]["1"] == {"1": 3}


def test_rejected_builtin_plan_falls_back_to_direct_whole_home() -> None:
    """An explicitly rejected plan still allows the direct whole-home start."""
    client = _client(J3_PRODUCT_KEY)
    client.state.clean_plans = [
        {"id": 7, "map_id": 1, "clean_mode": CleanMode.SMART, "is_custom": False}
    ]
    rejected = CommandResponse(result_code=CommandResult.GENERAL_FAILED)
    success = CommandResponse(result_code=CommandResult.SUCCESS)
    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        side_effect=[rejected, success],
    ) as send:
        assert asyncio.run(client.start()) is success
    assert [call.args[0] for call in send.await_args_list] == [
        TOPIC_CMD_START_CLEAN_PLAN,
        TOPIC_CMD_EASY_CLEAN,
    ]
    area = _decode(send.await_args_list[1].kwargs["payload"])["3"]
    assert area["2"] == 3  # WHOLE_MAP zone type


def test_j3_start_uses_easy_clean_endpoint() -> None:
    """Model ax3 (J3) uses only StartEasyClean, exactly like the App."""
    client = _client(product_id=J3_PRODUCT_KEY)
    success = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client, "send_command", new_callable=AsyncMock, return_value=success
    ) as send_command:
        assert asyncio.run(client.start()) is success

    send_command.assert_awaited_once()
    assert send_command.await_args.args[0] == TOPIC_CMD_EASY_CLEAN
    assert _decode(send_command.await_args.kwargs["payload"])["1"] == CleanMode.SMART


@pytest.mark.parametrize("product_id", [J3_PRODUCT_KEY, "EHf6cRNRGT"])
def test_start_targets_whole_home_without_downloading_map(product_id: str) -> None:
    """Starting is independent of map downloads and uses the App endpoint."""
    client = _client(product_id)
    client.state.current_map_id = 42
    cached_map = MapData(rooms=[RoomInfo(room_id=i) for i in range(1, 4)])
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
        areas = decoded["3"] if isinstance(decoded["3"], list) else [decoded["3"]]
        assert [area["1"] for area in areas] == [1, 2, 3]
        assert all(area["2"] == 3 and area["6"]["4"] == 1 for area in areas)
    else:
        assert decoded["1"]["1"] == 42
        assert decoded["1"]["2"]["1"] == {"1": 3}


def test_start_clean_plan_sends_plan_id() -> None:
    """The explicit saved-plan start carries only the plan ID on its topic."""
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
