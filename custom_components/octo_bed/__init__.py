"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_BEDS,
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_PAIRED,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import CombinedOctoBedClient, OctoBedClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.COVER,
    Platform.SENSOR,
]


def _normalize_address(addr: str) -> str:
    """Normalize address to no-colon form for device identifiers."""
    return addr.upper().replace(":", "")


def get_device_configs(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[tuple[OctoBedClient | CombinedOctoBedClient, DeviceInfo, str]]:
    """Return list of (client, device_info, device_suffix) for this entry (1 or 3 devices)."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data is None:
        return []
    if isinstance(data, OctoBedClient):
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title or "Octo Bed",
            manufacturer="Octo",
        )
        return [(data, device_info, entry.unique_id or entry.entry_id)]
    if isinstance(data, dict) and data.get("paired"):
        beds = data["beds"]
        clients = data["clients"]
        combined = data["combined"]
        suffix1 = _normalize_address(beds[0]["address"])
        suffix2 = _normalize_address(beds[1]["address"])
        suffix_combined = f"paired_{entry.entry_id}"
        return [
            (clients[0], DeviceInfo(identifiers={(DOMAIN, suffix1)}, name=beds[0]["name"], manufacturer="Octo"), suffix1),
            (clients[1], DeviceInfo(identifiers={(DOMAIN, suffix2)}, name=beds[1]["name"], manufacturer="Octo"), suffix2),
            (combined, DeviceInfo(identifiers={(DOMAIN, suffix_combined)}, name=entry.title or "Both beds", manufacturer="Octo"), suffix_combined),
        ]
    return []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octo Bed from a config entry (single or paired)."""
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

    if entry.data.get(CONF_PAIRED) and entry.data.get(CONF_BEDS):
        return await _setup_paired_entry(hass, entry)
    return await _setup_single_entry(hass, entry)


async def _setup_single_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a single bed."""
    address = entry.data["address"]
    pin = entry.data["pin"]
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


async def _setup_paired_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a paired (two-bed) entry: two clients + combined, three logical devices."""
    beds = entry.data[CONF_BEDS]
    if len(beds) != 2:
        _LOGGER.error("Paired entry must have exactly 2 beds")
        return False

    clients: list[OctoBedClient] = []
    for i, bed in enumerate(beds):
        address = bed["address"]
        pin = bed["pin"]
        bleak_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if not bleak_device:
            _LOGGER.error("Could not find Octo bed at address %s", address)
            if clients:
                for c in clients:
                    await c.disconnect()
            return False

        def _make_resolver(addr: str):
            async def _resolver():
                return bluetooth.async_ble_device_from_address(
                    hass, addr, connectable=True
                )
            return _resolver

        client = OctoBedClient(
            bleak_device,
            pin,
            disconnect_callback=lambda: _LOGGER.warning("Octo bed disconnected"),
            device_resolver=_make_resolver(address),
        )
        if not await client.connect():
            _LOGGER.error("Failed to connect to Octo bed at %s", address)
            for c in clients:
                await c.disconnect()
            return False
        clients.append(client)

    combined = CombinedOctoBedClient(clients[0], clients[1])
    hass.data[DOMAIN][entry.entry_id] = {
        "paired": True,
        "clients": clients,
        "combined": combined,
        "beds": beds,
        "entry": entry,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data is None:
        pass
    elif isinstance(data, dict) and data.get("paired"):
        for client in data.get("clients", []):
            await client.disconnect()
    elif isinstance(data, OctoBedClient):
        await data.disconnect()

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
