"""Switch entities for Octo Bed (movement control + synchro mode)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed switches from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]
    uid = entry.unique_id or entry.entry_id

    # The under-bed light moved to the light platform; drop the old switch entity
    ent_reg = er.async_get(hass)
    old_light = ent_reg.async_get_entity_id("switch", DOMAIN, f"{uid}_light")
    if old_light:
        _LOGGER.info("Removing legacy light switch entity %s (now a light entity)", old_light)
        ent_reg.async_remove(old_light)

    device_info = DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    entities: list[SwitchEntity] = [
        OctoBedMovementSwitch(
            client, "both_up", "Both Up", "mdi:arrow-up-bold", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "both_down", "Both Down", "mdi:arrow-down-bold", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "head_up", "Head Up", "mdi:arrow-up", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "head_down", "Head Down", "mdi:arrow-down", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "feet_up", "Feet Up", "mdi:arrow-up", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "feet_down", "Feet Down", "mdi:arrow-down", device_info, entry, uid
        ),
    ]

    # Linked/synchro drive mode (only on beds that report the capability)
    if client.has_synchro:
        entities.append(OctoBedSynchroSwitch(client, device_info, uid))

    async_add_entities(entities)


class OctoBedSynchroSwitch(SwitchEntity):
    """Toggle the bed's linked (synchro) drive mode."""

    _attr_has_entity_name = True
    _attr_name = "Synchro mode"
    _attr_icon = "mdi:link-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str
    ) -> None:
        """Initialize the synchro switch."""
        self._client = client
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_synchro"

    async def async_added_to_hass(self) -> None:
        """Register for connection updates."""
        await super().async_added_to_hass()
        self._client.register_connection_callback(self._on_connection_changed)

    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.is_connected()

    @property
    def is_on(self) -> bool | None:
        return self._client.synchro_active

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self._client.set_synchro_mode(True):
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self._client.set_synchro_mode(False):
            self.async_write_ha_state()


class OctoBedMovementSwitch(SwitchEntity):
    """Representation of an Octo Bed movement switch."""

    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(
        self,
        client: OctoBedClient,
        action: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the movement switch."""
        self._client = client
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{unique_id_prefix}_move_{action}"
        self._attr_device_info = device_info
        self._is_on: bool = False
        self._entry = entry
        self._task: asyncio.Task[None] | None = None

    def _travel_seconds(self) -> int:
        """Full travel seconds for this action, read live so calibration updates apply."""
        opts = self._entry.options
        default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        head = opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, default)
        feet = opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, default)
        if "both" in self._action:
            return max(head, feet)
        if "head" in self._action:
            return head
        return feet

    async def async_added_to_hass(self) -> None:
        """Register for calibration and connection updates."""
        await super().async_added_to_hass()
        self._client.register_calibration_state_callback(self._on_calibration_state_changed)
        self._client.register_connection_callback(self._on_connection_changed)

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self.async_write_ha_state()

    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        """Update availability when the connection state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available when connected and no calibration is active."""
        return self._client.is_connected() and not self._client.is_calibration_active()

    @property
    def is_on(self) -> bool:
        """Return true if the movement is active."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start movement in the configured direction."""
        if self._task and not self._task.done():
            return

        self._is_on = True
        self.async_write_ha_state()

        self._task = asyncio.create_task(self._movement_loop())
        self._client.register_movement_task(self._task)

        # Register which part is moving to prevent conflicts
        if "both" in self._action:
            self._client.register_active_movement("both", self._task)
        elif "head" in self._action:
            self._client.register_active_movement("head", self._task)
        elif "feet" in self._action:
            self._client.register_active_movement("feet", self._task)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop movement."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self._client.stop()
        self._is_on = False
        self._task = None
        self.async_write_ha_state()

    async def _movement_loop(self) -> None:
        """Continuously send movement commands until switched off or full travel reached."""
        method = getattr(self._client, self._action, None)
        if not method or not callable(method):
            _LOGGER.error("Unknown movement action %s", self._action)
            return

        going_up = "up" in self._action
        if "both" in self._action:
            start_position = self._client.get_both_position()
            position_setter = self._client.set_both_position
        elif "head" in self._action:
            start_position = self._client.get_head_position()
            position_setter = self._client.set_head_position
        else:  # feet
            start_position = self._client.get_feet_position()
            position_setter = self._client.set_feet_position

        target_position = 100 if going_up else 0
        position_delta = target_position - start_position
        full_travel = self._travel_seconds()
        end_time = time.monotonic() + full_travel
        start_time = time.monotonic()
        cancelled = False
        try:
            while time.monotonic() < end_time:
                if self._task and self._task.cancelled():
                    cancelled = True
                    break
                await method()

                elapsed = time.monotonic() - start_time
                progress = min(1.0, elapsed / full_travel)
                position_setter(int(round(start_position + position_delta * progress)))

                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            elapsed = time.monotonic() - start_time
            progress = min(1.0, elapsed / full_travel)
            position_setter(int(round(start_position + position_delta * progress)))

            # If we reached full-travel time (not cancelled), send stop command.
            if not cancelled:
                try:
                    await self._client.send_stop()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Failed to send stop after switch move", exc_info=True)

            self._is_on = False
            self._task = None
            self.async_write_ha_state()
