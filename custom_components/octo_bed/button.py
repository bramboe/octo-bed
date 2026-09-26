"""Button entities for Octo Bed."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_SHOW_CALIBRATION_BUTTONS,
    CONF_SOFT_PRESETS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
    SIGNAL_BED_UPDATE,
    SOFT_PRESET_SLOTS,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


def _travel_times(entry: ConfigEntry) -> tuple[int, int]:
    """Return (head, feet) full travel seconds from entry options."""
    opts = entry.options or {}
    default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
    return (
        opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, default),
        opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, default),
    )


async def _run_to_position_tracked(
    client: OctoBedClient, head: int, feet: int, head_travel: int, feet_travel: int
) -> None:
    """Run to a position as a registered movement so Stop can cancel it."""
    task = asyncio.create_task(
        client.run_to_position(head, feet, head_travel, feet_travel)
    )
    client.register_movement_task(task)
    client.register_active_movement("both", task)
    try:
        await task
    except asyncio.CancelledError:
        pass


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
        OctoBedButton(client, "stop", "mdi:stop", device_info, uid),
    ]

    # Hardware memory presets (detected via feature discovery)
    for slot in range(client.memory_slot_count):
        buttons.append(OctoBedPresetButton(client, slot, device_info, uid))
        buttons.append(OctoBedSavePresetButton(client, slot, device_info, uid))

    # Software presets: position pairs stored by the integration, for beds
    # without hardware memory slots
    if client.memory_slot_count == 0:
        for slot in range(1, SOFT_PRESET_SLOTS + 1):
            buttons.append(OctoBedSoftPresetButton(client, entry, slot, device_info, uid))
            buttons.append(OctoBedSaveSoftPresetButton(client, entry, slot, device_info, uid))

    if entry.options.get(CONF_SHOW_CALIBRATION_BUTTONS, True):
        buttons.extend([
            OctoBedCalibrateButton(client, entry, "calibrate_head", "mdi:arrow-up-bold", device_info, uid, calibration_disabled_paired),
            OctoBedCalibrateButton(client, entry, "calibrate_feet", "mdi:arrow-up-bold", device_info, uid, calibration_disabled_paired),
            OctoBedCompleteCalibrationButton(client, entry, device_info, uid, calibration_disabled_paired),
        ])

    # Sync position buttons. Created for every other configured bed, whether
    # or not it has finished loading: the other bed's client is looked up when
    # it is needed, so the buttons survive either bed being reloaded.
    if entry.data.get(CONF_IS_GROUP):
        for member_id in entry.data.get(CONF_MEMBER_ENTRY_IDS) or []:
            member_entry = hass.config_entries.async_get_entry(member_id)
            if member_entry is None:
                continue
            buttons.append(
                OctoBedSyncToBedButton(
                    client, entry, device_info, uid,
                    source_entry_id=member_id,
                    source_title=member_entry.title or "Octo Bed",
                )
            )
    else:
        for other in hass.config_entries.async_entries(DOMAIN):
            if (
                other.entry_id == entry.entry_id
                or other.data.get(CONF_IS_GROUP)
                or other.source == SOURCE_IGNORE
                or not other.data.get("address")
            ):
                continue
            buttons.append(
                OctoBedSyncToOtherButton(
                    client, entry, device_info, uid,
                    other_entry_id=other.entry_id,
                    other_title=other.title or "Octo Bed",
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
        icon: str,
        device_info: DeviceInfo,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._action = action
        self._attr_translation_key = action
        self._attr_icon = icon
        self._attr_unique_id = f"{unique_id_prefix}_{action}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register for calibration and connection updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_calibration_state_changed)
        )
        self.async_on_remove(
            self._client.register_connection_callback(self._on_connection_changed)
        )
    @callback
    def _on_calibration_state_changed(self) -> None:
        """Update availability when calibration state changes."""
        self.async_write_ha_state()

    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        """Update availability when the connection state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available whenever connected.

        Deliberately stays available during calibration: Stop is the
        emergency exit that aborts a calibration session without saving.
        """
        return self._client.is_connected()

    async def async_press(self) -> None:
        """Press the button."""
        method = getattr(self._client, self._action, None)
        if method and callable(method):
            await method()


class OctoBedPresetButton(ButtonEntity):
    """Recall a hardware memory preset stored in the bed itself."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:bed-clock"

    def __init__(
        self,
        client: OctoBedClient,
        slot: int,
        device_info: DeviceInfo,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the preset button."""
        self._client = client
        self._slot = slot
        self._attr_translation_key = "preset"
        self._attr_translation_placeholders = {"number": str(slot + 1)}
        self._attr_unique_id = f"{unique_id_prefix}_preset_{slot + 1}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register for connection updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_connection_callback(self._on_connection_changed)
        )
    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.is_connected() and not self._client.is_calibration_active()

    async def async_press(self) -> None:
        """Recall the preset. The dead-reckoned position may drift afterwards."""
        await self._client.recall_memory_preset(self._slot)


class OctoBedSoftPresetButton(ButtonEntity):
    """Move the bed to a position pair stored by the integration."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:bed-clock"

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        slot: int,
        device_info: DeviceInfo,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the software preset button."""
        self._client = client
        self._entry = entry
        self._slot = slot
        self._attr_translation_key = "preset"
        self._attr_translation_placeholders = {"number": str(slot)}
        self._attr_unique_id = f"{unique_id_prefix}_soft_preset_{slot}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register for connection and calibration updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_connection_callback(self._on_client_state_changed)
        )
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_calibration_changed)
        )
    @callback
    def _on_client_state_changed(self, connected: bool) -> None:
        self.async_write_ha_state()

    @callback
    def _on_calibration_changed(self) -> None:
        self.async_write_ha_state()

    def _stored(self) -> dict | None:
        """Return the stored {head, feet} positions for this slot, if any."""
        presets = self._entry.options.get(CONF_SOFT_PRESETS) or {}
        preset = presets.get(str(self._slot))
        if isinstance(preset, dict) and "head" in preset and "feet" in preset:
            return preset
        return None

    @property
    def available(self) -> bool:
        """Available when connected, idle and the slot has been saved."""
        return (
            self._client.is_connected()
            and not self._client.is_calibration_active()
            and self._stored() is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Expose the stored positions."""
        preset = self._stored()
        if preset is None:
            return None
        return {"head": int(preset["head"]), "feet": int(preset["feet"])}

    async def async_press(self) -> None:
        """Move head and feet to the stored positions."""
        preset = self._stored()
        if preset is None:
            _LOGGER.warning("Preset %d has not been saved yet", self._slot)
            return
        head_travel, feet_travel = _travel_times(self._entry)
        await _run_to_position_tracked(
            self._client,
            int(preset["head"]),
            int(preset["feet"]),
            head_travel,
            feet_travel,
        )


class OctoBedSaveSoftPresetButton(ButtonEntity):
    """Save the current positions to an integration-stored preset slot."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:content-save"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        slot: int,
        device_info: DeviceInfo,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the save preset button."""
        self._client = client
        self._entry = entry
        self._slot = slot
        self._attr_translation_key = "save_preset"
        self._attr_translation_placeholders = {"number": str(slot)}
        self._attr_unique_id = f"{unique_id_prefix}_save_soft_preset_{slot}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register for connection updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_connection_callback(self._on_connection_changed)
        )
    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.is_connected() and not self._client.is_calibration_active()

    async def async_press(self) -> None:
        """Store the current head/feet positions in this slot."""
        options = dict(self._entry.options)
        presets = dict(options.get(CONF_SOFT_PRESETS) or {})
        presets[str(self._slot)] = {
            "head": self._client.get_head_position(),
            "feet": self._client.get_feet_position(),
        }
        options[CONF_SOFT_PRESETS] = presets
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        _LOGGER.info(
            "Saved preset %d: head %d%%, feet %d%%",
            self._slot,
            presets[str(self._slot)]["head"],
            presets[str(self._slot)]["feet"],
        )


