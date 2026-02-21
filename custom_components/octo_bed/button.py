"""Button entities for Octo Bed."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


def _is_entry_in_paired_group(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if this bed entry is a member of a 'Both beds' group (calibration only on group)."""
    if entry.data.get(CONF_IS_GROUP):
        return False
    for other in hass.config_entries.async_entries(DOMAIN):
        if not other.data.get(CONF_IS_GROUP):
            continue
        if entry.entry_id in (other.data.get(CONF_MEMBER_ENTRY_IDS) or []):
            return True
    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed buttons from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]
    uid = entry.unique_id or entry.entry_id
    calibration_disabled_paired = _is_entry_in_paired_group(hass, entry)

    device_info = DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    buttons: list[ButtonEntity] = [
        OctoBedButton(client, "stop", "Stop", "mdi:stop", device_info, uid),
    ]
    if entry.options.get(CONF_SHOW_CALIBRATION_BUTTONS, True):
        buttons.extend([
            OctoBedCalibrateButton(client, entry, "calibrate_head", "Calibrate head", "mdi:arrow-up-bold", device_info, uid, calibration_disabled_paired),
            OctoBedCalibrateButton(client, entry, "calibrate_feet", "Calibrate feet", "mdi:arrow-up-bold", device_info, uid, calibration_disabled_paired),
            OctoBedCompleteCalibrationButton(client, entry, device_info, uid, calibration_disabled_paired),
        ])

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
        unique_id_prefix: str,
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{unique_id_prefix}_{action}"
        self._attr_device_info = device_info
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self._attr_available = not self._client.is_calibration_active()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if calibration is not active (Stop allowed only when not calibrating)."""
        return not self._client.is_calibration_active()

    async def async_press(self) -> None:
        """Press the button."""
        method = getattr(self._client, self._action, None)
        if method and callable(method):
            await method()


class OctoBedCalibrateButton(ButtonEntity):
    """Button to start calibration for head or feet."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        action: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        disabled_when_paired: bool = False,
    ) -> None:
        """Initialize the calibration button."""
        self._client = client
        self._entry = entry
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{unique_id_prefix}_{action}"
        self._attr_device_info = device_info
        self._part = "head" if "head" in action else "feet"
        self._disabled_when_paired = disabled_when_paired
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable when paired (calibrate via Both beds) or when calibration is active."""
        if self._disabled_when_paired:
            return False
        return not self._client.is_calibration_active()

    async def async_press(self) -> None:
        """Start calibration: move this part up and start counting seconds."""
        await self._client.start_calibration(self._part)


class OctoBedCompleteCalibrationButton(ButtonEntity):
    """Button to complete calibration: save duration as 100% travel and return bed to 0%."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_name = "Complete calibration session"
    _attr_icon = "mdi:check-circle"

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        disabled_when_paired: bool = False,
    ) -> None:
        """Initialize the complete calibration button."""
        self._client = client
        self._entry = entry
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_complete_calibration"
        self._disabled_when_paired = disabled_when_paired
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable when paired (calibrate via Both beds) or when not in tracking phase."""
        if self._disabled_when_paired:
            return False
        return self._client.is_calibrating()

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
