"""Data models for Narwal vacuum state."""

from __future__ import annotations

import contextlib
import logging
import math
import struct
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .const import CommandResult, WorkingStatus

_LOGGER = logging.getLogger(__name__)


def _int_or_default(value: Any, default: int = 0) -> int:
    """Convert an untrusted protocol value to an integer fallback."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except TypeError, ValueError:
        return default


@dataclass
class DeviceInfo:
    """Device identity from get_device_info response."""

    product_key: str = ""
    device_id: str = ""
    firmware_version: str = ""


@dataclass
class RoomInfo:
    """A room on the map.

    Fields from get_map / get_editable_map field 2.12:
      field 1: room_id (matches pixel value >> 8 in map grid)
      field 2: room_sub_type — ROOM_TYPE protocol enum (0=unspecified,
               1=main bedroom, 2=secondary room, 3=living room, 4=kitchen,
               5=study, 6=bathroom, 7=dining room, 8=corridor, 9=balcony,
               10=utility room, 11=cloak room, 12=nursery, 13=recreation,
               14=shower room, 15=other)
      field 3: user-assigned name (UTF-8, empty if not named by user)
      field 4: category (1=room, 2=utility/small space)
      field 8: instance_index (1-based, for numbering duplicates: Bathroom 1, 2, 3...)
    """

    room_id: int = 0
    name: str = ""  # user-assigned name from field 3
    room_sub_type: int = 0  # ROOM_TYPE enum from field 2
    category: int = 0  # 1=room, 2=utility (field 4)
    instance_index: int = 0  # numbering for duplicates (field 8)
    model_key: str = ""  # product_key — selects per-model name overrides

    # ROOM_TYPE enum → default display name
    ROOM_TYPE_NAMES: ClassVar[dict[int, str]] = {
        0: "Room",
        1: "Primary Bedroom",
        2: "Secondary Bedroom",
        3: "Living Room",
        4: "Kitchen",
        5: "Study",
        6: "Bathroom",
        7: "Dining Room",
        8: "Corridor",
        9: "Balcony",
        10: "Utility Room",
        11: "Cloak Room",
        12: "Nursery",
        13: "Recreation Room",
        14: "Shower Room",
        15: "Other",
    }

    # Per-model overrides where Narwal renamed sub-types between models.
    # Confirmed on Narwal Flow 2 (QxMSPG6VSO, firmware v01.07.16.01) — see #22.
    MODEL_ROOM_TYPE_OVERRIDES: ClassVar[dict[str, dict[int, str]]] = {
        "QxMSPG6VSO": {  # Flow 2
            1: "Master Bedroom",
            5: "Bathroom",
            10: "Corridor",
        },
    }

    @property
    def display_name(self) -> str:
        """Return user name if set, otherwise generate default from ROOM_TYPE enum.

        Matches Narwal app behavior: unnamed rooms show their type name
        with an instance number suffix for duplicates (e.g. "Bathroom 2").
        Per-model overrides apply where Narwal renamed sub-types (e.g. Flow 2
        renames sub_type 1 → "Master Bedroom" vs Flow 1's "Primary Bedroom").
        """
        if self.name:
            return self.name
        overrides = self.MODEL_ROOM_TYPE_OVERRIDES.get(self.model_key, {})
        base = overrides.get(self.room_sub_type) or self.ROOM_TYPE_NAMES.get(
            self.room_sub_type, "Room"
        )
        if self.instance_index > 1:
            return f"{base} {self.instance_index}"
        return base


@dataclass
class ObstacleInfo:
    """An obstacle/furniture annotation on the map.

    Parsed from get_map field 2.32 (MapFurnitureInfoList).
    The typeId maps to the protocol furniture enum.

    Protobuf field mapping:
      bbp field 1 -> id (int32)
      bbp field 2 -> typeId (uint32, furniture enum)
      bbp field 3.1.1 -> centerX (float32)
      bbp field 3.1.2 -> centerY (float32)
      bbp field 3.2 -> width (float32)
      bbp field 3.3 -> height (float32)
      bbp field 4 -> angle (float32, degrees)
    """

    id: int = 0
    type_id: int = 0  # Protocol furniture enum
    center_x: float = 0.0  # Raw native-map X coordinate
    center_y: float = 0.0  # Raw native-map Y coordinate
    width: float = 0.0  # Object width in grid units
    height: float = 0.0  # Object height in grid units
    angle: float = 0.0  # Rotation in degrees

    # Full protocol furniture type enum
    TYPE_NAMES: ClassVar[dict[int, str]] = {
        0: "Placeholder",
        1: "Single Bed",
        2: "Double Bed",
        3: "Baby Bed",
        4: "Dining Table",
        5: "Round Table",
        6: "Tea Table",
        7: "Round Tea Table",
        8: "TV Stand",
        9: "Bedside Table",
        10: "Locker",
        11: "Wardrobe",
        12: "Shoe Cabinet",
        13: "Armchair",
        14: "Sofa",
        15: "L-Shaped Sofa",
        16: "Lazy Chair",
        17: "Chair",
        18: "Bar Chair",
        19: "Cat Toilet",
        20: "Pet Feeder",
        21: "Pet House",
        22: "Washing Machine",
        23: "Refrigerator",
        24: "Air Conditioner",
        25: "Fan",
        26: "Potted Plant",
        27: "Floor Mirror",
        28: "Toilet",
        29: "Piano",
        30: "U-Shaped Sofa",
        31: "Desk",
        32: "Grand Piano",
        33: "Washbasin",
        34: "Stove",
        75: "Cat House",
        76: "Dog House",
        77: "Round Placeholder",
        78: "Weighing Scale",
    }

    @property
    def display_name(self) -> str:
        """Return human-readable name for the obstacle type."""
        return self.TYPE_NAMES.get(self.type_id, f"Object {self.type_id}")

    def to_grid_coords(self, origin_x: int, origin_y: int) -> tuple[float, float]:
        """Convert native-map coordinates to the renderer's grid frame.

        Same transform as dock/robot: pixel = raw - origin.
        """
        return (self.center_x - origin_x, self.center_y - origin_y)


def _to_float32(val: Any) -> float | None:
    """Convert a protobuf value to float32.

    blackboxprotobuf may return fixed32 fields as either:
      - Python float (if it detects wire type 5 as float)
      - Python int (raw uint32 bit pattern)
    Handle both cases.
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, int):
        try:
            result = struct.unpack("f", struct.pack("I", val & 0xFFFFFFFF))[0]
            return result if math.isfinite(result) else None
        except struct.error:
            return None
    return None


