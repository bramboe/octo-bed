"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.COVER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octo Bed from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Ensure options exist for existing entries (migration)
    opts = dict(entry.options) if entry.options else {}
    if not opts:
        opts[CONF_FULL_TRAVEL_SECONDS] = DEFAULT_FULL_TRAVEL_SECONDS
    default_travel = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
    if CONF_HEAD_FULL_TRAVEL_SECONDS not in opts:
        opts[CONF_HEAD_FULL_TRAVEL_SECONDS] = default_travel
    if CONF_FEET_FULL_TRAVEL_SECONDS not in opts:
        opts[CONF_FEET_FULL_TRAVEL_SECONDS] = default_travel
    if CONF_SHOW_CALIBRATION_BUTTONS not in opts:
        opts[CONF_SHOW_CALIBRATION_BUTTONS] = True
    if opts != (entry.options or {}):
        hass.config_entries.async_update_entry(entry, options=opts)

    address = entry.data["address"]
    pin = entry.data["pin"]

    # Resolve Bluetooth device
    bleak_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )

    if not bleak_device:
        _LOGGER.error("Could not find Octo bed at address %s", address)
        return False

    async def _get_device():
        return bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )

    client = OctoBedClient(
        bleak_device,
        pin,
        disconnect_callback=lambda: _LOGGER.warning("Octo bed disconnected"),
        device_resolver=_get_device,
    )

    if not await client.connect():
        _LOGGER.error("Failed to connect to Octo bed")
        return False

    hass.data[DOMAIN][entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if client := hass.data[DOMAIN].get(entry.entry_id):
        await client.disconnect()

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
