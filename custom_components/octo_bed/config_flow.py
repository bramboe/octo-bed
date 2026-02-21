"""Config flow for Octo Bed integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BEDS,
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_PAIRED,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

# Octo bed peripheral address from captures - can also discover by name
OCTO_BED_NAMES = ("Octo", "OCTO", "octo")


def format_address(address: str) -> str:
    """Format a Bluetooth address."""
    return address.upper().replace(":", "")


class OctoBedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Octo Bed."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._pairing_bed1: dict[str, str] | None = None  # address, pin, device_name
        self._pairing_address2: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(format_address(discovery_info.address))
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        # Always show MAC address to differentiate multiple RC2 devices
        device_name = discovery_info.name or "Octo Bed"
        self.context["title_placeholders"] = {
            "name": f"{device_name} ({discovery_info.address})"
        }
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to add manually."""
        if user_input is not None:
            address = user_input["address"].upper().replace(" ", "").replace(":", "")
            if len(address) != 12 or not all(c in "0123456789ABCDEF" for c in address):
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {vol.Required("address"): str}
                    ),
                    errors={"base": "invalid_address"},
                )
            address = ":".join(address[i : i + 2] for i in range(0, 12, 2))
            await self.async_set_unique_id(address.replace(":", ""))
            self._abort_if_unique_id_configured()
            self._discovery_info = None
            return await self.async_step_pin()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("address"): str
                }
            ),
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
                        device_name = (user_input.get("device_name") or "").strip()
                        title = f"Octo Bed ({address})" if not device_name else device_name
                        pair_with_another = user_input.get("pair_with_another", False)
                        if pair_with_another:
                            self._pairing_bed1 = {
                                "address": address,
                                "pin": pin,
                                "device_name": device_name or f"Octo Bed ({address})",
                            }
                            return await self.async_step_add_second_bed()
                        return self.async_create_entry(
                            title=title,
                            data={
                                "address": address,
                                "pin": pin,
                            },
                            options={
                                CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                                CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                            },
                        )

        schema = vol.Schema(
            {
                vol.Required("pin", default=user_input.get("pin", "") if user_input else ""): str,
                vol.Optional("device_name", default=user_input.get("device_name", "") if user_input else ""): str,
                vol.Optional("pair_with_another", default=user_input.get("pair_with_another", False) if user_input else False): bool,
            }
        )

        return self.async_show_form(
            step_id="pin",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_add_second_bed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for second bed address to pair, or skip to add only first bed."""
        if user_input is not None:
            address_second = (user_input.get("address") or "").strip()
            if not address_second:
                # Skip pairing: add only first bed
                if not self._pairing_bed1:
                    return self.async_abort(reason="pairing_lost")
                bed1 = self._pairing_bed1
                title = bed1["device_name"]
                self._pairing_bed1 = None
                return self.async_create_entry(
                    title=title,
                    data={"address": bed1["address"], "pin": bed1["pin"]},
                    options={
                        CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                        CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                    },
                )
            # Validate second bed address
            address = address_second.upper().replace(" ", "").replace(":", "")
            if len(address) != 12 or not all(c in "0123456789ABCDEF" for c in address):
                return self.async_show_form(
                    step_id="add_second_bed",
                    data_schema=self._add_second_bed_schema(user_input),
                    errors={"base": "invalid_address"},
                )
            address = ":".join(address[i : i + 2] for i in range(0, 12, 2))
            # Must be different from first bed
            addr1 = (self._pairing_bed1 or {}).get("address", "")
            if address.upper() == addr1.upper().replace(":", ""):
                return self.async_show_form(
                    step_id="add_second_bed",
                    data_schema=self._add_second_bed_schema(user_input),
                    errors={"base": "same_as_first"},
                )
            self._pairing_address2 = address
            return await self.async_step_pin_second()

        return self.async_show_form(
            step_id="add_second_bed",
            data_schema=self._add_second_bed_schema(None),
        )

    def _add_second_bed_schema(
        self, user_input: dict[str, Any] | None
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(
                    "address",
                    default=user_input.get("address", "") if user_input else "",
                ): str,
            }
        )

    async def async_step_pin_second(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """PIN entry for second bed when pairing."""
        errors: dict[str, str] = {}
        address = self._pairing_address2 or ""

        if user_input is not None:
            pin = user_input.get("pin", "")
            if len(pin) != 4 or not pin.isdigit():
                errors["base"] = "invalid_pin"
            else:
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
                        device_name_2 = (user_input.get("device_name") or "").strip()
                        if not device_name_2:
                            device_name_2 = f"Octo Bed ({address})"
                        bed1 = self._pairing_bed1 or {}
                        bed2_name = device_name_2
                        bed1_name = bed1.get("device_name") or f"Octo Bed ({bed1.get('address', '')})"
                        # Create paired entry: one entry with both beds
                        await self.async_set_unique_id(
                            f"paired_{format_address(bed1['address'])}_{format_address(address)}"
                        )
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=f"{bed1_name} & {bed2_name}",
                            data={
                                CONF_PAIRED: True,
                                CONF_BEDS: [
                                    {
                                        "address": bed1["address"],
                                        "pin": bed1["pin"],
                                        "name": bed1_name,
                                    },
                                    {
                                        "address": address,
                                        "pin": pin,
                                        "name": bed2_name,
                                    },
                                ],
                            },
                            options={
                                CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                                CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                            },
                        )

        schema = vol.Schema(
            {
                vol.Required("pin", default=user_input.get("pin", "") if user_input else ""): str,
                vol.Optional("device_name", default=user_input.get("device_name", "") if user_input else ""): str,
            }
        )
        return self.async_show_form(
            step_id="pin_second",
            data_schema=schema,
            description_placeholders={"address": address},
            errors=errors,
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
            return self.async_create_entry(
                title="",
                data={
                    CONF_HEAD_FULL_TRAVEL_SECONDS: head,
                    CONF_FEET_FULL_TRAVEL_SECONDS: feet,
                    CONF_SHOW_CALIBRATION_BUTTONS: user_input[CONF_SHOW_CALIBRATION_BUTTONS],
                },
            )

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
