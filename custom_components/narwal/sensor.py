"""Sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import ConsumableInfo, NarwalNotification, NarwalState
from .narwal_client.const import (
    CONSUMABLE_MAINTAIN_TYPE_MAP,
    CONSUMABLE_REPLACE_TYPE_MAP,
)


@dataclass(frozen=True, kw_only=True)
class NarwalSensorEntityDescription(SensorEntityDescription):
    """Describes a Narwal sensor entity."""

    value_fn: Callable[[NarwalState], float | str | None]


SENSOR_DESCRIPTIONS: tuple[NarwalSensorEntityDescription, ...] = (
    NarwalSensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # battery_level comes from field 2 (real-time SOC as float32)
        value_fn=lambda state: state.battery_level if state.battery_level > 0 else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_area",
        translation_key="cleaning_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 2 is coveredArea, already expressed in m².
        value_fn=lambda state: (
            round(state.cleaning_area, 2)
            if state.cleaning_area > 0 and isfinite(state.cleaning_area)
            else None
        ),
    ),
    NarwalSensorEntityDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 3 is session elapsed seconds.
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: state.cleaning_time if state.cleaning_time > 0 else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal sensor entities."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        NarwalSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(NarwalChargingStateSensor(coordinator))
    entities.append(NarwalLatestNotificationSensor(coordinator))
    entities.append(NarwalCleanProgressSensor(coordinator))
    known_consumables: set[str] = set()
    task_record_added = False
    optional_added: set[str] = set()

    def _add_dynamic_entities() -> None:
        nonlocal task_record_added
        state = coordinator.data
        if state is None:
            return
        if state.task_records_supported and not task_record_added:
            task_record_added = True
            async_add_entities([NarwalTaskRecordSensor(coordinator)])
        optional: list[tuple[str, SensorEntity]] = []
        if state.battery_details_supported:
            optional.append(("battery_health_details", NarwalBatteryHealthSensor(coordinator)))
        if state.dry_mop_remaining_time_supported:
            optional.append(("dry_mop_remaining_time", NarwalDryMopRemainingSensor(coordinator)))
        if state.dust_bag_health_supported:
            optional.append(("dust_bag_health", NarwalDustBagHealthSensor(coordinator)))
        if state.clean_timeline_supported:
            optional.append(("clean_timeline", NarwalCleanTimelineSensor(coordinator)))
        new_optional = [entity for key, entity in optional if key not in optional_added]
        optional_added.update(key for key, _entity in optional)
        if new_optional:
            async_add_entities(new_optional)
        new_keys = set(state.consumables) - known_consumables
        if not new_keys:
            return
        known_consumables.update(new_keys)
        async_add_entities(
            [
                NarwalConsumableSensor(
                    coordinator,
                    state.consumables[key],
                )
                for key in sorted(new_keys)
            ]
        )

    async_add_entities(entities)
    _add_dynamic_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_dynamic_entities))


class NarwalSensor(NarwalEntity, SensorEntity):
    """A Narwal sensor entity."""

    entity_description: NarwalSensorEntityDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._set_entity_identity("sensor", description.key)

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        state = self.coordinator.data
        if state is None:
            return None
        return self.entity_description.value_fn(state)


class NarwalChargingStateSensor(NarwalEntity, SensorEntity):
    """Sensor showing charging state: Charging, Fully Charged, or unavailable."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "charging_state"
    _attr_options = ["charging", "fully_charged", "not_charging"]

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        """Initialize the charging state sensor."""
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "charging_state")

    @property
    def native_value(self) -> str | None:
        """Return charging state.

        Returns None (unavailable) when not docked.
        """
        state = self.coordinator.data
        if state is None:
            return None
        if not state.is_docked:
            return "not_charging"
        if state.battery_level >= 100:
            return "fully_charged"
        return "charging"

    @property
    def icon(self) -> str:
        """Return icon based on charging state."""
        if self.native_value == "fully_charged":
            return "mdi:battery"
        if self.native_value == "charging":
            return "mdi:battery-charging"
        if self.native_value == "not_charging":
            return "mdi:battery-off-outline"
        return "mdi:battery-unknown"


