"""Light entity for the Octo Bed under-bed light."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Octo Bed light from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]
    uid = entry.unique_id or entry.entry_id

    device_info = DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    async_add_entities([OctoBedLight(client, device_info, uid)])


class OctoBedLight(LightEntity, RestoreEntity):
    """Under-bed light. State is assumed: the bed does not report it."""

    _attr_has_entity_name = True
    _attr_name = "Light"
    _attr_assumed_state = True

    def __init__(
        self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str
    ) -> None:
        """Initialize the light."""
        self._client = client
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_light"
        self._attr_is_on = None  # Unknown until first command
        if client.has_rgbwi_light:
            self._attr_supported_color_modes = {ColorMode.RGBW}
            self._attr_color_mode = ColorMode.RGBW
            self._attr_rgbw_color = (255, 255, 255, 255)
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF

    async def async_added_to_hass(self) -> None:
        """Restore the last assumed state and register callbacks."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        self._client.register_connection_callback(self._on_connection_changed)
        self._client.register_calibration_state_callback(self._on_calibration_state_changed)

    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        self.async_write_ha_state()

    @callback
    def _on_calibration_state_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available when connected and no calibration is running."""
        return self._client.is_connected() and not self._client.is_calibration_active()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on (optionally with an RGBW color)."""
        rgbw = kwargs.get("rgbw_color")
        if rgbw is not None and self._client.has_rgbwi_light:
            if await self._client.set_light_color_rgbw(rgbw):
                self._attr_rgbw_color = rgbw
                self._attr_is_on = True
                self.async_write_ha_state()
            return
        if await self._client.light_on():
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        if await self._client.light_off():
            self._attr_is_on = False
            self.async_write_ha_state()
