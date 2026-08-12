"""Select entities for Narwal cleaning preferences."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.translation import async_get_translations

from . import NarwalConfigEntry
from .const import DOMAIN
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CustomCleanStrategy, DryMopStrength, MopHumidity

MOP_HUMIDITY_OPTIONS: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
DRY_MOP_STRENGTH_OPTIONS: dict[str, DryMopStrength] = {
    "quiet": DryMopStrength.QUIET,
    "strong": DryMopStrength.STRONG,
    "smart": DryMopStrength.SMART,
}
CLEAN_STRATEGY_OPTIONS: dict[str, CustomCleanStrategy] = {
    "area": CustomCleanStrategy.AREA,
    "room": CustomCleanStrategy.ROOM,
    "smart": CustomCleanStrategy.SMART,
}
CLEAN_PLAN_MODE_TRANSLATION_KEYS: dict[int, str] = {
    0: "customization",
    1: "freo_mind",
    2: "vacuum",
    3: "mop",
    4: "vacuum_and_mop",
    5: "vacuum_then_mop",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal cleaning preference selects."""
    coordinator = entry.runtime_data
    translations = await async_get_translations(hass, hass.config.language, "selector", {DOMAIN})
    known: set[str] = set()

    def _add_supported() -> None:
        entities: list[tuple[str, SelectEntity]] = []
        if coordinator.supports_mop_humidity and "mop_humidity" not in known:
            entities.append(("mop_humidity", NarwalMopHumiditySelect(coordinator)))
        if coordinator.supports_clean_strategy and "clean_strategy" not in known:
            entities.append(("clean_strategy", NarwalCleanStrategySelect(coordinator)))
        if coordinator.supports_dry_mop_strength and "dry_mop_strength" not in known:
            entities.append(("dry_mop_strength", NarwalDryMopStrengthSelect(coordinator)))
        if coordinator.data and coordinator.data.clean_plans and "clean_plan" not in known:
            entities.append(
                (
                    "clean_plan",
                    NarwalCleanPlanSelect(coordinator, translations),
                )
            )
        if (
            coordinator.data
            and coordinator.data.saved_maps_supported
            and len(coordinator.data.saved_maps) > 1
            and "map" not in known
        ):
            entities.append(("map", NarwalMapSelect(coordinator)))
        if (
            coordinator.data
            and coordinator.data.voice_packs_supported
            and coordinator.data.voice_packs
            and "voice_pack" not in known
        ):
            entities.append(("voice_pack", NarwalVoicePackSelect(coordinator)))
        known.update(key for key, _entity in entities)
        if entities:
            async_add_entities([entity for _key, entity in entities])

    _add_supported()
    entry.async_on_unload(coordinator.async_add_listener(_add_supported))


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity):
    """Set how wet the robot keeps its mop pads."""

    _attr_translation_key = "mop_humidity"
    _attr_icon = "mdi:water-percent"
    _attr_options = list(MOP_HUMIDITY_OPTIONS)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "mop_humidity")
        self._current_option: str | None = None

    @property
    def current_option(self) -> str | None:
        """Return the robot value or the last value selected in HA."""
        if self._current_option is not None:
            return self._current_option
        raw = self.coordinator.client.state.raw_base_status.get("29")
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return None
        return next(
            (name for name, value in MOP_HUMIDITY_OPTIONS.items() if int(value) == level),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        """Set mop humidity."""
        level = MOP_HUMIDITY_OPTIONS.get(option)
        if level is None:
            raise HomeAssistantError(f"Unsupported mop humidity option: {option}")
        response = await self._async_await_command(
            self.coordinator.client.set_mop_humidity(level),
            "set mop humidity",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        self._current_option = option
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class NarwalConfigSelect(NarwalEntity, SelectEntity):
    """Base class for selects backed by config/get and config/set."""

    config_field: str

    @property
    def current_option(self) -> str | None:
        """Return the option reported by the robot configuration."""
        raw = self.coordinator.device_config.get(self.config_field)
        if isinstance(raw, dict):
            raw = raw.get("1")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return next(
            (option for option, enum_value in self.option_map.items() if enum_value == value),
            None,
        )


class NarwalCleanStrategySelect(NarwalConfigSelect):
    """Select the default custom cleaning strategy."""

    _attr_translation_key = "clean_strategy"
    _attr_icon = "mdi:strategy"
    _attr_options = list(CLEAN_STRATEGY_OPTIONS)
    config_field = "18"
    option_map = CLEAN_STRATEGY_OPTIONS

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "clean_strategy")

    async def async_select_option(self, option: str) -> None:
        strategy = CLEAN_STRATEGY_OPTIONS.get(option)
        if strategy is None:
            raise HomeAssistantError(f"Unsupported cleaning strategy: {option}")
        response = await self._async_await_command(
            self.coordinator.client.set_custom_clean_strategy(
                strategy,
                self.coordinator.device_config.get("18")
                if isinstance(self.coordinator.device_config.get("18"), dict)
                else None,
            ),
            "set cleaning strategy",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        current = self.coordinator.device_config.get("18")
        if isinstance(current, dict):
            current["1"] = int(strategy)
        else:
            self.coordinator.device_config["18"] = {"1": int(strategy)}
        self.async_write_ha_state()


class NarwalDryMopStrengthSelect(NarwalConfigSelect):
    """Select the station mop drying strength."""

    _attr_translation_key = "dry_mop_strength"
    _attr_icon = "mdi:fan-chevron-up"
    _attr_options = list(DRY_MOP_STRENGTH_OPTIONS)
    config_field = "12"
    option_map = DRY_MOP_STRENGTH_OPTIONS

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "dry_mop_strength")

    async def async_select_option(self, option: str) -> None:
        strength = DRY_MOP_STRENGTH_OPTIONS.get(option)
        if strength is None:
            raise HomeAssistantError(f"Unsupported drying strength: {option}")
        response = await self._async_await_command(
            self.coordinator.client.set_dry_mop_strength(strength),
            "set mop drying strength",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        self.coordinator.device_config["12"] = int(strength)
        self.async_write_ha_state()


def _named_options(values: list[dict[str, object]], id_key: str = "id") -> dict[str, int]:
    """Build stable, unique select labels from app-provided names."""
    result: dict[str, int] = {}
    for value in values:
        item_id = _optional_int(value.get(id_key))
        if item_id is None:
            continue
        name = str(value.get("name") or item_id)
        label = name if name not in result else f"{name} ({item_id})"
        result[label] = item_id
    return result


def _optional_int(value: object) -> int | None:
    """Return an integer for a cloud option ID without accepting booleans."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_plan_options(
    values: list[dict[str, object]], translations: dict[str, str]
) -> dict[str, int]:
    """Build plan options using translated names when no custom name exists."""
    named_values = []
    for value in values:
        plan = dict(value)
        if not plan.get("name"):
            clean_mode = _optional_int(plan.get("clean_mode"))
            translation_key = CLEAN_PLAN_MODE_TRANSLATION_KEYS.get(clean_mode)
            translated_name = (
                translations.get(
                    f"component.{DOMAIN}.selector.clean_plan.options.{translation_key}"
                )
                if translation_key
                else None
            )
            plan_id = _optional_int(plan.get("id"))
            plan["name"] = translated_name or (
                f"Cleaning plan {plan_id}" if plan_id is not None else "Cleaning plan"
            )
        named_values.append(plan)
    return _named_options(named_values)


class NarwalCleanPlanSelect(NarwalEntity, SelectEntity):
    """Choose a saved cleaning plan without starting it."""

    _attr_translation_key = "clean_plan"
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator: NarwalCoordinator, translations: dict[str, str]) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "clean_plan")
        self._translations = translations

    @property
    def _option_map(self) -> dict[str, int]:
        state = self.coordinator.data
        return _clean_plan_options(state.clean_plans, self._translations) if state else {}

    @property
    def options(self) -> list[str]:
        return list(self._option_map)

    @property
    def current_option(self) -> str | None:
        options = self._option_map
        selected = self.coordinator.selected_clean_plan_id
        if selected is None and options:
            selected = next(iter(options.values()))
            self.coordinator.selected_clean_plan_id = selected
        return next(
            (name for name, plan_id in options.items() if plan_id == selected),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        plan_id = self._option_map.get(option)
        if plan_id is None:
            raise HomeAssistantError(f"Unsupported cleaning plan: {option}")
        self.coordinator.selected_clean_plan_id = plan_id
        self.async_write_ha_state()


class NarwalMapSelect(NarwalEntity, SelectEntity):
    """Switch between maps saved on the robot."""

    _attr_translation_key = "map"
    _attr_icon = "mdi:map-marker-multiple"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "map")

    @property
    def _option_map(self) -> dict[str, int]:
        state = self.coordinator.data
        return _named_options(state.saved_maps) if state else {}

    @property
    def options(self) -> list[str]:
        return list(self._option_map)

    @property
    def current_option(self) -> str | None:
        state = self.coordinator.data
        current = state.current_map_id if state else None
        return next(
            (name for name, map_id in self._option_map.items() if map_id == current),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        map_id = self._option_map.get(option)
        if map_id is None:
            raise HomeAssistantError(f"Unsupported map: {option}")
        response = await self._async_await_command(
            self.coordinator.client.switch_map(map_id),
            "switch map",
        )
        if not response.success:
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        await self.coordinator.client.get_map()
        await self.coordinator.client.get_all_maps()
        self.coordinator.async_set_updated_data(self.coordinator.client.state)


class NarwalVoicePackSelect(NarwalEntity, SelectEntity):
    """Select an official voice package returned by the Narwal cloud."""

    _attr_translation_key = "voice_pack"
    _attr_icon = "mdi:account-voice"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator)
        self._set_entity_identity("select", "voice_pack")

    @property
    def _option_map(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for value in self.coordinator.client.state.voice_packs:
            resource_id = str(value.get("resource_id") or "")
            if not resource_id:
                continue
            name = str(value.get("name") or resource_id)
            label = name if name not in result else f"{name} ({resource_id})"
            result[label] = {key: str(item or "") for key, item in value.items()}
        return result

    @property
    def options(self) -> list[str]:
        return list(self._option_map)

    @property
    def current_option(self) -> str | None:
        current = self.coordinator.client.state.current_voice.get("resource_id")
        return next(
            (
                option
                for option, value in self._option_map.items()
                if value["resource_id"] == current
            ),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        pack = self._option_map.get(option)
        if pack is None:
            raise HomeAssistantError(f"Unsupported voice package: {option}")
        language = self.coordinator.client.state.current_voice.get("language", "")
        if not language:
            raise HomeAssistantError("The robot did not report its voice language")
        response = await self._async_await_command(
            self.coordinator.client.set_custom_voice(
                language=language,
                resource_id=pack["resource_id"],
                version=pack["version"],
            ),
            "set voice package",
        )
        # The protocol treats RESULT_TYPE_SUCCESS (1) and
        # SWITCH_VOICE_SUCCESS (3, an already-downloaded package) as success.
        if response.result_code not in (1, 3):
            raise HomeAssistantError(
                f"Narwal action failed with result code {response.result_code}"
            )
        self.coordinator.client.state.current_voice = {
            "language": language,
            "version": pack["version"],
            "description": pack.get("description") or pack.get("name", ""),
            "resource_id": pack["resource_id"],
        }
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.client.state)
