"""Narwal Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import CONF_PASSWORD, CONF_PHONE, PLATFORMS
from .coordinator import NarwalCoordinator
from .narwal_client import NarwalAuthenticationError, NarwalConnectionError

type NarwalConfigEntry = ConfigEntry[NarwalCoordinator]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    if not entry.data.get(CONF_PHONE) or not entry.data.get(CONF_PASSWORD):
        raise ConfigEntryAuthFailed("Narwal cloud credentials are required")

    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException as err:
        # Connecting succeeds before the remaining discovery/platform setup.
        # Preserve the original error while rolling back any partial setup.
        try:
            await coordinator.async_shutdown()
        except BaseException:
            _LOGGER.exception("Failed to shut down Narwal after setup failure")
        if isinstance(err, NarwalAuthenticationError):
            raise ConfigEntryAuthFailed("Narwal cloud credentials were rejected") from err
        if isinstance(err, NarwalConnectionError):
            raise ConfigEntryNotReady("Cannot connect to Narwal vacuum") from err
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    # DataUpdateCoordinator registers async_shutdown with the config entry.
    # Home Assistant runs that callback after the platforms unload successfully.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