class NarwalLatestNotificationSensor(NarwalEntity, SensorEntity):
    """Show the most recent cloud notification as persistent entity state."""

    _attr_translation_key = "latest_notification"
    _attr_icon = "mdi:message-alert-outline"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "latest_notification")

    @property
    def native_value(self) -> str | None:
        """Return a recorder-safe summary of the latest notification."""
        notification = self._latest_notification
        if notification is None:
            return None
        title = notification.title.strip()
        content = notification.content.strip()
        summary = title
        if content and content != title:
            summary = f"{title} — {content}" if title else content
        if not summary:
            return notification.message_id
        if len(summary) <= MAX_LENGTH_STATE_STATE:
            return summary
        return f"{summary[: MAX_LENGTH_STATE_STATE - 3]}..."

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the complete latest notification for dashboards and automations."""
        notification = self._latest_notification
        if notification is None:
            return {}
        return {
            "message_id": notification.message_id,
            "title": notification.title,
            "content": notification.content,
            "category": notification.category,
            "created_at": notification.created_at,
            "is_read": notification.is_read,
        }

    @property
    def _latest_notification(self) -> NarwalNotification | None:
        """Return the newest message while preserving cloud order for timestamp ties."""
        state = self.coordinator.data
        if state is None or not state.notifications:
            return None
        return max(state.notifications, key=lambda notification: notification.created_at)

    @property
    def available(self) -> bool:
        """Cloud messages remain available while the robot sleeps."""
        return self.coordinator.last_update_success


class NarwalTaskRecordSensor(NarwalEntity, SensorEntity):
    """Expose the latest cleaning task as a readable summary."""

    _attr_translation_key = "task_record"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "task_record")

    @property
    def native_value(self) -> str | None:
        """Return the latest task name instead of its time or opaque ID."""
        state = self.coordinator.data
        record = state.latest_task_record if state else None
        if not record:
            return None
        name = _task_record_name(record, _task_record_detail(state, record))
        return name[:MAX_LENGTH_STATE_STATE]

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return a concise, human-readable summary of the latest task."""
        state = self.coordinator.data
        if not state:
            return {}
        attributes: dict[str, object] = {"record_count": len(state.task_records)}
        record = state.latest_task_record
        if not record:
            return attributes
        if (started_at := _task_record_datetime(record, "start_time")) is not None:
            attributes["started_at"] = _local_task_time(started_at)
        if (ended_at := _task_record_datetime(record, "end_time")) is not None:
            attributes["ended_at"] = _local_task_time(ended_at)
        duration = _task_record_number(record, "duration")
        if duration is not None and duration >= 0:
            minutes = round(duration / 60, 1)
            attributes["duration_minutes"] = int(minutes) if minutes.is_integer() else minutes
        detail = _task_record_detail(state, record)
        attributes["task_name"] = _task_record_name(record, detail)
        return attributes


def _task_record_datetime(record: dict[str, object], key: str) -> datetime | None:
    """Convert the app's confirmed Unix-seconds task fields to UTC."""
    timestamp = _task_record_number(record, key)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _local_task_time(value: datetime) -> str:
    """Format a task timestamp in Home Assistant's configured local timezone."""
    return dt_util.as_local(value).isoformat(sep=" ", timespec="seconds")


def _task_record_id(record: dict[str, object]) -> str | None:
    """Return the identifier used by the bounded detail cache."""
    value = (
        record.get("clean_record_id")
        or record.get("record_id")
        or record.get("report_id")
        or record.get("id")
    )
    return str(value) if value is not None else None


def _task_record_detail(
    state: NarwalState,
    record: dict[str, object],
) -> dict[str, object] | None:
    """Return a fetched detail without exposing its raw structure."""
    record_id = _task_record_id(record)
    return state.task_record_details.get(record_id) if record_id is not None else None


def _task_record_name(
    record: dict[str, object],
    detail: dict[str, object] | None,
) -> str:
    """Prefer the confirmed custom name and otherwise use a translatable fallback."""
    for source in (record, detail):
        if source is None:
            continue
        value = source.get("custom_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "cleaning_task"


def _task_record_number(record: dict[str, object], key: str) -> float | None:
    """Return a finite numeric task field without accepting booleans."""
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


class NarwalBatteryHealthSensor(NarwalEntity, SensorEntity):
    """Battery health state with all reported detail fields."""

    _attr_translation_key = "battery_health_details"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "battery_health_details")

    @property
    def native_value(self) -> int | str | None:
        state = self.coordinator.data
        return state.battery_details.get("health_state") if state else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.coordinator.data
        return dict(state.battery_details) if state else {}


class NarwalDryMopRemainingSensor(NarwalEntity, SensorEntity):
    """Raw remaining drying time returned by the station."""

    _attr_translation_key = "dry_mop_remaining_time"
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "dry_mop_remaining_time")

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data
        return state.dry_mop_remaining_time if state else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # The remainTime protocol field does not declare a unit.
        return {"protocol_unit": "not declared by protocol"}


class NarwalDustBagHealthSensor(NarwalEntity, SensorEntity):
    """Latest dust-bag health value with its app history."""

    _attr_translation_key = "dust_bag_health"
    _attr_icon = "mdi:delete-variant"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "dust_bag_health")

    @property
    def native_value(self) -> int | float | str | None:
        state = self.coordinator.data
        latest = state.dust_bag_health[-1] if state and state.dust_bag_health else None
        if not latest:
            return None
        for key in ("end_health_Degree", "end_health_degree", "health_degree"):
            if latest.get(key) is not None:
                return latest[key]
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.coordinator.data
        return {"history": state.dust_bag_health[-31:]} if state else {}


class NarwalCleanTimelineSensor(NarwalEntity, SensorEntity):
    """Current real-time cleaning timeline."""

    _attr_translation_key = "clean_timeline"
    _attr_icon = "mdi:timeline-clock"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "clean_timeline")

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data
        return len(state.clean_timeline) if state else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.coordinator.data
        return {"events": state.clean_timeline[-20:]} if state else {}


