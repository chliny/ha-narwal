"""Protocol constants, enums, and field mappings for Narwal vacuum."""

from enum import IntEnum

# --- Cloud command topics ---
# Common
TOPIC_CMD_YELL = "common/yell"
TOPIC_CMD_GET_DEVICE_INFO = "common/get_device_info"
TOPIC_CMD_GET_FEATURE_LIST = "common/get_feature_list"
TOPIC_CMD_GET_BASE_STATUS = "status/get_device_base_status"
TOPIC_CMD_GET_CONFIG = "config/get"
TOPIC_CMD_SET_CONFIG = "config/set"
TOPIC_CMD_GET_BATTERY_INFO = "info/battery_info"
TOPIC_CMD_GET_CLEAN_PROGRESS = "info/get_clean_progress_info"
TOPIC_CMD_GET_CLEAN_TIMELINE = "info/get_clean_time_line"

# Task control
TOPIC_CMD_PAUSE = "task/pause"
TOPIC_CMD_RESUME = "task/resume"
TOPIC_CMD_FORCE_END = "task/force_end"
TOPIC_CMD_CANCEL = "task/cancel"

# Supply/dock
TOPIC_CMD_RECALL = "supply/recall"
TOPIC_CMD_WASH_MOP = "supply/wash_mop"
TOPIC_CMD_DRY_MOP = "supply/dry_mop"
TOPIC_CMD_WASH_AND_DRY_MOP = "supply/wash_and_dry_mop"
TOPIC_CMD_DUST_GATHERING = "supply/dust_gathering"
TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME = "supply/get_dry_mop_remain_time"

# Cleaning (Pita protocol — correct for AX12)
TOPIC_CMD_START_CLEAN = "clean/plan/start"  # whole-house clean (empty payload)
TOPIC_CMD_EASY_CLEAN = "clean/easy_clean/start"
TOPIC_CMD_SET_FAN_LEVEL = "clean/set_fan_level"
TOPIC_CMD_SET_MOP_HUMIDITY = "clean/set_mop_humidity"
TOPIC_CMD_GET_CURRENT_TASK = "clean/current_clean_task/get"
TOPIC_CMD_GET_CLEAN_PLANS = "clean/plan/get"

# Map
TOPIC_CMD_GET_MAP = "map/get_map"
TOPIC_CMD_GET_ALL_MAPS = "map/get_all_reduced_maps"
TOPIC_CMD_SWITCH_MAP = "map/switch_map"

# Consumables
TOPIC_CMD_GET_CONSUMABLE_INFO = "consumable/get_consumable_info"
TOPIC_CMD_RESET_CONSUMABLE_INFO = "consumable/reset_consumable_info"

# Camera (developer commands)
TOPIC_CMD_TAKE_PICTURE = "developer/take_picture"
TOPIC_CMD_SET_LED = "developer/led_control"

TOPIC_CMD_ACTIVE_ROBOT = "common/active_robot_publish"

# Command response timeout
COMMAND_RESPONSE_TIMEOUT = 5.0  # seconds

# Cloud consumable type -> robot ConsumableInfoPayload enum.
CONSUMABLE_MAINTAIN_TYPE_MAP: dict[int, int] = {
    13: 1,
    1: 2,
    14: 4,
    15: 6,
    16: 7,
    17: 8,
    23: 9,
    22: 10,
}
CONSUMABLE_REPLACE_TYPE_MAP: dict[int, int] = {
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
    18: 8,
    9: 20,
    20: 21,
    21: 21,
    10: 22,
    11: 23,
    12: 24,
}


class CommandResult(IntEnum):
    """Response code from command field 1."""

    SUCCESS = 1
    NOT_APPLICABLE = 2  # e.g., set_fan_level when not cleaning
    CONFLICT = 3  # e.g., recall when already recalling


