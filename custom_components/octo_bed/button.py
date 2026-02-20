"""Button entities for Octo Bed."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
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
        OctoBedButton(client, "stop", "Stop", "mdi:stop", device_info),
        OctoBedCalibrateButton(client, entry, "calibrate_head", "Calibrate head", "mdi:arrow-up-bold", device_info),
        OctoBedCalibrateButton(client, entry, "calibrate_feet", "Calibrate feet", "mdi:arrow-up-bold", device_info),
        OctoBedCompleteCalibrationButton(client, entry, device_info),
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


class OctoBedCalibrateButton(ButtonEntity):
    """Button to start calibration for head or feet."""

    _attr_has_entity_name = True

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        action: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the calibration button."""
        self._client = client
        self._entry = entry
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"octo_bed_{action}"
        self._attr_device_info = device_info
        self._part = "head" if "head" in action else "feet"

    async def async_press(self) -> None:
        """Start calibration: move this part up and start counting seconds."""
        await self._client.start_calibration(self._part)


class OctoBedCompleteCalibrationButton(ButtonEntity):
    """Button to complete calibration: save duration as 100% travel and return bed to 0%."""

    _attr_has_entity_name = True
    _attr_name = "Complete calibration session"
    _attr_icon = "mdi:check-circle"
    _attr_unique_id = "octo_bed_complete_calibration"

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the complete calibration button."""
        self._client = client
        self._entry = entry
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Complete calibration: save duration and move bed part back to 0%."""
        part, duration_seconds = await self._client.complete_calibration()
        if part is None or duration_seconds <= 0:
            _LOGGER.warning("Complete calibration pressed but no calibration was active")
            return
        # Save duration as full travel for this part
        options = dict(self._entry.options)
        if part == "head":
            options[CONF_HEAD_FULL_TRAVEL_SECONDS] = int(round(duration_seconds))
        else:
            options[CONF_FEET_FULL_TRAVEL_SECONDS] = int(round(duration_seconds))
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        # Move this part down for the same duration (return to 0%)
        await self._client.move_part_down_for_seconds(part, duration_seconds)
