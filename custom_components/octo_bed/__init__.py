"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import logging
from types import MappingProxyType

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_PAIR_WITH_ENTRY_ID,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .group_client import GroupOctoBedClient
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.COVER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octo Bed from a config entry (single bed or group)."""
    hass.data.setdefault(DOMAIN, {})

    # Group entry: control both beds as one device
    if entry.data.get(CONF_IS_GROUP):
        member_ids = entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        if len(member_ids) < 2:
            _LOGGER.error("Group entry has fewer than 2 members")
            return False
        clients = []
        for eid in member_ids:
            if eid not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Group member %s not yet set up", eid)
                return False
            clients.append(hass.data[DOMAIN][eid])
        hass.data[DOMAIN][entry.entry_id] = GroupOctoBedClient(clients)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

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

    # After adding 2nd bed with "pair": create group entry then remove flag
    pair_with = entry.data.get(CONF_PAIR_WITH_ENTRY_ID)
    if pair_with:
        other = hass.config_entries.async_get_entry(pair_with)
        if other and not other.data.get(CONF_IS_GROUP):
            other_entry = hass.config_entries.async_get_entry(pair_with)
            group_options = other_entry.options if other_entry else {}
            if not group_options:
                group_options = {
                    CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                    CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                    CONF_SHOW_CALIBRATION_BUTTONS: False,
                }
            group_data = {
                CONF_IS_GROUP: True,
                CONF_MEMBER_ENTRY_IDS: [pair_with, entry.entry_id],
            }
            # ConfigEntry constructor added required args in newer HA (discovery_keys, minor_version, subentries_data)
            try:
                group_entry = ConfigEntry(
                    version=1,
                    minor_version=0,
                    domain=DOMAIN,
                    title="Both beds",
                    data=group_data,
                    options=group_options,
                    source=SOURCE_IMPORT,
                    unique_id=f"group_{pair_with}_{entry.entry_id}",
                    discovery_keys=MappingProxyType({}),
                    subentries_data=None,
                )
            except TypeError:
                group_entry = ConfigEntry(
                    version=1,
                    domain=DOMAIN,
                    title="Both beds",
                    data=group_data,
                    options=group_options,
                    source=SOURCE_IMPORT,
                    unique_id=f"group_{pair_with}_{entry.entry_id}",
                )
            hass.config_entries.async_add_entry(hass, group_entry)
        new_data = {k: v for k, v in entry.data.items() if k != CONF_PAIR_WITH_ENTRY_ID}
        hass.config_entries.async_update_entry(entry, data=new_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    stored = hass.data[DOMAIN].get(entry.entry_id)
    if isinstance(stored, GroupOctoBedClient):
        pass
    elif stored is not None:
        await stored.disconnect()

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
