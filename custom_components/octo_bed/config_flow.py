"""Config flow for Octo Bed integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

try:  # HA 2024.4+
    from homeassistant.config_entries import ConfigFlowResult
except ImportError:  # pragma: no cover - older cores
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult

from .const import (
    CONF_DEVICE_ADDRESS,
    CONF_FEET_FULL_TRAVEL_SECONDS,
    CONF_FULL_TRAVEL_SECONDS,
    CONF_GROUP_OPTIONS,
    CONF_HEAD_FULL_TRAVEL_SECONDS,
    CONF_IS_GROUP,
    CONF_MEMBER_ENTRY_IDS,
    CONF_PAIR_CALIBRATE,
    CONF_PAIR_WITH_ENTRY_ID,
    CONF_SHOW_CALIBRATION_BUTTONS,
    DEFAULT_FULL_TRAVEL_SECONDS,
    DOMAIN,
    OCTO_BED_SERVICE_UUID,
)
from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)

OCTO_BED_NAMES = ("Octo", "OCTO", "octo", "RC2")

# Sentinel for "do not pair" in the pair_choice step. An empty string cannot
# be used: the frontend treats it as an unfilled required field.
NO_PAIR = "none"


def _is_real_bed(entry: ConfigEntry) -> bool:
    """True for actual bed entries (not groups, not ignored discoveries)."""
    return (
        not (entry.data or {}).get(CONF_IS_GROUP)
        and entry.source != config_entries.SOURCE_IGNORE
    )


def _bed_label(entry: ConfigEntry) -> str:
    """Human-readable label for a bed entry (title, with address if not already in it)."""
    title = entry.title or "Octo Bed"
    data = entry.data or {}
    address = data.get("address") or data.get(CONF_DEVICE_ADDRESS) or ""
    if address and address not in title:
        return f"{title} ({address})"
    return title


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


def _get_related_entry_ids(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
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
        self._pin_validated = False
        self._address: str | None = None
        self._pin: str | None = None
        self._device_name: str = ""
        self._other_beds: list[ConfigEntry] | None = None
        self._pair_calibrate: bool = True

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(format_address(discovery_info.address))
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        device_name = discovery_info.name or "Octo Bed"
        self.context["title_placeholders"] = {
            "name": f"{device_name} ({discovery_info.address})"
        }
        return await self.async_step_confirm()

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create the internal 'Both beds' group entry (started by the integration)."""
        if not import_data or not import_data.get(CONF_IS_GROUP):
            return self.async_abort(reason="not_supported")
        member_ids = list(import_data.get(CONF_MEMBER_ENTRY_IDS) or [])
        if len(member_ids) < 2:
            return self.async_abort(reason="need_two_beds")
        member_set = set(member_ids)
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if not e.data.get(CONF_IS_GROUP):
                continue
            if set(e.data.get(CONF_MEMBER_ENTRY_IDS) or []) == member_set:
                return self.async_abort(reason="already_paired")
        await self.async_set_unique_id(f"group_{'_'.join(sorted(member_ids))}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Both beds",
            data={CONF_IS_GROUP: True, CONF_MEMBER_ENTRY_IDS: member_ids},
            options=dict(import_data.get(CONF_GROUP_OPTIONS) or {}),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step: choose how to add or set up an Octo bed."""
        if user_input is not None:
            method = user_input.get("method")
            if method == "manual":
                return await self.async_step_manual_address()
            if method == "pair":
                return await self.async_step_pair_existing()
            return await self.async_step_pick_bed()

        # Build method list: always Discover beds, Enter address; add Pair when 2+ beds exist
        non_group = [
            e for e in self.hass.config_entries.async_entries(DOMAIN)
            if _is_real_bed(e)
        ]
        method_options = [
            ("discovered", "Choose from discovered beds"),
            ("manual", "Enter Bluetooth address manually"),
        ]
        if len(non_group) >= 2:
            method_options.append(("pair", "Pair two existing beds"))

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="discovered"): vol.In(
                        dict(method_options)
                    ),
                }
            ),
        )

    async def async_step_pair_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a 'Both beds' device from two existing beds."""
        non_group = [
            e for e in self.hass.config_entries.async_entries(DOMAIN)
            if _is_real_bed(e)
        ]
        if len(non_group) < 2:
            return self.async_abort(reason="need_two_beds")

        if user_input is None:
            return self.async_show_form(
                step_id="pair_existing",
                data_schema=self._pair_existing_schema(non_group),
            )

        # Exactly 2 beds: use them directly, no need to ask which two
        if len(non_group) == 2:
            entry1, entry2 = non_group[0], non_group[1]
            eid1, eid2 = entry1.entry_id, entry2.entry_id
        else:
            eid1 = user_input.get("bed_1")
            eid2 = user_input.get("bed_2")
            if not eid1 or not eid2 or eid1 == eid2:
                return self.async_show_form(
                    step_id="pair_existing",
                    data_schema=self._pair_existing_schema(non_group),
                    errors={"base": "select_two_different"},
                )
            entry1 = self.hass.config_entries.async_get_entry(eid1)
            entry2 = self.hass.config_entries.async_get_entry(eid2)
            if not entry1 or not entry2:
                return self.async_abort(reason="entry_not_found")
        calibrate_both = bool(user_input.get("calibrate_both", True))

        member_ids = [eid1, eid2]
        member_set = set(member_ids)
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if not e.data.get(CONF_IS_GROUP):
                continue
            existing = set(e.data.get(CONF_MEMBER_ENTRY_IDS) or [])
            if existing == member_set:
                return self.async_abort(reason="already_paired")

        group_options = dict(entry1.options or {})
        if not group_options:
            group_options = {
                CONF_HEAD_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
                CONF_FEET_FULL_TRAVEL_SECONDS: DEFAULT_FULL_TRAVEL_SECONDS,
            }
        # Unify calibration: ensure both beds have the same head/feet travel as the group
        head = group_options.get(
            CONF_HEAD_FULL_TRAVEL_SECONDS,
            group_options.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS),
        )
        feet = group_options.get(
            CONF_FEET_FULL_TRAVEL_SECONDS,
            group_options.get(CONF_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS),
        )
        group_options[CONF_HEAD_FULL_TRAVEL_SECONDS] = head
        group_options[CONF_FEET_FULL_TRAVEL_SECONDS] = feet
        # User choice: calibrate both beds together via the combined device
        group_options[CONF_SHOW_CALIBRATION_BUTTONS] = calibrate_both
        for member_entry in (entry1, entry2):
            merged = dict(member_entry.options or {})
            merged[CONF_HEAD_FULL_TRAVEL_SECONDS] = head
            merged[CONF_FEET_FULL_TRAVEL_SECONDS] = feet
            merged[CONF_SHOW_CALIBRATION_BUTTONS] = calibrate_both
            self.hass.config_entries.async_update_entry(member_entry, options=merged)

        await self.async_set_unique_id(f"group_{'_'.join(sorted(member_ids))}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Both beds",
            data={CONF_IS_GROUP: True, CONF_MEMBER_ENTRY_IDS: member_ids},
            options=group_options,
        )

    def _pair_existing_schema(
        self, non_group: list[ConfigEntry]
    ) -> vol.Schema:
        """Build schema: bed pickers (only when more than 2 beds) + calibrate toggle."""
        schema: dict[Any, Any] = {}
        if len(non_group) > 2:
            choices = [(e.entry_id, _bed_label(e)) for e in non_group]
            schema[vol.Required("bed_1")] = vol.In(dict(choices))
            schema[vol.Required("bed_2")] = vol.In(dict(choices))
        schema[vol.Required("calibrate_both", default=True)] = bool
        return vol.Schema(schema)

    async def async_step_manual_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
    ) -> ConfigFlowResult:
        """Show list of discovered Octo beds to add."""
        configured_addrs = {
            (e.unique_id or "").upper().replace(":", "")
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if not e.data.get(CONF_IS_GROUP) and (e.unique_id or e.data.get("address"))
        }
        for e in self.hass.config_entries.async_entries(DOMAIN):
            if e.data.get(CONF_IS_GROUP):
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
    ) -> ConfigFlowResult:
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

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                            if _is_real_bed(e)
                        ]
                        self._other_beds = other_beds
                        if other_beds:
                            return await self.async_step_pair_choice()
                        return self._create_bed_entry()

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

    async def async_step_calibrate_choice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask whether both beds should be calibrated together via the combined device."""
        if user_input is not None:
            self._pair_calibrate = bool(user_input.get("calibrate_both", True))
            return self._create_bed_entry()

        return self.async_show_form(
            step_id="calibrate_choice",
            data_schema=vol.Schema(
                {vol.Required("calibrate_both", default=True): bool}
            ),
        )

    def _create_bed_entry(self) -> ConfigFlowResult:
        """Create the config entry for the bed (and optionally group)."""
        title = f"Octo Bed ({self._address})" if not self._device_name else self._device_name
        data = {"address": self._address, "pin": self._pin}
        if self._pair_with_entry_id:
            data[CONF_PAIR_WITH_ENTRY_ID] = self._pair_with_entry_id
            data[CONF_PAIR_CALIBRATE] = getattr(self, "_pair_calibrate", True)
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
    ) -> ConfigFlowResult:
        """Ask whether to pair this bed with another as one combined device."""
        if user_input is not None:
            pair = (user_input.get("pair") or NO_PAIR).strip()
            self._pair_with_entry_id = None if pair in ("", NO_PAIR) else pair
            if self._pair_with_entry_id:
                return await self.async_step_calibrate_choice()
            return self._create_bed_entry()

        other_beds = self._other_beds
        if other_beds is None:
            other_beds = [
                e for e in self.hass.config_entries.async_entries(DOMAIN)
                if _is_real_bed(e)
            ]
        if not other_beds:
            return self._create_bed_entry()

        pair_options = [(e.entry_id, _bed_label(e)) for e in other_beds]
        pair_options.insert(0, (NO_PAIR, "No, keep as separate devices"))
        return self.async_show_form(
            step_id="pair_choice",
            data_schema=vol.Schema(
                {
                    vol.Required("pair", default=NO_PAIR): vol.In(dict(pair_options)),
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
    ) -> ConfigFlowResult:
        """Single Configuration step: travel times and calibration controls."""
        if user_input is not None:
            head = int(user_input.get(CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS))
            feet = int(user_input.get(CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS))
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
        )

    def _options_schema(
        self, input_values: dict[str, Any] | None = None
    ) -> vol.Schema:
        """Build options schema: travel times + show calibration buttons."""
        opts = input_values or self.config_entry.options
        current_head = opts.get(CONF_HEAD_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        current_feet = opts.get(CONF_FEET_FULL_TRAVEL_SECONDS, DEFAULT_FULL_TRAVEL_SECONDS)
        current_buttons = opts.get(CONF_SHOW_CALIBRATION_BUTTONS, True)
        travel_selector = NumberSelector(
            NumberSelectorConfig(
                min=5, max=120, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_HEAD_FULL_TRAVEL_SECONDS, default=current_head
                ): travel_selector,
                vol.Required(
                    CONF_FEET_FULL_TRAVEL_SECONDS, default=current_feet
                ): travel_selector,
                vol.Required(
                    CONF_SHOW_CALIBRATION_BUTTONS,
                    default=current_buttons,
                ): bool,
            }
        )
