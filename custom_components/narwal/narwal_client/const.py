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
TOPIC_CMD_GET_CURRENT_VOICE_INFO = "config/language/get_current_voice_info"
TOPIC_CMD_SET_CUSTOM_VOICE = "config/language/set_custom_voice"
TOPIC_CMD_GET_BATTERY_INFO = "info/battery_info"
TOPIC_CMD_GET_CLEAN_PROGRESS = "info/get_clean_progress_info"
TOPIC_CMD_GET_CLEAN_TIMELINE = "info/get_clean_time_line"

# Task control
TOPIC_CMD_PAUSE = "task/pause"
TOPIC_CMD_RESUME = "task/resume"  # App's error-dialog Continue also uses this topic.
# J4 robot_exception catalog: 0x02100018 / app code 1023 = cannot find base.
BASE_STATION_NOT_FOUND_ERROR = 0x02100018
# Distinct J4 error 0x02100017 means return-to-base failed; do not conflate them.
TOPIC_CMD_FORCE_END = "task/force_end"
TOPIC_CMD_CANCEL = "task/cancel"

# Supply/dock
TOPIC_CMD_RECALL = "supply/recall"
TOPIC_CMD_WASH_MOP = "supply/wash_mop"
TOPIC_CMD_DRY_MOP = "supply/dry_mop"
TOPIC_CMD_WASH_AND_DRY_MOP = "supply/wash_and_dry_mop"
TOPIC_CMD_DUST_GATHERING = "supply/dust_gathering"
TOPIC_CMD_GET_DRY_MOP_REMAIN_TIME = "supply/get_dry_mop_remain_time"

# APK StartClean.Request field 1 carries a CleanTask; StartWithPlan.Request
# on clean/plan/start instead expects a varint planId (and optional mapId).
TOPIC_CMD_START_CLEAN = "clean/start_clean"
TOPIC_CMD_START_CLEAN_PLAN = "clean/plan/start"
TOPIC_CMD_EASY_CLEAN = "clean/easy_clean/start"
# The App's home start branches on DeviceModel: ax3 converts the CleanTask
# into StartEasyClean.Request, every other model uses StartClean.Request on
# clean/start_clean.  Product keys come from the APK's DeviceModel map.
EASY_CLEAN_PRODUCT_KEYS: frozenset[str] = frozenset({"cUlfJN5JYP"})  # ax3 / J3
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

# App NRConsumableType.off_14 -> ConsumableInfoPayload enum value. Only
# unambiguous name matches in the unpacked app are eligible for MQTT reset.
# Silver ions (110/111) are reset through the cloud v1 endpoint instead.
CONSUMABLE_CLOUD_RESET_TYPES = frozenset({110, 111})
CONSUMABLE_MAINTAIN_TYPE_MAP: dict[int, int] = {
    101: 1,  # dustBox
    1: 2,  # dustFilter
    104: 4,  # washRibs
    106: 6,  # universalWheel
    107: 7,  # cliffSensor
    108: 8,  # sideDistanceSensor
}
CONSUMABLE_REPLACE_TYPE_MAP: dict[int, int] = {
    1: 1,  # dustFilter
    2: 2,  # mop
    3: 3,  # sideBrush
    4: 4,  # cleanTankFilter
    5: 5,  # rollerBrush
    6: 6,  # detergent
    109: 8,  # dustBag
    20: 20,  # stationDustBag
    22: 22,  # curingAgent
    23: 23,  # heavyDetergent
    24: 24,  # innerDustBox
}


class CommandResult(IntEnum):
    """Response code from command field 1."""

    SUCCESS = 1
    GENERAL_FAILED = 2
    # Compatibility alias for callers of versions that incorrectly named the
    # protocol's general-failure result. Internal code must use the real name.
    NOT_APPLICABLE = GENERAL_FAILED
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
    """Suction fan speed levels from the SweepMode protocol enum."""

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


class CleanMode(IntEnum):
    """Cleaning mode from the App ``CleanMode`` protobuf enum.

    Values come from ``app_protol/proto/core/CleanTask.pbenum.dart`` and are
    shared by ``CleanParam.mode`` and ``CleanPlan.cleanMode``.  The App shows
    ``SMART`` as "鲸灵托管" (Freo Mind) and ``SWEEP_MOP_SYNC`` as "扫拖同时".
    """

    UNSPECIFIED = 0
    SMART = 1
    SWEEP = 2
    MOP = 3
    SWEEP_MOP_SYNC = 4
    SWEEP_THEN_MOP = 5
    COMPOSITE_CLEAN = 6


class CleanTaskType(IntEnum):
    """Task type from the App ``CleanTask.taskType`` protobuf enum.

    Values come from ``app_protol/proto/core/CleanTask.pbenum.dart``.  A
    whole-map task started by the App itself uses the mode-specific
    ``GENERAL_*`` type, and ``SMART`` mode leaves the type unspecified.
    """

    UNSPECIFIED = 0
    GENERAL_SWEEP = 1
    GENERAL_MOP = 2
    GENERAL_SWEEP_THEN_MOP = 3
    GENERAL_SWEEP_MOP_SYNC = 4
    CUSTOM_CLEAN = 5
    SHORTCUT = 6
    SCHEDULED = 7
    NAVO_CLEAN = 8


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

    ``coveredArea`` is a protobuf double and is already in m².
    """

    AREA = 2
    ELAPSED_TIME = 3
    DRYING_BAG_TIME = 10
    TOTAL_DRYING_BAG_TIME = 11
    DRY_STATION_BAG_TIME = 12
    TOTAL_DRY_STATION_BAG_TIME = 13
    STATION_BAG_STERILIZATION_TIME = 14
    TOTAL_STATION_BAG_STERILIZATION_TIME = 15
