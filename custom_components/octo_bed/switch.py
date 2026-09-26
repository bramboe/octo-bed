"""Switch entities for Octo Bed (movement control + synchro mode)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octo Bed switches from a config entry."""
    client: OctoBedClient = hass.data[DOMAIN][entry.entry_id]
    uid = entry.unique_id or entry.entry_id

    # The under-bed light moved to the light platform; drop the old switch entity
    ent_reg = er.async_get(hass)
    old_light = ent_reg.async_get_entity_id("switch", DOMAIN, f"{uid}_light")
    if old_light:
        _LOGGER.info("Removing legacy light switch entity %s (now a light entity)", old_light)
        ent_reg.async_remove(old_light)

    device_info = DeviceInfo(
        identifiers={(DOMAIN, uid)},
        name=entry.title or "Octo Bed",
        manufacturer="Octo",
    )

    entities: list[SwitchEntity] = [
        OctoBedMovementSwitch(
            client, "both_up", "mdi:arrow-up-bold", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "both_down", "mdi:arrow-down-bold", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "head_up", "mdi:arrow-up", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "head_down", "mdi:arrow-down", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "feet_up", "mdi:arrow-up", device_info, entry, uid
        ),
        OctoBedMovementSwitch(
            client, "feet_down", "mdi:arrow-down", device_info, entry, uid
        ),
    ]

    # Linked/synchro drive mode (only on beds that report the capability)
    if client.has_synchro:
        entities.append(OctoBedSynchroSwitch(client, device_info, uid))

    async_add_entities(entities)


class OctoBedSynchroSwitch(SwitchEntity):
    """Toggle the bed's linked (synchro) drive mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "synchro"
    _attr_icon = "mdi:link-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, client: OctoBedClient, device_info: DeviceInfo, unique_id_prefix: str
    ) -> None:
        """Initialize the synchro switch."""
        self._client = client
        self._attr_device_info = device_info
        self._attr_unique_id = f"{unique_id_prefix}_synchro"

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
        return self._client.is_connected()

    @property
    def is_on(self) -> bool | None:
        return self._client.synchro_active

    async def async_turn_on(self, **kwargs: Any) -> None:
        if await self._client.set_synchro_mode(True):
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if await self._client.set_synchro_mode(False):
            self.async_write_ha_state()


class OctoBedMovementSwitch(SwitchEntity):
    """Hold-to-run movement: on drives the part(s) towards the end stop."""

    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(
        self,
        client: OctoBedClient,
        action: str,
        icon: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the movement switch."""
        self._client = client
        self._action = action
        self._attr_translation_key = action
        self._attr_icon = icon
        self._attr_unique_id = f"{unique_id_prefix}_move_{action}"
        self._attr_device_info = device_info
        self._entry = entry
        self._task: asyncio.Task[Any] | None = None
        self._up = action.endswith("_up")
        self._part = action.split("_", 1)[0]  # "head", "feet" or "both"
        self._parts = ("head", "feet") if self._part == "both" else (self._part,)

    def _travel_seconds(self) -> tuple[float, float]:
        """(head, feet) full travel seconds, read live so calibration applies."""
        opts = self._entry.options
        default = opts.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        return (
            float(opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, default)),
            float(opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, default)),
        )

    async def async_added_to_hass(self) -> None:
        """Register for calibration and connection updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.register_calibration_state_callback(self._on_state_changed)
        )
        self.async_on_remove(
            self._client.register_connection_callback(self._on_state_changed)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop driving the motors when the entity goes away."""
        if await self._cancel_task():
            await self._client.send_stop()

    @callback
    def _on_state_changed(self, *_args: Any) -> None:
        """Update availability when calibration or connection state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available when connected and no calibration is active."""
        return self._client.is_connected() and not self._client.is_calibration_active()

    @property
    def is_on(self) -> bool:
        """Return true while the movement is running."""
        return self._task is not None and not self._task.done()

    async def _cancel_task(self) -> bool:
        task = self._task
        self._task = None
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    @callback
    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        if self._task is task:
            self._task = None
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Movement %s failed: %s", self._action, task.exception())
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start movement in the configured direction."""
        if self.is_on:
            return
        head_travel, feet_travel = self._travel_seconds()
        task = asyncio.create_task(
            self._client.run_hold(self._up, self._parts, head_travel, feet_travel)
        )
        self._task = task
        self._client.register_movement_task(task)
        # Register which part is moving so conflicting moves get cancelled
        self._client.register_active_movement(self._part, task)
        task.add_done_callback(self._on_task_done)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop movement."""
        await self._cancel_task()
        await self._client.stop()
        self.async_write_ha_state()