def _to_float64(val: Any) -> float | None:
    """Convert a schema-less protobuf fixed64 value to a double."""
    if isinstance(val, bool):
        return None
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, int):
        try:
            result = struct.unpack("<d", struct.pack("<Q", val & 0xFFFFFFFFFFFFFFFF))[0]
            return result if math.isfinite(result) else None
        except struct.error:
            return None
    return None


def _parse_obstacles(field32: dict) -> list[ObstacleInfo]:
    """Parse obstacle/furniture annotations from bbp-decoded field 2.32.

    Args:
        field32: The decoded dict from map payload field "32".

    Returns:
        List of ObstacleInfo objects. Skips items that fail to parse.
    """
    items = field32.get("1", [])
    if isinstance(items, dict):
        items = [items]

    obstacles: list[ObstacleInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pos = item.get("3", {})
            center = pos.get("1", {}) if isinstance(pos, dict) else {}

            cx = _to_float32(center.get("1")) if isinstance(center, dict) else None
            cy = _to_float32(center.get("2")) if isinstance(center, dict) else None
            w = _to_float32(pos.get("2")) if isinstance(pos, dict) else None
            h = _to_float32(pos.get("3")) if isinstance(pos, dict) else None
            angle = _to_float32(item.get("4"))

            obstacles.append(
                ObstacleInfo(
                    id=int(item.get("1", 0)),
                    type_id=int(item.get("2", 0)),
                    center_x=cx or 0.0,
                    center_y=cy or 0.0,
                    width=w or 0.0,
                    height=h or 0.0,
                    angle=angle or 0.0,
                )
            )
        except ValueError, TypeError, AttributeError:
            continue
    return obstacles


@dataclass
class MapData:
    """Map data from get_map response."""

    width: int = 0
    height: int = 0
    resolution: int = 0
    rooms: list[RoomInfo] = field(default_factory=list)
    compressed_map: bytes = b""
    area: int = 0
    created_at: int = 0
    dock_x: float | None = None  # dock position in grid coordinates
    dock_y: float | None = None
    origin_x: int = 0  # Legacy renderer X offset from border.left (field 2.6.3)
    origin_y: int = 0  # Legacy renderer Y offset from border.bottom (field 2.6.1)
    obstacles: list[ObstacleInfo] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, decoded: dict[str, Any], product_key: str = "") -> MapData:
        """Parse map data from a get_map field5 response.

        Args:
            decoded: blackboxprotobuf-decoded get_map response.
            product_key: Device product key — propagated to RoomInfo so model-
                specific room-type name overrides apply (see #22 for Flow 2).
        """
        payload = decoded.get("2")
        if not isinstance(payload, dict) or not payload:
            return cls()

        rooms = []
        room_list = payload.get("12", [])
        if isinstance(room_list, dict):
            room_list = [room_list]
        for room in room_list:
            if isinstance(room, dict):
                name_raw = room.get("3", b"")
                if isinstance(name_raw, bytes):
                    name = name_raw.decode("utf-8", errors="replace")
                elif isinstance(name_raw, str):
                    # blackboxprotobuf sometimes returns "b'...'" strings
                    name = name_raw
                    if name.startswith("b'") and name.endswith("'"):
                        name = name[2:-1]
                else:
                    name = str(name_raw)
                rooms.append(
                    RoomInfo(
                        room_id=_int_or_default(room.get("1")),
                        name=name,
                        room_sub_type=_int_or_default(room.get("2")),
                        category=_int_or_default(room.get("4")),
                        instance_index=_int_or_default(room.get("8")),
                        model_key=product_key,
                    )
                )

        compressed = payload.get("17", b"")
        if isinstance(compressed, str):
            compressed = compressed.encode("latin-1")

        resolution = _int_or_default(payload.get("3"))

        # Native StaticMapPayload field 6 is Border (bottom, top, left, right),
        # not the PoseData origin in field 7. Retain the existing renderer
        # offsets until native captures establish the full frame transform;
        # the app's separate BX3 conversion is not evidence for rescaling here.
        origin_x = 0
        origin_y = 0
        field6 = payload.get("6")
        if isinstance(field6, dict):
            origin_x = _int_or_default(field6.get("3"))
            origin_y = _int_or_default(field6.get("1"))

        # Parse dock position from field 8 (dock/charging station location).
        # Field 8 structure: {1: {1: x, 2: y}, 2: heading_rad}.
        # Keep the same raw-coordinate offset as display_map and furniture.
        dock_x = None
        dock_y = None
        field8 = payload.get("8")
        if isinstance(field8, dict) and resolution > 0:
            pos = field8.get("1")
            if isinstance(pos, dict) and "1" in pos and "2" in pos:
                try:
                    x = _to_float32(pos["1"])
                    y = _to_float32(pos["2"])
                    if x is not None and y is not None:
                        dock_x = x - origin_x
                        dock_y = y - origin_y
                except struct.error, OverflowError, ValueError, TypeError:
                    pass

        # Parse obstacle/furniture annotations from field 32 (MapFurnitureInfoList)
        obstacles: list[ObstacleInfo] = []
        field32 = payload.get("32")
        if isinstance(field32, dict):
            obstacles = _parse_obstacles(field32)

        return cls(
            width=_int_or_default(payload.get("4")),
            height=_int_or_default(payload.get("5")),
            resolution=resolution,
            rooms=rooms,
            compressed_map=compressed if isinstance(compressed, bytes) else b"",
            area=_int_or_default(payload.get("33")),
            created_at=_int_or_default(payload.get("34")),
            dock_x=dock_x,
            dock_y=dock_y,
            origin_x=origin_x,
            origin_y=origin_y,
            obstacles=obstacles,
            raw=payload,
        )


