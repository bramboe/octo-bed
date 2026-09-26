"""Octo Bed integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_CALIBRATE_ON_ADD,
    CONF_FEATURES,
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_GROUP_OPTIONS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_PAIR_CALIBRATE,
    CONF_PAIR_WITH_ENTRY_ID,
    CONF_PROXY_SOURCE,
    CONF_SHOW_CALIBRATION_BUTTONS,
    CONF_SOFT_PRESETS,
    CONNECT_GATE_KEY,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
    PROXY_SOURCE_AUTO,
    SIGNAL_BED_UPDATE,
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

# How long a freshly added bed may take to connect before the calibration
# that was requested in the add flow is given up.
CALIBRATE_ON_ADD_CONNECT_TIMEOUT = 120.0
STOP_TIMEOUT = 10.0
CAPABILITY_RELOADS_KEY = f"{DOMAIN}_capability_reloads"


@callback
def _async_resolve_ble_device(
    hass: HomeAssistant, address: str, source: str | None
) -> BLEDevice | None:
    """Return a BLEDevice for the bed, pinned to a specific proxy when requested.

    When ``source`` names a specific Bluetooth proxy/adapter (its scanner
    source MAC), only that scanner is used, so Home Assistant will not move the
    connection onto a different (e.g. more distant) proxy. Falls back to the
    default best-path selection when the pinned proxy currently cannot reach
    the bed, or when no source is pinned (PROXY_SOURCE_AUTO / unset).
    """
    if source and source != PROXY_SOURCE_AUTO:
        for scanner_device in bluetooth.async_scanner_devices_by_address(
            hass, address, connectable=True
        ):
            if scanner_device.scanner.source == source:
                return scanner_device.ble_device
        _LOGGER.debug(
            "Pinned Bluetooth proxy %s cannot currently reach Octo bed %s; "
            "falling back to automatic proxy selection",
            source,
            address,
        )
    return bluetooth.async_ble_device_from_address(hass, address, connectable=True)


def _entity_shape(capabilities: dict[str, Any] | None) -> tuple[int, bool, bool]:
    """The capabilities that decide which entities a bed gets."""
    caps = capabilities or {}
    return (
        int(caps.get("memory_slots") or 0),
        bool(caps.get("has_rgbwi_light")),
        bool(caps.get("has_synchro")),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octo Bed from a config entry (single bed or group)."""
    hass.data.setdefault(DOMAIN, {})
    if entry.data.get(CONF_IS_GROUP):
        return await _async_setup_group(hass, entry)
    return await _async_setup_bed(hass, entry)


