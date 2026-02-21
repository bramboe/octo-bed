"""Helpers for the Octo Bed integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_IS_GROUP, CONF_MEMBER_ENTRY_IDS, DOMAIN


def entry_is_member_of_group(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if this config entry is a member of any 'Both beds' group."""
    if entry.data.get(CONF_IS_GROUP):
        return False
    entry_id = entry.entry_id
    for other in hass.config_entries.async_entries(DOMAIN):
        if not other.data.get(CONF_IS_GROUP):
            continue
        member_ids = other.data.get(CONF_MEMBER_ENTRY_IDS) or []
        if entry_id in member_ids:
            return True
    return False
