"""Narwal Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import CONF_PASSWORD, CONF_PHONE, PLATFORMS
from .coordinator import NarwalCoordinator
from .narwal_client import NarwalAuthenticationError, NarwalConnectionError

type NarwalConfigEntry = ConfigEntry[NarwalCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    if not entry.data.get(CONF_PHONE) or not entry.data.get(CONF_PASSWORD):
        raise ConfigEntryAuthFailed("Narwal cloud credentials are required")

    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalAuthenticationError as err:
        raise ConfigEntryAuthFailed(
            "Narwal cloud credentials were rejected"
        ) from err
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady("Cannot connect to Narwal vacuum") from err

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_shutdown()

    return unload_ok
