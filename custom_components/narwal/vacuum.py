"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import vacuum as vacuum_component
from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .const import FAN_SPEED_LIST, FAN_SPEED_MAP
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CommandResponse, CommandResult, ServiceResult, WorkingStatus

_LOGGER = logging.getLogger(__name__)

# Segment and clean-area were added in HA 2026.3, which is the minimum.
_Segment = vacuum_component.Segment
_CLEAN_AREA_FEATURE: VacuumEntityFeature = VacuumEntityFeature.CLEAN_AREA

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
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Narwal vacuum entity."""
    coordinator = entry.runtime_data
    async_add_entities([NarwalVacuum(coordinator)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "vacuum"
    _BASE_SUPPORTED_FEATURES = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.LOCATE
    ) | (_CLEAN_AREA_FEATURE if _Segment is not None else VacuumEntityFeature(0))

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the vacuum entity."""
        super().__init__(coordinator)
        self._set_entity_identity("vacuum", "vacuum")

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Expose suction control only when the device reports it."""
        features = self._BASE_SUPPORTED_FEATURES
        if self.coordinator.supports_fan_speed:
            features |= VacuumEntityFeature.FAN_SPEED
        return features

    @property
    def fan_speed_list(self) -> list[str]:
        """Return supported fan choices when suction control is available."""
        return FAN_SPEED_LIST if self.coordinator.supports_fan_speed else []

    @staticmethod
    def _require_success(response: CommandResponse, action: str) -> None:
        """Raise a safe HA error when the robot rejects an action.

        Device service results carry actionable states (power low, mop or dust
        box missing, already in the station, ...), so report the protocol name
        instead of a bare number when it is known.
        """
        if response.success:
            return
        code = response.result_code
        try:
            reason = ServiceResult(code).name if code is not None else "UNKNOWN"
        except ValueError:
            reason = f"UNKNOWN({code})"
        raise HomeAssistantError(f"Narwal {action} failed with result code {code} ({reason})")

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
        # Only trust pause while a cleaning task is active.
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
            _LOGGER.debug(
                "Unmapped robot status/task type %s (%d) while off-dock; reporting IDLE",
                state.working_status.name,
                state.working_status.value,
            )
            return VacuumActivity.IDLE
        return VacuumActivity.IDLE

    @property
    def fan_speed(self) -> str | None:
        """Return the latest fan speed reported by the robot."""
        raw = self.coordinator.client.state.raw_base_status.get("26")
        if raw is None:
            return None
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return None
        return next(
            (name for name, value in FAN_SPEED_MAP.items() if int(value) == level),
            None,
        )

    # Robot actions may need time to load a map and plan a route.
    _ACTION_TIMEOUT = 10.0

    async def async_start(self) -> None:
        """Start or resume cleaning.

        A new clean first tries the built-in Freo Mind plan for the active map,
        then direct Freo Mind and sweep-and-mop. The plan select entity affects
        only the explicit start-plan button, not this default action.
        """
        state = self.coordinator.data
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
            if response.result_code == CommandResult.CONFLICT:
                # The robot already has an active task, so the start intent is
                # satisfied even if this exact command was not accepted.
                _LOGGER.info("Robot already has an active task; treating start as accepted")
            else:
                self._require_success(response, "start")
        await self._async_refresh_after_write()

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop cleaning."""
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

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Return to the dock."""
        response = await self._async_await_command(
            self.coordinator.client.return_to_base(timeout=self._ACTION_TIMEOUT),
            "return-to-base",
        )
        self._require_success(response, "return-to-base")
        await self._async_refresh_after_write()

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum — robot says 'Robot is here'."""
        response = await self._async_await_command(
            self.coordinator.client.locate(),
            "locate",
        )
        self._require_success(response, "locate")
        await self._async_refresh_after_write()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set the fan speed."""
        level = FAN_SPEED_MAP.get(fan_speed)
        if level is None:
            raise HomeAssistantError(f"Unsupported fan speed: {fan_speed}")
        response = await self._async_await_command(
            self.coordinator.client.set_fan_speed(level),
            "set fan speed",
        )
        self._require_success(response, "set fan speed")
        # Reflect the accepted value immediately. The next complete base-status
        # payload replaces raw_base_status, so app/device changes still win.
        self.coordinator.client.state.raw_base_status["26"] = int(level)
        self.async_write_ha_state()
        await self._async_refresh_after_write()

    # --- Segment API (HA 2026.3 room-specific cleaning) ---

    async def async_get_segments(self) -> list[Any]:
        """Return cleanable room segments from map data.

        Maps RoomInfo from get_map to HA Segment objects and preserves the
        protocol-provided display names.
        Falls back to HA-cached last_seen_segments when map data is not yet
        loaded (robot asleep at startup), so clean_area works without waking
        the robot first.
        Returns [] when map data is not yet loaded.
        """
        state = self.coordinator.data
        if state is None or state.map_data is None:
            # Robot sleeping — return cached segments so clean_area still works
            last = getattr(self, "last_seen_segments", None)
            return list(last) if last else []
        return [
            _Segment(
                id=str(room.room_id),
                name=room.display_name,
                group=None,  # Map field 4 is floor texture, not a room category.
            )
            for room in state.map_data.rooms
            if room.room_id > 0
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        """Clean specific rooms by segment IDs.

        Converts string segment IDs back to integer room IDs and sends
        a room-specific clean command to the robot.
        """
        if not segment_ids:
            raise HomeAssistantError("Select at least one Narwal room to clean")
        try:
            room_ids = list(dict.fromkeys(int(segment_id) for segment_id in segment_ids))
        except (TypeError, ValueError) as err:
            raise HomeAssistantError("Narwal room IDs must be integers") from err
        if any(room_id <= 0 for room_id in room_ids):
            raise HomeAssistantError("Narwal room IDs must be positive integers")
        _LOGGER.info("Starting room-specific clean: rooms=%s", room_ids)
        response = await self._async_await_command(
            self.coordinator.client.start_rooms(room_ids),
            "room clean",
        )
        code = response.result_code
        try:
            result_name = CommandResult(code).name if code is not None else "UNKNOWN(None)"
        except ValueError:
            result_name = f"UNKNOWN({code})"
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
        if self.registry_entry is None:
            return
        last = getattr(self, "last_seen_segments", None)
        if last is None:
            return  # No mapping configured yet
        state = self.coordinator.data
        if state is None or state.map_data is None:
            return
        current_set = {
            (str(r.room_id), r.display_name) for r in state.map_data.rooms if r.room_id > 0
        }
        last_set = {(s.id, s.name) for s in last}
        if current_set != last_set:
            _LOGGER.info(
                "Segment change detected: %d -> %d rooms",
                len(last_set),
                len(current_set),
            )
            create_issue = getattr(self, "async_create_segments_issue", None)
            if create_issue is not None:
                create_issue()
