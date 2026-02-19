"""Button entities for Octo Bed."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
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
    """Set up Octo Bed buttons from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Octo Bed",
        manufacturer="Octo",
    )

    buttons = [
        OctoBedButton(client, "both_down", "Both Down", "mdi:arrow-down", device_info),
        OctoBedButton(client, "both_up", "Both Up", "mdi:arrow-up", device_info),
        OctoBedButton(
            client, "both_up_continuous", "Both Up (Continuous)", "mdi:arrow-up-bold", device_info
        ),
        OctoBedButton(client, "feet_down", "Feet Down", "mdi:arrow-down", device_info),
        OctoBedButton(client, "feet_up", "Feet Up", "mdi:arrow-up", device_info),
        OctoBedButton(client, "head_down", "Head Down", "mdi:arrow-down", device_info),
        OctoBedButton(client, "head_up", "Head Up", "mdi:arrow-up", device_info),
        OctoBedButton(
            client, "head_up_continuous", "Head Up (Continuous)", "mdi:arrow-up-bold", device_info
        ),
        OctoBedButton(client, "stop", "Stop", "mdi:stop", device_info),
    ]

    async_add_entities(buttons)


class OctoBedButton(ButtonEntity):
    """Representation of an Octo Bed button."""

    _attr_has_entity_name = True

    def __init__(
        self,
        client: OctoBedClient,
        action: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"octo_bed_{action}"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Press the button."""
        method = getattr(self._client, self._action, None)
        if method and callable(method):
            await method()
