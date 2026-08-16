"""Switch entities for Narwal station tasks."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import WorkingStatus


@dataclass(frozen=True, kw_only=True)
class NarwalConfigSwitchDescription(SwitchEntityDescription):
    """Describe a boolean config/get and config/set field pair."""

    get_field: int
    set_field: int


CONFIG_SWITCHES = (
    NarwalConfigSwitchDescription(
        key="high_altitude_mode",
        translation_key="high_altitude_mode",
        icon="mdi:image-filter-hdr",
        entity_category=EntityCategory.CONFIG,
        get_field=5,
        set_field=6,
    ),
    NarwalConfigSwitchDescription(
        key="automatic_detergent",
        translation_key="automatic_detergent",
        icon="mdi:cup-water",
        entity_category=EntityCategory.CONFIG,
        get_field=6,
        set_field=7,
    ),
    NarwalConfigSwitchDescription(
        key="child_lock",
        translation_key="child_lock",
        icon="mdi:account-lock",
        entity_category=EntityCategory.CONFIG,
        get_field=7,
        set_field=8,
    ),
    NarwalConfigSwitchDescription(
        key="carpet_cleaning",
        translation_key="carpet_cleaning",
        icon="mdi:rug",
        entity_category=EntityCategory.CONFIG,
        get_field=8,
        set_field=9,
    ),
    NarwalConfigSwitchDescription(
        key="boost_crossing",
        translation_key="boost_crossing",
        icon="mdi:stairs-up",
        entity_category=EntityCategory.CONFIG,
        get_field=10,
        set_field=11,
    ),
    NarwalConfigSwitchDescription(
        key="smart_dirt_detection",
        translation_key="smart_dirt_detection",
        icon="mdi:auto-fix",
        entity_category=EntityCategory.CONFIG,
        get_field=11,
        set_field=12,
    ),
    NarwalConfigSwitchDescription(
        key="custom_carpet_cleaning",
        translation_key="custom_carpet_cleaning",
        icon="mdi:rug",
        entity_category=EntityCategory.CONFIG,
        get_field=16,
        set_field=16,
    ),
    NarwalConfigSwitchDescription(
        key="return_to_main_map",
        translation_key="return_to_main_map",
        icon="mdi:map-marker-left",
        entity_category=EntityCategory.CONFIG,
        get_field=20,
        set_field=20,
    ),
    NarwalConfigSwitchDescription(
        key="pet_mode",
        translation_key="pet_mode",
        icon="mdi:paw",
        entity_category=EntityCategory.CONFIG,
        get_field=22,
        set_field=22,
    ),
    NarwalConfigSwitchDescription(
        key="smart_deep_cleaning",
        translation_key="smart_deep_cleaning",
        icon="mdi:sparkles",
        entity_category=EntityCategory.CONFIG,
        get_field=23,
        set_field=23,
    ),
    NarwalConfigSwitchDescription(
        key="moisture_proof_pad",
        translation_key="moisture_proof_pad",
        icon="mdi:water-off",
        entity_category=EntityCategory.CONFIG,
        get_field=26,
        set_field=26,
    ),
    NarwalConfigSwitchDescription(
        key="automatic_dust_collection",
        translation_key="automatic_dust_collection",
        icon="mdi:delete-sweep",
        entity_category=EntityCategory.CONFIG,
        get_field=27,
        set_field=27,
    ),
    NarwalConfigSwitchDescription(
        key="smart_dust_collection",
        translation_key="smart_dust_collection",
        icon="mdi:delete-clock",
        entity_category=EntityCategory.CONFIG,
        get_field=28,
        set_field=28,
    ),
    NarwalConfigSwitchDescription(
        key="quiet_dust_collection",
        translation_key="quiet_dust_collection",
        icon="mdi:volume-low",
        entity_category=EntityCategory.CONFIG,
        get_field=30,
        set_field=30,
    ),
    NarwalConfigSwitchDescription(
        key="dry_robot_dust_bag",
        translation_key="dry_robot_dust_bag",
        icon="mdi:weather-windy",
        entity_category=EntityCategory.CONFIG,
        get_field=31,
        set_field=31,
    ),
    NarwalConfigSwitchDescription(
        key="hot_water_mop_washing",
        translation_key="hot_water_mop_washing",
        icon="mdi:water-thermometer",
        entity_category=EntityCategory.CONFIG,
        get_field=32,
        set_field=32,
    ),
    NarwalConfigSwitchDescription(
        key="heavy_dirt_deep_cleaning",
        translation_key="heavy_dirt_deep_cleaning",
        icon="mdi:spray-bottle",
        entity_category=EntityCategory.CONFIG,
        get_field=33,
        set_field=33,
    ),
    NarwalConfigSwitchDescription(
        key="voice_control",
        translation_key="voice_control",
        icon="mdi:account-voice",
        entity_category=EntityCategory.CONFIG,
        get_field=36,
        set_field=36,
    ),
    NarwalConfigSwitchDescription(
        key="ai_voice_effects",
        translation_key="ai_voice_effects",
        icon="mdi:waveform",
        entity_category=EntityCategory.CONFIG,
        get_field=39,
        set_field=39,
    ),
    NarwalConfigSwitchDescription(
        key="waiting_voice_effects",
        translation_key="waiting_voice_effects",
        icon="mdi:message-processing",
        entity_category=EntityCategory.CONFIG,
        get_field=40,
        set_field=40,
    ),
    NarwalConfigSwitchDescription(
        key="dust_sensor_detection",
        translation_key="dust_sensor_detection",
        icon="mdi:blur",
        entity_category=EntityCategory.CONFIG,
        get_field=43,
        set_field=43,
    ),
    NarwalConfigSwitchDescription(
        key="valuable_item_guard",
        translation_key="valuable_item_guard",
        icon="mdi:shield-star",
        entity_category=EntityCategory.CONFIG,
        get_field=50,
        set_field=48,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Narwal station switches."""
    del hass
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_supported() -> None:
        entities: list[SwitchEntity] = []
        if "wash_and_dry_mop" not in known:
            entities.append(NarwalWashAndDryMopSwitch(coordinator))
            known.add("wash_and_dry_mop")
        for description in CONFIG_SWITCHES:
            if (
                description.key not in known
                and str(description.get_field) in coordinator.device_config
            ):
                entities.append(NarwalConfigSwitch(coordinator, description))
                known.add(description.key)
        if entities:
            async_add_entities(entities)

    _add_supported()
    entry.async_on_unload(coordinator.async_add_listener(_add_supported))


