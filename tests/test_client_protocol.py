"""Regression tests for Narwal map download and command failure handling."""

from __future__ import annotations

import asyncio
import zlib
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.narwal.narwal_client.client import NarwalClient, NarwalCommandError
from custom_components.narwal.narwal_client.const import CommandResult
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    DeviceInfo,
    MapData,
    MapDisplayData,
)


def _client() -> NarwalClient:
    return NarwalClient(device_id="device", product_id="product")


async def _call_inline(function: object, *args: object, **kwargs: object) -> object:
    """Run an offloaded test callable without creating Python 3.14 worker threads."""
    return function(*args, **kwargs)  # type: ignore[operator]


def _valid_map_response(*, map_id: int = 7) -> CommandResponse:
    packed_pixels = b"\x0a\x04\x00\x00\x00\x00"
    return CommandResponse(
        result_code=CommandResult.SUCCESS,
        data={
            "1": CommandResult.SUCCESS,
            "2": {
                "1": map_id,
                "3": 60,
                "4": 2,
                "5": 2,
                "17": zlib.compress(packed_pixels),
            },
        },
    )


def test_get_device_info_failure_preserves_existing_identity() -> None:
    client = _client()
    existing = DeviceInfo(
        product_key="product",
        device_id="device",
        firmware_version="1.0",
    )
    client.state.device_info = existing
    response = CommandResponse(
        result_code=CommandResult.CONFLICT,
        data={"1": CommandResult.CONFLICT},
    )

    with (
        patch.object(client, "send_command", new_callable=AsyncMock, return_value=response),
        pytest.raises(NarwalCommandError),
    ):
        asyncio.run(client.get_device_info())

    assert client.state.device_info is existing


def test_get_map_failure_preserves_existing_map() -> None:
    client = _client()
    existing = MapData(width=2, height=2, resolution=60, compressed_map=b"old")
    client.state.map_data = existing
    failure = CommandResponse(result_code=CommandResult.CONFLICT, data={})

    with (
        patch.object(client, "send_command", new_callable=AsyncMock, return_value=failure),
        pytest.raises(NarwalCommandError),
    ):
        asyncio.run(client.get_map())

    assert client.state.map_data is existing


def test_get_map_commits_a_valid_map_to_state() -> None:
    client = _client()
    response = _valid_map_response(map_id=23)

    with (
        patch.object(client, "send_command", new_callable=AsyncMock, return_value=response),
        patch(
            "custom_components.narwal.narwal_client.client.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=_call_inline,
        ),
    ):
        result = asyncio.run(client.get_map())

    assert result is client.state.map_data
    assert (result.width, result.height) == (2, 2)
    assert client.state.current_map_id == 23


@pytest.mark.parametrize(
    "compressed",
    [
        b"\x78\x9c\x00broken",
        zlib.compress(b"\x0a\x02\x00\x00"),
    ],
)
def test_get_map_rejects_corrupt_or_short_pixel_payload(compressed: bytes) -> None:
    client = _client()
    existing = MapData(width=1, height=1, resolution=60, compressed_map=b"old")
    client.state.map_data = existing
    response = _valid_map_response()
    response.data["2"]["17"] = compressed  # type: ignore[index]

    with (
        patch.object(client, "send_command", new_callable=AsyncMock, return_value=response),
        patch(
            "custom_components.narwal.narwal_client.client.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=_call_inline,
        ),
        pytest.raises(NarwalCommandError),
    ):
        asyncio.run(client.get_map())

    assert client.state.map_data is existing


def test_switch_map_invalidates_an_in_flight_map_download() -> None:
    """A map response from before a successful switch must not commit afterward."""

    async def run_test() -> None:
        client = _client()
        validation_started = asyncio.Event()
        release_stale_validation = asyncio.Event()
        validation_count = 0

        async def validate_in_order(
            _function: object,
            *_args: object,
            **_kwargs: object,
        ) -> bool:
            nonlocal validation_count
            validation_count += 1
            if validation_count == 1:
                validation_started.set()
                await release_stale_validation.wait()
            return True

        responses = [
            _valid_map_response(map_id=7),
            CommandResponse(result_code=CommandResult.SUCCESS),
            _valid_map_response(map_id=99),
        ]
        with (
            patch.object(
                client,
                "send_command",
                new_callable=AsyncMock,
                side_effect=responses,
            ),
            patch(
                "custom_components.narwal.narwal_client.client.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=validate_in_order,
            ),
        ):
            stale_download = asyncio.create_task(client.get_map())
            await validation_started.wait()
            try:
                switch_response = await client.switch_map(99)
                fresh_map = await client.get_map()
            finally:
                release_stale_validation.set()

            assert switch_response.success
            assert fresh_map.raw["1"] == 99
            with pytest.raises(NarwalCommandError, match="superseded"):
                await stale_download

        assert client.state.current_map_id == 99
        assert client.state.map_data is fresh_map

    asyncio.run(run_test())


def test_get_all_maps_failure_preserves_existing_state() -> None:
    """A rejected saved-map query must not erase the last valid map list."""
    client = _client()
    existing_maps = [{"id": 7, "name": "Home", "is_main": True}]
    client.state.saved_maps = existing_maps
    client.state.saved_maps_supported = True
    failure = CommandResponse(
        result_code=CommandResult.CONFLICT,
        data={
            "1": CommandResult.CONFLICT,
            "2": {"1": 99, "28": "Wrong", "30": 1},
        },
    )

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=failure,
    ):
        response = asyncio.run(client.get_all_maps())

    assert response is failure
    assert client.state.saved_maps is existing_maps
    assert client.state.saved_maps_supported
    assert client.state.current_map_id is None


def test_switch_map_clears_old_static_and_display_maps_on_success() -> None:
    client = _client()
    client.state.map_data = MapData(
        width=2,
        height=2,
        resolution=60,
        compressed_map=b"old",
    )
    client.state.map_display_data = MapDisplayData(robot_x=1.0, robot_y=1.0)
    response = CommandResponse(result_code=CommandResult.SUCCESS)

    with patch.object(
        client,
        "send_command",
        new_callable=AsyncMock,
        return_value=response,
    ):
        result = asyncio.run(client.switch_map(99))

    assert result is response
    assert client.state.current_map_id == 99
    assert client.state.map_data is None
    assert client.state.map_display_data is None
