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
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
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

    # Sync position buttons: only when there is at least one other bed
    if entry.data.get(CONF_IS_GROUP):
        member_ids = entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        for member_id in member_ids:
            member_entry = hass.config_entries.async_get_entry(member_id)
            if not member_entry:
                continue
            other_client = hass.data.get(DOMAIN, {}).get(member_id)
            if other_client is None:
                continue
            title = member_entry.title or "Octo Bed"
            buttons.append(
                OctoBedSyncToBedButton(
                    client, entry, device_info, uid,
                    source_entry_id=member_id,
                    source_title=title,
                )
            )
    else:
        other_beds = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if not e.data.get(CONF_IS_GROUP) and e.entry_id != entry.entry_id
        ]
        for other in other_beds:
            if hass.data.get(DOMAIN, {}).get(other.entry_id) is None:
                continue
            title = other.title or "Octo Bed"
            buttons.append(
                OctoBedSyncToOtherButton(
                    client, entry, device_info, uid,
                    other_entry_id=other.entry_id,
                    other_title=title,
                )
            )

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
        # When paired (group): keep head/feet travel in sync on both member beds
        if self._entry.data.get(CONF_IS_GROUP):
            for eid in self._entry.data.get(CONF_MEMBER_ENTRY_IDS) or []:
                other = self.hass.config_entries.async_get_entry(eid)
                if other is not None:
                    merged = dict(other.options or {})
                    merged[CONF_HEAD_FULL_TRAVEL_SECONDS] = options.get(
                        CONF_HEAD_FULL_TRAVEL_SECONDS, merged.get(CONF_HEAD_FULL_TRAVEL_SECONDS)
                    )
                    merged[CONF_FEET_FULL_TRAVEL_SECONDS] = options.get(
                        CONF_FEET_FULL_TRAVEL_SECONDS, merged.get(CONF_FEET_FULL_TRAVEL_SECONDS)
                    )
                    self.hass.config_entries.async_update_entry(other, options=merged)
        # Move this part down for the same duration (return to 0%)
        await self._client.move_part_down_for_seconds(part, duration_seconds)


class OctoBedSyncToOtherButton(ButtonEntity):
    """Button on an individual bed: copy the other bed's position to this bed."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_icon = "mdi:sync"

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        other_entry_id: str,
        other_title: str,
    ) -> None:
        """Initialize the sync button."""
        self._client = client
        self._entry = entry
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_sync_to_{other_entry_id}"
        self._attr_name = f"Sync to {other_title} position"
        self._other_entry_id = other_entry_id
        self._other_title = other_title
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    async def async_added_to_hass(self) -> None:
        """Register for position updates on both beds so availability stays in sync."""
        await super().async_added_to_hass()
        domain_data = self.hass.data.get(DOMAIN) or {}
        other_client = domain_data.get(self._other_entry_id)
        if other_client is not None:
            other_client.register_position_callback(self._on_source_position_changed)
        self._client.register_position_callback(self._on_source_position_changed)

    @callback
    def _on_source_position_changed(self, part: str, position: int) -> None:
        """Update availability when either bed's position changes."""
        self.async_write_ha_state()

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self.async_write_ha_state()

    def _source_at_zero(self) -> bool:
        """True if the other bed has both head and feet at 0%."""
        domain_data = self.hass.data.get(DOMAIN) or {}
        other_client = domain_data.get(self._other_entry_id)
        if other_client is None:
            return True
        return (
            other_client.get_head_position() == 0
            and other_client.get_feet_position() == 0
        )

    def _positions_already_match(self) -> bool:
        """True if this bed and the other bed have the same head and feet position."""
        domain_data = self.hass.data.get(DOMAIN) or {}
        other_client = domain_data.get(self._other_entry_id)
        if other_client is None:
            return True
        return (
            self._client.get_head_position() == other_client.get_head_position()
            and self._client.get_feet_position() == other_client.get_feet_position()
        )

    def _calibration_differs_from_other(self) -> str | None:
        """When only 2 individual beds (no group): return message if calibration differs, else None."""
        all_entries = list(self.hass.config_entries.async_entries(DOMAIN))
        non_group = [e for e in all_entries if not (e.data or {}).get(CONF_IS_GROUP)]
        has_group = any((e.data or {}).get(CONF_IS_GROUP) for e in all_entries)
        if len(non_group) != 2 or has_group:
            return None
        other_entry = self.hass.config_entries.async_get_entry(self._other_entry_id)
        if not other_entry:
            return None
        opts_self = self._entry.options or {}
        opts_other = other_entry.options or {}
        default = DEFAULT_FULL_TRAVEL_SECONDS
        head_self = opts_self.get(CONF_HEAD_FULL_TRAVEL_SECONDS, opts_self.get(CONF_FULL_TRAVEL_SECONDS, default))
        feet_self = opts_self.get(CONF_FEET_FULL_TRAVEL_SECONDS, opts_self.get(CONF_FULL_TRAVEL_SECONDS, default))
        head_other = opts_other.get(CONF_HEAD_FULL_TRAVEL_SECONDS, opts_other.get(CONF_FULL_TRAVEL_SECONDS, default))
        feet_other = opts_other.get(CONF_FEET_FULL_TRAVEL_SECONDS, opts_other.get(CONF_FULL_TRAVEL_SECONDS, default))
        if head_self != head_other or feet_self != feet_other:
            return "Calibration differs from other bed"
        return None

    @property
    def available(self) -> bool:
        """Unavailable during calibration, when the other bed is at 0%, when calibration differs (2 beds only), or when positions already match."""
        if self._client.is_calibration_active():
            return False
        if self._source_at_zero():
            return False
        if self._calibration_differs_from_other():
            return False
        if self._positions_already_match():
            return False
        return True

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose unavailable reason when calibration differs or positions already match."""
        reason = self._calibration_differs_from_other()
        if reason:
            return {"unavailable_reason": reason}
        if self._positions_already_match():
            return {"unavailable_reason": "Beds are already at the same position"}
        return {}

    async def async_press(self) -> None:
        """Copy the other bed's head/feet position to this bed."""
        domain_data = self.hass.data.get(DOMAIN) or {}
        other_client = domain_data.get(self._other_entry_id)
        if not other_client:
            _LOGGER.warning("Other bed %s not available for sync", self._other_title)
            return
        head = other_client.get_head_position()
        feet = other_client.get_feet_position()
        opts = self._entry.options or {}
        default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        head_travel = opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, default)
        feet_travel = opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, default)
        await self._client.run_to_position(head, feet, head_travel, feet_travel)


