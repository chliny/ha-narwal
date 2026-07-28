"""Sensor entities for Narwal vacuum."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import ConsumableInfo, NarwalState


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
        # working_status field 13 is cm²; divide by 10000 for m².
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: round(state.cleaning_area / 10000, 2)
        if state.cleaning_area > 0
        else None,
    ),
    NarwalSensorEntityDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        # working_status field 3 is session elapsed seconds.
        # NEEDS LIVE VALIDATION: only populated during active cleaning.
        value_fn=lambda state: state.cleaning_time
        if state.cleaning_time > 0
        else None,
    ),
    NarwalSensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware_version or None,
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
    known_consumables: set[str] = set()
    task_record_added = False

    def _add_dynamic_entities() -> None:
        nonlocal task_record_added
        state = coordinator.data
        if state is None:
            return
        if state.task_records_supported and not task_record_added:
            task_record_added = True
            async_add_entities([NarwalTaskRecordSensor(coordinator)])
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


class NarwalTaskRecordSensor(NarwalEntity, SensorEntity):
    """Expose the latest cleaning task returned by the cloud."""

    _attr_translation_key = "task_record"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("sensor", "task_record")

    @property
    def native_value(self) -> str | None:
        """Return the latest task record identifier."""
        state = self.coordinator.data
        record = state.latest_task_record if state else None
        if not record:
            return None
        value = record.get("record_id") or record.get("report_id") or record.get("id")
        return str(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return a concise summary of the latest task."""
        state = self.coordinator.data
        record = state.latest_task_record if state else None
        if not record:
            return {}
        keys = (
            "task_type",
            "record_type",
            "record_status",
            "area",
            "duration",
            "start_time",
            "end_time",
            "error_code",
            "custom_name",
        )
        return {key: record[key] for key in keys if record.get(key) is not None}


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
        return {
            "type": consumable.type_id,
            "category": consumable.category,
            "code": consumable.code,
            "used_duration": consumable.usage_duration,
            "total_duration": consumable.total_duration,
            "subtitle": consumable.subtitle,
        }

    @property
    def available(self) -> bool:
        """Cloud lifetime data does not depend on the robot being awake."""
        return self.coordinator.last_update_success and self._consumable is not None