class OctoBedSavePresetButton(ButtonEntity):
    """Save the current position to a hardware memory slot."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:content-save"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        client: OctoBedClient,
        slot: int,
        device_info: DeviceInfo,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the save preset button."""
        self._client = client
        self._slot = slot
        self._attr_translation_key = "save_preset"
        self._attr_translation_placeholders = {"number": str(slot + 1)}
        self._attr_unique_id = f"{unique_id_prefix}_save_preset_{slot + 1}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register for connection updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_connection_callback(self._on_connection_changed)
        )
    @callback
    def _on_connection_changed(self, connected: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.is_connected() and not self._client.is_calibration_active()

    async def async_press(self) -> None:
        """Save the current position to this slot."""
        await self._client.save_memory_preset(self._slot)


class OctoBedCalibrateButton(ButtonEntity):
    """Button to start calibration for head or feet."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        action: str,
        icon: str,
        device_info: DeviceInfo,
        unique_id_prefix: str,
        disabled_when_paired: bool = False,
    ) -> None:
        """Initialize the calibration button."""
        self._client = client
        self._entry = entry
        self._action = action
        self._attr_translation_key = action
        self._attr_icon = icon
        self._attr_unique_id = f"{unique_id_prefix}_{action}"
        self._attr_device_info = device_info
        self._part = "head" if "head" in action else "feet"
        self._disabled_when_paired = disabled_when_paired

    async def async_added_to_hass(self) -> None:
        """Register for calibration state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_calibration_state_changed)
        )
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
        """Start calibration: drive this part to 0% first, then measure upward travel."""
        opts = self._entry.options or {}
        default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        key = (
            CONF_HEAD_FULL_TRAVEL_SECONDS
            if self._part == "head"
            else CONF_FEET_FULL_TRAVEL_SECONDS
        )
        down_seconds = opts.get(key, default)
        await self._client.start_calibration(self._part, down_seconds)