class OctoBedSyncToBedButton(ButtonEntity):
    """Button on 'Both beds' device: set both beds to the chosen bed's position."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_icon = "mdi:sync"

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        source_entry_id: str,
        source_title: str,
    ) -> None:
        """Initialize the sync button."""
        self._client = client
        self._entry = entry
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_sync_to_{source_entry_id}"
        self._attr_name = f"Sync to {source_title} position"
        self._source_entry_id = source_entry_id
        self._source_title = source_title
        client.register_calibration_state_callback(self._on_calibration_state_changed)

    async def async_added_to_hass(self) -> None:
        """Register for position updates on both member beds so availability stays in sync."""
        await super().async_added_to_hass()
        domain_data = self.hass.data.get(DOMAIN) or {}
        member_ids = self._entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        for eid in member_ids:
            client = domain_data.get(eid)
            if client is not None:
                client.register_position_callback(self._on_source_position_changed)

    @callback
    def _on_source_position_changed(self, part: str, position: int) -> None:
        """Update availability when either bed's position changes."""
        self.async_write_ha_state()

    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self.async_write_ha_state()

    def _source_at_zero(self) -> bool:
        """True if the source bed has both head and feet at 0%."""
        domain_data = self.hass.data.get(DOMAIN) or {}
        source_client = domain_data.get(self._source_entry_id)
        if source_client is None:
            return True
        return (
            source_client.get_head_position() == 0
            and source_client.get_feet_position() == 0
        )

    def _both_beds_already_at_source_position(self) -> bool:
        """True if both beds are already at the source bed's position (no sync needed)."""
        domain_data = self.hass.data.get(DOMAIN) or {}
        source_client = domain_data.get(self._source_entry_id)
        if source_client is None:
            return True
        member_ids = self._entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        sh, sf = source_client.get_head_position(), source_client.get_feet_position()
        for eid in member_ids:
            client = domain_data.get(eid)
            if client is None:
                return False
            if client.get_head_position() != sh or client.get_feet_position() != sf:
                return False
        return True

    @property
    def available(self) -> bool:
        """Unavailable during calibration, when the source bed is at 0%, or when both beds already match source position."""
        if self._client.is_calibration_active():
            return False
        if self._source_at_zero():
            return False
        if self._both_beds_already_at_source_position():
            return False
        return True

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose unavailable reason when both beds already at source position."""
        if self._both_beds_already_at_source_position() and not self._source_at_zero():
            return {"unavailable_reason": "Beds are already at the same position"}
        return {}

    async def async_press(self) -> None:
        """Set both beds to the source bed's head/feet position."""
        domain_data = self.hass.data.get(DOMAIN) or {}
        source_client = domain_data.get(self._source_entry_id)
        if not source_client:
            _LOGGER.warning("Source bed %s not available for sync", self._source_title)
            return
        head = source_client.get_head_position()
        feet = source_client.get_feet_position()
        opts = self._entry.options or {}
        default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        head_travel = opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, default)
        feet_travel = opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, default)
        await self._client.run_to_position(head, feet, head_travel, feet_travel)