@dataclass
class MapDisplayData:
    """Real-time robot position from map/display_map broadcasts.

    Sent every ~1.5s during active cleaning. Contains raw robot position,
    heading in radians, and a small cleaned-area grid overlay (NOT the full
    house map — that comes from get_map).

    Validated field layout (live capture 2026-02-28, 13 broadcasts):
      field 1.1: {1: x, 2: y} — robot position as float32
      field 1.2: heading as float32 radians
      field 5: dock/reference position (constant, same format)
      field 7: cleaned-area grid {1: width, 2: height, 3: compressed_bytes}
      field 10: timestamp in milliseconds since epoch
      field 12: active room list
    """

    robot_x: float = 0.0  # Raw native-map coordinates; physical units unconfirmed
    robot_y: float = 0.0
    robot_heading: float = 0.0  # degrees (converted from radians for renderer)
    timestamp: int = 0  # milliseconds since epoch (field 10)
    # Dock/reference position from field 5 (same coordinate system as robot)
    dock_ref_x: float = 0.0
    dock_ref_y: float = 0.0

    def to_grid_coords(
        self,
        resolution: int,
        origin_x: int,
        origin_y: int,
    ) -> tuple[float, float] | None:
        """Convert native-map position to the renderer's grid frame.

        Preserve pixel = raw - offset, shared with dock and furniture.
        Native app positions may already be grid-based; BX3's separate
        physical-coordinate conversion must not be applied without native
        evidence. Border offsets are not the protocol's field 7 origin.

        Args:
            resolution: Positive map resolution; a validity gate, not a scale.
            origin_x: X pixel offset (MapData.origin_x, from field 2.6.3).
            origin_y: Y pixel offset (MapData.origin_y, from field 2.6.1).

        Returns:
            (pixel_x, pixel_y) tuple, or None if no valid position.
        """
        if self.robot_x == 0.0 and self.robot_y == 0.0:
            return None
        if resolution <= 0:
            return None
        px = self.robot_x - origin_x
        py = self.robot_y - origin_y
        return (px, py)

    @classmethod
    def from_broadcast(cls, decoded: dict[str, Any]) -> MapDisplayData:
        """Parse display_map broadcast payload."""
        import math

        result = cls()

        # Robot position — field 1.1 = {1: x, 2: y}, field 1.2 = heading_rad
        field1 = decoded.get("1", {})
        if isinstance(field1, dict):
            pos = field1.get("1", {})
            if isinstance(pos, dict):
                x_f = _to_float32(pos.get("1"))
                if x_f is not None and math.isfinite(x_f):
                    result.robot_x = x_f
                y_f = _to_float32(pos.get("2"))
                if y_f is not None and math.isfinite(y_f):
                    result.robot_y = y_f

            heading_raw = field1.get("2")
            if heading_raw is not None:
                h_f = _to_float32(heading_raw)
                if h_f is not None and math.isfinite(h_f):
                    result.robot_heading = math.degrees(h_f)

        # Dock/reference position — field 5 (same format as field 1)
        field5 = decoded.get("5", {})
        if isinstance(field5, dict):
            pos5 = field5.get("1", {})
            if isinstance(pos5, dict):
                dx = _to_float32(pos5.get("1"))
                if dx is not None and math.isfinite(dx):
                    result.dock_ref_x = dx
                dy = _to_float32(pos5.get("2"))
                if dy is not None and math.isfinite(dy):
                    result.dock_ref_y = dy

        # Timestamp — field 10 (milliseconds since epoch)
        if "10" in decoded:
            with contextlib.suppress(ValueError, TypeError):
                result.timestamp = int(decoded["10"])

        return result


