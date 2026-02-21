"""Constants for the Octo Bed integration."""

DOMAIN = "octo_bed"

# BLE
OCTO_BED_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
COMMAND_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
COMMAND_HANDLE = 0x0011
NOTIFY_HANDLE = 0x0012

# Config
CONF_PIN = "pin"
CONF_DEVICE_ADDRESS = "device_address"
CONF_ENTRY_IDS = "entry_ids"
CONF_PAIR_WITH_ENTRY_ID = "pair_with_entry_id"
CONF_TYPE = "type"
TYPE_SINGLE = "single"
TYPE_COMBINED = "combined"
CONF_FULL_TRAVEL_SECONDS = "full_travel_seconds"
CONF_HEAD_FULL_TRAVEL_SECONDS = "head_full_travel_seconds"
CONF_FEET_FULL_TRAVEL_SECONDS = "feet_full_travel_seconds"
CONF_SHOW_CALIBRATION_BUTTONS = "show_calibration_buttons"
DEFAULT_FULL_TRAVEL_SECONDS = 30

# Commands (hex values to write to Handle 0x0011)
CMD_BOTH_DOWN = bytes.fromhex("4002710001060640")
CMD_BOTH_UP = bytes.fromhex("4002700001070640")
CMD_BOTH_UP_CONTINUOUS = bytes.fromhex("4002710001080440")
CMD_FEET_UP = bytes.fromhex("4002700001090440")
CMD_FEET_DOWN = bytes.fromhex("4002710001080440")
CMD_HEAD_DOWN = bytes.fromhex("40027100010a0240")
CMD_HEAD_UP = bytes.fromhex("40027000010b0240")
CMD_HEAD_UP_CONTINUOUS = bytes.fromhex("4002710001080440")
CMD_HEAD_UP_DOWN_CONTINUOUS = bytes.fromhex("4002710001080440")
CMD_STOP = bytes.fromhex("4002710001000040")
# Light control (captured from nRF sniffer)
CMD_LIGHT_OFF = bytes.fromhex("4020720008df000102010101010040")
CMD_LIGHT_ON = bytes.fromhex("4020720008de000102010101010140")

# PIN authentication - format: 402043000400 + 4 PIN digits as bytes + 40
# PIN digits: 0-9 encoded as 0x00-0x09
CMD_PIN_PREFIX = bytes.fromhex("402043000400")
CMD_PIN_SUFFIX = bytes.fromhex("40")

# Notifications that require PIN (keep-alive / re-auth)
# 40214400001b40 = periodic keep-alive request from bed
# 40217f0000e040 = initial "no PIN given" / auth required (from packet captures)
NOTIFY_PIN_REQUIRED = bytes.fromhex("40214400001b40")
NOTIFY_PIN_REQUIRED_ALT = bytes.fromhex("40217f0000e040")
NOTIFY_PIN_ACCEPTED = bytes.fromhex("40214300011a0140")
NOTIFY_PIN_REJECTED = bytes.fromhex("40214300011b0040")
