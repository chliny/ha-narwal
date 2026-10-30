"""Shared Narwal cloud protocol commands and state handling."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import Any

from .const import (
    COMMAND_RESPONSE_TIMEOUT,
    CONSUMABLE_MAINTAIN_TYPE_MAP,
    CONSUMABLE_REPLACE_TYPE_MAP,
    EASY_CLEAN_PRODUCT_KEYS,
    TOPIC_CMD_CANCEL,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_EASY_CLEAN,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_ALL_MAPS,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_BATTERY_INFO,
    TOPIC_CMD_GET_CLEAN_PLANS,
    TOPIC_CMD_GET_CLEAN_PROGRESS,
    TOPIC_CMD_GET_CLEAN_TIMELINE,
    TOPIC_CMD_GET_CONFIG,
    TOPIC_CMD_GET_CONSUMABLE_INFO,
    TOPIC_CMD_GET_CURRENT_TASK,
    TOPIC_CMD_GET_CURRENT_VOICE_INFO,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESET_CONSUMABLE_INFO,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_CONFIG,
    TOPIC_CMD_SET_CUSTOM_VOICE,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_START_CLEAN_PLAN,
    TOPIC_CMD_SWITCH_MAP,
    TOPIC_CMD_TAKE_PICTURE,
    TOPIC_CMD_WASH_AND_DRY_MOP,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_YELL,
    CleanMode,
    CleanTaskType,
    CommandResult,
    CustomCleanStrategy,
    DryMopStrength,
    FanLevel,
    MopHumidity,
)
from .map_renderer import (
    MAX_COMPRESSED_MAP_BYTES,
    map_dimensions_are_safe,
    map_payload_is_valid,
)
from .models import CommandResponse, DeviceInfo, MapData, MapDisplayData, NarwalState, _to_float64

_LOGGER = logging.getLogger(__name__)


class NarwalConnectionError(Exception):
    """Raised when the cloud or robot connection fails."""


class NarwalAuthenticationError(NarwalConnectionError):
    """Raised when Narwal cloud credentials are no longer accepted."""


class NarwalCommandError(Exception):
    """Raised when a robot command fails or times out."""


class NarwalCommandTimeoutError(NarwalCommandError):
    """Raised when a command receives no reply within its deadline."""


class NarwalClient:
    """Transport base containing Narwal cloud commands and state parsing."""

    _ZONE_TYPE_ROOM = 1
    _ZONE_TYPE_WHOLE_MAP = 3
    # Retry another mode only after an explicit rejection. A silent attempt
    # might already be running, so do not send a second start command.
    _START_ATTEMPT_TIMEOUT = 5.0
    _START_RETRY_DELAY = 0.8
    _START_TOPIC_LABELS = {
        TOPIC_CMD_START_CLEAN: "StartClean",
        TOPIC_CMD_EASY_CLEAN: "easy clean",
    }

    def __init__(self, *, device_id: str, product_id: str) -> None:
        self.device_id = device_id
        self.product_id = product_id
        self.topic_prefix = f"/{product_id}"
        self.state = NarwalState()
        self.on_state_update: Callable[[NarwalState], None] | None = None
        self._robot_awake = False
        self._last_broadcast_time = 0.0
        self._last_display_map_time = 0.0
        self._command_lock = asyncio.Lock()
        self._should_reconnect = True
        self._map_generation = 0
        self.map_refresh_needed = False
        self._reported_map_version: int | None = None
        self._display_map_seq: int | None = None
        self._observed_map_id: int | None = None

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

    async def _handle_broadcast(self, short_topic: str, payload: bytes) -> bool:
        """Decode a cloud broadcast and return whether it changed known state."""
        try:
            decoded = self._decode_protobuf(payload)
        except Exception:
            _LOGGER.debug("Failed to decode protobuf for topic %s", short_topic)
            return False

        if not isinstance(decoded, dict) or not decoded:
            return False
        schemas: dict[str, dict[str, type | tuple[type, ...]]] = {
            "status/working_status": {"1": (int, float), "2": (int, float), "3": int, "4": int},
            "status/robot_base_status": {
                "2": (int, float),
                "3": (dict, list),
                "11": int,
                "13": (bytes, str),
                "30": int,
                "36": int,
                "38": (int, float),
                "46": (dict, list),
                "47": int,
            },
            "upgrade/upgrade_status": {"2": int, "4": int, "7": (bytes, str), "8": (bytes, str)},
            "status/download_status": {"1": int, "3": int},
            "map/display_map": {"1": dict, "5": dict, "7": dict, "8": dict},
            "status/time_line_status": {"1": (dict, list)},
        }
        known_fields = schemas.get(short_topic, {})
        if not decoded.keys() & known_fields.keys():
            return False
        for field, expected in known_fields.items():
            if field not in decoded:
                continue
            value = decoded[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, expected)
                or (isinstance(value, float) and not math.isfinite(value))
                or (isinstance(value, list) and not all(isinstance(item, dict) for item in value))
            ):
                return False

        try:
            handled = True
            if short_topic == "status/working_status":
                self.state.update_from_working_status(decoded)
            elif short_topic == "status/robot_base_status":
                self.state.update_from_base_status(decoded)
                self._observe_map_id(decoded.get("30"))
            elif short_topic == "upgrade/upgrade_status":
                self.state.update_from_upgrade_status(decoded)
            elif short_topic == "status/download_status":
                self.state.update_from_download_status(decoded)
            elif short_topic == "map/display_map":
                self.state.map_display_data = MapDisplayData.from_broadcast(decoded)
                self._last_display_map_time = time.monotonic()
                entire_map = decoded.get("8")
                if isinstance(entire_map, dict):
                    self._observe_map_version(entire_map.get("2"))
                incremental = decoded.get("7")
                if isinstance(incremental, dict):
                    self._observe_map_sequence(incremental)
            elif short_topic == "status/time_line_status":
                self._update_clean_timeline(decoded)
            else:
                handled = False

            if handled:
                self._last_broadcast_time = time.monotonic()
                self._robot_awake = True
                if self.on_state_update is not None:
                    self.on_state_update(self.state)
            return handled
        except Exception:
            _LOGGER.debug(
                "Discarding invalid protobuf state for topic %s",
                short_topic,
                exc_info=True,
            )
            return False

    @staticmethod
    def _decode_protobuf(payload: bytes) -> dict[str, Any]:
        """Decode a protobuf payload without a schema."""
        import blackboxprotobuf

        decoded, _ = blackboxprotobuf.decode_message(payload)
        return {str(key): value for key, value in decoded.items()}

    def _observe_map_id(self, value: Any) -> None:
        """Observe RobotBaseStatus.mapId (field 30), including App switches.

        This only invalidates the cached map. ``current_map_id`` is not changed
        by broadcasts, since the base-status ID may refer to a different map
        domain. Room-start requests separately select an ID via _active_map_id.
        Compare reported IDs to avoid repeated invalidation when they differ.
        """
        if type(value) is not int or value <= 0 or value == self._observed_map_id:
            return
        self._observed_map_id = value
        if value == self.state.current_map_id:
            return
        self.state.map_data = None  # Never show the previous map's room IDs.
        self.state.map_display_data = None
        self._reported_map_version = None
        self._display_map_seq = None
        self._map_generation += 1
        self.map_refresh_needed = True

    def _observe_map_version(self, value: Any) -> None:
        """Observe DisplayMap.entireMapData.mapVersion, not its live seqId."""
        if type(value) is not int or value <= 0 or value == self._reported_map_version:
            return
        self._reported_map_version = value
        cached = self.state.map_data
        if cached is not None and cached.raw.get("2") == value:
            return
        # Do not discard the last good image/room list during a same-map edit.
        # Generation guards also reject a download started before this event.
        self._map_generation += 1
        self.map_refresh_needed = True

    def _observe_map_sequence(self, incremental: dict[str, Any]) -> None:
        """Reload on a sequence gap, not ordinary contiguous live updates.

        The App compares its cached sequence with IncrementalMapData's
        startEqId (1) and currentSeqId (2), discarding old packets and asking
        for a fresh map when the start is ahead of the cached sequence.
        """
        start, current = incremental.get("1"), incremental.get("2")
        if type(start) is not int or type(current) is not int or not 0 <= start <= current:
            return
        previous = self._display_map_seq
        if previous is None and self.state.map_data is not None:
            cached_seq = self.state.map_data.raw.get("22")
            if type(cached_seq) is int and cached_seq >= 0:
                previous = cached_seq
        if previous is not None and current <= previous:
            return
        self._display_map_seq = current
        if previous is None or previous < start:
            self._map_generation += 1
            self.map_refresh_needed = True

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
        return cls._encode_varint(field_num << 3 | 2) + cls._encode_varint(len(value)) + value

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
        """Start cleaning, preferring a built-in Freo Mind plan when available.

        The select entity's chosen plan is only used by the explicit plan
        button. After a built-in plan is rejected, try direct Freo Mind and
        then sweep-and-mop on the model's start endpoint. Saved-plan metadata
        does not prove its room scope, which is determined by the device.

        Firmware differs in which payload shape it accepts, so the next variant
        is tried after an explicit rejection. A missing reply is ambiguous -
        the robot may already be cleaning - so it stops the chain and surfaces
        the transport error instead of risking a second task. Before reporting
        that error, do not infer success from a status query: sleeping firmware
        can return a stale task from a previous cleaning session.
        """
        del kwargs
        attempts = self._start_attempts()
        _LOGGER.debug(
            "Start variants for product %s: %s",
            self.product_id,
            ", ".join(label for label, _topic, _payload in attempts),
        )
        last_response: CommandResponse | None = None
        for index, (label, topic, payload) in enumerate(attempts, start=1):
            if index > 1:
                await asyncio.sleep(self._START_RETRY_DELAY)
            try:
                response = await self.send_command(
                    topic,
                    payload=payload,
                    timeout=self._START_ATTEMPT_TIMEOUT,
                )
            except NarwalAuthenticationError:
                # New credentials are required before any variant can succeed.
                raise
            except (NarwalCommandError, NarwalConnectionError) as err:
                # No reply is ambiguous: the robot may have accepted this task.
                # Sending another variant could start a second, different clean.
                # Only an explicit rejection justifies trying the next variant.
                _LOGGER.warning(
                    "Start attempt %d/%d (%s) got no usable reply; stopping: %s",
                    index,
                    len(attempts),
                    label,
                    err,
                )
                raise
            if response.success:
                _LOGGER.info("Start accepted by attempt %d/%d (%s)", index, len(attempts), label)
                return response
            if response.result_code == CommandResult.CONFLICT:
                # An active task makes further variants pointless and may mean
                # an earlier attempt was accepted after all.
                _LOGGER.info(
                    "Start attempt %d/%d (%s) reports an active task",
                    index,
                    len(attempts),
                    label,
                )
                return response
            last_response = response
            _LOGGER.warning(
                "Start attempt %d/%d (%s) rejected with result code %s",
                index,
                len(attempts),
                label,
                response.result_code,
            )

        if last_response is not None:
            return last_response
        return CommandResponse(result_code=CommandResult.GENERAL_FAILED)

    def _start_attempts(self) -> list[tuple[str, str, bytes]]:
        """Return the ordered start variants: label, topic and payload.

        Try an available saved plan first, then the model's direct CleanTask
        endpoint in Freo Mind mode, and finally sweep-and-mop after an explicit
        rejection. The saved plan's room scope is determined by the device.
        """
        freo_mind = self._build_clean_payload_v2(
            clean_mode=CleanMode.SMART,
            task_type=None,
            enable_smart_mode=True,
        )
        sweep_mop = self._build_clean_payload_v2()
        freo_mind_easy = self._convert_clean_to_easy_payload(freo_mind)
        sweep_mop_easy = self._convert_clean_to_easy_payload(sweep_mop)
        # The App sends exactly one start endpoint per DeviceModel: ax3/J3 uses
        # StartEasyClean, every other model StartClean. The other endpoint is
        # not a fallback - on the observed J3 it never answers at all, which
        # only delayed the real rejection and hid its result code.
        (topic,) = self._start_endpoint_order()
        attempts: list[tuple[str, str, bytes]] = []
        plan = self._start_plan_payload()
        if plan is not None:
            attempts.append(("saved clean plan", TOPIC_CMD_START_CLEAN_PLAN, plan))
        for mode_name, clean_task, easy_payload in (
            ("Freo Mind", freo_mind, freo_mind_easy),
            ("sweep and mop", sweep_mop, sweep_mop_easy),
        ):
            payload = easy_payload if topic == TOPIC_CMD_EASY_CLEAN else clean_task
            attempts.append((f"{mode_name} on {self._START_TOPIC_LABELS[topic]}", topic, payload))
        return attempts

    def _active_map_id(self) -> int | None:
        """Select a map ID for room starts, preferring base-status field 30.

        The static-map ID is used if no base-status ID has been seen. These IDs
        can differ; room starts use this value, while automatic plan selection
        and direct whole-home payloads use the cached static-map ID.
        """
        reported = self.state.raw_base_status.get("30")
        if type(reported) is int and reported > 0:
            return reported
        cached = self.state.current_map_id
        if type(cached) is int and cached > 0:
            return cached
        return None

    def _start_plan_payload(self) -> bytes | None:
        """Build an eligible built-in Freo Mind plan request for this map.

        The selected plan belongs to the explicit plan button, not to default
        start: it may use another mode or cover only selected rooms. Saved-plan
        metadata does not establish room scope even for a smart plan.
        """
        current_map = self.state.current_map_id
        if type(current_map) is not int or current_map <= 0:
            return None
        for plan in self.state.clean_plans:
            plan_id = plan.get("id")
            if type(plan_id) is not int or plan_id <= 0:
                continue
            if self._as_int(plan.get("map_id")) != current_map:
                continue
            if plan.get("is_custom"):
                continue
            if plan.get("clean_mode") != int(CleanMode.SMART):
                continue
            # The observed App saved-plan start carries only planId; the plan
            # determines its own map and room scope.
            return self._encode_varint_field(1, plan_id)
        return None

    def _start_endpoint_order(self) -> tuple[str, ...]:
        """Return the single CleanTask start endpoint the App uses per model.

        ax3/J3 converts the CleanTask to StartEasyClean; every other model uses
        StartClean. The other endpoint is not a fallback: on the tested J3 it
        never replied, which only delayed the real rejection.

        clean/plan/start is tried first only when an eligible built-in smart
        plan exists for the current static map.
        """
        if self.product_id in EASY_CLEAN_PRODUCT_KEYS:  # ax3 / J3
            return (TOPIC_CMD_EASY_CLEAN,)
        return (TOPIC_CMD_START_CLEAN,)

    def _build_easy_clean_payload(self, clean_mode: int) -> bytes:
        """Build whole-house StartEasyClean with the App's mode-specific options."""
        return self._convert_clean_to_easy_payload(
            self._build_clean_payload_v2(clean_mode=clean_mode)
        )

    def _build_clean_payload_v2(
        self,
        room_ids: list[int] | None = None,
        suction: int | None = None,
        mop_humidity: int | None = None,
        clean_mode: int = CleanMode.SWEEP_MOP_SYNC,
        task_type: int | None = CleanTaskType.SHORTCUT,
        enable_smart_mode: bool = False,
    ) -> bytes:
        """Build StartClean.Request with one whole-map or several room items.

        The App sets CleanTask.option.enableSmartMode for smart cleaning; its
        CleanTask.taskType is UNSPECIFIED (zero) for this mode.  These flags
        are distinct from CleanParam.mode and must not be confused with the
        saved-plan ID request on ``clean/plan/start``.
        """
        import blackboxprotobuf

        if clean_mode == CleanMode.SMART:
            enable_smart_mode = True
        if enable_smart_mode:
            task_type = None
        if suction is None:
            reported_fan = self.state.raw_base_status.get("26")
            # App CleanParam defines six FanLevel values, beyond our four UI choices.
            suction = (
                reported_fan
                if type(reported_fan) is int and 0 <= reported_fan <= 5
                else int(FanLevel.NORMAL)
            )
        if mop_humidity is None:
            reported_humidity = self.state.raw_base_status.get("29")
            mop_humidity = (
                reported_humidity
                if type(reported_humidity) is int and 0 <= reported_humidity <= 3
                else 2
            )

        # CleanParam protobuf: 1=mode, 2=fanLevel, 4=mopHumidity.
        # Field 3 is mopStrengthLevel and field 7 is sweepMopSyncTime,
        # not a generic cleaning-pass count or mop humidity.
        clean_params = {
            "1": clean_mode,
            "2": suction,
            "4": mop_humidity,
        }
        if enable_smart_mode:
            # App J3 room-smart capture carries normal mop strength explicitly.
            clean_params["3"] = 1  # MopStrengthLevel.NORMAL
            clean_params["9"] = 1  # CleanParam.enableSmartMode
        # The App converts these mode-specific counts into area cleanCount.
        # Omitting them serializes zero passes instead of a one-pass clean.
        if enable_smart_mode or clean_mode == CleanMode.SWEEP_MOP_SYNC:
            clean_params["7"] = 1  # sweepMopSyncTime
        elif clean_mode == CleanMode.SWEEP:
            clean_params["5"] = 1  # sweepTime
        elif clean_mode == CleanMode.MOP:
            clean_params["6"] = 1  # mopTime
        elif clean_mode == CleanMode.SWEEP_THEN_MOP:
            clean_params["5"] = clean_params["6"] = 1
        else:
            raise ValueError("Unsupported Narwal clean mode")
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
                        "4": {"type": "int"},
                        "5": {"type": "uint"},
                        "6": {"type": "uint"},
                        "7": {"type": "uint"},
                        "9": {"type": "int"},
                    },
                },
                "3": {"type": "int"},
            },
        }
        map_id = self.state.current_map_id
        if type(map_id) is not int or map_id <= 0:
            raise NarwalCommandError(
                "Cannot start cleaning because the active map ID is unavailable"
            )
        clean_task: dict[str | int, Any] = {
            "1": map_id,
            "2": clean_items if len(clean_items) > 1 else clean_items[0],
            "3": {"1": 1} if enable_smart_mode else {},
        }
        if task_type:
            clean_task["5"] = task_type
        message: dict[str | int, Any] = {"1": clean_task}
        typedef: Any = {
            "1": {
                "type": "message",
                "message_typedef": {
                    "1": {"type": "int"},
                    "2": item_type,
                    "3": {"type": "message", "message_typedef": {"1": {"type": "int"}}},
                    "5": {"type": "int"},
                },
            }
        }
        return blackboxprotobuf.encode_message(message, typedef)

    def _convert_clean_to_easy_payload(self, clean_request: bytes) -> bytes:
        """Convert our homogeneous CleanTask as the ax3 App does.

        CleanAreaOption keeps each zone's ID/type, then carries the mode's
        nested area option (sweep=4, mop=5, sync/smart=6, sweep-then-mop=7).
        Counts and humidity are copied, not omitted or mistaken for strength.
        For WHOLE_MAP the App duplicates the whole-house option with each
        room ID from the currently cached map, without changing its zone type.
        """
        task = self._decode_protobuf(clean_request)["1"]
        items = task["2"]
        if isinstance(items, dict):
            items = [items]
        smart = bool(task.get("3", {}).get("1"))
        mode = CleanMode.SMART if smart else items[0]["2"]["1"]
        payload = self._encode_varint_field(1, int(mode))
        payload += self._encode_varint_field(2, task["1"])
        for item in items:
            zone, params = item["1"], item["2"]
            area = self._encode_varint_field(2, zone["1"])
            if zone["1"] == self._ZONE_TYPE_ROOM:
                area = self._encode_varint_field(1, zone["2"]) + area
            # The App clamps FanLevel.SUPER (5) to SweepFanLevel.STRONG (3).
            fan = params["2"] if 0 <= params["2"] <= 4 else 3
            humidity = params["4"]
            if mode == CleanMode.SWEEP:
                option_field = 4
                option = self._encode_varint_field(1, fan)
                option += self._encode_varint_field(2, params["5"])
            elif mode == CleanMode.MOP:
                option_field = 5
                option = self._encode_varint_field(2, humidity)
                option += self._encode_varint_field(3, params["6"])
            elif mode in (CleanMode.SMART, CleanMode.SWEEP_MOP_SYNC):
                option_field = 6
                option = self._encode_varint_field(1, fan)
                if "3" in params:
                    option += self._encode_varint_field(2, params["3"])
                option += self._encode_varint_field(3, humidity)
                option += self._encode_varint_field(4, 1 if smart else params["7"])
            elif mode == CleanMode.SWEEP_THEN_MOP:
                option_field = 7
                sweep = self._encode_varint_field(1, fan)
                sweep += self._encode_varint_field(2, params["5"])
                mop = self._encode_varint_field(2, humidity)
                mop += self._encode_varint_field(3, params["6"])
                option = self._encode_bytes_field(1, sweep) + self._encode_bytes_field(2, mop)
            else:
                raise ValueError("Unsupported Narwal easy-clean mode")
            area += self._encode_bytes_field(option_field, option)
            cached_map = self.state.map_data
            cached_id = cached_map.raw.get("1") if cached_map is not None else None
            if (
                zone["1"] == self._ZONE_TYPE_WHOLE_MAP
                and cached_map is not None
                and not self.map_refresh_needed
                and (cached_id is None or cached_id == task["1"])
            ):
                # The App takes allRoomIds from its cached static map, then
                # clones this WHOLE_HOUSE option once per room. A stale cache
                # can still miss App-side edits; event refresh does not prove
                # that every external edit has been observed.
                room_ids = list(
                    dict.fromkeys(
                        room.room_id
                        for room in cached_map.rooms
                        if type(room.room_id) is int and room.room_id > 0
                    )
                )
                if room_ids:
                    for room_id in room_ids:
                        payload += self._encode_bytes_field(
                            3, self._encode_varint_field(1, room_id) + area
                        )
                    continue
            payload += self._encode_bytes_field(3, area)
        return payload

    async def start_rooms(self, room_ids: list[int]) -> CommandResponse:
        """Start only selected rooms, using the model's App endpoint.

        Room cleaning uses Freo Mind (鲸灵托管) with the selected rooms, the
        same mode the App offers for a room selection: the request carries
        cleanMode=SMART and one ROOM CleanAreaOption per selected room. No
        saved plan is created or modified. A timeout is not a rejection: the
        robot may already be cleaning, so no duplicate task is sent.
        """
        if not room_ids or any(type(room_id) is not int or room_id <= 0 for room_id in room_ids):
            raise ValueError("Select at least one positive integer Narwal room ID")
        room_ids = list(dict.fromkeys(room_ids))
        active_map = self._active_map_id()
        if active_map is not None:
            self.state.current_map_id = active_map
        payload = self._build_clean_payload_v2(room_ids, clean_mode=CleanMode.SMART)
        topic = self._start_endpoint_order()[0]
        if topic == TOPIC_CMD_EASY_CLEAN:
            payload = self._convert_clean_to_easy_payload(payload)
        cached_rooms = {
            room.room_id: room.display_name
            for room in (self.state.map_data.rooms if self.state.map_data else [])
            if room.room_id > 0
        }
        _LOGGER.info(
            "Room clean request: product=%s endpoint=%s map=%s (base-status map=%s) rooms=%s"
            " selected=%s cached_rooms=%s",
            self.product_id,
            topic,
            self.state.current_map_id,
            self.state.raw_base_status.get("30"),
            room_ids,
            {room_id: cached_rooms.get(room_id, "<not in cached map>") for room_id in room_ids},
            sorted(cached_rooms.items()),
        )
        try:
            return await self.send_command(topic, payload=payload, timeout=10.0)
        except (NarwalCommandError, NarwalConnectionError) as err:
            _LOGGER.warning(
                "Room clean request failed: product=%s endpoint=%s map=%s rooms=%s error=%s",
                self.product_id,
                topic,
                self.state.current_map_id,
                room_ids,
                type(err).__name__,
            )
            raise

    async def start_easy_clean(self) -> CommandResponse:
        """Start explicit whole-house Freo Mind using the App request schema."""
        return await self.send_command(
            TOPIC_CMD_EASY_CLEAN,
            payload=self._build_easy_clean_payload(CleanMode.SMART),
            timeout=10.0,
        )

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
            if value is None:
                return ""
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").rstrip("\n")
            return str(value).rstrip("\n")

        info = DeviceInfo(
            product_key=_clean(response.data.get("1", "")),
            device_id=_clean(response.data.get("2", "")),
            firmware_version=_clean(response.data.get("3", "")),
        )
        if not info.product_key or not info.device_id:
            raise NarwalCommandError("Device-info response is empty or invalid")
        self.state.device_info = info
        if info.firmware_version:
            self.state.firmware_version = info.firmware_version
        return info

    async def get_feature_list(self) -> dict[int, int]:
        """Query the robot's feature flags."""
        response = await self.send_command(TOPIC_CMD_GET_FEATURE_LIST)
        try:
            features = {int(key): int(value) for key, value in response.data.items()}
        except (TypeError, ValueError) as err:
            raise NarwalCommandError("Feature-list response is invalid") from err
        if not features or (1 in features and features[1] not in (0, 1)):
            raise NarwalCommandError("Feature-list response is empty or invalid")
        return features

    async def get_config(self) -> dict[str, Any]:
        """Read device settings used for capability discovery."""
        response = await self.send_command(TOPIC_CMD_GET_CONFIG)
        if not response.success:
            raise NarwalCommandError(
                f"Cannot read device config (result code {response.result_code})"
            )
        config = response.data.get("2", {})
        if not isinstance(config, dict) or not config:
            raise NarwalCommandError("Device-config response is empty or invalid")
        return config

    async def set_dry_mop_strength(self, strength: DryMopStrength | int) -> CommandResponse:
        """Set the station mop drying strength."""
        # SetConfig.Request field 13 in app 2.6.90.
        return await self.send_command(
            TOPIC_CMD_SET_CONFIG,
            self._encode_varint_field(13, int(strength)),
        )

    async def set_volume(self, percentage: int) -> CommandResponse:
        """Set speaker volume (SetConfig.Request.volumePercentage, field 1)."""
        return await self.send_command(
            TOPIC_CMD_SET_CONFIG,
            self._encode_varint_field(1, max(0, min(100, int(percentage)))),
        )

    async def set_boolean_config(self, set_field: int, enabled: bool) -> CommandResponse:
        """Set one boolean SetConfig field recovered from the Android app."""
        return await self.send_command(
            TOPIC_CMD_SET_CONFIG,
            self._encode_varint_field(set_field, int(enabled)),
        )

    async def get_current_voice_info(self) -> dict[str, str]:
        """Read the active custom voice package metadata."""
        response = await self.send_command(TOPIC_CMD_GET_CURRENT_VOICE_INFO)
        raw = response.data.get("2")
        if not isinstance(raw, dict):
            return {}

        def _text(value: Any) -> str:
            return (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else str(value or "")
            )

        voice = {
            "language": _text(raw.get("1")),
            "version": _text(raw.get("2")),
            "description": _text(raw.get("3")),
            "resource_id": _text(raw.get("4")),
        }
        self.state.current_voice = voice
        return voice

    async def set_custom_voice(
        self, *, language: str, resource_id: str, version: str
    ) -> CommandResponse:
        """Select a cloud voice package using the app's exact MQTT fields."""
        payload = (
            self._encode_bytes_field(1, language.encode())
            + self._encode_bytes_field(2, resource_id.encode())
            + self._encode_bytes_field(3, version.encode())
        )
        return await self.send_command(TOPIC_CMD_SET_CUSTOM_VOICE, payload)

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
                    nested += self._encode_bytes_field(3, self._encode_custom_clean_order(value))
            all_clean_orders = current.get("4")
            if not isinstance(all_clean_orders, list):
                all_clean_orders = [all_clean_orders]
            for value in all_clean_orders:
                if isinstance(value, dict):
                    nested += self._encode_bytes_field(4, self._encode_custom_clean_order(value))
        payload = self._encode_bytes_field(18, nested)
        return await self.send_command(TOPIC_CMD_SET_CONFIG, payload)

    async def get_status(self, full_update: bool = True) -> CommandResponse:
        """Query the robot's base status."""
        response = await self.send_command(TOPIC_CMD_GET_BASE_STATUS)
        status_data = response.data.get("2", {})
        if status_data:
            if full_update:
                self.state.update_from_base_status(status_data)
                self._observe_map_id(status_data.get("30"))
            else:
                self.state.update_battery_from_base_status(status_data)
        return response

    async def get_current_task(self) -> CommandResponse:
        """Query the current cleaning task."""
        return await self.send_command(TOPIC_CMD_GET_CURRENT_TASK)

    async def get_map(self) -> MapData:
        """Download and parse the active map."""
        map_generation = self._map_generation
        response = await self.send_command(TOPIC_CMD_GET_MAP, timeout=15.0)
        if not response.success:
            raise NarwalCommandError(f"Cannot download map (result code {response.result_code})")
        product_key = (
            self.state.device_info.product_key if self.state.device_info is not None else ""
        )
        try:
            candidate = MapData.from_response(
                response.data,
                product_key=product_key,
            )
        except (OverflowError, TypeError, UnicodeError, ValueError) as err:
            raise NarwalCommandError("Cannot parse the downloaded map") from err

        if (
            not map_dimensions_are_safe(candidate.width, candidate.height)
            or candidate.resolution <= 0
            or not candidate.compressed_map
            or len(candidate.compressed_map) > MAX_COMPRESSED_MAP_BYTES
        ):
            raise NarwalCommandError("Downloaded map is empty or invalid")
        if not await asyncio.to_thread(
            map_payload_is_valid,
            candidate.compressed_map,
            candidate.width,
            candidate.height,
        ):
            raise NarwalCommandError("Downloaded map pixel data is invalid")
        if map_generation != self._map_generation:
            raise NarwalCommandError("Downloaded map was superseded by a map switch or edit")
        version = candidate.raw.get("2")
        self._reported_map_version = version if type(version) is int and version > 0 else None
        sequence = candidate.raw.get("22")
        self._display_map_seq = sequence if type(sequence) is int and sequence >= 0 else None

        map_id = self._as_int(candidate.raw.get("1"))
        self.state.map_data = candidate
        if map_id is not None:
            self.state.current_map_id = map_id
        self.map_refresh_needed = False
        return candidate

    async def get_all_maps(self) -> CommandResponse:
        """Download and retain the reduced list of saved maps."""
        response = await self.send_command(TOPIC_CMD_GET_ALL_MAPS, timeout=15.0)
        if not response.success:
            return response
        values = response.data.get("2", [])
        if isinstance(values, dict):
            values = [values]
        maps: list[dict[str, Any]] = []
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict):
                continue
            map_id = self._as_int(value.get("1"))
            if map_id is None:
                continue
            maps.append(
                {
                    "id": map_id,
                    "name": self._as_text(value.get("28")) or f"Map {map_id}",
                    "is_main": bool(self._as_int(value.get("30")) or 0),
                }
            )
        self.state.saved_maps = maps
        self.state.saved_maps_supported = True
        if self.state.current_map_id is None:
            current = next((item for item in maps if item["is_main"]), None)
            if current:
                self.state.current_map_id = int(current["id"])
        return response

    async def switch_map(self, map_id: int) -> CommandResponse:
        """Switch the robot's active map."""
        response = await self.send_command(
            TOPIC_CMD_SWITCH_MAP,
            self._encode_varint_field(1, int(map_id)),
            timeout=15.0,
        )
        if response.success:
            self._map_generation += 1
            self._reported_map_version = None
            self._display_map_seq = None
            self.map_refresh_needed = True
            self.state.current_map_id = int(map_id)
            self.state.map_data = None
            self.state.map_display_data = None
            self._last_display_map_time = 0.0
        return response

    async def get_battery_info(self) -> CommandResponse:
        """Read the battery-health detail payload."""
        response = await self.send_command(TOPIC_CMD_GET_BATTERY_INFO)
        names = {
            "1": "full_charge_capacity",
            "2": "charge_cycle_count",
            "3": "serial_number",
            "4": "state_of_charge",
            "5": "temperature",
            "6": "voltage",
            "7": "current",
            "8": "health_state",
            "9": "rated_capacity",
        }
        details: dict[str, Any] = {}
        for field, name in names.items():
            value = response.data.get(field)
            if field == "3" and isinstance(value, (bytes, str)):
                details[name] = self._plain_value(value)
            elif field in {"5", "6", "7"}:
                decoded = _to_float64(value)
                if decoded is not None:
                    details[name] = decoded
            elif type(value) is int:
                details[name] = value
        # BatteryInfo.Response has no ServiceResult field; field 1 is capacity.
        if details:
            self.state.battery_details = details
            self.state.battery_details_supported = True
        return response

    async def get_dry_mop_remaining_time(self) -> CommandResponse:
        """Read the remaining mop-drying time reported by the station."""
        response = await self.send_command(TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME)
        remain = self._as_int(response.data.get("2"))
        if response.success and remain is not None:
            self.state.dry_mop_remaining_time = remain
            self.state.dry_mop_remaining_time_supported = True
        return response

    async def get_consumable_info(self) -> CommandResponse:
        """Read robot-side consumable maintenance/replacement status."""
        response = await self.send_command(TOPIC_CMD_GET_CONSUMABLE_INFO)
        payload = response.data.get("1")
        if isinstance(payload, dict) and all(
            type(item) is int
            for field in ("1", "2")
            for item in (
                payload.get(field, [])
                if isinstance(payload.get(field, []), list)
                else [payload[field]]
            )
        ):
            self.state.consumable_maintain_items = self._int_values(payload.get("1"))
            self.state.consumable_replace_items = self._int_values(payload.get("2"))
            self.state.mqtt_consumables_supported = True
        return response

    async def reset_consumable(self, *, category: str, protocol_value: int) -> CommandResponse:
        """Reset one robot-side consumable counter."""
        if category not in ("maintain", "replace"):
            raise ValueError("Unknown consumable category")
        allowed = (
            set(CONSUMABLE_MAINTAIN_TYPE_MAP.values())
            if category == "maintain"
            else set(CONSUMABLE_REPLACE_TYPE_MAP.values())
        )
        if type(protocol_value) is not int or protocol_value not in allowed:
            raise ValueError("Unknown consumable protocol value")
        field = 1 if category == "maintain" else 2
        nested = self._encode_varint_field(field, protocol_value)
        response = await self.send_command(
            TOPIC_CMD_RESET_CONSUMABLE_INFO,
            self._encode_bytes_field(1, nested),
        )
        if response.success:
            await self.get_consumable_info()
        return response

    async def get_clean_plans(self) -> CommandResponse:
        """Read saved cleaning plans/quick tasks."""
        response = await self.send_command(TOPIC_CMD_GET_CLEAN_PLANS)
        values = response.data.get("2", [])
        if isinstance(values, dict):
            values = [values]
        plans: list[dict[str, Any]] = []
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict):
                continue
            plan_id = self._as_int(value.get("1"))
            if plan_id is None:
                continue
            plans.append(
                {
                    "id": plan_id,
                    "map_id": self._as_int(value.get("2")),
                    "clean_mode": self._as_int(value.get("3")),
                    "is_custom": bool(self._as_int(value.get("4")) or 0),
                    # The app only uses customName when it is non-empty. Its
                    # fallback is the localized CleanMode name, which is
                    # applied by the HA select entity.
                    "name": self._as_text(value.get("5")),
                    "shown_in_station": bool(self._as_int(value.get("6")) or 0),
                    "perform_times": self._as_int(value.get("7")),
                    "order": self._as_int(value.get("8")),
                }
            )
        if response.success:
            self.state.clean_plans = plans
            self.state.clean_plans_supported = True
        return response

    async def start_clean_plan(self, plan_id: int) -> CommandResponse:
        """Start one saved plan using StartWithPlan.Request.planId.

        The observed App saved-plan request also carries only planId. The
        saved plan, not this request, determines which rooms are cleaned.
        """
        return await self.send_command(
            TOPIC_CMD_START_CLEAN_PLAN,
            self._encode_varint_field(1, int(plan_id)),
            timeout=10.0,
        )

    async def get_clean_timeline(self) -> CommandResponse:
        """Request current cleaning timeline data."""
        response = await self.send_command(TOPIC_CMD_GET_CLEAN_TIMELINE)
        timeline = response.data.get("2")
        if response.success and isinstance(timeline, dict):
            self._update_clean_timeline(timeline)
        return response

    async def get_clean_progress(self) -> CommandResponse:
        """Request the current clean-progress detail payload."""
        response = await self.send_command(TOPIC_CMD_GET_CLEAN_PROGRESS)
        # The response schema is unverified. Retain diagnostics, not a capability
        # or a guessed ServiceResult/cleaning-progress interpretation.
        if response.data:
            self.state.clean_progress = self._plain_dict(response.data)
        return response

    def _update_clean_timeline(self, decoded: dict[str, Any]) -> None:
        """Store the repeated RobotEventNode list without inventing enum labels."""
        values = decoded.get("1", [])
        if isinstance(values, dict):
            values = [values]
        self.state.clean_timeline = [
            self._plain_dict(value) for value in values if isinstance(value, dict)
        ]
        self.state.clean_timeline_supported = True

    @classmethod
    def _plain_dict(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {str(key): cls._plain_value(item) for key, item in value.items()}

    @classmethod
    def _plain_value(cls, value: Any) -> Any:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, dict):
            return cls._plain_dict(value)
        if isinstance(value, list):
            return [cls._plain_value(item) for item in value]
        return value

    @staticmethod
    def _as_text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value) if value not in (None, "") else ""

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except TypeError, ValueError:
            return None

    @classmethod
    def _int_values(cls, value: Any) -> set[int]:
        values = value if isinstance(value, list) else [value]
        return {parsed for item in values if (parsed := cls._as_int(item)) is not None}

    async def take_picture(self) -> bytes | None:
        """Request the camera's encrypted raw image."""
        try:
            response = await self.send_command(
                TOPIC_CMD_TAKE_PICTURE,
                timeout=15.0,
            )
        except NarwalCommandError, NarwalConnectionError:
            return None
        return response.data.get("2") if response.result_code == CommandResult.SUCCESS else None

    async def set_led(self, on: bool) -> None:
        """Reject an unverified fill-light control instead of sending invalid protobuf.

        developer/led_control expects nested LedFreeControl or StationLed
        messages, not a boolean. Its relationship to a camera fill light is
        unverified; do not guess a device command.
        """
        del on
        raise NarwalCommandError("Camera fill-light control is not supported")
