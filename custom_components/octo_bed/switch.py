"""Switch entities for Octo Bed (light + movement control)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS, DOMAIN, CMD_STOP
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed switches from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Octo Bed",
        manufacturer="Octo",
    )

    full_travel_seconds = entry.options.get(
        CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS
    )

    entities: list[SwitchEntity] = [
        OctoBedBothToggleSwitch(
            client, device_info, full_travel_seconds
        ),
        OctoBedMovementSwitch(
            client, "head_up", "Head Up", "mdi:arrow-up", device_info, full_travel_seconds
        ),
        OctoBedMovementSwitch(
            client, "head_down", "Head Down", "mdi:arrow-down", device_info, full_travel_seconds
        ),
        OctoBedMovementSwitch(
            client, "feet_up", "Feet Up", "mdi:arrow-up", device_info, full_travel_seconds
        ),
        OctoBedMovementSwitch(
            client, "feet_down", "Feet Down", "mdi:arrow-down", device_info, full_travel_seconds
        ),
        OctoBedLightSwitch(client, device_info),
    ]

    async_add_entities(entities)


class OctoBedLightSwitch(SwitchEntity):
    """Representation of Octo Bed under-bed light switch."""

    _attr_has_entity_name = True
    _attr_name = "Light"
    _attr_icon = "mdi:lightbulb"
    _attr_unique_id = "octo_bed_light"

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo) -> None:
        """Initialize the light switch."""
        self._client = client
        self._attr_device_info = device_info
        self._is_on: bool | None = None  # Unknown state initially

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        if await self._client.light_on():
            self._is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        if await self._client.light_off():
            self._is_on = False
            self.async_write_ha_state()


class OctoBedBothToggleSwitch(SwitchEntity):
    """Representation of a toggle switch for both bed sides (up/down)."""

    _attr_has_entity_name = True
    _attr_name = "Both"
    _attr_icon = "mdi:bed"
    _attr_unique_id = "octo_bed_both"

    def __init__(
        self,
        client: OctoBedClient,
        device_info: DeviceInfo,
        full_travel_seconds: int,
    ) -> None:
        """Initialize the both toggle switch."""
        self._client = client
        self._attr_device_info = device_info
        self._is_on: bool = False  # False = down, True = up
        self._full_travel_seconds = full_travel_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def is_on(self) -> bool:
        """Return true if the bed is up (on) or down (off)."""
        # If there's an active task, use the task state
        if self._task and not self._task.done():
            return self._is_on
        # Otherwise, check actual position from shared state
        # Consider "on" if position is > 50%
        return self._client.get_both_position() > 50

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Move both sides up."""
        # Cancel any existing movement
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            await self._client.stop()

        self._is_on = True
        self.async_write_ha_state()

        self._task = asyncio.create_task(self._movement_loop(True))
        self._client.register_movement_task(self._task)
        self._client.register_active_movement("both", self._task)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Move both sides down."""
        # Cancel any existing movement
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            await self._client.stop()

        self._is_on = False
        self.async_write_ha_state()

        self._task = asyncio.create_task(self._movement_loop(False))
        self._client.register_movement_task(self._task)
        self._client.register_active_movement("both", self._task)

    async def _movement_loop(self, going_up: bool) -> None:
        """Continuously send movement commands until switched off or full travel reached."""
        method = self._client.both_up if going_up else self._client.both_down
        
        # Get current position from shared state
        start_position = self._client.get_both_position()
        target_position = 100 if going_up else 0
        position_delta = target_position - start_position
        
        end_time = time.monotonic() + self._full_travel_seconds
        start_time = time.monotonic()
        cancelled = False
        try:
            while time.monotonic() < end_time:
                # Check if this movement was cancelled due to conflict
                if self._task and self._task.cancelled():
                    cancelled = True
                    break
                await method()
                
                # Update position based on elapsed time
                elapsed = time.monotonic() - start_time
                progress = min(1.0, elapsed / self._full_travel_seconds)
                new_position = int(round(start_position + position_delta * progress))
                self._client.set_both_position(new_position)
                
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            # Turned off by user or stop button
            cancelled = True
        finally:
            # Update position based on how far we got
            elapsed = time.monotonic() - start_time
            progress = min(1.0, elapsed / self._full_travel_seconds)
            final_position = int(round(start_position + position_delta * progress))
            self._client.set_both_position(final_position)
            
            # If we reached full-travel time (not cancelled), send stop command.
            if not cancelled:
                try:
                    await self._client._send_command(CMD_STOP)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Failed to send stop after switch move", exc_info=True)

            # Reset state when task completes
            self._task = None
            self.async_write_ha_state()


class OctoBedMovementSwitch(SwitchEntity):
    """Representation of an Octo Bed movement switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        client: OctoBedClient,
        action: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        full_travel_seconds: int,
    ) -> None:
        """Initialize the movement switch."""
        self._client = client
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"octo_bed_move_{action}"
        self._attr_device_info = device_info
        self._is_on: bool = False
        self._full_travel_seconds = full_travel_seconds
        self._task: asyncio.Task[None] | None = None

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
        if "head" in self._action:
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

        # Determine which part we're moving and get current position
        going_up = "up" in self._action
        if "head" in self._action:
            start_position = self._client.get_head_position()
            target_position = 100 if going_up else 0
            position_setter = self._client.set_head_position
        elif "feet" in self._action:
            start_position = self._client.get_feet_position()
            target_position = 100 if going_up else 0
            position_setter = self._client.set_feet_position
        else:
            # Unknown action, can't track position
            start_position = 0
            target_position = 100 if going_up else 0
            position_setter = None
        
        position_delta = target_position - start_position
        end_time = time.monotonic() + self._full_travel_seconds
        start_time = time.monotonic()
        cancelled = False
        try:
            while time.monotonic() < end_time:
                # Check if this movement was cancelled due to conflict
                if self._task and self._task.cancelled():
                    cancelled = True
                    break
                await method()
                
                # Update position based on elapsed time
                if position_setter:
                    elapsed = time.monotonic() - start_time
                    progress = min(1.0, elapsed / self._full_travel_seconds)
                    new_position = int(round(start_position + position_delta * progress))
                    position_setter(new_position)
                
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            # Turned off by user or stop button
            cancelled = True
        finally:
            # Update position based on how far we got
            if position_setter:
                elapsed = time.monotonic() - start_time
                progress = min(1.0, elapsed / self._full_travel_seconds)
                final_position = int(round(start_position + position_delta * progress))
                position_setter(final_position)
            
            # If we reached full-travel time (not cancelled), send stop command.
            if not cancelled:
                try:
                    await self._client._send_command(CMD_STOP)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Failed to send stop after switch move", exc_info=True)

            # Reset state when task completes
            self._is_on = False
            self._task = None
            self.async_write_ha_state()
