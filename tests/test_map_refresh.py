"""Event-driven static-map refreshes, without periodic map downloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.narwal.coordinator import NarwalCoordinator
from custom_components.narwal.narwal_client import NarwalCommandError
from custom_components.narwal.narwal_client.client import NarwalClient
from custom_components.narwal.narwal_client.const import CommandResult, WorkingStatus
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    MapData,
    NarwalState,
    RoomInfo,
)


def _client() -> NarwalClient:
    client = NarwalClient(device_id="test-device", product_id="test-product")
    client.state.current_map_id = 42
    client.state.map_data = MapData(
        rooms=[RoomInfo(room_id=i) for i in range(1, 4)],
        raw={"1": 42, "2": 10, "22": 5},
    )
    return client


def _broadcast(client: NarwalClient, topic: str, decoded: dict) -> bool:
    with patch.object(client, "_decode_protobuf", return_value=decoded):
        return asyncio.run(client._handle_broadcast(topic, b"test"))


def _coordinator(refresh_count: int, state: NarwalState) -> NarwalCoordinator:
    """Isolate polling from HA scheduling and optional cloud interfaces."""
    coordinator = object.__new__(NarwalCoordinator)
    coordinator.client = SimpleNamespace(
        connected=True,
        state=state,
        map_refresh_needed=False,
        get_status=AsyncMock(),
        get_map=AsyncMock(),
        subscribe_to_topics=AsyncMock(),
    )
    coordinator._map_fetch_pending = False
    coordinator._map_refresh_retry_at = 0.0
    coordinator._feature_discovery_complete = True
    coordinator._config_discovery_complete = True
    coordinator._extended_refresh_count = refresh_count
    coordinator._fast_poll_remaining = 0
    coordinator._prev_working_status = WorkingStatus.UNKNOWN
    coordinator._async_refresh_cloud_metadata = AsyncMock()
    coordinator._async_maybe_refresh_firmware_update = AsyncMock()
    coordinator._async_refresh_extended_state = AsyncMock()
    coordinator._async_update_device_firmware_version = Mock()
    coordinator.async_set_updated_data = Mock()
    return coordinator


@pytest.mark.parametrize("refresh_count", [0, 4, 9, 29, 999])
def test_unchanged_map_is_not_downloaded_on_poll(refresh_count: int) -> None:
    state = _client().state
    coordinator = _coordinator(refresh_count, state)
    asyncio.run(coordinator._async_update_data())
    coordinator.client.get_map.assert_not_awaited()


def test_changed_map_replaces_cached_three_rooms_with_seven() -> None:
    state = _client().state
    coordinator = _coordinator(0, state)
    coordinator.client.map_refresh_needed = True
    updated_map = MapData(rooms=[RoomInfo(room_id=i) for i in range(10, 17)])

    async def download() -> MapData:
        state.map_data = updated_map
        coordinator.client.map_refresh_needed = False
        return updated_map

    coordinator.client.get_map.side_effect = download
    result = asyncio.run(coordinator._async_update_data())
    coordinator.client.get_map.assert_awaited_once_with()
    assert result.map_data is updated_map
    assert len(result.map_data.rooms) == 7
    assert result.current_map_id == 42


def test_missing_map_is_retried_without_periodic_downloads() -> None:
    coordinator = _coordinator(0, NarwalState())
    asyncio.run(coordinator._async_update_data())
    coordinator.client.get_map.assert_awaited_once_with()


def test_failed_map_refresh_keeps_cache_and_backs_off() -> None:
    state = _client().state
    previous_map = state.map_data
    coordinator = _coordinator(0, state)
    coordinator.client.map_refresh_needed = True
    coordinator.client.get_map.side_effect = NarwalCommandError("map unavailable")

    async def run() -> None:
        with patch("custom_components.narwal.coordinator.time.monotonic", return_value=100):
            assert await coordinator._async_update_data() is state
            assert not coordinator._map_refresh_due()
            assert await coordinator._async_update_data() is state
        assert coordinator._map_refresh_retry_at == 160
        with patch("custom_components.narwal.coordinator.time.monotonic", return_value=161):
            assert coordinator._map_refresh_due()

    asyncio.run(run())
    coordinator.client.get_map.assert_awaited_once_with()
    assert state.map_data is previous_map
    assert coordinator.client.map_refresh_needed
    assert not coordinator._map_fetch_pending


def test_push_refresh_is_coalesced_while_download_is_pending() -> None:
    async def run() -> None:
        coordinator = _coordinator(0, _client().state)
        coordinator.client.map_refresh_needed = True
        tasks = []

        def schedule(_hass, coroutine, _name):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        coordinator.hass = Mock()
        coordinator.config_entry = SimpleNamespace(
            async_create_background_task=Mock(side_effect=schedule)
        )
        coordinator._on_state_update(coordinator.client.state)
        coordinator._on_state_update(coordinator.client.state)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        coordinator.client.get_map.assert_awaited_once_with()
        assert not coordinator._map_fetch_pending

    asyncio.run(run())


def test_same_version_and_robot_positions_do_not_invalidate_map() -> None:
    client = _client()
    assert _broadcast(client, "map/display_map", {"1": {}, "8": {"1": 100, "2": 10}})
    assert _broadcast(client, "map/display_map", {"1": {}, "5": {}})
    assert not client.map_refresh_needed
    assert client._map_generation == 0


def test_new_version_invalidates_once_and_keeps_last_good_map() -> None:
    client = _client()
    previous_map = client.state.map_data
    for _ in range(3):
        assert _broadcast(client, "map/display_map", {"8": {"2": 11}})
    assert client.map_refresh_needed
    assert client._map_generation == 1
    assert client.state.map_data is previous_map


def test_contiguous_incremental_sequences_do_not_download_maps() -> None:
    client = _client()
    for start, current in [(5, 6), (6, 7), (6, 7), (5, 6)]:
        assert _broadcast(client, "map/display_map", {"7": {"1": start, "2": current}})
    assert client._display_map_seq == 7
    assert not client.map_refresh_needed


def test_incremental_sequence_gap_invalidates_once() -> None:
    client = _client()
    for start, current in [(8, 9), (8, 9), (9, 10)]:
        assert _broadcast(client, "map/display_map", {"7": {"1": start, "2": current}})
    assert client.map_refresh_needed
    assert client._map_generation == 1
    assert client._display_map_seq == 10


@pytest.mark.parametrize("version", [None, True, 0, -1, b"11", {}])
def test_invalid_version_does_not_invalidate_map(version) -> None:
    client = _client()
    _broadcast(client, "map/display_map", {"8": {"2": version}})
    assert not client.map_refresh_needed


def test_map_id_change_invalidates_old_rooms_only_once() -> None:
    client = _client()
    assert _broadcast(client, "status/robot_base_status", {"30": 43})
    assert _broadcast(client, "status/robot_base_status", {"30": 43})
    assert client.state.current_map_id == 43
    assert client.state.map_data is None
    assert client.map_refresh_needed
    assert client._map_generation == 1


def test_repeated_base_map_id_does_not_loop_when_ids_differ() -> None:
    """A base-status map ID that differs from the static map ID fires once."""
    client = _client()
    _broadcast(client, "status/robot_base_status", {"30": 43})
    assert client._map_generation == 1
    restored = MapData(rooms=[RoomInfo(room_id=i) for i in range(10, 17)])
    client.state.map_data = restored
    client.state.current_map_id = 42  # What a successful get_map reports again.
    for _ in range(3):
        _broadcast(client, "status/robot_base_status", {"30": 43})
    assert client._map_generation == 1
    assert client.state.map_data is restored


def test_existing_status_poll_detects_external_map_switch() -> None:
    client = _client()
    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=CommandResponse(data={"2": {"30": 43}}),
    ):
        asyncio.run(client.get_status())
    assert client.state.current_map_id == 43
    assert client.map_refresh_needed


def test_valid_map_download_clears_invalidation_and_seeds_sequences() -> None:
    client = _client()
    client._observe_map_version(11)
    candidate = MapData(
        width=2, height=2, resolution=1, compressed_map=b"test", raw={"1": 42, "2": 11, "22": 9}
    )
    with (
        patch.object(
            client,
            "send_command",
            new_callable=AsyncMock,
            return_value=CommandResponse(result_code=CommandResult.SUCCESS),
        ),
        patch.object(MapData, "from_response", return_value=candidate),
        patch(
            "custom_components.narwal.narwal_client.client.map_payload_is_valid", return_value=True
        ),
    ):
        assert asyncio.run(client.get_map()) is candidate
    assert not client.map_refresh_needed
    assert client._reported_map_version == 11
    assert client._display_map_seq == 9


def test_map_change_during_download_is_not_lost() -> None:
    client = _client()
    previous_map = client.state.map_data
    candidate = MapData(
        width=2, height=2, resolution=1, compressed_map=b"test", raw={"1": 42, "2": 10, "22": 5}
    )

    async def download(*_args, **_kwargs):
        client._observe_map_version(11)
        return CommandResponse(result_code=CommandResult.SUCCESS)

    with (
        patch.object(client, "send_command", new_callable=AsyncMock, side_effect=download),
        patch.object(MapData, "from_response", return_value=candidate),
        patch(
            "custom_components.narwal.narwal_client.client.map_payload_is_valid", return_value=True
        ),
        pytest.raises(NarwalCommandError, match="superseded"),
    ):
        asyncio.run(client.get_map())
    assert client.state.map_data is previous_map
    assert client.map_refresh_needed
