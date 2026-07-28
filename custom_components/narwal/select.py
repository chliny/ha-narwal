"""Select entities for Narwal cleaning preferences."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Narwal cleaning preference selects."""
    del hass
    coordinator = entry.runtime_data
    known: set[str] = set()

    def _add_supported() -> None:
        entities: list[tuple[str, SelectEntity]] = []
        if coordinator.supports_mop_humidity:
            entities.append(("mop_humidity", NarwalMopHumiditySelect(coordinator)))
        if coordinator.supports_clean_strategy:
            entities.append(("clean_strategy", NarwalCleanStrategySelect(coordinator)))
        if coordinator.supports_dry_mop_strength:
            entities.append(
                ("dry_mop_strength", NarwalDryMopStrengthSelect(coordinator))
            )
        new = [entity for key, entity in entities if key not in known]
        known.update(key for key, _entity in entities)
        if new:
            async_add_entities(new)

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