async def _async_setup_group(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the 'Both beds' device on top of the member beds' clients."""
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
    domain_data = hass.data[DOMAIN]
    missing = [eid for eid in member_ids if not isinstance(domain_data.get(eid), OctoBedClient)]
    if missing:
        raise ConfigEntryNotReady(f"Waiting for member beds to finish setup: {missing}")
    domain_data[entry.entry_id] = GroupOctoBedClient(
        [domain_data[eid] for eid in member_ids]
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_migrate_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Make sure options written by older versions have every key."""
    opts = dict(entry.options) if entry.options else {}
    if not opts:
        opts[CONF_FULL_TRAVEL_SECONDS] = DEFAULT_FULL_TRAVEL_SECONDS
    default_travel = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
    opts.setdefault(CONF_HEAD_FULL_TRAVEL_SECONDS, default_travel)
    opts.setdefault(CONF_FEET_FULL_TRAVEL_SECONDS, default_travel)
    opts.setdefault(CONF_SHOW_CALIBRATION_BUTTONS, True)
    if opts != (entry.options or {}):
        hass.config_entries.async_update_entry(entry, options=opts)


async def _async_setup_bed(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one bed.

    Never waits for the bed: entities are created right away (unavailable
    until connected) and the client's connection manager connects as soon as
    the bed is in range, so a sleeping or unreachable bed can never hold up
    Home Assistant's startup.
    """
    _async_migrate_options(hass, entry)
    address: str = entry.data["address"].upper()
    pin: str = entry.data["pin"]

    @callback
    def _resolve() -> BLEDevice | None:
        # Re-read the option every time so a changed proxy pin takes effect on
        # the next connection attempt.
        return _async_resolve_ble_device(
            hass, address, entry.options.get(CONF_PROXY_SOURCE, PROXY_SOURCE_AUTO)
        )

    @callback
    def _present() -> bool:
        return bluetooth.async_address_present(hass, address, connectable=True)

    @callback
    def _on_features(capabilities: dict[str, Any]) -> None:
        stored = entry.data.get(CONF_FEATURES)
        if stored == capabilities:
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_FEATURES: capabilities}
        )
        if _entity_shape(stored) == _entity_shape(capabilities):
            return
        # At most one capability reload per bed per Home Assistant run, so a
        # bed that reports differently on every connection cannot cause a
        # reload loop.
        reloaded: set[str] = hass.data.setdefault(CAPABILITY_RELOADS_KEY, set())
        if entry.entry_id in reloaded:
            _LOGGER.warning(
                "Capabilities of Octo bed %s changed again; restart Home Assistant "
                "to update its entities",
                entry.title,
            )
            return
        reloaded.add(entry.entry_id)
        _LOGGER.info(
            "Capabilities of Octo bed %s changed; reloading to update its entities",
            entry.title,
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    client = OctoBedClient(
        _resolve(),
        pin,
        address=address,
        name=entry.title or "Octo Bed",
        device_resolver=_resolve,
        presence_checker=_present,
        connect_gate=hass.data.setdefault(CONNECT_GATE_KEY, asyncio.Lock()),
        features_callback=_on_features,
    )
    client.load_capabilities(entry.data.get(CONF_FEATURES))
    hass.data[DOMAIN][entry.entry_id] = client

    # Other devices (sync buttons on the other bed) follow this bed's changes
    @callback
    def _dispatch_update(*_args: Any) -> None:
        async_dispatcher_send(hass, SIGNAL_BED_UPDATE, entry.entry_id)

    entry.async_on_unload(client.register_position_callback(_dispatch_update))
    entry.async_on_unload(client.register_connection_callback(_dispatch_update))

    # After adding a 2nd bed with "pair": create the group entry via an import flow
    pair_with = entry.data.get(CONF_PAIR_WITH_ENTRY_ID)
    if pair_with:
        _async_start_group_flow(hass, entry, pair_with)
        new_data = {
            k: v
            for k, v in entry.data.items()
            if k not in (CONF_PAIR_WITH_ENTRY_ID, CONF_PAIR_CALIBRATE)
        }
        hass.config_entries.async_update_entry(entry, data=new_data)

    # Strip the one-shot calibration flag first so a reload or restart never
    # re-triggers the movement.
    calibrate_on_add = bool(entry.data.get(CONF_CALIBRATE_ON_ADD))
    if calibrate_on_add:
        hass.config_entries.async_update_entry(
            entry,
            data={k: v for k, v in entry.data.items() if k != CONF_CALIBRATE_ON_ADD},
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Wake the connection manager the moment the bed advertises again, e.g.
    # right after it dropped the connection or came back into range.
    @callback
    def _on_advertisement(
        _service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        client.async_on_advertisement()

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    client.start()

    if calibrate_on_add:
        down_seconds = entry.options.get(
            CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS
        )

        async def _async_calibrate_when_connected() -> None:
            if not await client.ensure_connected(timeout=CALIBRATE_ON_ADD_CONNECT_TIMEOUT):
                _LOGGER.warning(
                    "Could not start the initial calibration of %s: the bed is not "
                    "reachable. Use the calibration buttons once it is connected",
                    entry.title,
                )
                return
            _LOGGER.info(
                "Starting initial calibration (head) for %s as requested during setup",
                entry.title,
            )
            await client.start_calibration("head", down_seconds)

        entry.async_create_background_task(
            hass,
            _async_calibrate_when_connected(),
            f"octo_bed_initial_calibration_{entry.entry_id}",
        )

    # Drop the connection as soon as Home Assistant begins shutting down, so a
    # bed that is out of reach can never delay a restart.
    async def _async_on_stop(_event: Event) -> None:
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await client.async_close()
        except TimeoutError:
            _LOGGER.debug("Closing Octo bed %s on stop timed out", address)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop)
    )

    _async_refresh_groups(hass, entry.entry_id, client)
    return True


@callback
def _async_refresh_groups(
    hass: HomeAssistant, member_entry_id: str, client: OctoBedClient
) -> None:
    """Reload a loaded 'Both beds' group that still holds an old client of this bed.

    Happens when a member bed is reloaded (options change, integration update):
    without this the group would keep controlling the old, closed client and
    show the pair as disconnected until Home Assistant restarts.
    """
    for other in hass.config_entries.async_entries(DOMAIN):
        if not other.data.get(CONF_IS_GROUP):
            continue
        if member_entry_id not in (other.data.get(CONF_MEMBER_ENTRY_IDS) or []):
            continue
        if other.state is not ConfigEntryState.LOADED:
            continue
        group = hass.data.get(DOMAIN, {}).get(other.entry_id)
        if isinstance(group, GroupOctoBedClient) and group.has_member(client):
            continue
        _LOGGER.debug("Reloading '%s' to pick up the reloaded bed", other.title)
        hass.config_entries.async_schedule_reload(other.entry_id)


def _async_start_group_flow(
    hass: HomeAssistant, entry: ConfigEntry, pair_with: str
) -> None:
    """Start an import flow that creates the 'Both beds' group entry."""
    other = hass.config_entries.async_get_entry(pair_with)
    if not other or other.data.get(CONF_IS_GROUP):
        return

    # User choice from the pairing flow: calibrate both beds via the group device
    calibrate_both = bool(entry.data.get(CONF_PAIR_CALIBRATE, True))

    group_options = dict(other.options or {})
    if not group_options:
        group_options = {
            CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
            CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
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
    group_options[CONF_SHOW_CALIBRATION_BUTTONS] = calibrate_both
    # Soft presets and the proxy pin are per bed; never copy them to the group
    group_options.pop(CONF_SOFT_PRESETS, None)
    group_options.pop(CONF_PROXY_SOURCE, None)
    for member in (entry, other):
        merged = dict(member.options or {})
        merged[CONF_HEAD_FULL_TRAVEL_SECONDS] = head
        merged[CONF_FEET_FULL_TRAVEL_SECONDS] = feet
        merged[CONF_SHOW_CALIBRATION_BUTTONS] = calibrate_both
        hass.config_entries.async_update_entry(member, options=merged)

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
        # Closing the group is a no-op; member beds own their connections
        if stored is not None:
            await stored.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry.

    Removing a bed also removes any 'Both beds' group containing it. Removing
    the group reloads its member beds so their own calibration controls
    become active again.
    """
    removed_id = entry.entry_id
    if entry.data.get(CONF_IS_GROUP):
        for entry_id in entry.data.get(CONF_MEMBER_ENTRY_IDS) or []:
            if hass.config_entries.async_get_entry(entry_id) is not None:
                hass.config_entries.async_schedule_reload(entry_id)
        return
    for other in hass.config_entries.async_entries(DOMAIN):
        if not other.data.get(CONF_IS_GROUP):
            continue
        if removed_id in (other.data.get(CONF_MEMBER_ENTRY_IDS) or []):
            _LOGGER.info("Removing group entry 'Both beds' because a member bed was removed")
            await hass.config_entries.async_remove(other.entry_id)
            break