@dataclass
class Position:
    """Robot position from map/display_map."""

    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0


@dataclass
class CommandResponse:
    """Response from a command sent to the robot."""

    result_code: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    raw_payload: bytes = b""

    @property
    def success(self) -> bool:
        return self.result_code == CommandResult.SUCCESS

    @property
    def general_failed(self) -> bool:
        """Return whether the robot reported a general execution failure."""
        return self.result_code == CommandResult.GENERAL_FAILED

    @property
    def not_applicable(self) -> bool:
        """Compatibility alias for the former, incorrectly named property."""
        return self.general_failed


@dataclass(frozen=True, slots=True)
class ConsumableInfo:
    """Consumable lifetime returned by the Narwal app cloud API."""

    key: str
    name: str
    type_id: int
    category: int
    total_duration: int
    usage_duration: int
    subtitle: str = ""
    code: str = ""
    reset_supported: bool = False

    @property
    def remaining_percentage(self) -> int | None:
        """Return remaining lifetime normalized to Home Assistant's 0-100 range."""
        if self.total_duration <= 0:
            return None
        remaining = (self.total_duration - self.usage_duration) / self.total_duration
        return max(0, min(100, round(remaining * 100)))


@dataclass(frozen=True, slots=True)
class NarwalNotification:
    """A device message synchronized from the Narwal cloud."""

    message_id: str
    title: str = field(default="", repr=False)
    content: str = field(default="", repr=False)
    created_at: int = 0
    is_read: bool = False
    device_id: str = field(default="", repr=False)

    @property
    def category(self) -> str:
        """Classify consumable reminders while preserving other messages."""
        text = f"{self.title} {self.content}".casefold()
        consumable_terms = (
            "耗材",
            "用尽",
            "更换",
            "滤网",
            "边刷",
            "滚刷",
            "拖布",
            "尘袋",
            "清洁剂",
            "consumable",
            "replace",
            "filter",
            "brush",
            "mop",
            "dust bag",
            "detergent",
        )
        return "consumable" if any(term in text for term in consumable_terms) else "notification"