class NarwalCleanProgressSensor(NarwalEntity, SensorEntity):
    """Keep the latest real-time progress snapshot while the robot is idle."""

    _attr_translation_key = "clean_progress"
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "clean_progress")
        self._progress_state: str | None = None
        self._progress_attributes: dict[str, object] = {}
        self._capture_active_progress()

    @property
    def native_value(self) -> str | None:
        """Return the last status captured during an active cleaning task."""
        return self._progress_state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the last counters and detail captured during cleaning."""
        return self._progress_attributes.copy()

    def _capture_active_progress(self) -> bool:
        """Capture live values only while a cleaning task is active."""
        state = self.coordinator.data
        if state is None or not state.has_cleaning_task:
            return False
        self._progress_state = state.working_status.name.lower()
        self._progress_attributes = {
            "cleaning_area_m2": state.cleaning_area,
            "cleaning_time_seconds": state.cleaning_time,
            "progress": state.clean_progress.copy(),
        }
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state only for active cleaning updates."""
        if self._capture_active_progress():
            self.async_write_ha_state()


_CONSUMABLE_ICON_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "mdi:air-filter",
        (
            "filter",
            "hepa",
            "滤芯",
            "滤网",
            "滤",
            "过滤",
        ),
    ),
    (
        "mdi:brush",
        (
            "side brush",
            "edge brush",
            "边刷",
        ),
    ),
    (
        "mdi:broom",
        (
            "main brush",
            "roller brush",
            "roll brush",
            "brush roll",
            "brush",
            "滚刷",
            "主刷",
            "胶刷",
            "毛刷",
        ),
    ),
    (
        "mdi:delete-empty",
        (
            "dust bag",
            "dustbin",
            "dust box",
            "dust collector",
            "dust",
            "尘袋",
            "集尘",
            "尘盒",
        ),
    ),
    (
        "mdi:bottle-tonic-outline",
        (
            "detergent",
            "cleaning solution",
            "cleaner",
            "solution",
            "清洁液",
            "清洁剂",
            "洗涤剂",
        ),
    ),
    (
        "mdi:water",
        (
            "sewage",
            "waste water",
            "dirty water",
            "clean water",
            "water tank",
            "污水",
            "清水",
            "水箱",
        ),
    ),
    (
        "mdi:tshirt-crew-outline",
        (
            "mop",
            "mopping pad",
            "mop pad",
            "rag",
            "拖布",
            "抹布",
        ),
    ),
)


def _consumable_icon(consumable: ConsumableInfo | None) -> str:
    """Return an icon that matches the app-reported consumable function."""
    if consumable is None:
        return "mdi:progress-wrench"
    text = " ".join(
        (
            consumable.name,
            consumable.subtitle,
            consumable.code,
            str(consumable.type_id),
            str(consumable.category),
        )
    ).lower()
    for icon, keywords in _CONSUMABLE_ICON_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return icon
    return "mdi:progress-wrench"


class NarwalConsumableSensor(NarwalEntity, SensorEntity):
    """Remaining lifetime of one app-reported consumable."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        consumable: ConsumableInfo,
    ) -> None:
        super().__init__(coordinator)
        self._consumable_key = consumable.key
        self._attr_name = consumable.name
        self._set_entity_identity(
            "sensor",
            f"consumable_{consumable.key}",
        )

    @property
    def _consumable(self) -> ConsumableInfo | None:
        state = self.coordinator.data
        if state is None:
            return None
        return state.consumables.get(self._consumable_key)

    @property
    def native_value(self) -> int | None:
        """Return the remaining percentage used by the Narwal app."""
        consumable = self._consumable
        return consumable.remaining_percentage if consumable else None

    @property
    def icon(self) -> str:
        """Return an icon for the reported consumable function."""
        return _consumable_icon(self._consumable)

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Expose protocol values useful for diagnostics and automations."""
        consumable = self._consumable
        if consumable is None:
            return {}
        attributes: dict[str, int | str] = {
            "type": consumable.type_id,
            "category": consumable.category,
            "code": consumable.code,
            "used_duration": consumable.usage_duration,
            "total_duration": consumable.total_duration,
            "subtitle": consumable.subtitle,
        }
        state = self.coordinator.data
        mapping = (
            CONSUMABLE_MAINTAIN_TYPE_MAP
            if consumable.category == 1
            else CONSUMABLE_REPLACE_TYPE_MAP
        )
        protocol_value = mapping.get(consumable.type_id)
        if state and state.mqtt_consumables_supported and protocol_value is not None:
            values = (
                state.consumable_maintain_items
                if consumable.category == 1
                else state.consumable_replace_items
            )
            attributes["mqtt_reported"] = protocol_value in values
            attributes["mqtt_protocol_value"] = protocol_value
        return attributes

    @property
    def available(self) -> bool:
        """Cloud lifetime data does not depend on the robot being awake."""
        return self.coordinator.last_update_success and self._consumable is not None
