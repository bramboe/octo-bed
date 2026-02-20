"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_FULL_TRAVEL_SECONDS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SWITCH, Platform.COVER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octo Bed from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Ensure options exist for existing entries (migration)
    if not entry.options:
        hass.config_entries.async_update_entry(
            entry,
            options={CONF_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS},
        )

    address = entry.data["address"]
    pin = entry.data["pin"]

    # Prefer connectable adapters (e.g. ESPHome Bluetooth proxy) so connections go through proxy
    connectable_count = bluetooth.async_scanner_count(hass, connectable=True)
    if connectable_count == 0:
        _LOGGER.warning(
            "No connectable Bluetooth adapter found. Add an ESPHome Bluetooth proxy "
            "(or another connectable adapter) so the Octo bed can be reached."
        )

    # Resolve Bluetooth device from HA's bluetooth stack (uses proxy if it sees the bed)
    # Try multiple times in case device is still being discovered by proxy
    bleak_device = None
    for attempt in range(3):
        bleak_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if bleak_device:
            break
        if attempt < 2:
            _LOGGER.debug(
                "Device not found yet (attempt %d/3), waiting for proxy to discover...",
                attempt + 1,
            )
            await asyncio.sleep(2)  # Wait for proxy to discover device

    if not bleak_device:
        _LOGGER.error(
            "Could not find Octo bed at address %s after 3 attempts. "
            "Ensure the bed is powered on, in range of an ESPHome Bluetooth proxy "
            "(or connectable adapter), and try pressing a button on the remote to wake the bed. "
            "Check Home Assistant → Settings → Devices & Services → Bluetooth to verify "
            "the proxy is active and can see the bed.",
            address,
        )
        return False

    _LOGGER.info(
        "Found Octo bed device at %s (source: %s)",
        address,
        getattr(bleak_device, "source", "unknown"),
    )

    async def _get_device():
        """Get fresh device info (useful after reconnection)."""
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
        _LOGGER.error(
            "Failed to establish connection to Octo bed at %s. "
            "Check logs above for specific error details.",
            address,
        )
        return False

    _LOGGER.info("Octo bed integration setup complete for %s", address)

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
