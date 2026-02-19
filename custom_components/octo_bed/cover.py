"""Cover entities for Octo Bed (head, feet, both)."""

from __future__ import annotations

import asyncio
import logging
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
    MOVEMENT_COMMAND_INTERVAL_SEC,
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
        self._current_position: int | None = 0  # Assume down at start
        self._target_position: int | None = None
        self._move_task: asyncio.Task[None] | None = None
        self._attr_is_closed = True  # 0% = closed

    @property
    def current_cover_position(self) -> int | None:
        """Return current position (0 = down, 100 = up)."""
        return self._current_position

    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        return (
            self._move_task is not None
            and self._target_position is not None
            and self._target_position < (self._current_position or 0)
        )

    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        return (
            self._move_task is not None
            and self._target_position is not None
            and self._target_position > (self._current_position or 0)
        )

    def _get_up_command(self) -> str:
        """Get the up command method name for this cover type."""
        if self._cover_type == "head":
            return "head_up_continuous"
        if self._cover_type == "feet":
            return "feet_up"
        return "both_up_continuous"

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
        """Move cover to target position (0-100). Sends movement command every 340ms (per official app)."""
        current = self._current_position if self._current_position is not None else 0
        if target == current:
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

        start = asyncio.get_running_loop().time()
        try:
            while asyncio.get_running_loop().time() - start < duration:
                if self._move_task and self._move_task.cancelled():
                    break
                if not await method():
                    _LOGGER.warning("Movement command failed")
                    break
                await asyncio.sleep(MOVEMENT_COMMAND_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass
        finally:
            await self._client.stop()

        if not self._move_task or self._move_task.cancelled():
            return
        self._current_position = target
        self._target_position = None
        self._move_task = None
        self._attr_is_closed = target == 0
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (move to 100%)."""
        await self.async_set_cover_position(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (move to 0%)."""
        await self.async_set_cover_position(0)

    async def async_set_cover_position(self, position: int | None = None, **kwargs: Any) -> None:
        """Move the cover to a specific position (0-100)."""
        position = position if position is not None else kwargs.get("position", 0)
        if position < 0 or position > 100:
            return

        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
            try:
                await self._move_task
            except asyncio.CancelledError:
                pass
            await self._client.stop()

        self._target_position = position
        self._move_task = asyncio.create_task(self._async_move_to_position(position))
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
        self.async_write_ha_state()