class OctoBedCompleteCalibrationButton(ButtonEntity):
    """Button to complete calibration: save duration as 100% travel and return bed to 0%."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = "complete_calibration"
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

    async def async_added_to_hass(self) -> None:
        """Register for calibration state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_calibration_state_changed)
        )
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
        # Clamp to the same range the options flow allows (5-120 s)
        clamped = max(5.0, min(120.0, duration_seconds))
        if clamped != duration_seconds:
            _LOGGER.warning(
                "Measured travel time %.1f s for %s is outside 5-120 s; clamped to %.0f s",
                duration_seconds,
                part,
                clamped,
            )
            duration_seconds = clamped
        # Save duration as full travel for this part
        options = dict(self._entry.options)
        if part == "head":
            options[CONF_HEAD_FULL_TRAVEL_SECONDS] = round(duration_seconds)
        else:
            options[CONF_FEET_FULL_TRAVEL_SECONDS] = round(duration_seconds)
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


class _OctoBedSyncButtonBase(ButtonEntity):
    """Shared plumbing for the sync-position buttons.

    Other beds are looked up by config entry id whenever they are needed and
    their changes arrive through a dispatcher signal, so the button keeps
    working when either bed is reloaded or finishes loading later.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_icon = "mdi:sync"

    def __init__(
        self,
        client: OctoBedClient,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        unique_id: str,
        bed_title: str,
    ) -> None:
        self._client = client
        self._entry = entry
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id
        self._attr_translation_key = "sync_to"
        self._attr_translation_placeholders = {"bed": bed_title}

    async def async_added_to_hass(self) -> None:
        """Follow calibration of this device and changes of any bed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_update)
        )
        self.async_on_remove(
            self._client.register_position_callback(self._on_position)
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_BED_UPDATE, self._on_update)
        )

    @callback
    def _on_position(self, _part: str, _position: int) -> None:
        self.async_write_ha_state()

    @callback
    def _on_update(self, *_args: object) -> None:
        self.async_write_ha_state()

    def _bed_client(self, entry_id: str) -> OctoBedClient | None:
        client = (self.hass.data.get(DOMAIN) or {}).get(entry_id)
        return client if isinstance(client, OctoBedClient) else None


