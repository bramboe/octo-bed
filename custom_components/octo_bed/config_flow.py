"""Config flow for Octo Bed integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

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
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
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
                )

        schema = vol.Schema(
            {
                vol.Required("pin"): vol.All(
                    str,
                    vol.Length(min=4, max=4),
                    vol.Match(r"^\d{4}$", message="PIN must be 4 digits"),
                ),
            }
        )

        return self.async_show_form(
            step_id="pin",
            data_schema=schema,
            errors=errors,
        )
