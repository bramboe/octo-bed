"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import asyncio
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
        domain_data = hass.data.get(DOMAIN) or {}
        missing = [eid for eid in member_ids if eid not in domain_data]
        if missing:
            _LOGGER.debug(
                "Group entry waiting for members %s; will retry shortly",
                missing,
            )

            async def _retry_group_setup() -> None:
                await asyncio.sleep(10)
                await hass.config_entries.async_reload(entry.entry_id)

            hass.async_create_task(_retry_group_setup())
            return False
        clients = [hass.data[DOMAIN][eid] for eid in member_ids]
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

    # After adding 2nd bed with "pair": create group entry once both members are set up
    pair_with = entry.data.get(CONF_PAIR_WITH_ENTRY_ID)
    if pair_with:
        member_ids = [pair_with, entry.entry_id]
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
                CONF_MEMBER_ENTRY_IDS: member_ids,
            }
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

            async def _add_group_when_ready() -> None:
                domain_data = hass.data.get(DOMAIN) or {}
                for _ in range(60):  # up to 30 s at 0.5 s interval
                    if all(eid in domain_data for eid in member_ids):
                        await hass.config_entries.async_add(group_entry)
                        break
                    await asyncio.sleep(0.5)
                    domain_data = hass.data.get(DOMAIN) or {}
                else:
                    _LOGGER.warning(
                        "Group entry not added: members %s not ready in time",
                        member_ids,
                    )

            hass.async_create_task(_add_group_when_ready())
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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry. If it was a bed, remove any 'Both beds' group containing it.
    If it was the group, reload the two member beds so their calibration controls become active."""
    removed_id = entry.entry_id
    if entry.data.get(CONF_IS_GROUP):
        member_ids = entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        for entry_id in member_ids:
            try:
                await hass.config_entries.async_reload(entry_id)
            except Exception:  # noqa: BLE001
                pass
        return
    for other in hass.config_entries.async_entries(DOMAIN):
        if not other.data.get(CONF_IS_GROUP):
            continue
        member_ids = other.data.get(CONF_MEMBER_ENTRY_IDS) or []
        if removed_id in member_ids:
            _LOGGER.info("Removing group entry 'Both beds' because a member bed was removed")
            await hass.config_entries.async_remove(other.entry_id)
            break
