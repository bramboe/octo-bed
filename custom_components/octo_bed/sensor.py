"""Diagnostic sensors for Octo Bed (MAC, positions, connection status)."""

from __future__ import annotations

import logging
from datetime import timedelta
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_IS_GROUP, CONF_MEMBER_ENTRY_IDS, DOMAIN
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


def _is_entry_in_paired_group(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if this bed entry is a member of a 'Both beds' group."""
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
    """Set up Octo Bed diagnostic sensors from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]
    uid = entry.unique_id or entry.entry_id
    calibration_disabled_paired = _is_entry_in_paired_group(hass, entry)

    device_info = DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    sensors = [
        OctoBedCalibrationStatusSensor(client, device_info, uid, calibration_disabled_paired),
        OctoBedMacAddressSensor(client, device_info, uid),
        OctoBedHeadPositionSensor(client, device_info, uid),
        OctoBedFeetPositionSensor(client, device_info, uid),
        OctoBedConnectionStatusSensor(client, device_info, uid),
    ]

    async_add_entities(sensors)


class OctoBedCalibrationStatusSensor(SensorEntity):
    """Sensor showing current calibration status (Diagnostics section)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_icon = "mdi:ruler"
    _attr_name = "Calibration status"

    def __init__(
        self,
        client: OctoBedClient,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        unavailable_when_paired: bool = False,
    ) -> None:
        """Initialize the calibration status sensor."""
        self._client = client
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_calibration_status"
        self._unavailable_when_paired = unavailable_when_paired
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update state when calibration state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable when this bed is paired (calibrate via Both beds device)."""
        return not self._unavailable_when_paired

    @property
    def native_value(self) -> str:
        """Return human-readable calibration status for display."""
        state, part = self._client.get_calibration_status()
        if state == "idle":
            return "Inactive"
        if state == "tracking":
            return f"Measuring full travel ({part})" if part else "Measuring full travel"
        if state == "returning":
            return f"Returning to start ({part})" if part else "Returning to start"
        return "Inactive"

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return raw state and part for automation."""
        state, part = self._client.get_calibration_status()
        raw = "idle"
        if state == "tracking":
            raw = f"tracking_{part}" if part else "tracking"
        elif state == "returning":
            raw = f"returning_{part}" if part else "returning"
        return {"part": part, "state": raw}


class OctoBedDiagnosticSensor(SensorEntity):
    """Base class for Octo Bed diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(
        self,
        client: OctoBedClient,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        unique_id_suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the sensor."""
        self._client = client
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_{unique_id_suffix}"
        self._attr_name = name
        self._attr_icon = icon


class OctoBedMacAddressSensor(OctoBedDiagnosticSensor):
    """Sensor exposing the bed's Bluetooth MAC address."""

    _attr_icon = "mdi:bluetooth"

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str) -> None:
        """Initialize the MAC address sensor."""
        super().__init__(
            client,
            device_info,
            unique_id_prefix,
            "mac_address",
            "MAC address",
            "mdi:bluetooth",
        )

    @property
    def native_value(self) -> str:
        """Return the Bluetooth MAC address."""
        return self._client.get_device_address()


class OctoBedHeadPositionSensor(OctoBedDiagnosticSensor):
    """Sensor exposing head position (0–100%)."""

    _attr_native_unit_of_measurement = "%"

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str) -> None:
        """Initialize the head position sensor."""
        super().__init__(
            client,
            device_info,
            unique_id_prefix,
            "head_position",
            "Head position",
            "mdi:arrow-up-down",
        )
        self._client.register_position_callback(self._on_position_changed)

    @callback
    def _on_position_changed(self, part: str, position: int) -> None:
        """Update state when position changes."""
        if part == "head":
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        """Return head position 0–100%."""
        return self._client.get_head_position()


class OctoBedFeetPositionSensor(OctoBedDiagnosticSensor):
    """Sensor exposing feet position (0–100%)."""

    _attr_native_unit_of_measurement = "%"

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str) -> None:
        """Initialize the feet position sensor."""
        super().__init__(
            client,
            device_info,
            unique_id_prefix,
            "feet_position",
            "Feet position",
            "mdi:arrow-up-down",
        )
        self._client.register_position_callback(self._on_position_changed)

    @callback
    def _on_position_changed(self, part: str, position: int) -> None:
        """Update state when position changes."""
        if part == "feet":
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        """Return feet position 0–100%."""
        return self._client.get_feet_position()


class OctoBedConnectionStatusSensor(OctoBedDiagnosticSensor):
    """Sensor exposing connection status with Bluetooth proxy."""

    _attr_icon = "mdi:bluetooth-connect"
    _attr_should_poll = True
    _attr_update_interval = timedelta(seconds=30)

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str) -> None:
        """Initialize the connection status sensor."""
        super().__init__(
            client,
            device_info,
            unique_id_prefix,
            "connection_status",
            "Connection status",
            "mdi:bluetooth-connect",
        )

    @property
    def native_value(self) -> str:
        """Return connection status: connected or disconnected."""
        return "connected" if self._client.is_connected() else "disconnected"

