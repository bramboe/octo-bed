"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ENTRY_IDS,
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_PAIR_WITH_ENTRY_ID,
    CONF_SHOW_CALIBRATION_BUTTONS,
    CONF_TYPE,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
    TYPE_COMBINED,
)
from .octo_bed_client import CombinedOctoBedClient, OctoBedClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.COVER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octo Bed from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if entry.data.get(CONF_TYPE) == TYPE_COMBINED:
        return await _async_setup_combined_entry(hass, entry)

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

    # If this entry was added with "pair with existing", create the combined config entry
    pair_with = entry.data.get(CONF_PAIR_WITH_ENTRY_ID)
    if pair_with:
        other_id = pair_with
        new_data = {k: v for k, v in entry.data.items() if k != CONF_PAIR_WITH_ENTRY_ID}
        hass.config_entries.async_update_entry(entry, data=new_data)
        combined_id = f"combined_{other_id}_{entry.entry_id}"
        combined_entry = ConfigEntry(
            version=1,
            minor_version=0,
            domain=DOMAIN,
            title="Both beds",
            data={CONF_TYPE: TYPE_COMBINED, CONF_ENTRY_IDS: [other_id, entry.entry_id]},
            options=dict(entry.options),
            source=entry.source,
            unique_id=combined_id,
            entry_id=combined_id,
        )
        hass.config_entries.async_add(combined_entry)

    return True


async def _async_setup_combined_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a combined (paired) entry that uses two existing bed clients."""
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

    entry_ids = entry.data.get(CONF_ENTRY_IDS, [])
    if len(entry_ids) != 2:
        _LOGGER.error("Combined entry must have exactly 2 entry_ids")
        return False
    client1 = hass.data.get(DOMAIN, {}).get(entry_ids[0])
    client2 = hass.data.get(DOMAIN, {}).get(entry_ids[1])
    if not client1 or not client2:
        _LOGGER.error("Combined entry: one or both bed clients not loaded yet")
        return False
    combined = CombinedOctoBedClient(client1, client2)
    hass.data[DOMAIN][entry.entry_id] = combined
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    client = hass.data[DOMAIN].get(entry.entry_id)
    if client and entry.data.get(CONF_TYPE) != TYPE_COMBINED:
        await client.disconnect()

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
