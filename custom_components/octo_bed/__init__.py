"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, SOURCE_IGNORE, SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_GROUP_OPTIONS,
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
    Platform.LIGHT,
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
        for member_id in member_ids:
            member = hass.config_entries.async_get_entry(member_id)
            if member is None or member.source == SOURCE_IGNORE:
                _LOGGER.error(
                    "Group member %s is missing or an ignored discovery entry; "
                    "remove this 'Both beds' device and pair two configured beds",
                    member_id,
                )
                return False
        domain_data = hass.data.get(DOMAIN) or {}
        missing = [eid for eid in member_ids if eid not in domain_data]
        if missing:
            raise ConfigEntryNotReady(
                f"Waiting for member beds to finish setup: {missing}"
            )
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
        raise ConfigEntryNotReady(
            f"Could not find Octo bed at address {address}; "
            "ensure it is powered and in range of a Bluetooth adapter/proxy"
        )

    async def _get_device():
        return bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )

    client = OctoBedClient(
        bleak_device,
        pin,
        disconnect_callback=lambda: _LOGGER.warning(
            "Octo bed %s disconnected; reconnecting in background", address
        ),
        device_resolver=_get_device,
    )

    try:
        if not await client.connect():
            raise ConfigEntryNotReady(f"Failed to connect to Octo bed at {address}")
    except asyncio.CancelledError:
        raise  # allow HA to handle setup cancellation (e.g. reload during connect)

    # Best effort: query bed capabilities (memory presets, synchro, RGBW light)
    await client.discover_features()

    hass.data[DOMAIN][entry.entry_id] = client

    # After adding 2nd bed with "pair": create the group entry via an import flow
    pair_with = entry.data.get(CONF_PAIR_WITH_ENTRY_ID)
    if pair_with:
        _async_start_group_flow(hass, entry, pair_with)
        new_data = {k: v for k, v in entry.data.items() if k != CONF_PAIR_WITH_ENTRY_ID}
        hass.config_entries.async_update_entry(entry, data=new_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def _async_start_group_flow(
    hass: HomeAssistant, entry: ConfigEntry, pair_with: str
) -> None:
    """Start an import flow that creates the 'Both beds' group entry."""
    other = hass.config_entries.async_get_entry(pair_with)
    if not other or other.data.get(CONF_IS_GROUP):
        return

    group_options = dict(other.options or {})
    if not group_options:
        group_options = {
            CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
            CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
            CONF_SHOW_CALIBRATION_BUTTONS: False,
        }
    # Unify calibration: both beds get the same head/feet travel as the group
    head = group_options.get(
        CONF_HEAD_FULL_TRAVEL_SECONDS,
        group_options.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS),
    )
    feet = group_options.get(
        CONF_FEET_FULL_TRAVEL_SECONDS,
        group_options.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS),
    )
    group_options[CONF_HEAD_FULL_TRAVEL_SECONDS] = head
    group_options[CONF_FEET_FULL_TRAVEL_SECONDS] = feet
    current_opts = dict(entry.options or {})
    current_opts[CONF_HEAD_FULL_TRAVEL_SECONDS] = head
    current_opts[CONF_FEET_FULL_TRAVEL_SECONDS] = feet
    if CONF_SHOW_CALIBRATION_BUTTONS in group_options:
        current_opts[CONF_SHOW_CALIBRATION_BUTTONS] = group_options[CONF_SHOW_CALIBRATION_BUTTONS]
    hass.config_entries.async_update_entry(entry, options=current_opts)

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_IS_GROUP: True,
                CONF_MEMBER_ENTRY_IDS: [pair_with, entry.entry_id],
                CONF_GROUP_OPTIONS: group_options,
            },
        )
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data[DOMAIN].pop(entry.entry_id, None)
        # Group client disconnect is a no-op; member beds own their connections
        if stored is not None:
            await stored.disconnect()

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