class OctoBedSyncToOtherButton(_OctoBedSyncButtonBase):
    """Button on an individual bed: copy the other bed's position to this bed."""

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
        super().__init__(
            client,
            entry,
            device_info,
            f"{unique_id_prefix}_sync_to_{other_entry_id}",
            other_title,
        )
        self._other_entry_id = other_entry_id
        self._other_title = other_title

    def _calibration_differs_from_other(self) -> str | None:
        """With exactly two separate beds: a reason when their calibration differs."""
        entries = list(self.hass.config_entries.async_entries(DOMAIN))
        beds = [e for e in entries if not (e.data or {}).get(CONF_IS_GROUP)]
        if len(beds) != 2 or any((e.data or {}).get(CONF_IS_GROUP) for e in entries):
            return None
        other_entry = self.hass.config_entries.async_get_entry(self._other_entry_id)
        if other_entry is None:
            return None
        if _travel_times(self._entry) != _travel_times(other_entry):
            return "Calibration differs from other bed"
        return None

    def _unavailable_reason(self) -> str | None:
        other = self._bed_client(self._other_entry_id)
        if other is None:
            return "Other bed is not loaded"
        if not (self._client.is_connected() and other.is_connected()):
            return "A bed is not connected"
        if self._client.is_calibration_active():
            return "Calibration in progress"
        if other.get_head_position() == 0 and other.get_feet_position() == 0:
            return "Other bed is flat"
        reason = self._calibration_differs_from_other()
        if reason:
            return reason
        if (
            self._client.get_head_position() == other.get_head_position()
            and self._client.get_feet_position() == other.get_feet_position()
        ):
            return "Beds are already at the same position"
        return None

    @property
    def available(self) -> bool:
        return self._unavailable_reason() is None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        reason = self._unavailable_reason()
        return {"unavailable_reason": reason} if reason else {}

    async def async_press(self) -> None:
        """Copy the other bed's head/feet position to this bed."""
        other = self._bed_client(self._other_entry_id)
        if other is None:
            _LOGGER.warning("Other bed %s not available for sync", self._other_title)
            return
        head_travel, feet_travel = _travel_times(self._entry)
        await _run_to_position_tracked(
            self._client,
            other.get_head_position(),
            other.get_feet_position(),
            head_travel,
            feet_travel,
        )


class OctoBedSyncToBedButton(_OctoBedSyncButtonBase):
    """Button on 'Both beds' device: set both beds to the chosen bed's position."""

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
        super().__init__(
            client,
            entry,
            device_info,
            f"{unique_id_prefix}_sync_to_{source_entry_id}",
            source_title,
        )
        self._source_entry_id = source_entry_id
        self._source_title = source_title

    def _unavailable_reason(self) -> str | None:
        source = self._bed_client(self._source_entry_id)
        if source is None:
            return "Source bed is not loaded"
        if not self._client.is_connected():
            return "A bed is not connected"
        if self._client.is_calibration_active():
            return "Calibration in progress"
        head, feet = source.get_head_position(), source.get_feet_position()
        if head == 0 and feet == 0:
            return "Source bed is flat"
        members = [
            self._bed_client(eid)
            for eid in self._entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        ]
        if all(
            m is not None
            and m.get_head_position() == head
            and m.get_feet_position() == feet
            for m in members
        ):
            return "Beds are already at the same position"
        return None

    @property
    def available(self) -> bool:
        return self._unavailable_reason() is None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        reason = self._unavailable_reason()
        return {"unavailable_reason": reason} if reason else {}

    async def async_press(self) -> None:
        """Set both beds to the source bed's head/feet position."""
        source = self._bed_client(self._source_entry_id)
        if source is None:
            _LOGGER.warning("Source bed %s not available for sync", self._source_title)
            return
        head_travel, feet_travel = _travel_times(self._entry)
        await _run_to_position_tracked(
            self._client,
            source.get_head_position(),
            source.get_feet_position(),
            head_travel,
            feet_travel,
        )