class WorkingStatus(IntEnum):
    """Current task type from RobotTaskStatus field 1.

    These values come from the app's ``RobotTaskStatus.TaskType`` protobuf.
    Robot errors, pause state, and dock presence are separate fields and must
    not be inferred by assigning different meanings to these task values.
    """

    UNSPECIFIED = 0
    IDLE = 1
    SWEEP = 2
    MOP = 3
    SWEEP_THEN_MOP = 4
    SWEEP_AND_MOP = 5
    SMART_CLEAN = 6
    BUILD_MAP = 7
    PARKING = 8
    DEPARTURE = 9
    RECALL = 10
    NET_CONFIG = 11
    UPGRADE = 12
    RELOCALIZATION = 13
    WASH_AND_DRY_MOP = 14
    PAIRING = 15
    UPDATE_MAP = 16
    COMPOSITE_CLEAN = 17
    DEBUG = 18
    DUST_GATHERING = 19
    DRY_STATION_BAG = 20
    TELECONTROL = 21
    MAINTAIN_FLOOR = 22
    SEARCH_PERSON = 23
    VIDEO_CRUISE = 24
    PUMP_TROUBLESHOOTING = 25
    ROBOT_SEWAGE_BOX_PUMP_TROUBLESHOOTING = 26
    SEARCH_PET = 27
    WAITING_COMMAND = 28

    @property
    def is_cleaning_task(self) -> bool:
        """Return whether the app classifies this as a cleaning task."""
        return self in {
            self.SWEEP,
            self.MOP,
            self.SWEEP_THEN_MOP,
            self.SWEEP_AND_MOP,
            self.SMART_CLEAN,
            self.COMPOSITE_CLEAN,
            self.TELECONTROL,
            self.MAINTAIN_FLOOR,
        }

    @property
    def is_returning_task(self) -> bool:
        """Return whether this task moves the robot back to the station."""
        return self in {self.PARKING, self.RECALL, self.RELOCALIZATION}

    @property
    def is_station_task(self) -> bool:
        """Return whether this task can only run at the station."""
        return self in {
            self.WASH_AND_DRY_MOP,
            self.DUST_GATHERING,
            self.DRY_STATION_BAG,
        }

    # Keep UNKNOWN as the public fallback spelling used throughout the client.
    UNKNOWN = UNSPECIFIED
    ERROR = 99


class FanLevel(IntEnum):
    """Suction fan speed levels (SweepMode from APK)."""

    QUIET = 0
    NORMAL = 1
    STRONG = 2
    MAX = 3


class MopHumidity(IntEnum):
    """Mop wetness levels."""

    DRY = 0
    NORMAL = 1
    WET = 2


class DryMopStrength(IntEnum):
    """Mop drying levels from the Android app protobuf."""

    QUIET = 1
    STRONG = 2
    SMART = 3


class CustomCleanStrategy(IntEnum):
    """Custom cleaning strategies from the Android app protobuf."""

    AREA = 1
    ROOM = 2
    SMART = 3


# robot_base_status field numbers
class BaseStatusField(IntEnum):
    """Field numbers in the robot_base_status protobuf message.

    Battery notes (confirmed via 35-min monitor capture, 2026-02-27):
      Field 2  = real-time battery level as IEEE 754 float32
                 (e.g. 1118175232 → 83.0%, matching app display ~84%)
      Field 38 = static battery health (always 100; design capacity, not SOC)
    """

    BATTERY_LEVEL = 2  # real-time SOC as float32 — CONFIRMED
    MODE_STATE = 3
    SESSION_ID = 13
    SENSOR_DATA = 25
    TIMESTAMP = 36
    BATTERY_HEALTH = 38  # static, always 100 (design capacity)
    BATTERY_CAPACITY = 41


# upgrade_status field numbers
class UpgradeStatusField(IntEnum):
    """Field numbers in the upgrade_status protobuf message."""

    STATUS_CODE = 4
    CURRENT_FIRMWARE = 7
    TARGET_FIRMWARE = 8


# working_status field numbers
class WorkingStatusField(IntEnum):
    """Field numbers in the working_status protobuf message.

    Confirmed via live test (2026-02-27):
      3  = current session elapsed seconds (confirmed: 2136→2159 over 35-min clean)
      13 = cleaning area in cm² (confirmed: 18000 = 1.8m²)
      15 = 600 during cleaning (possibly cumulative or constant)
      6  = 1 during cleaning (observed in plan-based clean; may vary by mode)
      10 = time since docked in seconds (post-dock only, counts up)
      11 = 2700 post-dock (unknown, constant)

    Also broadcast during cleaning:
      status/time_line_status — timeline/history data
      developer/planning_debug_info — navigation debug (collision count, stall count)
    """

    ELAPSED_TIME = 3  # current session elapsed seconds — CONFIRMED
    AREA = 13  # cm² — CONFIRMED (18000 = 1.8m²)
    CUMULATIVE_TIME = 15  # 600 during cleaning (purpose uncertain)
    TIME_SINCE_DOCKED = 10  # seconds since docked (post-dock only)
