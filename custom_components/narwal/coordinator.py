"""DataUpdateCoordinator for Narwal vacuum."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AREA_CODE,
    CONF_CONNECTION_TYPE,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PRODUCT_KEY,
    CONNECTION_CLOUD,
    DOMAIN,
)
from .narwal_client import (
    DeviceInfo,
    NarwalAuthenticationError,
    NarwalClient,
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


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Push-mode coordinator for Narwal vacuum.

    Primary data source is WebSocket broadcasts (every ~1.5s when awake).
    Fallback polling every 60s via get_status() in case broadcasts stop.

    Setup is kept fast: connect, try a few commands (which may time out if
    the robot is asleep), then start the listener. The listener's keepalive
    loop handles waking the robot — no blocking wake call during setup.
    """

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
        if entry.data[CONF_CONNECTION_TYPE] == CONNECTION_CLOUD:
            self.client = NarwalCloudClient(
                async_get_clientsession(hass),
                phone=entry.data[CONF_PHONE],
                password=entry.data[CONF_PASSWORD],
                area_code=entry.data[CONF_AREA_CODE],
                country_code=entry.data[CONF_COUNTRY_CODE],
                device_id=entry.data["device_id"],
                product_id=product_key,
            )
        else:
            self.client = NarwalClient(
                host=entry.data["host"],
                port=entry.data["port"],
                device_id=entry.data["device_id"],
                topic_prefix=f"/{product_key}",
            )
        self._listen_task: asyncio.Task[None] | None = None
        self._fast_poll_remaining = 0
        self._prev_working_status = WorkingStatus.UNKNOWN
        self._map_fetch_pending = False
        self._last_display_map_resub: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 5  # 5 * 60s = 5 minutes before entities go unavailable

    async def async_setup(self) -> None:
        """Connect to the vacuum and start the WebSocket listener.

        Queries initial state BEFORE starting the listener to avoid
        concurrent recv issues (see 446be16). Each command is wrapped in
        try/except so setup never crashes if the robot is asleep.
        The listener's keepalive loop handles waking independently.
        """
        await self.client.connect()

        if isinstance(self.client, NarwalCloudClient) and self.client.cloud_device:
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
            await self.client.get_map()
        except Exception:
            _LOGGER.debug("Could not fetch initial map")

        await self._async_refresh_cloud_metadata()

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
            f"{DOMAIN}_ws_listener",
        )

        state = self.client.state
        _LOGGER.info(
            "Narwal startup: status=%s, battery=%d, docked=%s, awake=%s",
            state.working_status.name, state.battery_level,
            state.is_docked, self.client.robot_awake,
        )

        # If robot didn't respond, use fast polling to catch it when it wakes
        if state.working_status == WorkingStatus.UNKNOWN:
            self._fast_poll_remaining = FAST_POLL_MAX
            self.update_interval = FAST_POLL_INTERVAL
            _LOGGER.info(
                "Robot asleep — fast polling every %ds until it responds",
                int(FAST_POLL_INTERVAL.total_seconds()),
            )

    async def _async_refresh_cloud_metadata(self) -> None:
        """Refresh optional cloud-only data without affecting robot control."""
        if not isinstance(self.client, NarwalCloudClient):
            return
        try:
            await self.client.get_cloud_metadata()
        except NarwalAuthenticationError:
            _LOGGER.warning("Narwal cloud credentials require reauthentication")
            self.config_entry.async_start_reauth(self.hass)
        except Exception:
            _LOGGER.debug(
                "Could not refresh Narwal consumables or notifications",
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
        """Handle a push state update from the WebSocket listener."""
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

        # Detect a cleaning-task → idle/station-task transition.
        # Broadcast dock fields are stale after docking — immediate poll
        # refreshes them so UI shows DOCKED instead of IDLE.
        # On older FW the transition is → STANDBY; on v01.07.23+ it may
        # go directly to DOCKED_V2(2).
        if state.uses_task_type_schema:
            now_idle_or_station = (
                state.working_status == WorkingStatus.IDLE
                or state.working_status.is_station_task
            )
            was_cleaning = self._prev_working_status.is_cleaning_task
        else:
            now_idle_or_station = state.working_status in (
                WorkingStatus.STANDBY,
                WorkingStatus.DOCKED_V2,
                WorkingStatus.DOCKED,
                WorkingStatus.CHARGED,
            )
            was_cleaning = self._prev_working_status in (
                WorkingStatus.CLEANING,
                WorkingStatus.CLEANING_ALT,
            )
        if now_idle_or_station and was_cleaning:
            _LOGGER.info("Return-to-dock detected, refreshing dock status")
            self.hass.async_create_task(self._refresh_dock_status())
        self._prev_working_status = state.working_status

        # display_map dropout recovery: if cleaning but no display_map for
        # 30s, re-send topic subscription. Only subscription — no wake burst
        # (wake bursts during cleaning cause pause bouncing).
        is_cleaning = state.has_cleaning_task
        if is_cleaning:
            display_age = self.client.last_display_map_age
            now = time.monotonic()
            if (
                display_age > 30.0
                and now - self._last_display_map_resub > 45.0
            ):
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
        """Polling fallback — fetch status if no push updates arrived.

        Reconnection is handled by the listener loop's exponential backoff.
        We do NOT call client.connect() here to avoid racing with the listener
        and violating the single-WS-connection-per-IP constraint.

        On poll failure, returns stale data for up to _max_failures consecutive
        failures (~5 minutes) before raising UpdateFailed.
        """
        try:
            if not self.client.connected:
                raise NarwalConnectionError("Not connected")
            await self.client.get_status(full_update=True)
        except Exception as err:
            self._consecutive_failures += 1
            await self._async_refresh_cloud_metadata()
            if self._consecutive_failures >= self._max_failures:
                raise UpdateFailed(
                    f"Vacuum unreachable for {self._consecutive_failures} consecutive polls"
                ) from err
            _LOGGER.debug(
                "Poll %d/%d failed (robot may be asleep): %s",
                self._consecutive_failures, self._max_failures, err,
            )
            return self.client.state  # stale data keeps entities available
        else:
            self._consecutive_failures = 0

        await self._async_refresh_cloud_metadata()

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