@dataclass
class NarwalState:
    """Complete state of a Narwal vacuum.

    Updated incrementally as different topic messages arrive.
    """

    # Core status
    working_status: WorkingStatus = WorkingStatus.UNKNOWN
    battery_level: int = 0  # real-time SOC from field 2 (float32)
    battery_health: int = 0  # static design capacity from field 38 (always 100)
    firmware_version: str = ""
    firmware_target: str = ""
    firmware_update_info: dict[str, Any] = field(default_factory=dict)
    firmware_update_supported: bool = False

    # Device identity
    device_info: DeviceInfo | None = None

    # Cloud account metadata.
    consumables: dict[str, ConsumableInfo] = field(default_factory=dict)
    notifications: list[NarwalNotification] = field(default_factory=list)
    latest_task_record: dict[str, Any] | None = None
    task_records: list[dict[str, Any]] = field(default_factory=list)
    task_record_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_records_supported: bool = False
    task_records_map_id: int | None = None
    dust_bag_health: list[dict[str, Any]] = field(default_factory=list)
    dust_bag_health_current: dict[str, Any] = field(default_factory=dict)
    dust_bag_health_supported: bool = False

    # Optional MQTT features. A corresponding ``*_supported`` flag becomes
    # true only after that robot successfully answers the protocol topic.
    battery_details: dict[str, Any] = field(default_factory=dict)
    battery_details_supported: bool = False
    dry_mop_remaining_time: int | None = None
    dry_mop_remaining_time_supported: bool = False
    consumable_maintain_items: set[int] = field(default_factory=set)
    consumable_replace_items: set[int] = field(default_factory=set)
    mqtt_consumables_supported: bool = False
    clean_plans: list[dict[str, Any]] = field(default_factory=list)
    clean_plans_supported: bool = False
    saved_maps: list[dict[str, Any]] = field(default_factory=list)
    saved_maps_supported: bool = False
    current_map_id: int | None = None
    clean_timeline: list[dict[str, Any]] = field(default_factory=list)
    clean_timeline_supported: bool = False
    clean_progress: dict[str, Any] = field(default_factory=dict)
    clean_progress_supported: bool = False
    current_voice: dict[str, str] = field(default_factory=dict)
    voice_packs: list[dict[str, Any]] = field(default_factory=list)
    voice_packs_supported: bool = False

    # Session
    session_id: str = ""
    timestamp: int = 0

    # Position (from map data)
    position: Position | None = None

    # Cleaning stats
    cleaning_area: float = 0.0  # m²
    cleaning_time: int = 0  # seconds

    # Map
    map_data: MapData | None = None
    map_display_data: MapDisplayData | None = None

    # Vision obstacles (camera-detected transient objects during cleaning)
    # Download/upgrade status
    download_status: int = 0
    upgrade_status_code: int = 0

    # Pause overlay.
    is_paused: bool = False

    # RobotTaskStatus sub-state for the current task. The field number depends
    # on TaskType (for example, SMART_CLEAN uses field 8).
    task_sub_status: int = 0

    # IdleStatus from RobotTaskStatus field 3:
    # 1/6=in station, 2/7=outside station.
    idle_status: int = 0

    # Deprecated legacy fields retained for state compatibility. The schema
    # identifies sub-fields 3/10/12 as Idle/Recall/WashAndDry task statuses,
    # not independent dock signals, so they are no longer populated.
    dock_sub_state: int = 0

    # Returning flag (field 3 sub-field 7: 1=returning to dock)
    # Confirmed via live test: appears when robot is navigating back to dock
    is_returning_to_dock: bool = False

    dock_activity: int = 0

    dock_presence: int = 0

    # StationContactType from top-level field 11. Live captures show 1 when
    # undocked; only known contact values 2 and 3 are docking evidence.
    # Validated via dock_research.py guided test (5 captures):
    #   2 = on dock (all 3 on-dock captures)
    #   1 = off dock (both off-dock captures)
    station_contact_type: int = 0

    # ChargingStatus enum from top-level field 47. It describes charging state
    # and must not be treated as independent proof that the robot is docked.
    charging_status: int = 0

    # RobotBaseStatus field 1: repeated ErrorCode, sub-field 1 = identityCode.
    # Full base-status snapshots replace rather than accumulate active errors.
    error_codes: tuple[int, ...] = ()

    # Raw data for fields we haven't fully decoded yet
    raw_base_status: dict[str, Any] = field(default_factory=dict)
    raw_working_status: dict[str, Any] = field(default_factory=dict)

    @property
    def has_cleaning_task(self) -> bool:
        """Return whether the active protocol state represents cleaning."""
        return self.working_status.is_cleaning_task

    @property
    def is_cleaning(self) -> bool:
        """True when actively cleaning (not paused, not returning to dock)."""
        return self.has_cleaning_task and not self.is_paused and not self.is_returning

    @property
    def is_docked(self) -> bool:
        """Return whether the task state or dock signals indicate docking.

        Field values vary by firmware, so task types take priority and the
        remaining dock fields are used only for idle or unmapped states.
        """
        if self.working_status.is_station_task:
            return True
        if self.working_status.is_cleaning_task or self.working_status.is_returning_task:
            return False
        if self.working_status == WorkingStatus.IDLE:
            if self.idle_status in (1, 6):
                return True
            if self.idle_status in (2, 3, 4, 5, 7):
                return False
        # For idle, unknown, or unmapped states, check top-level dock signals.
        return self.station_contact_type in {2, 3}

    @property
    def is_returning(self) -> bool:
        """True when the robot is actively returning to the dock.

        Dedicated RECALL/PARKING/RELOCALIZATION task types are authoritative.
        Cleaning task sub-status enums also expose recall and parking phases.
        """
        if self.working_status.is_returning_task:
            return True
        returning_sub_statuses = {
            WorkingStatus.SWEEP: {1, 6, 10, 11},
            WorkingStatus.MOP: {1, 2, 3, 10, 14, 15},
            WorkingStatus.SWEEP_AND_MOP: {1, 2, 3, 10, 14, 15},
            WorkingStatus.SWEEP_THEN_MOP: {1, 2, 3, 12, 16, 17},
            WorkingStatus.SMART_CLEAN: {1, 2, 3, 14, 18, 19},
        }
        return self.task_sub_status in returning_sub_statuses.get(self.working_status, set())

    def update_from_working_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded working_status message.

        WorkingStatus protobuf fields:
          Field 2 = coveredArea (double, already expressed in m²)
          Field 3 = timeConsuming (seconds)

        Because the message is decoded without a schema, blackboxprotobuf
        exposes field 2 as the raw fixed64 bit pattern rather than a float.
        """
        self.raw_working_status = decoded
        time_consuming = decoded.get("3")
        if (
            time_consuming is not None
            and not isinstance(time_consuming, bool)
            and not (
                isinstance(time_consuming, float)
                and (not math.isfinite(time_consuming) or time_consuming < 0)
            )
        ):
            with contextlib.suppress(ValueError, TypeError, OverflowError):
                seconds = int(time_consuming)
                if seconds >= 0:
                    self.cleaning_time = seconds
        if "2" in decoded:
            area = _to_float64(decoded["2"])
            if area is not None and area >= 0:
                self.cleaning_area = area

    def update_from_base_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded robot_base_status message.

        Battery (confirmed via 35-min monitor capture):
          Field 2  = real-time battery as IEEE 754 float32
                     (1118175232 → 83.0%, matching app ~84%)
          Field 38 = static battery health (always 100; design capacity)

        Field 3 is RobotTaskStatus. Its sub-field 1 is TaskType, sub-field 2
        is pauseStatus, and task-specific sub-states start at sub-field 3.

        Field 46 is RobotTaskStatusV2. Any TaskStatus.isPaused value in its
        statusList overrides the legacy field 3.2 pause flag.

        Station/charging fields:
          Field 11 = StationContactType (validated as a dock contact signal)
          Field 47 = ChargingStatus (not an independent dock signal)

        Note: field 32 mirrors field 3 exactly (redundant).
        """
        self.raw_base_status = decoded
        errors = decoded.get("1", [])
        if isinstance(errors, dict):
            errors = [errors]
        self.error_codes = (
            tuple(
                code
                for item in errors
                if isinstance(item, dict)
                if type(code := item.get("1")) is int and 0 < code <= 0xFFFFFFFF
            )
            if isinstance(errors, list)
            else ()
        )
        # This method consumes a full RobotBaseStatus message. Reset optional
        # protobuf fields first so omitted/default values cannot leave stale
        # task, pause, station-contact, or charging overlays behind.
        self.working_status = WorkingStatus.UNKNOWN
        self.is_paused = False
        self.task_sub_status = 0
        self.idle_status = 0
        self.is_returning_to_dock = False
        self.dock_sub_state = 0
        self.dock_activity = 0
        self.dock_presence = 0
        self.station_contact_type = 0
        self.charging_status = 0

        # Field 11 = StationContactType (2/3=docked, 1=undocked).
        if "11" in decoded:
            self.station_contact_type = _int_or_default(decoded["11"])
        # Field 47 = ChargingStatus enum; retained for diagnostics only.
        if "47" in decoded:
            self.charging_status = _int_or_default(decoded["47"])
        # Field 3 is a RobotTaskStatus nested message: {1: task_type, ...}
        # Sub-field layout differs across firmware versions. Fields 3 onward
        # are task-specific status enums, not independent dock indicators.
        # bbp may also return a list for repeated messages.
        field3 = decoded.get("3")
        if isinstance(field3, list):
            field3 = field3[0] if field3 else None
        if isinstance(field3, dict):
            if "1" in field3:
                try:
                    raw_status = int(field3["1"])
                    if not 0 <= raw_status <= 28:
                        raise ValueError
                    self.working_status = WorkingStatus(raw_status)
                except ValueError, TypeError:
                    raw_val = field3["1"]
                    _LOGGER.warning(
                        "Unknown RobotTaskStatus TaskType value: %s; treating it as UNSPECIFIED",
                        raw_val,
                    )
                    self.working_status = WorkingStatus.UNKNOWN
            # RobotTaskStatus field 2 is the pause overlay. Task-specific
            # status fields start at field 3 (IdleStatus) and follow the
            # protobuf declaration order.
            self.is_paused = bool(_int_or_default(field3.get("2")))
            task_field = {
                WorkingStatus.IDLE: 3,
                WorkingStatus.SWEEP: 4,
                WorkingStatus.MOP: 5,
                WorkingStatus.SWEEP_AND_MOP: 6,
                WorkingStatus.SWEEP_THEN_MOP: 7,
                WorkingStatus.SMART_CLEAN: 8,
                WorkingStatus.BUILD_MAP: 9,
                WorkingStatus.RECALL: 10,
                WorkingStatus.WASH_AND_DRY_MOP: 12,
                WorkingStatus.UPDATE_MAP: 15,
                WorkingStatus.WAITING_COMMAND: 16,
                WorkingStatus.COMPOSITE_CLEAN: 17,
                WorkingStatus.DUST_GATHERING: 18,
                WorkingStatus.TELECONTROL: 19,
                WorkingStatus.MAINTAIN_FLOOR: 20,
                WorkingStatus.SEARCH_PERSON: 21,
                WorkingStatus.VIDEO_CRUISE: 22,
                WorkingStatus.SEARCH_PET: 23,
            }.get(self.working_status)
            try:
                self.task_sub_status = (
                    int(field3.get(str(task_field), 0)) if task_field is not None else 0
                )
            except ValueError, TypeError:
                self.task_sub_status = 0
            if self.working_status == WorkingStatus.IDLE:
                self.idle_status = self.task_sub_status
            self.is_returning_to_dock = self.is_returning
            # Log unrecognized sub-fields for future firmware mapping
            _known_f3 = {str(field_number) for field_number in range(1, 24)}
            _unknown_f3 = set(field3.keys()) - _known_f3
            if _unknown_f3:
                _LOGGER.debug(
                    "field3 contains %d unrecognized sub-fields",
                    len(_unknown_f3),
                )
        elif field3 is not None:
            _LOGGER.warning(
                "field3 is %s (expected dict); state may not update",
                type(field3).__name__,
            )

        field46 = decoded.get("46")
        if isinstance(field46, list):
            field46 = field46[0] if field46 else None
        if isinstance(field46, dict):
            status_list = field46.get("1", [])
            if isinstance(status_list, dict):
                status_list = [status_list]
            elif not isinstance(status_list, list):
                status_list = []
            pause_values = [
                bool(_int_or_default(status["3"]))
                for status in status_list
                if isinstance(status, dict) and "3" in status
            ]
            if pause_values:
                self.is_paused = any(pause_values)
        if "2" in decoded:
            # Field 2 = real-time battery SOC as float32
            # (e.g. 1118175232 → 83.0%; bbp may return int or float)
            bat = _to_float32(decoded["2"])
            if bat is not None and math.isfinite(bat) and 0 <= bat <= 100:
                self.battery_level = round(bat)
        if "38" in decoded:
            # Field 38 = static battery health (always 100, design capacity)
            self.battery_health = _int_or_default(decoded["38"], self.battery_health)
        if "36" in decoded:
            self.timestamp = _int_or_default(decoded["36"], self.timestamp)
        if "13" in decoded:
            raw = decoded["13"]
            if isinstance(raw, bytes):
                self.session_id = raw.decode("utf-8", errors="replace")
            else:
                self.session_id = str(raw)
                if self.session_id.startswith("b'"):
                    self.session_id = self.session_id[2:-1]

    def update_battery_from_base_status(self, decoded: dict[str, Any]) -> None:
        """Update ONLY hardware-sampled fields from a base_status response.

        Used when the robot is not broadcasting (deep sleep on dock).
        In this mode, get_status() returns current battery (hardware counter)
        but stale working_status (firmware cache from last active session).
        We update only the fields we can trust.
        """
        self.raw_base_status = decoded
        if "2" in decoded:
            bat = _to_float32(decoded["2"])
            if bat is not None and math.isfinite(bat) and 0 <= bat <= 100:
                self.battery_level = round(bat)
        if "38" in decoded:
            self.battery_health = _int_or_default(decoded["38"], self.battery_health)
        if "36" in decoded:
            self.timestamp = _int_or_default(decoded["36"], self.timestamp)

    def update_from_upgrade_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded upgrade_status message."""
        if "7" in decoded:
            raw = decoded["7"]
            if isinstance(raw, bytes):
                self.firmware_version = raw.decode("utf-8", errors="replace")
            else:
                self.firmware_version = str(raw)
                if self.firmware_version.startswith("b'"):
                    self.firmware_version = self.firmware_version[2:-1]
        if "8" in decoded:
            raw = decoded["8"]
            if isinstance(raw, bytes):
                self.firmware_target = raw.decode("utf-8", errors="replace")
            else:
                self.firmware_target = str(raw)
                if self.firmware_target.startswith("b'"):
                    self.firmware_target = self.firmware_target[2:-1]
        if "4" in decoded:
            self.upgrade_status_code = _int_or_default(decoded["4"], self.upgrade_status_code)

    def update_from_download_status(self, decoded: dict[str, Any]) -> None:
        """Update state from a decoded download_status message."""
        if "1" in decoded:
            self.download_status = _int_or_default(decoded["1"], self.download_status)
