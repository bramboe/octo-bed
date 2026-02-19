"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

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

_WAIT_FOR_DEVICE_SEC = 30.0


def _get_ble_device(hass: HomeAssistant, address: str):
    """Get BLEDevice for address (try connectable=True, then connectable=False for proxy)."""
    device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if device is None:
        device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=False
        )
    return device


def _address_present(hass: HomeAssistant, address: str) -> bool:
    """True if any adapter (including Bluetooth proxy) has seen this address."""
    return (
        bluetooth.async_address_present(hass, address, connectable=True)
        or bluetooth.async_address_present(hass, address, connectable=False)
    )


async def _wait_for_ble_device(
    hass: HomeAssistant, address: str, max_sec: float = _WAIT_FOR_DEVICE_SEC
):
    """Wait for the BLE device to appear (bed may advertise intermittently)."""
    import asyncio

    addr = (address or "").strip()
    if not addr:
        return None
    deadline = hass.loop.time() + max_sec
    while hass.loop.time() < deadline:
        device = _get_ble_device(hass, addr)
        if device:
            return device
        await asyncio.sleep(1.0)
    return None


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

    # Check if device is seen by any Bluetooth adapter (proxy or local)
    if not _address_present(hass, address):
        _LOGGER.warning(
            "Octo bed at %s not seen by any Bluetooth adapter. "
            "Ensure the bed is powered on and in range. With a proxy, try pressing a button on the remote to wake the bed.",
            address,
        )

    # Resolve Bluetooth device (try connectable, then non-connectable for proxy)
    bleak_device = _get_ble_device(hass, address)
    if not bleak_device:
        _LOGGER.info("Waiting up to %.0fs for Octo bed at %s to appear...", _WAIT_FOR_DEVICE_SEC, address)
        bleak_device = await _wait_for_ble_device(hass, address)
    if not bleak_device:
        _LOGGER.error(
            "Could not find Octo bed at %s. Use the bed base BLE address (e.g. F6:21:DD:DD:6F:19), not the remote.",
            address,
        )
        return False

    async def _get_device():
        return _get_ble_device(hass, address)

    client = OctoBedClient(
        bleak_device,
        pin,
        disconnect_callback=lambda: _LOGGER.warning("Octo bed disconnected"),
        device_resolver=_get_device,
    )

    if not await client.connect():
        _LOGGER.error("Failed to connect to Octo bed at %s", address)
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