class NarwalConfigSwitch(NarwalEntity, SwitchEntity):
    """Boolean setting exposed only when config/get reports the field."""

    entity_description: NarwalConfigSwitchDescription

    def __init__(
        self,
        coordinator: NarwalCoordinator,
        description: NarwalConfigSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._set_entity_identity("switch", description.key)

    @property
    def is_on(self) -> bool | None:
        """Return the setting reported by config/get."""
        raw = self.coordinator.device_config.get(str(self.entity_description.get_field))
        if isinstance(raw, bool):
            return raw
        if not isinstance(raw, (int, str, bytes, bytearray)):
            return None
        try:
            return bool(int(raw))
        except (TypeError, ValueError):
            return None

    async def _async_set(self, enabled: bool) -> None:
        response = await self._async_await_command(
            self.coordinator.client.set_boolean_config(
                self.entity_description.set_field,
                enabled,
            ),
            f"set {self.entity_description.key}",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        self.coordinator.device_config[str(self.entity_description.get_field)] = int(enabled)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable the setting."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable the setting."""
        await self._async_set(False)


class NarwalWashAndDryMopSwitch(NarwalEntity, SwitchEntity):
    """Start or stop the station wash-and-dry mop cycle."""

    _attr_translation_key = "wash_and_dry_mop"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("switch", "wash_and_dry_mop")

    @property
    def is_on(self) -> bool:
        """Return whether the station is washing and drying mop pads."""
        state = self.coordinator.client.state
        return state.working_status == WorkingStatus.WASH_AND_DRY_MOP

    async def async_turn_on(self, **kwargs: object) -> None:
        """Start the station wash-and-dry cycle."""
        response = await self._async_await_command(
            self.coordinator.client.wash_and_dry_mop(),
            "start wash and dry mop",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        """End the station wash-and-dry cycle."""
        response = await self._async_await_command(
            self.coordinator.client.end_wash_and_dry_mop(),
            "end wash and dry mop",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.async_request_refresh()
