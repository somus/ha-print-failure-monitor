"""Detect failed 3D prints from a camera feed using decision models."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .alerts import async_listen_for_actions, async_remove_alerts
from .const import CONF_MAX_STORAGE_MB, CONF_RETENTION_DAYS, DOMAIN
from .coordinator import PrintFailureCoordinator, async_remove_state
from .history import async_remove_history
from .panel import async_setup_panel
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.IMAGE, Platform.SENSOR, Platform.SWITCH]
CLEANUP_INTERVAL = timedelta(hours=1)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type PrintFailureConfigEntry = ConfigEntry[PrintFailureCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the sidebar panel, its API and the actions once, shared by every monitored printer."""
    async_setup_services(hass)
    integration = await async_get_integration(hass, DOMAIN)
    await async_setup_panel(hass, str(integration.version))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PrintFailureConfigEntry) -> bool:
    coordinator = PrintFailureCoordinator(hass, entry)
    await coordinator.history.async_load()
    await coordinator.async_restore()

    async def cleanup(_now=None) -> None:
        try:
            await coordinator.history.async_cleanup(
                coordinator.option(CONF_RETENTION_DAYS), coordinator.option(CONF_MAX_STORAGE_MB)
            )
        except OSError as err:  # a full or read-only disk must not stop monitoring
            _LOGGER.error("Cleaning up frame history failed: %s", err)

    await cleanup()
    entry.async_on_unload(async_track_time_interval(hass, cleanup, CLEANUP_INTERVAL))

    # Start idle and run the first check in the background, so setup never waits on the camera or the model.
    coordinator.async_set_updated_data(coordinator.snapshot)
    entry.runtime_data = coordinator
    entry.async_on_unload(async_listen_for_actions(hass, coordinator))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN} first check")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PrintFailureConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the printer's saved frames and history, and any alert or repair issue still showing, when it's removed."""
    async_remove_alerts(hass, entry)
    await async_remove_history(hass, entry.entry_id)
    await async_remove_state(hass, entry.entry_id)
