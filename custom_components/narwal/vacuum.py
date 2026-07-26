"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)

try:
    from homeassistant.components.vacuum import Segment
except ImportError:
    Segment = None  # HA < 2026.3 — room cleaning unavailable
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import FAN_SPEED_LIST, FAN_SPEED_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResponse, CommandResult, WorkingStatus

_LOGGER = logging.getLogger(__name__)

WORKING_STATUS_TO_ACTIVITY: dict[WorkingStatus, VacuumActivity] = {
    WorkingStatus.IDLE: VacuumActivity.IDLE,
    WorkingStatus.WASH_AND_DRY_MOP: VacuumActivity.DOCKED,
    WorkingStatus.DUST_GATHERING: VacuumActivity.DOCKED,
    WorkingStatus.DRY_STATION_BAG: VacuumActivity.DOCKED,
    WorkingStatus.PARKING: VacuumActivity.RETURNING,
    WorkingStatus.RECALL: VacuumActivity.RETURNING,
    WorkingStatus.RELOCALIZATION: VacuumActivity.RETURNING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "vacuum"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
    ) | (VacuumEntityFeature.CLEAN_AREA if Segment is not None else VacuumEntityFeature(0))
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the vacuum entity."""
        super().__init__(coordinator)
        self._set_entity_identity("vacuum", "vacuum")
        self._last_fan_speed: str | None = None

    @staticmethod
    def _require_success(response: CommandResponse, action: str) -> None:
        """Raise a safe HA error when the robot rejects an action."""
        if not response.success:
            raise HomeAssistantError(
                f"Narwal {action} failed with result code "
                f"{response.result_code}"
            )

    async def _async_refresh_after_write(self) -> None:
        """Refresh coordinator data after a successful write."""
        await self.coordinator.async_request_refresh()

    @property
    def activity(self) -> VacuumActivity:
        """Return the current vacuum activity."""
        state = self.coordinator.data
        if state is None:
            return VacuumActivity.IDLE
        is_cleaning_state = state.has_cleaning_task
        # Legacy LAN pause state can stay stale after docking, so only trust it
        # while a cleaning task is active. Pause takes priority over returning.
        if state.is_paused and is_cleaning_state:
            return VacuumActivity.PAUSED
        # Check returning before cleaning because cleaning task sub-states can
        # represent recall/parking phases.
        if state.is_returning:
            return VacuumActivity.RETURNING
        if state.is_cleaning:
            return VacuumActivity.CLEANING
        if state.is_docked:
            return VacuumActivity.DOCKED
        activity = WORKING_STATUS_TO_ACTIVITY.get(state.working_status)
        if activity is not None:
            return activity
        # An unknown status isn't evidence that the robot is cleaning. This
        # commonly occurs when a cloud-connected robot hasn't responded yet.
        if not state.is_docked:
            _LOGGER.warning(
                "Unmapped robot status/task type %s (%d) while off-dock; "
                "reporting IDLE",
                state.working_status.name, state.working_status.value,
            )
            return VacuumActivity.IDLE
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Return the current fan speed.

        The robot protocol does not broadcast the active fan speed setting,
        so we track the last value set via the integration. Returns None
        until the user sets a fan speed for the first time.
        """
        return self._last_fan_speed

    # Timeout for action commands (start/stop/return) — robot may need
    # time to load map, plan route, etc., especially after waking.
    _ACTION_TIMEOUT = 10.0

    async def _ensure_awake(self) -> None:
        """Wake the robot if it is not broadcasting.

        Sends a wake burst and waits for broadcasts. If the robot doesn't
        respond, the command is still attempted — it may work even without
        a wake confirmation (e.g., shallow sleep).
        """
        client = self.coordinator.client
        if not client.robot_awake:
            _LOGGER.debug("Robot not awake — sending wake burst")
            await client.wake(timeout=10.0)

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        await self._ensure_awake()
        state = self.coordinator.data
        # is_paused stays stale after docking — only trust it during cleaning
        is_cleaning = state and state.has_cleaning_task
        if is_cleaning and state.is_paused:
            response = await self._async_await_command(
                self.coordinator.client.resume(timeout=self._ACTION_TIMEOUT),
                "resume",
            )
            self._require_success(response, "resume")
        else:
            response = await self._async_await_command(
                self.coordinator.client.start(),
                "start",
            )
            self._require_success(response, "start")
        await self._async_refresh_after_write()

    async def async_stop(self, **kwargs) -> None:
        """Stop cleaning."""
        await self._ensure_awake()
        response = await self._async_await_command(
            self.coordinator.client.stop(),
            "stop",
        )
        self._require_success(response, "stop")
        await self._async_refresh_after_write()

    async def async_pause(self) -> None:
        """Pause cleaning."""
        response = await self._async_await_command(
            self.coordinator.client.pause(),
            "pause",
        )
        self._require_success(response, "pause")
        await self._async_refresh_after_write()

    async def async_return_to_base(self, **kwargs) -> None:
        """Return to the dock."""
        await self._ensure_awake()
        response = await self._async_await_command(
            self.coordinator.client.return_to_base(
                timeout=self._ACTION_TIMEOUT
            ),
            "return-to-base",
        )
        self._require_success(response, "return-to-base")
        await self._async_refresh_after_write()

    async def async_locate(self, **kwargs) -> None:
        """Locate the vacuum — robot says 'Robot is here'."""
        await self._ensure_awake()
        response = await self._async_await_command(
            self.coordinator.client.locate(),
            "locate",
        )
        self._require_success(response, "locate")
        await self._async_refresh_after_write()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        """Set the fan speed."""
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            raise HomeAssistantError(f"Unsupported fan speed: {fan_speed}")
        response = await self._async_await_command(
            self.coordinator.client.set_fan_speed(level),
            "set fan speed",
        )
        self._require_success(response, "set fan speed")
        self._last_fan_speed = fan_speed
        self.async_write_ha_state()
        await self._async_refresh_after_write()

    # --- Segment API (HA 2026.3 room-specific cleaning) ---

    async def async_get_segments(self) -> list:
        """Return cleanable room segments from map data.

        Maps RoomInfo from get_map to HA Segment objects and preserves the
        protocol-provided display names.
        Falls back to HA-cached last_seen_segments when map data is not yet
        loaded (robot asleep at startup), so clean_area works without waking
        the robot first.
        Returns [] when HA < 2026.3 (Segment class unavailable).
        """
        if Segment is None:
            return []
        state = self.coordinator.data
        if state is None or state.map_data is None:
            # Robot sleeping — return cached segments so clean_area still works
            last = getattr(self, "last_seen_segments", None)
            return list(last) if last else []
        return [
            Segment(
                id=str(room.room_id),
                name=room.display_name,
                group="Rooms" if room.category == 1 else "Utility" if room.category == 2 else None,
            )
            for room in state.map_data.rooms
            if room.room_id > 0
        ]

    async def async_clean_segments(
        self, segment_ids: list[str], **kwargs: Any
    ) -> None:
        """Clean specific rooms by segment IDs.

        Converts string segment IDs back to integer room IDs and sends
        a room-specific clean command to the robot.
        """
        await self._ensure_awake()
        room_ids = [int(sid) for sid in segment_ids]
        _LOGGER.info("Starting room-specific clean: rooms=%s", room_ids)
        response = await self._async_await_command(
            self.coordinator.client.start_rooms(room_ids),
            "room clean",
        )
        try:
            result_name = CommandResult(response.result_code).name
        except ValueError:
            result_name = f"UNKNOWN({response.result_code})"
        _LOGGER.info(
            "Room clean response: %s (code=%s), rooms=%s",
            result_name,
            response.result_code,
            room_ids,
        )
        self._require_success(response, f"room clean ({result_name})")
        await self._async_refresh_after_write()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._check_segment_changes()
        super()._handle_coordinator_update()

    def _check_segment_changes(self) -> None:
        """Detect segment changes and raise repair issue if needed.

        Compares current room data against last_seen_segments (managed by HA).
        If rooms have changed (added, removed, or renamed), creates a repair
        issue so the user can update their segment-to-area mappings.
        """
        last = getattr(self, "last_seen_segments", None)
        if last is None:
            return  # No mapping configured yet
        state = self.coordinator.data
        if state is None or state.map_data is None:
            return
        current_set = {
            (str(r.room_id), r.display_name)
            for r in state.map_data.rooms
            if r.room_id > 0
        }
        last_set = {(s.id, s.name) for s in last}
        if current_set != last_set:
            _LOGGER.info(
                "Segment change detected: %d -> %d rooms",
                len(last_set), len(current_set),
            )
            self.async_create_segments_issue()
