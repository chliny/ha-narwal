"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AREA_CODE,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PRODUCT_KEY,
    DOMAIN,
    FEATURE_SUPPORT_QUIET_DUST_GATHERING,
    FEATURE_SUPPORT_SINGLE_DRY_MOP,
)
from .narwal_client import (
    DeviceInfo,
    NarwalAuthenticationError,
    NarwalCloudClient,
    NarwalConnectionError,
    NarwalState,
)
from .narwal_client.const import WorkingStatus

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

# Fast re-poll when state is incomplete (robot asleep at startup)
FAST_POLL_INTERVAL = timedelta(seconds=10)
FAST_POLL_MAX = 6  # up to 60s of fast polling before falling back to normal
FIRMWARE_REFRESH_INTERVAL = 60 * 60

APP_LANGUAGE_CODES = {
    "de": "de-DE",
    "en": "en-US",
    "es": "es-ES",
    "fr": "fr-FR",
    "he": "he-IL",
    "it": "it-IT",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "pl": "pl-PL",
    "ru": "ru-RU",
    "th": "th-TH",
    "tr": "tr-TR",
    "vi": "vi-VN",
}


def _app_language_code(language: str) -> str:
    """Map a Home Assistant locale to one of the APK's language tags."""
    normalized = language.replace("_", "-")
    if normalized.lower() in {"zh-hant", "zh-tw", "zh-hk"}:
        return "zh-TW"
    if normalized.lower().startswith("zh"):
        return "zh-CN"
    return APP_LANGUAGE_CODES.get(normalized.split("-", 1)[0].lower(), "en-US")


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Cloud push coordinator with polling fallback."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        product_key = entry.data[CONF_PRODUCT_KEY]
        self.client = NarwalCloudClient(
            async_get_clientsession(hass),
            phone=entry.data[CONF_PHONE],
            password=entry.data[CONF_PASSWORD],
            area_code=entry.data[CONF_AREA_CODE],
            country_code=entry.data[CONF_COUNTRY_CODE],
            device_id=entry.data["device_id"],
            product_id=product_key,
        )
        self._listen_task: asyncio.Task[None] | None = None
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._map_fetch_pending = False
        self._last_display_map_resub: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable
        self.feature_flags: dict[int, int] = {}
        self.device_config: dict[str, object] = {}
        self._feature_discovery_complete = False
        self._config_discovery_complete = False
        self._extended_refresh_count = 0
        self._last_firmware_refresh = 0.0
        self.selected_clean_plan_id: int | None = None

    @property
    def supports_single_dry_mop(self) -> bool:
        """Return whether the station supports drying the mop independently."""
        return self.feature_flags.get(FEATURE_SUPPORT_SINGLE_DRY_MOP) == 1

    @property
    def supports_fan_speed(self) -> bool:
        """Return whether base status declares a suction setting."""
        return "26" in self.client.state.raw_base_status

    @property
    def supports_mop_humidity(self) -> bool:
        """Return whether base status declares a mop humidity setting."""
        return "29" in self.client.state.raw_base_status

    @property
    def supports_dust_gathering(self) -> bool:
        """Return whether station dust gathering is configurable."""
        return (
            "27" in self.device_config
            or self.feature_flags.get(FEATURE_SUPPORT_QUIET_DUST_GATHERING) == 1
        )

    @property
    def supports_dry_mop_strength(self) -> bool:
        """Return whether the station exposes mop drying strength."""
        return "12" in self.device_config

    @property
    def supports_clean_strategy(self) -> bool:
        """Return whether the robot exposes a custom cleaning strategy."""
        return "18" in self.device_config

    @property
    def supports_volume(self) -> bool:
        """Return whether config/get exposes speaker volume."""
        return "1" in self.device_config

    async def async_setup(self) -> None:
        """Connect to the cloud and start the MQTT listener."""
        await self.client.connect()

        if self.client.cloud_device:
            cloud_device = self.client.cloud_device
            self.client.state.device_info = DeviceInfo(
                product_key=cloud_device.product_id,
                device_id=cloud_device.device_id,
                firmware_version=cloud_device.firmware_version,
            )
            self.client.state.firmware_version = cloud_device.firmware_version

        # Fetch initial state BEFORE starting listener (no concurrent recv)
        try:
            await self.client.get_device_info()
        except Exception:
            _LOGGER.debug("Could not fetch device info at startup")

        try:
            await self.client.get_status(full_update=True)
        except Exception:
            _LOGGER.debug("Could not fetch initial status")

        try:
            self.feature_flags = await self.client.get_feature_list()
            self._feature_discovery_complete = True
        except Exception:
            _LOGGER.debug("Could not fetch device feature flags at startup")

        try:
            self.device_config = await self.client.get_config()
            self._config_discovery_complete = True
        except Exception:
            _LOGGER.debug("Could not fetch device configuration at startup")

        try:
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        await self._async_refresh_extended_state(include_static=True)
        await self._async_refresh_cloud_metadata(task_record_pages=3)
        await self._async_refresh_firmware_update()

        # Subscribe to broadcast topics (display_map, working_status, etc.)
        # Must be sent before listener starts so display_map flows during cleaning.
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Could not send topic subscription at startup")

        self.async_set_updated_data(self.client.state)

        # Set up push callback and start persistent listener
        self.client.on_state_update = self._on_state_update
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_listen(),
            f"{DOMAIN}_mqtt_listener",
        )

        state = self.client.state
        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name,
            state.battery_level,
            state.is_docked,
            self.client.robot_awake,
        )

        # If robot didn't respond, use fast polling to catch it when it wakes
        if state.working_status == WorkingStatus.UNKNOWN:
            self._fast_poll_remaining = FAST_POLL_MAX
            self.update_interval = FAST_POLL_INTERVAL
            _LOGGER.info(
                "Robot asleep — fast polling every %ds until it responds",
                int(FAST_POLL_INTERVAL.total_seconds()),
            )

    async def _async_refresh_cloud_metadata(
        self,
        *,
        task_record_pages: int = 1,
    ) -> None:
        """Refresh optional account data without affecting robot control."""
        try:
            await self.client.get_cloud_metadata(
                task_record_pages=task_record_pages,
            )
        except NarwalAuthenticationError:
            _LOGGER.warning("Narwal cloud credentials require reauthentication")
            self.config_entry.async_start_reauth(self.hass)
        except Exception:
            _LOGGER.debug(
                "Could not refresh Narwal consumables or notifications",
                exc_info=True,
            )

    async def _async_refresh_firmware_update(self) -> None:
        """Refresh the read-only cloud firmware availability state."""
        try:
            await self.client.get_firmware_update()
        except NarwalAuthenticationError:
            _LOGGER.warning("Narwal cloud credentials require reauthentication")
            self.config_entry.async_start_reauth(self.hass)
        except Exception:
            _LOGGER.debug(
                "Could not refresh Narwal firmware availability",
                exc_info=True,
            )
        finally:
            self._last_firmware_refresh = time.monotonic()

    async def _async_maybe_refresh_firmware_update(self) -> None:
        """Refresh firmware availability no more than once per hour."""
        if (
            time.monotonic() - self._last_firmware_refresh
            >= FIRMWARE_REFRESH_INTERVAL
        ):
            await self._async_refresh_firmware_update()

    async def _async_refresh_extended_state(self, *, include_static: bool) -> None:
        """Probe and refresh optional robot-side APK protocol features."""
        calls: list[Callable[[], Awaitable[object]]] = [
            self.client.get_dry_mop_remaining_time,
            self.client.get_clean_progress,
            self.client.get_clean_timeline,
        ]
        if include_static:
            calls.extend(
                (
                    self.client.get_battery_info,
                    self.client.get_consumable_info,
                    self.client.get_clean_plans,
                    self.client.get_all_maps,
                    self.client.get_current_voice_info,
                )
            )
        for call in calls:
            try:
                await call()
            except Exception:
                _LOGGER.debug(
                    "Optional Narwal feature query %s is unsupported or unavailable",
                    call.__name__,
                )
        if include_static and self.client.state.current_voice.get("language"):
            try:
                await self.client.get_voice_packs(
                    _app_language_code(self.hass.config.language)
                )
            except Exception:
                _LOGGER.debug(
                    "Optional Narwal voice-package catalog is unavailable",
                    exc_info=True,
                )

    async def _async_listen(self) -> None:
        """Run the transport listener and request reauth on credential failure."""
        try:
            await self.client.start_listening()
        except NarwalAuthenticationError:
            _LOGGER.warning("Narwal cloud credentials require reauthentication")
            self.config_entry.async_start_reauth(self.hass)

    def _on_state_update(self, state: NarwalState) -> None:
        """Handle a push state update from MQTT."""
        # Push data arriving means robot is reachable — reset failure counter
        self._consecutive_failures = 0

        # Fetch static map if missing (get_map failed at startup)
        if state.map_data is None and not self._map_fetch_pending:
            self._map_fetch_pending = True
            self.config_entry.async_create_background_task(
                self.hass,
                self._fetch_missing_map(),
                f"{DOMAIN}_map_fetch",
            )

        # Poll immediately after a cleaning task becomes idle or a station
        # task so dock fields do not remain stale.
        now_idle_or_station = (
            state.working_status == WorkingStatus.IDLE or state.working_status.is_station_task
        )
        was_cleaning = self._prev_working_status.is_cleaning_task
        if now_idle_or_station and was_cleaning:
            _LOGGER.info("Return-to-dock detected, refreshing dock status")
            self.hass.async_create_task(self._refresh_dock_status())
        self._prev_working_status = state.working_status

        # Renew cloud status publication if live map updates stop mid-clean.
        is_cleaning = state.has_cleaning_task
        if is_cleaning:
            display_age = self.client.last_display_map_age
            now = time.monotonic()
            if display_age > 30.0 and now - self._last_display_map_resub > 45.0:
                _LOGGER.info(
                    "display_map dropout (%.0fs) — re-subscribing to topics",
                    display_age,
                )
                self._last_display_map_resub = now
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._resub_topics(),
                    f"{DOMAIN}_resub",
                )

        self.async_set_updated_data(state)

        # Broadcast arrived — switch back to normal polling if in fast mode
        if self._fast_poll_remaining > 0:
            self._fast_poll_remaining = 0
            self.update_interval = POLL_INTERVAL
            _LOGGER.info(
                "Broadcast received (status=%s) — normal polling restored",
                state.working_status.name,
            )

    async def _fetch_missing_map(self) -> None:
        """Fetch static map when it's missing (get_map failed at startup)."""
        try:
            await self.client.get_map()
            _LOGGER.info("Static map loaded (was missing at startup)")
        except Exception:
            _LOGGER.debug("Map fetch failed — will retry on next broadcast")
            self._map_fetch_pending = False
            return
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Topic subscription failed after map load")
        self.async_set_updated_data(self.client.state)

    async def _resub_topics(self) -> None:
        """Re-send topic subscription to recover display_map during cleaning."""
        try:
            await self.client.subscribe_to_topics()
        except Exception:
            _LOGGER.debug("Topic re-subscription failed")

    async def _refresh_dock_status(self) -> None:
        """Immediate get_status() after return-to-dock to refresh dock fields."""
        try:
            await self.client.get_status(full_update=True)
            self.async_set_updated_data(self.client.state)
        except Exception:
            _LOGGER.debug("Failed to refresh dock status after transition")

    async def _async_update_data(self) -> NarwalState:
        """Poll status when push updates are unavailable."""
        try:
            if not self.client.connected:
                raise NarwalConnectionError("Not connected")
            await self.client.get_status(full_update=True)
        except Exception as err:
            self._consecutive_failures += 1
            await self._async_refresh_cloud_metadata()
            await self._async_maybe_refresh_firmware_update()
            if self._consecutive_failures >= self._max_failures:
                raise UpdateFailed(
                    f"Vacuum unreachable for {self._consecutive_failures} consecutive polls"
                ) from err
            _LOGGER.debug(
                "Poll %d/%d failed (robot may be asleep): %s",
                self._consecutive_failures,
                self._max_failures,
                err,
            )
            return self.client.state  # stale data keeps entities available
        else:
            self._consecutive_failures = 0

        if not self._feature_discovery_complete:
            with contextlib.suppress(Exception):
                self.feature_flags = await self.client.get_feature_list()
                self._feature_discovery_complete = True
        if not self._config_discovery_complete:
            with contextlib.suppress(Exception):
                self.device_config = await self.client.get_config()
                self._config_discovery_complete = True

        self._extended_refresh_count += 1
        await self._async_refresh_cloud_metadata(
            task_record_pages=(3 if self._extended_refresh_count % 30 == 0 else 1),
        )
        await self._async_maybe_refresh_firmware_update()
        await self._async_refresh_extended_state(
            include_static=self._extended_refresh_count % 5 == 0
        )

        # Retry map fetch if it failed during setup
        if self.client.state.map_data is None:
            with contextlib.suppress(Exception):
                await self.client.get_map()

        # Manage fast poll countdown
        if self._fast_poll_remaining > 0:
            if self.client.state.working_status != WorkingStatus.UNKNOWN:
                self._fast_poll_remaining = 0
                self.update_interval = POLL_INTERVAL
            else:
                self._fast_poll_remaining -= 1
                if self._fast_poll_remaining <= 0:
                    self.update_interval = POLL_INTERVAL

        return self.client.state

    async def async_shutdown(self) -> None:
        """Disconnect from the vacuum."""
        await self.client.disconnect()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task
        await super().async_shutdown()
