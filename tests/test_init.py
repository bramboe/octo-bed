"""Integration tests: config entry setup, reloads, groups and cleanup."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octo_bed.const import (
    CONF_FEATURES,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    DOMAIN,
)
from custom_components.octo_bed.group_client import GroupOctoBedClient
from custom_components.octo_bed.octo_bed_client import OctoBedClient

from .fake_bed import FakeBed, memory_slots_feature
from .test_client_connection import wait_for

OPTIONS = {
    "head_full_travel_seconds": 30,
    "feet_full_travel_seconds": 30,
    "show_calibration_buttons": True,
}


class FakeBluetooth:
    """Stands in for Home Assistant's bluetooth integration."""

    def __init__(self) -> None:
        self.devices: dict[str, Any] = {}
        self.present: dict[str, bool] = {}
        self.callbacks: dict[str, list[Callable[..., None]]] = {}

    def add(self, bed: FakeBed, present: bool = True) -> None:
        self.devices[bed.address] = bed.device
        self.present[bed.address] = present

    def advertise(self, address: str) -> None:
        self.present[address] = True
        for listener in list(self.callbacks.get(address, [])):
            listener(None, bluetooth.BluetoothChange.ADVERTISEMENT)

    def ble_device_from_address(self, _hass: Any, address: str, connectable: bool = True) -> Any:
        return self.devices.get(address) if self.present.get(address) else None

    def address_present(self, _hass: Any, address: str, connectable: bool = True) -> bool:
        return self.present.get(address, False)

    def register_callback(
        self, _hass: Any, listener: Callable[..., None], matcher: Any, _mode: Any
    ) -> Callable[[], None]:
        address = matcher["address"]
        self.callbacks.setdefault(address, []).append(listener)
        return lambda: self.callbacks[address].remove(listener)


@pytest.fixture
def fake_bluetooth(hass: HomeAssistant, backend: Any) -> Generator[FakeBluetooth]:
    fake = FakeBluetooth()
    # The real bluetooth stack is not needed; the functions below are all the
    # integration uses at runtime.
    hass.config.components.update({"bluetooth", "bluetooth_adapters"})
    with (
        patch.object(bluetooth, "async_ble_device_from_address", fake.ble_device_from_address),
        patch.object(bluetooth, "async_address_present", fake.address_present),
        patch.object(bluetooth, "async_register_callback", fake.register_callback),
        patch.object(bluetooth, "async_scanner_devices_by_address", lambda *a, **k: []),
    ):
        yield fake


def bed_entry(hass: HomeAssistant, bed: FakeBed, title: str, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=bed.address.replace(":", ""),
        data={"address": bed.address, "pin": "1234", **data},
        options=dict(OPTIONS),
    )
    entry.add_to_hass(hass)
    return entry


