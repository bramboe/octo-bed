"""Number entities for Octo Bed configuration (calibration travel times)."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

MIN_TRAVEL = 5
MAX_TRAVEL = 120


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed configuration number entities from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name="Octo Bed",
        manufacturer="Octo",
    )

    entities = [
        OctoBedConfigNumber(
            hass, entry, client, device_info,
            CONF_HEAD_FULL_TRAVEL_SECONDS,
            "Head full travel",
            "octo_bed_head_full_travel_seconds",
            "mdi:arrow-up-down",
        ),
        OctoBedConfigNumber(
            hass, entry, client, device_info,
            CONF_FEET_FULL_TRAVEL_SECONDS,
            "Feet full travel",
            "octo_bed_feet_full_travel_seconds",
            "mdi:arrow-up-down",
        ),
    ]

    async_add_entities(entities)


class OctoBedConfigNumber(NumberEntity):
    """Configuration number for head or feet full travel (seconds)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_native_min_value = MIN_TRAVEL
    _attr_native_max_value = MAX_TRAVEL
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "s"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OctoBedClient,
        device_info: DeviceInfo,
        option_key: str,
        name: str,
        unique_id: str,
        icon: str,
    ) -> None:
        """Initialize the config number."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._attr_device_info = device_info
        self._option_key = option_key
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_icon = icon

    @property
    def native_value(self) -> float:
        """Return current value from config entry options."""
        return float(
            self._entry.options.get(self._option_key, DEFAULT_FULL_TRAVEL_SECONDS)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update config entry option and reload so new value is used."""
        val = int(round(value))
        val = max(MIN_TRAVEL, min(MAX_TRAVEL, val))
        options = dict(self._entry.options)
        options[self._option_key] = val
        self._hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
        await self._hass.config_entries.async_reload(self._entry.entry_id)
