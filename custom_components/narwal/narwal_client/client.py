"""Shared Narwal cloud protocol commands and state handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .const import (
    COMMAND_RESPONSE_TIMEOUT,
    TOPIC_CMD_CANCEL,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_ALL_MAPS,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CONFIG,
    TOPIC_CMD_GET_CURRENT_TASK,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_CONFIG,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_LED,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_TAKE_PICTURE,
    TOPIC_CMD_WASH_AND_DRY_MOP,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_YELL,
    CommandResult,
    CustomCleanStrategy,
    DryMopStrength,
    FanLevel,
    MopHumidity,
)
from .models import CommandResponse, DeviceInfo, MapData, MapDisplayData, NarwalState

_LOGGER = logging.getLogger(__name__)


class NarwalConnectionError(Exception):
    """Raised when the cloud or robot connection fails."""


class NarwalAuthenticationError(NarwalConnectionError):
    """Raised when Narwal cloud credentials are no longer accepted."""


class NarwalCommandError(Exception):
    """Raised when a robot command fails or times out."""


class NarwalClient:
    """Transport base containing Narwal cloud commands and state parsing."""

    _DEFAULT_CLEAN_PAYLOAD = bytes.fromhex("0a0e12002a0a0a060803100218012a00")
    _ZONE_TYPE_ROOM = 1
    _ZONE_TYPE_WHOLE_MAP = 3

    def __init__(self, *, device_id: str, product_id: str) -> None:
        self.device_id = device_id
        self.topic_prefix = f"/{product_id}"
        self.state = NarwalState()
        self.on_state_update: Callable[[NarwalState], None] | None = None
        self._robot_awake = False
        self._last_broadcast_time = 0.0
        self._last_display_map_time = 0.0
        self._command_lock = asyncio.Lock()
        self._should_reconnect = True

    @property
    def last_broadcast_age(self) -> float:
        """Return seconds since the latest robot-originated message."""
        if self._last_broadcast_time <= 0:
            return 0.0
        return time.monotonic() - self._last_broadcast_time

    @property
    def last_display_map_age(self) -> float:
        """Return seconds since the latest live map update."""
        if self._last_display_map_time <= 0:
            return 999.0
        return time.monotonic() - self._last_display_map_time

    async def _handle_broadcast(self, short_topic: str, payload: bytes) -> None:
        """Decode a cloud broadcast and apply it to the shared state."""
        self._last_broadcast_time = time.monotonic()
        self._robot_awake = True
        try:
            decoded = self._decode_protobuf(payload)
        except Exception:
            _LOGGER.debug("Failed to decode protobuf for topic %s", short_topic)
            return

        if short_topic == "status/working_status":
            self.state.update_from_working_status(decoded)
        elif short_topic == "status/robot_base_status":
            self.state.update_from_base_status(decoded)
        elif short_topic == "upgrade/upgrade_status":
            self.state.update_from_upgrade_status(decoded)
        elif short_topic == "status/download_status":
            self.state.update_from_download_status(decoded)
        elif short_topic == "map/display_map":
            self.state.map_display_data = MapDisplayData.from_broadcast(decoded)
            self._last_display_map_time = time.monotonic()

        if self.on_state_update is not None:
            self.on_state_update(self.state)

    @staticmethod
    def _decode_protobuf(payload: bytes) -> dict[str, Any]:
        """Decode a protobuf payload without a schema."""
        import blackboxprotobuf

        decoded, _ = blackboxprotobuf.decode_message(payload)
        return decoded

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint."""
        result = []
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    @classmethod
    def _encode_varint_field(cls, field_num: int, value: int) -> bytes:
        """Encode a protobuf varint field."""
        return cls._encode_varint(field_num << 3) + cls._encode_varint(value)

    @classmethod
    def _encode_bytes_field(cls, field_num: int, value: bytes) -> bytes:
        """Encode a protobuf length-delimited field."""
        return (
            cls._encode_varint(field_num << 3 | 2)
            + cls._encode_varint(len(value))
            + value
        )

    @classmethod
    def _encode_custom_clean_order(cls, value: dict[str, Any]) -> bytes:
        """Encode the app's CustomCleanOrder while preserving its fields."""
        payload = b""
        for field_num in (1, 2, 3, 4):
            values = value.get(str(field_num))
            if not isinstance(values, list):
                values = [values]
            for item in values:
                if item is not None:
                    payload += cls._encode_varint_field(field_num, int(item))
        return payload

    async def send_command(
        self,
        short_topic: str,
        payload: bytes = b"",
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
    ) -> CommandResponse:
        """Send a command through the concrete cloud transport."""
        raise NotImplementedError

    async def locate(self) -> CommandResponse:
        """Trigger the robot's locate sound."""
        return await self.send_command(TOPIC_CMD_YELL)

    async def start(self, **kwargs: Any) -> CommandResponse:
        """Start whole-house cleaning with firmware-compatible payloads."""
        del kwargs
        response = await self.send_command(
            TOPIC_CMD_START_CLEAN,
            payload=self._DEFAULT_CLEAN_PAYLOAD,
            timeout=10.0,
        )
        if response.result_code != CommandResult.NOT_APPLICABLE:
            return response
        return await self.send_command(
            TOPIC_CMD_START_CLEAN,
            payload=self._build_clean_payload_v2(),
            timeout=10.0,
        )

    def _build_clean_payload_v2(
        self,
        room_ids: list[int] | None = None,
        suction: int = 3,
        mop_humidity: int = 2,
        passes: int = 1,
        clean_mode: int = 3,
    ) -> bytes:
        """Build the current whole-map or room cleaning payload."""
        import blackboxprotobuf

        clean_params = {
            "1": suction,
            "2": clean_mode,
            "3": passes,
            "7": mop_humidity,
        }
        if room_ids:
            clean_items = [
                {
                    "1": {"1": self._ZONE_TYPE_ROOM, "2": room_id},
                    "2": clean_params,
                    "3": index + 1,
                }
                for index, room_id in enumerate(room_ids)
            ]
        else:
            clean_items = [
                {
                    "1": {"1": self._ZONE_TYPE_WHOLE_MAP},
                    "2": clean_params,
                    "3": 1,
                }
            ]

        item_type = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {
                    "type": "message",
                    "message_typedef": {
                        "1": {"type": "int"},
                        "2": {"type": "uint"},
                    },
                },
                "2": {
                    "type": "message",
                    "message_typedef": {
                        "1": {"type": "int"},
                        "2": {"type": "int"},
                        "3": {"type": "int"},
                        "7": {"type": "int"},
                    },
                },
                "3": {"type": "int"},
            },
        }
        message = {
            "1": {
                "1": 1,
                "2": clean_items if len(clean_items) > 1 else clean_items[0],
                "3": {},
                "5": 6,
            }
        }
        typedef = {
            "1": {
                "type": "message",
                "message_typedef": {
                    "1": {"type": "int"},
                    "2": item_type,
                    "3": {"type": "message", "message_typedef": {}},
                    "5": {"type": "int"},
                },
            }
        }
        return blackboxprotobuf.encode_message(message, typedef)

    def _build_room_clean_payload(self, room_ids: list[int]) -> bytes:
        """Build the legacy flat room-cleaning payload."""
        if not room_ids:
            return self._DEFAULT_CLEAN_PAYLOAD

        import blackboxprotobuf

        room_entries = [
            {
                "1": room_id,
                "2": 2,
                "3": 1,
                "6": 3,
                "7": 2,
            }
            for room_id in room_ids
        ]
        room_type = {
            "type": "message",
            "seen_repeated": True,
            "message_typedef": {
                "1": {"type": "uint"},
                "2": {"type": "int"},
                "3": {"type": "int"},
                "6": {"type": "int"},
                "7": {"type": "int"},
            },
        }
        message = {
            "1": {
                "2": room_entries if len(room_entries) > 1 else room_entries[0],
                "5": {
                    "1": {"1": 3, "2": 2, "3": 1},
                    "5": {},
                },
            }
        }
        typedef = {
            "1": {
                "type": "message",
                "message_typedef": {
                    "2": room_type,
                    "5": {
                        "type": "message",
                        "message_typedef": {
                            "1": {
                                "type": "message",
                                "message_typedef": {
                                    "1": {"type": "int"},
                                    "2": {"type": "int"},
                                    "3": {"type": "int"},
                                },
                            },
                            "5": {"type": "message", "message_typedef": {}},
                        },
                    },
                },
            }
        }
        return blackboxprotobuf.encode_message(message, typedef)

    async def start_rooms(self, room_ids: list[int]) -> CommandResponse:
        """Start cleaning in the selected rooms."""
        if not room_ids:
            return await self.start()
        response = await self.send_command(
            TOPIC_CMD_START_CLEAN,
            payload=self._build_clean_payload_v2(room_ids),
            timeout=10.0,
        )
        if response.result_code != CommandResult.NOT_APPLICABLE:
            return response
        return await self.send_command(
            TOPIC_CMD_START_CLEAN,
            payload=self._build_room_clean_payload(room_ids),
            timeout=10.0,
        )

    async def start_easy_clean(self) -> CommandResponse:
        """Start quick cleaning."""
        return await self.send_command(TOPIC_CMD_EASY_CLEAN)

    async def pause(self) -> CommandResponse:
        """Pause the active task."""
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Resume the paused task."""
        return await self.send_command(TOPIC_CMD_RESUME, timeout=timeout)

    async def stop(self, timeout: float = 15.0) -> CommandResponse:
        """Force-stop the active task."""
        return await self.send_command(TOPIC_CMD_FORCE_END, timeout=timeout)

    async def end_wash_and_dry_mop(self, timeout: float = 15.0) -> CommandResponse:
        """End the station wash-and-dry task."""
        return await self.send_command(TOPIC_CMD_FORCE_END, timeout=timeout)

    async def cancel(self) -> CommandResponse:
        """Cancel the active task."""
        return await self.send_command(TOPIC_CMD_CANCEL)

    async def return_to_base(self, timeout: float = COMMAND_RESPONSE_TIMEOUT) -> CommandResponse:
        """Return to the charging dock."""
        return await self.send_command(TOPIC_CMD_RECALL, timeout=timeout)

    async def set_fan_speed(self, level: FanLevel | int) -> CommandResponse:
        """Set the suction level."""
        return await self.send_command(
            TOPIC_CMD_SET_FAN_LEVEL,
            b"\x08" + bytes([int(level) & 0x7F]),
        )

    async def set_mop_humidity(self, level: MopHumidity | int) -> CommandResponse:
        """Set the mop wetness level."""
        return await self.send_command(
            TOPIC_CMD_SET_MOP_HUMIDITY,
            b"\x08" + bytes([int(level) & 0x7F]),
        )

    async def wash_mop(self) -> CommandResponse:
        """Wash the mop pads."""
        return await self.send_command(TOPIC_CMD_WASH_MOP)

    async def dry_mop(self) -> CommandResponse:
        """Dry the mop pads."""
        return await self.send_command(TOPIC_CMD_DRY_MOP)

    async def wash_and_dry_mop(self) -> CommandResponse:
        """Run the station wash-and-dry cycle."""
        return await self.send_command(TOPIC_CMD_WASH_AND_DRY_MOP)

    async def empty_dustbin(self) -> CommandResponse:
        """Empty the dustbin at the station."""
        return await self.send_command(TOPIC_CMD_DUST_GATHERING)

    async def get_device_info(self) -> DeviceInfo:
        """Query device identity and firmware."""
        response = await self.send_command(TOPIC_CMD_GET_DEVICE_INFO)

        def _clean(value: Any) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").rstrip("\n")
            return str(value).rstrip("\n")

        info = DeviceInfo(
            product_key=_clean(response.data.get("1", "")),
            device_id=_clean(response.data.get("2", "")),
            firmware_version=_clean(response.data.get("3", "")),
        )
        self.state.device_info = info
        if info.firmware_version:
            self.state.firmware_version = info.firmware_version
        return info

    async def get_feature_list(self) -> dict[int, int]:
        """Query the robot's feature flags."""
        response = await self.send_command(TOPIC_CMD_GET_FEATURE_LIST)
        return {int(key): int(value) for key, value in response.data.items()}

    async def get_config(self) -> dict[str, Any]:
        """Read device settings used for capability discovery."""
        response = await self.send_command(TOPIC_CMD_GET_CONFIG)
        config = response.data.get("2", {})
        return config if isinstance(config, dict) else {}

    async def set_dry_mop_strength(
        self, strength: DryMopStrength | int
    ) -> CommandResponse:
        """Set the station mop drying strength."""
        # SetConfig.Request field 13 in app 2.6.90.
        return await self.send_command(
            TOPIC_CMD_SET_CONFIG,
            self._encode_varint_field(13, int(strength)),
        )

    async def set_custom_clean_strategy(
        self,
        strategy: CustomCleanStrategy | int,
        current: dict[str, Any] | None = None,
    ) -> CommandResponse:
        """Set the default custom cleaning strategy."""
        # SetConfig.Request field 18 contains CustomCleanStrategy; its field 1
        # is CustomCleanStrategyOption.
        nested = self._encode_varint_field(1, int(strategy))
        # Preserve the area frequency and clean-order fields returned by
        # config/get, matching the app's read-modify-write behavior.
        if current:
            area_option = current.get("2")
            if area_option is not None:
                nested += self._encode_varint_field(2, int(area_option))
            clean_orders = current.get("3")
            if not isinstance(clean_orders, list):
                clean_orders = [clean_orders]
            for value in clean_orders:
                if isinstance(value, dict):
                    nested += self._encode_bytes_field(
                        3, self._encode_custom_clean_order(value)
                    )
            all_clean_orders = current.get("4")
            if not isinstance(all_clean_orders, list):
                all_clean_orders = [all_clean_orders]
            for value in all_clean_orders:
                if isinstance(value, dict):
                    nested += self._encode_bytes_field(
                        4, self._encode_custom_clean_order(value)
                    )
        payload = self._encode_bytes_field(18, nested)
        return await self.send_command(TOPIC_CMD_SET_CONFIG, payload)

    async def get_status(self, full_update: bool = True) -> CommandResponse:
        """Query the robot's base status."""
        response = await self.send_command(TOPIC_CMD_GET_BASE_STATUS)
        status_data = response.data.get("2", {})
        if status_data:
            if full_update:
                self.state.update_from_base_status(status_data)
            else:
                self.state.update_battery_from_base_status(status_data)
        return response

    async def get_current_task(self) -> CommandResponse:
        """Query the current cleaning task."""
        return await self.send_command(TOPIC_CMD_GET_CURRENT_TASK)

    async def get_map(self) -> MapData:
        """Download and parse the active map."""
        response = await self.send_command(TOPIC_CMD_GET_MAP, timeout=15.0)
        product_key = (
            self.state.device_info.product_key if self.state.device_info is not None else ""
        )
        self.state.map_data = MapData.from_response(
            response.data,
            product_key=product_key,
        )
        return self.state.map_data

    async def get_all_maps(self) -> CommandResponse:
        """Download all saved maps."""
        return await self.send_command(TOPIC_CMD_GET_ALL_MAPS, timeout=15.0)

    async def take_picture(self) -> bytes | None:
        """Request the camera's encrypted raw image."""
        try:
            response = await self.send_command(
                TOPIC_CMD_TAKE_PICTURE,
                timeout=15.0,
            )
        except (NarwalCommandError, NarwalConnectionError):
            return None
        return response.data.get("2") if response.result_code == CommandResult.SUCCESS else None

    async def set_led(self, on: bool) -> None:
        """Turn the camera fill light on or off."""
        try:
            await self.send_command(
                TOPIC_CMD_SET_LED,
                payload=b"\x08\x01" if on else b"\x08\x00",
            )
        except (NarwalCommandError, NarwalConnectionError):
            _LOGGER.debug("Unable to set Narwal camera LED")
