"""Cover entities for Octo Bed (head, feet, both)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FULL_TRAVEL_SECONDS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
    CMD_STOP,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed covers from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Octo Bed",
        manufacturer="Octo",
    )

    covers = [
        OctoBedCover(client, "head", "Head", device_info, entry),
        OctoBedCover(client, "feet", "Feet", device_info, entry),
        OctoBedCover(client, "both", "Both", device_info, entry),
    ]

    async_add_entities(covers)


class OctoBedCover(CoverEntity):
    """Representation of an Octo Bed cover (head, feet, or both)."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        client: OctoBedClient,
        cover_type: str,
        name: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the cover."""
        self._client = client
        self._cover_type = cover_type
        self._attr_name = name
        self._attr_unique_id = f"octo_bed_cover_{cover_type}"
        self._attr_device_info = device_info
        self._entry = entry
        self._target_position: int | None = None
        self._move_task: asyncio.Task[None] | None = None
        self._attr_is_closed = True  # 0% = closed
        self._current_command: str | None = None  # Track which command is currently being sent
        # Register for position updates
        self._client.register_position_callback(self._on_position_changed)

    @property
    def current_cover_position(self) -> int | None:
        """Return current position (0 = down, 100 = up) from shared state."""
        if self._cover_type == "head":
            return self._client.get_head_position()
        elif self._cover_type == "feet":
            return self._client.get_feet_position()
        else:  # both
            return self._client.get_both_position()

    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        current = self.current_cover_position or 0
        return (
            self._move_task is not None
            and self._target_position is not None
            and self._target_position < current
        )

    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        current = self.current_cover_position or 0
        return (
            self._move_task is not None
            and self._target_position is not None
            and self._target_position > current
        )

    def _on_position_changed(self, part: str, position: int) -> None:
        """Callback when position changes in shared state."""
        # Update our display if this change affects us
        if (self._cover_type == "head" and part == "head") or \
           (self._cover_type == "feet" and part == "feet") or \
           (self._cover_type == "both" and part in ("head", "feet")):
            self._attr_is_closed = position == 0
            self.async_write_ha_state()

    def _get_up_command(self) -> str:
        """Get the up command method name for this cover type."""
        if self._cover_type == "head":
            return "head_up"
        if self._cover_type == "feet":
            return "feet_up"
        return "both_up"

    def _get_down_command(self) -> str:
        """Get the down command method name for this cover type."""
        if self._cover_type == "head":
            return "head_down"
        if self._cover_type == "feet":
            return "feet_down"
        return "both_down"

    def _get_full_travel_seconds(self) -> int:
        """Get full travel seconds from options."""
        return self._entry.options.get(
            CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS
        )

    async def _async_move_to_position(self, target: int) -> None:
        """Move cover to target position (0-100)."""
        # Get current position from shared state
        if self._cover_type == "head":
            current = self._client.get_head_position()
        elif self._cover_type == "feet":
            current = self._client.get_feet_position()
        else:  # both
            current = self._client.get_both_position()
        
        if target == current:
            return
        
        # Check if this movement was cancelled due to conflict
        if self._move_task and self._move_task.cancelled():
            return

        full_travel = self._get_full_travel_seconds()
        duration: float
        up: bool
        if target > current:
            up = True
            duration = (target - current) / 100.0 * full_travel
        else:
            up = False
            duration = (current - target) / 100.0 * full_travel

        cmd = self._get_up_command() if up else self._get_down_command()
        method = getattr(self._client, cmd, None)
        if not method or not callable(method):
            _LOGGER.error("Unknown command %s for cover %s", cmd, self._cover_type)
            return

        # Ensure we're only sending one command type at a time
        if self._current_command is not None and self._current_command != cmd:
            _LOGGER.warning("Cover %s: Command conflict detected! Was sending %s, now trying %s", 
                          self._cover_type, self._current_command, cmd)
        
        self._current_command = cmd
        _LOGGER.debug("Cover %s: Moving %s using command %s", self._cover_type, "up" if up else "down", cmd)

        # Continuously send movement command for the required duration and
        # update the visual position based on elapsed time.
        # Use a longer delay (0.375s) to match the bed's command interval from packet captures
        start_time = time.monotonic()
        end_time = start_time + duration
        cancelled = False
        try:
            while time.monotonic() < end_time:
                # Check if this movement was cancelled due to conflict
                if self._move_task and self._move_task.cancelled():
                    cancelled = True
                    break
                # Double-check we're still supposed to send this command
                if self._current_command != cmd:
                    _LOGGER.warning("Cover %s: Command changed during movement, stopping", self._cover_type)
                    cancelled = True
                    break
                await method()
                # Update current position proportionally to elapsed time so HA shows progress
                now = time.monotonic()
                elapsed = now - start_time
                frac = max(0.0, min(1.0, elapsed / duration)) if duration > 0 else 1.0
                new_pos = int(round(current + (target - current) * frac))
                
                # Update shared state based on cover type
                if self._cover_type == "head":
                    self._client.set_head_position(new_pos)
                elif self._cover_type == "feet":
                    self._client.set_feet_position(new_pos)
                else:  # both
                    self._client.set_both_position(new_pos)
                
                self._attr_is_closed = new_pos == 0
                self.async_write_ha_state()
                await asyncio.sleep(0.375)  # Match bed's natural command interval (~375ms from captures)
        except asyncio.CancelledError:
            # Stop requested (either user stop, stop button, or new target)
            cancelled = True
            # Send stop command to bed
            try:
                await self._client._send_command(CMD_STOP)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to send stop after cover move cancelled", exc_info=True)
            raise
        finally:
            # Update position to final position (either target if completed, or current progress if cancelled)
            if cancelled:
                # Update to current progress position
                now = time.monotonic()
                elapsed = now - start_time
                frac = max(0.0, min(1.0, elapsed / duration)) if duration > 0 else 0.0
                final_pos = int(round(current + (target - current) * frac))
            else:
                # Reached target
                final_pos = target
                try:
                    await self._client._send_command(CMD_STOP)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Failed to send stop after cover move", exc_info=True)

            if not self._move_task or self._move_task.cancelled():
                # Already cancelled, just update position
                if self._cover_type == "head":
                    self._client.set_head_position(final_pos)
                elif self._cover_type == "feet":
                    self._client.set_feet_position(final_pos)
                else:  # both
                    self._client.set_both_position(final_pos)
                self._target_position = None
                self._move_task = None
                self._current_command = None
                self._attr_is_closed = final_pos == 0
                self.async_write_ha_state()
                return

            # Snap to exact target at the end of the move and update shared state
            if self._cover_type == "head":
                self._client.set_head_position(final_pos)
            elif self._cover_type == "feet":
                self._client.set_feet_position(final_pos)
            else:  # both
                self._client.set_both_position(final_pos)
            
            self._target_position = None
            self._move_task = None
            self._current_command = None
            self._attr_is_closed = final_pos == 0
            self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (move to 100%)."""
        await self.async_set_cover_position(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (move to 0%)."""
        await self.async_set_cover_position(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position (0-100)."""
        position = kwargs.get("position", 0)
        if position < 0 or position > 100:
            return

        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
            try:
                await self._move_task
            except asyncio.CancelledError:
                pass

        self._target_position = position
        self._move_task = asyncio.create_task(self._async_move_to_position(position))
        self._client.register_movement_task(self._move_task)
        # Register which part is moving to prevent conflicts
        self._client.register_active_movement(self._cover_type, self._move_task)
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
            try:
                await self._move_task
            except asyncio.CancelledError:
                pass
        await self._client.stop()
        self._move_task = None
        self._target_position = None
        self._current_command = None
        self.async_write_ha_state()
