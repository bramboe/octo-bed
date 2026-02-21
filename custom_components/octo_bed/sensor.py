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

from .const import DOMAIN
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed diagnostic sensors from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    sensors = [
        OctoBedCalibrationStatusSensor(client, device_info),
        OctoBedMacAddressSensor(client, device_info),
        OctoBedHeadPositionSensor(client, device_info),
        OctoBedFeetPositionSensor(client, device_info),
        OctoBedConnectionStatusSensor(client, device_info),
    ]

    async_add_entities(sensors)


class OctoBedCalibrationStatusSensor(SensorEntity):
    """Sensor showing current calibration status (Configuration section)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_icon = "mdi:ruler"
    _attr_unique_id = "octo_bed_calibration_status"
    _attr_name = "Calibration status"

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo) -> None:
        """Initialize the calibration status sensor."""
        self._client = client
        self._attr_device_info = device_info
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update state when calibration state changes."""
        self.async_write_ha_state()

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
        unique_id_suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the sensor."""
        self._client = client
        self._attr_device_info = device_info
        self._attr_unique_id = f"octo_bed_{unique_id_suffix}"
        self._attr_name = name
        self._attr_icon = icon


class OctoBedMacAddressSensor(OctoBedDiagnosticSensor):
    """Sensor exposing the bed's Bluetooth MAC address."""

    _attr_icon = "mdi:bluetooth"

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo) -> None:
        """Initialize the MAC address sensor."""
        super().__init__(
            client,
            device_info,
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

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo) -> None:
        """Initialize the head position sensor."""
        super().__init__(
            client,
            device_info,
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

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo) -> None:
        """Initialize the feet position sensor."""
        super().__init__(
            client,
            device_info,
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

    def __init__(self, client: OctoBedClient, device_info: DeviceInfo) -> None:
        """Initialize the connection status sensor."""
        super().__init__(
            client,
            device_info,
            "connection_status",
            "Connection status",
            "mdi:bluetooth-connect",
        )

    @property
    def native_value(self) -> str:
        """Return connection status: connected or disconnected."""
        return "connected" if self._client.is_connected() else "disconnected"

