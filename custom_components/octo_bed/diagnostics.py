"""Diagnostics support for Octo Bed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PIN, DOMAIN

TO_REDACT = {CONF_PIN, "address", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    client = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    diagnostics: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
    }
    if client is not None:
        diagnostics["client"] = {
            "connected": client.is_connected(),
            "head_position": client.get_head_position(),
            "feet_position": client.get_feet_position(),
            "calibration": client.get_calibration_status(),
            "features": client.get_feature_summary(),
        }
    return diagnostics
