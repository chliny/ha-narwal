"""Real room-start workflows must never become whole-home or duplicate tasks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import blackboxprotobuf
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.narwal_client import (
    NarwalClient,
    NarwalCommandTimeoutError,
    NarwalConnectionError,
)
from custom_components.narwal.narwal_client.const import (
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_START_CLEAN,
    CommandResult,
)
from custom_components.narwal.narwal_client.models import CommandResponse, NarwalState
from custom_components.narwal.vacuum import NarwalVacuum

# DeviceModel.ax3 product key; the App converts its CleanTask to StartEasyClean.
J3_PRODUCT_KEY = "cUlfJN5JYP"


def _success() -> CommandResponse:
    return CommandResponse(result_code=CommandResult.SUCCESS)


def _client(product_id: str = "EHf6cRNRGT") -> NarwalClient:
    client = NarwalClient(device_id="test-device", product_id=product_id)
    client.state.current_map_id = 42
    return client


def _decode(payload: bytes) -> dict:
    decoded, _ = blackboxprotobuf.decode_message(payload)
    return decoded


def test_room_start_uses_start_clean_with_only_selected_rooms() -> None:
    """Models without the easy-clean endpoint still target only the selected rooms."""
    client = _client()
    send = AsyncMock(return_value=_success())

    with patch.object(client, "send_command", send):
        result = asyncio.run(client.start_rooms([5, 7]))

    assert result.success
    send.assert_awaited_once()
    assert send.await_args.args[0] == TOPIC_CMD_START_CLEAN
    task = _decode(send.await_args.kwargs["payload"])["1"]
    assert task["1"] == 42
    items = task["2"] if isinstance(task["2"], list) else [task["2"]]
    assert [item["1"]["2"] for item in items] == [5, 7]
    assert all(item["1"]["1"] == 1 for item in items)  # ROOM, never WHOLE_MAP=3.


def test_j3_room_smart_payload_matches_sanitized_app_capture() -> None:
    """Check the real J3 room-clean workflow against captured App fields.

    The user captured the sanitized request fields (not its raw bytes):
      1:1, 2:1, 3:[{1:3, 2:1, 6:{1:2, 2:1, 3:2, 4:1}}].
    Reconstruct the 20-byte protobuf fixture to check the sent endpoint body,
    with robot settings intentionally different from the captured SMART mode.
    The later App capture returned 1, which acknowledges but does not prove
    that the robot moved.
    """
    client = _client(J3_PRODUCT_KEY)
    client.state.current_map_id = 1
    # The physical settings differ; smart-room start must still match the App
    # capture rather than accidentally passing because the fixture is 2/2.
    client.state.raw_base_status.update({"26": 4, "29": 3})
    send = AsyncMock(return_value=_success())

    with patch.object(client, "send_command", send):
        asyncio.run(client.start_rooms([3]))

    send.assert_awaited_once()
    assert send.await_args.args[0] == TOPIC_CMD_EASY_CLEAN
    assert send.await_args.kwargs["payload"] == bytes.fromhex(
        "080110011a0e0803100132080802100118022001"
    )


def test_room_request_prefers_the_robot_reported_active_map() -> None:
    """The base-status mapId wins over a stale cached static map ID."""
    client = _client(J3_PRODUCT_KEY)
    client.state.current_map_id = 1
    client.state.raw_base_status["30"] = 9
    send = AsyncMock(return_value=_success())

    with patch.object(client, "send_command", send):
        asyncio.run(client.start_rooms([3]))

    assert _decode(send.await_args.kwargs["payload"])["2"] == 9
    assert client.state.current_map_id == 9


@pytest.mark.parametrize(
    "error",
    [
        NarwalCommandTimeoutError("No response"),
        NarwalConnectionError("MQTT disconnected"),
    ],
)
def test_room_start_does_not_duplicate_or_widen_on_transport_errors(
    error: Exception,
) -> None:
    """A silent failure might already be running, so no fallback start is sent."""
    client = _client(J3_PRODUCT_KEY)
    send = AsyncMock(side_effect=error)
    start = AsyncMock()

    with (
        patch.object(client, "send_command", send),
        patch.object(client, "start", start),
        pytest.raises(type(error)),
    ):
        asyncio.run(client.start_rooms([5]))

    send.assert_awaited_once()
    start.assert_not_awaited()


def _coordinator(state: NarwalState) -> SimpleNamespace:
    return SimpleNamespace(
        config_entry=SimpleNamespace(
            data={"device_id": "test-device", "model": "Narwal Test"}, title="Robot"
        ),
        client=SimpleNamespace(state=state, robot_awake=True),
        data=state,
        last_update_success=True,
        supports_fan_speed=False,
        supports_single_dry_mop=False,
        supports_dust_gathering=False,
        async_request_refresh=AsyncMock(),
    )


def test_vacuum_start_reports_connection_failure() -> None:
    """A start pressed while the cloud connection is down raises a usable error."""
    state = NarwalState()
    coordinator = _coordinator(state)
    error = NarwalConnectionError("disconnected")
    coordinator.client.start = AsyncMock(side_effect=error)
    vacuum = NarwalVacuum(coordinator)

    with pytest.raises(HomeAssistantError, match="cloud connection is unavailable") as raised:
        asyncio.run(vacuum.async_start())

    assert raised.value.__cause__ is error
    coordinator.async_request_refresh.assert_not_awaited()


def test_vacuum_start_reports_protocol_rejection_reason() -> None:
    """A device rejection shows the actionable protocol name, not a bare number."""
    state = NarwalState()
    coordinator = _coordinator(state)
    coordinator.client.start = AsyncMock(
        return_value=CommandResponse(result_code=10)  # MOP_NOT_INSTALLED
    )
    vacuum = NarwalVacuum(coordinator)

    with pytest.raises(HomeAssistantError, match=r"result code 10 \(MOP_NOT_INSTALLED\)"):
        asyncio.run(vacuum.async_start())

    coordinator.async_request_refresh.assert_not_awaited()