def client_of(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return hass.data[DOMAIN][entry.entry_id]


async def test_bed_connects_and_entities_follow(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, bed: FakeBed
) -> None:
    fake_bluetooth.add(bed)
    entry = bed_entry(hass, bed, "Bram")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    client: OctoBedClient = client_of(hass, entry)

    await wait_for(client.is_connected)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bram_connection_status").state == "connected"
    assert hass.states.get("cover.bram_head").state == "closed"

    seen: list[str] = []

    @callback
    def _record(event: Event) -> None:
        seen.append(event.data["new_state"].state)

    async_track_state_change_event(hass, ["cover.bram_head"], _record)
    bed.drop()
    await wait_for(lambda: seen[-2:] == ["unavailable", "closed"], timeout=5.0)


async def test_setup_does_not_wait_for_an_absent_bed(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, bed: FakeBed
) -> None:
    fake_bluetooth.add(bed, present=False)
    entry = bed_entry(hass, bed, "Bram")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.bram_connection_status").state == "disconnected"
    assert bed.connect_calls == 0

    fake_bluetooth.advertise(bed.address)
    await wait_for(client_of(hass, entry).is_connected, timeout=1.0)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.bram_connection_status").state == "connected"


async def test_unload_closes_client_and_releases_callbacks(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, bed: FakeBed
) -> None:
    fake_bluetooth.add(bed)
    entry = bed_entry(hass, bed, "Bram")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    client: OctoBedClient = client_of(hass, entry)
    await wait_for(client.is_connected)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not client.is_connected()
    assert bed.current is None
    assert client._connection_callbacks == []
    assert client._position_callbacks == []
    assert client._calibration_state_callbacks == []
    assert fake_bluetooth.callbacks[bed.address] == []


async def test_group_follows_a_reloaded_member(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    bed: FakeBed,
    second_bed: FakeBed,
) -> None:
    fake_bluetooth.add(bed)
    fake_bluetooth.add(second_bed)
    bram = bed_entry(hass, bed, "Bram")
    ianthe = bed_entry(hass, second_bed, "Ianthe")
    group = MockConfigEntry(
        domain=DOMAIN,
        title="Both beds",
        unique_id="group",
        data={CONF_IS_GROUP: True, CONF_MEMBER_ENTRY_IDS: [ianthe.entry_id, bram.entry_id]},
        options=dict(OPTIONS),
    )
    group.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert group.state is ConfigEntryState.LOADED
    await wait_for(lambda: client_of(hass, group).is_connected())

    # Reloading one bed (options change, update, ...) replaces its client;
    # the pair must follow instead of controlling the old, closed client.
    assert await hass.config_entries.async_reload(ianthe.entry_id)
    await hass.async_block_till_done()
    await wait_for(
        lambda: hass.states.get("sensor.both_beds_connection_status").state
        == "connected"
    )
    group_client: GroupOctoBedClient = client_of(hass, group)
    assert group_client.has_member(client_of(hass, ianthe))


async def test_discovered_capabilities_are_stored_and_applied(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, bed: FakeBed
) -> None:
    bed.capabilities.insert(0, memory_slots_feature(2))
    fake_bluetooth.add(bed)
    entry = bed_entry(hass, bed, "Bram")
    uid = entry.unique_id
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    # Before the bed ever connected: software presets.
    assert registry.async_get_entity_id("button", DOMAIN, f"{uid}_soft_preset_1")

    await wait_for(lambda: CONF_FEATURES in entry.data)
    await hass.async_block_till_done()
    assert entry.data[CONF_FEATURES]["memory_slots"] == 2
    # The entry reloaded and now has the bed's hardware presets.
    await wait_for(lambda: registry.async_get_entity_id("button", DOMAIN, f"{uid}_preset_2") is not None)
    await hass.async_block_till_done()
    assert client_of(hass, entry).memory_slot_count == 2


async def test_same_capabilities_do_not_reload(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth, bed: FakeBed
) -> None:
    fake_bluetooth.add(bed)
    entry = bed_entry(hass, bed, "Bram")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    client = client_of(hass, entry)

    await wait_for(lambda: CONF_FEATURES in entry.data)
    await hass.async_block_till_done()
    assert client_of(hass, entry) is client


async def test_sync_buttons_exist_on_both_beds(
    hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    bed: FakeBed,
    second_bed: FakeBed,
) -> None:
    """Regression: a bed that was not in range at startup got no sync button.

    Its setup was retried later, so the bed that did load never offered to
    sync to it.
    """
    fake_bluetooth.add(bed)
    fake_bluetooth.add(second_bed, present=False)
    bram = bed_entry(hass, bed, "Bram")
    ianthe = bed_entry(hass, second_bed, "Ianthe")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "button", DOMAIN, f"{bram.unique_id}_sync_to_{ianthe.entry_id}"
    )
    assert registry.async_get_entity_id(
        "button", DOMAIN, f"{ianthe.unique_id}_sync_to_{bram.entry_id}"
    )
