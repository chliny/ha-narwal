"""Event-driven static-map refreshes, without periodic map downloads."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from custom_components.narwal.narwal_client.client import NarwalClient
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    MapData,
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


def test_position_only_display_updates_do_not_request_map_refresh() -> None:
    """Live robot positions arrive every second and must not reload the map."""
    client = _client()
    previous_map = client.state.map_data
    assert _broadcast(client, "map/display_map", {"1": {}, "8": {"1": 100, "2": 10}})
    assert _broadcast(client, "map/display_map", {"1": {}, "5": {}})
    assert not client.map_refresh_needed
    assert client.state.map_data is previous_map


def test_new_map_version_requests_refresh_and_keeps_cached_map() -> None:
    """A new map version triggers one download while the last good map stays."""
    client = _client()
    previous_map = client.state.map_data
    for _ in range(3):
        assert _broadcast(client, "map/display_map", {"8": {"2": 11}})
    assert client.map_refresh_needed
    assert client.state.map_data is previous_map


def test_base_status_map_id_change_invalidates_cached_map_without_looping() -> None:
    """A map switch reported by base status drops the old map's rooms once."""
    client = _client()
    assert _broadcast(client, "status/robot_base_status", {"30": 43})
    assert _broadcast(client, "status/robot_base_status", {"30": 43})
    # The active map ID stays authoritative from get_map/switch_map so a start
    # request never carries a base-status ID from a different domain.
    assert client.state.current_map_id == 42
    assert client.state.map_data is None
    assert client.map_refresh_needed


def test_existing_status_poll_detects_external_map_switch() -> None:
    """A poll observes an App-side map switch and requests a fresh map."""
    client = _client()
    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=CommandResponse(data={"2": {"30": 43}}),
    ):
        asyncio.run(client.get_status())
    assert client.state.current_map_id == 42
    assert client.state.map_data is None
    assert client.map_refresh_needed
