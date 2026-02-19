"""Switch entities for Octo Bed (light control)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed light switch from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Octo Bed",
        manufacturer="Octo",
    )

    async_add_entities([OctoBedLightSwitch(client, device_info)])


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
