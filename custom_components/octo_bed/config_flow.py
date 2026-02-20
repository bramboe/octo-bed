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
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
)

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

                return self.async_create_entry(
                    title="Octo Bed",
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
                vol.Required("pin"): str,
            }
        )

        return self.async_show_form(
            step_id="pin",
            data_schema=schema,
            errors=errors,
        )


class OctoBedOptionsFlow(config_entries.OptionsFlow):
    """Handle Octo Bed options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            try:
                head = int(user_input.get(CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS))
                feet = int(user_input.get(CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS))
            except (TypeError, ValueError):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(),
                    description_placeholders={"calibration_description": "Set full travel time (seconds) for head and feet. These are updated when you complete calibration, or use 30 s as default."},
                    errors={"base": "invalid_number"},
                )
            if not (5 <= head <= 120 and 5 <= feet <= 120):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(user_input),
                    description_placeholders={"calibration_description": "Set full travel time (seconds) for head and feet. These are updated when you complete calibration, or use 30 s as default."},
                    errors={"base": "invalid_range"},
                )
            # Store and go to calibration controls step
            self._pending_calibration = {
                CONF_HEAD_FULL_TRAVEL_SECONDS: head,
                CONF_FEET_FULL_TRAVEL_SECONDS: feet,
            }
            return await self.async_step_calibration_controls()

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(),
            description_placeholders={"calibration_description": "Set full travel time (seconds) for head and feet. These are updated when you complete calibration, or use 30 s as default."},
        )

    async def async_step_calibration_controls(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Calibration controls: show/hide calibration buttons."""
        if user_input is not None:
            if hasattr(self, "_pending_calibration"):
                base = self._pending_calibration
            else:
                base = {
                    CONF_HEAD_FULL_TRAVEL_SECONDS: self.config_entry.options.get(
                        CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS
                    ),
                    CONF_FEET_FULL_TRAVEL_SECONDS: self.config_entry.options.get(
                        CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS
                    ),
                }
            data = {
                **base,
                CONF_SHOW_CALIBRATION_BUTTONS: user_input[CONF_SHOW_CALIBRATION_BUTTONS],
            }
            return self.async_create_entry(title="", data=data)

        current = self.config_entry.options.get(CONF_SHOW_CALIBRATION_BUTTONS, True)
        return self.async_show_form(
            step_id="calibration_controls",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SHOW_CALIBRATION_BUTTONS,
                        default=current,
                    ): bool,
                }
            ),
            description_placeholders={
                "calibration_controls_description": "Show or hide the Calibrate head, Calibrate feet, and Complete calibration buttons on the device.",
            },
        )

    def _options_schema(
        self, input_values: dict[str, Any] | None = None
    ) -> vol.Schema:
        """Build options schema with number input fields (default 30 if no calibration)."""
        opts = input_values or self.config_entry.options
        current_head = opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        current_feet = opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
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
            }
        )
