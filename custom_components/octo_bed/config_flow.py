"""Config flow for Octo Bed integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_PAIR_WITH_ENTRY_ID,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
    OCTO_BED_SERVICE_UUID,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

# Octo bed peripheral address from captures - can also discover by name
OCTO_BED_NAMES = ("Octo", "OCTO", "octo")


def format_address(address: str) -> str:
    """Format a Bluetooth address."""
    return address.upper().replace(":", "")


def _is_octo_bed(info: BluetoothServiceInfoBleak) -> bool:
    """Return True if this discovery looks like an Octo bed."""
    if info.name and info.name.strip() in OCTO_BED_NAMES:
        return True
    if info.service_uuids and OCTO_BED_SERVICE_UUID in info.service_uuids:
        return True
    return False


def _get_related_entry_ids(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> list[str]:
    """Return entry IDs that share the same pair (this entry + group + other member if paired)."""
    entry_id = entry.entry_id
    if entry.data.get(CONF_IS_GROUP):
        member_ids = entry.data.get(CONF_MEMBER_ENTRY_IDS) or []
        return [entry_id, *member_ids]
    for e in hass.config_entries.async_entries(DOMAIN):
        if not e.data.get(CONF_IS_GROUP):
            continue
        member_ids = e.data.get(CONF_MEMBER_ENTRY_IDS) or []
        if entry_id in member_ids:
            return [entry_id, e.entry_id, *(m for m in member_ids if m != entry_id)]
    return [entry_id]


class OctoBedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Octo Bed."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._pair_with_entry_id: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(format_address(discovery_info.address))
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        device_name = discovery_info.name or "Octo Bed"
        self.context["title_placeholders"] = {
            "name": f"{device_name} ({discovery_info.address})"
        }
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step: choose discovered bed or manual entry."""
        if user_input is not None:
            if user_input.get("method") == "manual":
                return await self.async_step_manual_address()
            return await self.async_step_pick_bed()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="discovered"): vol.In([
                        ("discovered", "Select from discovered beds"),
                        ("manual", "Enter Bluetooth address manually"),
                    ]),
                }
            ),
        )

    async def async_step_manual_address(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual Bluetooth address entry."""
        if user_input is not None:
            address = user_input["address"].upper().replace(" ", "").replace(":", "")
            if len(address) != 12 or not all(c in "0123456789ABCDEF" for c in address):
                return self.async_show_form(
                    step_id="manual_address",
                    data_schema=vol.Schema({vol.Required("address"): str}),
                    errors={"base": "invalid_address"},
                )
            address = ":".join(address[i : i + 2] for i in range(0, 12, 2))
            await self.async_set_unique_id(address.replace(":", ""))
            self._abort_if_unique_id_configured()
            self._discovery_info = None
            return await self.async_step_pin()

        return self.async_show_form(
            step_id="manual_address",
            data_schema=vol.Schema(
                {vol.Required("address"): str}
            ),
        )

    async def async_step_pick_bed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show list of discovered Octo beds to add."""
        configured_addrs = {
            (e.unique_id or "").upper().replace(":", "")
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if not e.data.get("is_group") and (e.unique_id or e.data.get("address"))
        }
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if e.data.get("is_group"):
                continue
            addr = (e.data.get("address") or e.unique_id or "").upper().replace(":", "")
            if len(addr) == 12:
                configured_addrs.add(addr)

        discovered = list(bluetooth.async_discovered_service_info(self.hass, connectable=True))
        beds: list[tuple[str, str]] = []
        seen_addrs: set[str] = set()
        for info in discovered:
            if not _is_octo_bed(info):
                continue
            addr_key = format_address(info.address)
            if addr_key in seen_addrs or addr_key in configured_addrs:
                continue
            seen_addrs.add(addr_key)
            label = f"{info.name or 'Octo Bed'} ({info.address})"
            beds.append((info.address, label))

        if user_input is not None:
            address = user_input.get("address")
            if address and address != "manual":
                await self.async_set_unique_id(format_address(address))
                self._abort_if_unique_id_configured()
                self._discovery_info = bluetooth.async_last_service_info(
                    self.hass, address, connectable=True
                )
                if not self._discovery_info:
                    self._discovery_info = None
                return await self.async_step_pin()
            return await self.async_step_manual_address()

        if not beds:
            return self.async_show_form(
                step_id="pick_bed",
                data_schema=vol.Schema(
                    {vol.Required("address", default="manual"): vol.In({"manual": "Enter address manually"})}
                ),
                description_placeholders={"message": "No new Octo beds found. Ensure beds are on and in range, or enter the address manually."},
            )

        options = {"manual": "Enter address manually"}
        options.update(dict(beds))
        return self.async_show_form(
            step_id="pick_bed",
            data_schema=vol.Schema(
                {vol.Required("address"): vol.In(options)}
            ),
            description_placeholders={"message": "Select a bed to add. Each bed needs its own PIN."},
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user-confirmation of discovered device."""
        if user_input is not None:
            return await self.async_step_pin()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": self._discovery_info.address
                if self._discovery_info
                else "",
            },
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OctoBedOptionsFlow:
        """Get the options flow for this handler."""
        return OctoBedOptionsFlow()

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Handle import from configuration.yaml."""
        return await self.async_step_user(import_data)

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle PIN entry - required to establish connection with the bed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pin = user_input.get("pin", "")
            if len(pin) != 4 or not pin.isdigit():
                errors["base"] = "invalid_pin"
            else:
                address = (
                    self._discovery_info.address
                    if self._discovery_info
                    else (self.unique_id or "")
                )
                if ":" not in address and len(address) == 12:
                    address = ":".join(
                        address[i : i + 2] for i in range(0, 12, 2)
                    )

                bleak_device = bluetooth.async_ble_device_from_address(
                    self.hass, address, connectable=True
                )
                if not bleak_device:
                    errors["base"] = "device_unavailable"
                else:
                    client = OctoBedClient(bleak_device, pin)
                    if not await client.connect_and_verify_pin():
                        await client.disconnect()
                        errors["base"] = "pin_rejected"
                    else:
                        await client.disconnect()
                        self._pin_validated = True
                        self._address = address
                        self._pin = pin
                        self._device_name = (user_input.get("device_name") or "").strip()
                        other_beds = [
                            e for e in self.hass.config_entries.async_entries(DOMAIN)
                            if not (e.data or {}).get(CONF_IS_GROUP) and getattr(e, "entry_id", None)
                        ]
                        self._other_beds = other_beds
                        if other_beds:
                            return await self.async_step_pair_choice()
                        return self._create_bed_entry()

        self._pin_validated = False

        schema = vol.Schema(
            {
                vol.Required("pin", default=user_input.get("pin", "") if user_input else ""): str,
                vol.Optional("device_name", default=user_input.get("device_name", "") if user_input else ""): str,
            }
        )

        return self.async_show_form(
            step_id="pin",
            data_schema=schema,
            errors=errors,
        )

    def _create_bed_entry(self) -> FlowResult:
        """Create the config entry for the bed (and optionally group)."""
        title = f"Octo Bed ({self._address})" if not self._device_name else self._device_name
        data = {"address": self._address, "pin": self._pin}
        if self._pair_with_entry_id:
            data[CONF_PAIR_WITH_ENTRY_ID] = self._pair_with_entry_id
        return self.async_create_entry(
            title=title,
            data=data,
            options={
                CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
            },
        )

    async def async_step_pair_choice(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask whether to pair this bed with another as one combined device."""
        if user_input is not None:
            self._pair_with_entry_id = (user_input.get("pair") or "").strip() or None
            return self._create_bed_entry()

        other_beds = getattr(self, "_other_beds", None)
        if other_beds is None:
            other_beds = [
                e for e in self.hass.config_entries.async_entries(DOMAIN)
                if not (e.data or {}).get(CONF_IS_GROUP)
            ]
        if not other_beds:
            return self._create_bed_entry()

        pair_options = [(e.entry_id, f"{e.title} ({(e.data or {}).get('address', '')})") for e in other_beds]
        pair_options.insert(0, ("", "No, keep as separate devices"))
        return self.async_show_form(
            step_id="pair_choice",
            data_schema=vol.Schema(
                {
                    vol.Required("pair", default=""): vol.In(dict(pair_options)),
                }
            ),
            description_placeholders={
                "message": "You can pair this bed with another to create a combined device that controls both beds together. Each bed remains its own device; the paired device adds shared controls.",
            },
        )


class OctoBedOptionsFlow(config_entries.OptionsFlow):
    """Handle Octo Bed options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Single Configuration step: travel times and calibration controls."""
        if user_input is not None:
            try:
                head = int(user_input.get(CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS))
                feet = int(user_input.get(CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS))
            except (TypeError, ValueError):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(),
                    description_placeholders={"config_description": "Set full travel time (seconds) for head and feet (updated by calibration or default 30 s). Optionally show or hide the calibration buttons on the device."},
                    errors={"base": "invalid_number"},
                )
            if not (5 <= head <= 120 and 5 <= feet <= 120):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(user_input),
                    description_placeholders={"config_description": "Set full travel time (seconds) for head and feet (updated by calibration or default 30 s). Optionally show or hide the calibration buttons on the device."},
                    errors={"base": "invalid_range"},
                )
            new_options = {
                CONF_HEAD_FULL_TRAVEL_SECONDS: head,
                CONF_FEET_FULL_TRAVEL_SECONDS: feet,
                CONF_SHOW_CALIBRATION_BUTTONS: user_input[CONF_SHOW_CALIBRATION_BUTTONS],
            }
            # When paired: keep head/feet travel in sync across group and both member beds
            for entry_id in _get_related_entry_ids(self.hass, self.config_entry):
                if entry_id == self.config_entry.entry_id:
                    continue
                other = self.hass.config_entries.async_get_entry(entry_id)
                if other is None:
                    continue
                merged = dict(other.options or {})
                merged[CONF_HEAD_FULL_TRAVEL_SECONDS] = head
                merged[CONF_FEET_FULL_TRAVEL_SECONDS] = feet
                merged[CONF_SHOW_CALIBRATION_BUTTONS] = new_options[CONF_SHOW_CALIBRATION_BUTTONS]
                self.hass.config_entries.async_update_entry(other, options=merged)
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(),
            description_placeholders={"config_description": "Set full travel time (seconds) for head and feet (updated by calibration or default 30 s). Optionally show or hide the calibration buttons on the device."},
        )

    def _options_schema(
        self, input_values: dict[str, Any] | None = None
    ) -> vol.Schema:
        """Build options schema: travel times + show calibration buttons."""
        opts = input_values or self.config_entry.options
        current_head = opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        current_feet = opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        current_buttons = opts.get(CONF_SHOW_CALIBRATION_BUTTONS, True)
        return vol.Schema(
            {
                vol.Required(
                    CONF_HEAD_FULL_TRAVEL_SECONDS,
                    default=str(current_head),
                ): str,
                vol.Required(
                    CONF_FEET_FULL_TRAVEL_SECONDS,
                    default=str(current_feet),
                ): str,
                vol.Required(
                    CONF_SHOW_CALIBRATION_BUTTONS,
                    default=current_buttons,
                ): bool,
            }
        )
