"""Cover entities for Octo Bed (head, feet, both)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .group_client import GroupOctoBedClient
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed covers from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]
    uid = entry.unique_id or entry.entry_id

    device_info = DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    async_add_entities(
        [
            OctoBedCover(client, "head", device_info, entry, uid),
            OctoBedCover(client, "feet", device_info, entry, uid),
            OctoBedCover(client, "both", device_info, entry, uid),
        ]
    )

    # octo_bed.move_to_position: set head and feet in one call (any bed cover works)
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "move_to_position",
        {
            vol.Optional("head"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Optional("feet"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        },
        "async_move_to_position_service",
    )


class OctoBedCover(CoverEntity, RestoreEntity):
    """Representation of an Octo Bed cover (head, feet, or both).

    The bed reports no position, so positions are dead-reckoned from the
    measured travel times by the client, which also drives the motors.
    """

    _attr_has_entity_name = True
    _attr_assumed_state = True
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
        device_info: DeviceInfo,
        entry: ConfigEntry,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the cover."""
        self._client = client
        self._cover_type = cover_type
        self._attr_translation_key = cover_type
        self._attr_unique_id = f"{unique_id_prefix}_cover_{cover_type}"
        self._attr_device_info = device_info
        self._entry = entry
        self._target_position: int | None = None
        self._move_task: asyncio.Task[Any] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known position and register callbacks."""
        await super().async_added_to_hass()
        # Restore the dead-reckoned position from before the restart. Member
        # beds restore their own positions; the group and the derived "both"
        # cover skip this.
        if self._cover_type in ("head", "feet") and not isinstance(
            self._client, GroupOctoBedClient
        ):
            last = await self.async_get_last_state()
            position = last.attributes.get("current_position") if last else None
            if position is not None:
                if self._cover_type == "head":
                    self._client.set_head_position(int(position))
                else:
                    self._client.set_feet_position(int(position))
                _LOGGER.debug("Restored %s position to %s%%", self._cover_type, position)
        self.async_on_remove(
            self._client.register_position_callback(self._on_position_changed)
        )
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_state_changed)
        )
        self.async_on_remove(
            self._client.register_connection_callback(self._on_state_changed)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop driving the motors when the entity goes away."""
        if await self._cancel_move():
            await self._client.send_stop()

    @callback
    def _on_state_changed(self, *_args: Any) -> None:
        """Update availability when calibration or connection state changes."""
        self.async_write_ha_state()

    @callback
    def _on_position_changed(self, part: str, _position: int) -> None:
        """Write state when a part this cover shows has moved."""
        if self._cover_type == "both" or part == self._cover_type:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available when connected and no calibration is active."""
        return self._client.is_connected() and not self._client.is_calibration_active()

    @property
    def current_cover_position(self) -> int:
        """Return current position (0 = down, 100 = up) from shared state."""
        if self._cover_type == "head":
            return self._client.get_head_position()
        if self._cover_type == "feet":
            return self._client.get_feet_position()
        return self._client.get_both_position()

    @property
    def is_closed(self) -> bool:
        """Closed means fully down."""
        return self.current_cover_position == 0

    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        target = self._moving_to()
        return target is not None and target < self.current_cover_position

    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        target = self._moving_to()
        return target is not None and target > self.current_cover_position

    def _moving_to(self) -> int | None:
        if self._move_task is None or self._move_task.done():
            return None
        return self._target_position

    def _travel_seconds(self) -> tuple[float, float]:
        """(head, feet) full travel seconds, read live so calibration applies."""
        opts = self._entry.options
        default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        return (
            float(opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, default)),
            float(opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, default)),
        )

    async def _cancel_move(self) -> bool:
        """Cancel the running move; returns True if one was running."""
        task = self._move_task
        self._move_task = None
        self._target_position = None
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    @callback
    def _on_move_done(self, task: asyncio.Task[Any]) -> None:
        if self._move_task is task:
            self._move_task = None
            self._target_position = None
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Moving %s failed: %s", self.entity_id, task.exception())
        if self.hass is not None:
            self.async_write_ha_state()

    async def _start_move(self, position: int) -> None:
        """Cancel any running move and start a new one to the given position."""
        position = max(0, min(100, int(position)))
        await self._cancel_move()
        head_travel, feet_travel = self._travel_seconds()
        head_target = position if self._cover_type in ("head", "both") else None
        feet_target = position if self._cover_type in ("feet", "both") else None
        self._target_position = position
        task = asyncio.create_task(
            self._client.run_to_position(
                head_target, feet_target, head_travel, feet_travel
            )
        )
        self._move_task = task
        self._client.register_movement_task(task)
        # Register which part is moving so conflicting moves get cancelled
        self._client.register_active_movement(self._cover_type, task)
        task.add_done_callback(self._on_move_done)
        self.async_write_ha_state()

    async def async_move_to_position_service(
        self, head: int | None = None, feet: int | None = None
    ) -> None:
        """Move head and feet to the given positions in one call (entity service)."""
        if head is None and feet is None:
            return
        head_travel, feet_travel = self._travel_seconds()
        task = asyncio.create_task(
            self._client.run_to_position(head, feet, head_travel, feet_travel)
        )
        self._client.register_movement_task(task)
        self._client.register_active_movement("both", task)
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (move to 100%)."""
        await self._start_move(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (move to 0%)."""
        await self._start_move(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position (0-100)."""
        await self._start_move(kwargs.get(ATTR_POSITION, 0))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self._cancel_move()
        await self._client.stop()
        self.async_write_ha_state()
