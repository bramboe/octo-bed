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

from .const import CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS, DOMAIN
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
        OctoBedMovementSwitch(
            client, "both_up", "Both Up", "mdi:arrow-up-bold", device_info, full_travel_seconds
        ),
        OctoBedMovementSwitch(
            client, "both_down", "Both Down", "mdi:arrow-down-bold", device_info, full_travel_seconds
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

        end_time = time.monotonic() + self._full_travel_seconds
        try:
            while time.monotonic() < end_time:
                await method()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            # Turned off by user
            return
        finally:
            # Reached full-travel time or switched off: ensure we send stop
            try:
                await self._client.stop()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to send stop after switch move", exc_info=True)

            self._is_on = False
            self._task = None
            self.async_write_ha_state()
