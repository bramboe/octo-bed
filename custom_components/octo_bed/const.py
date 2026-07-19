"""Constants for the Octo Bed integration."""

from . import protocol

DOMAIN = "octo_bed"

# BLE
OCTO_BED_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
COMMAND_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Config
CONF_PIN = "pin"
CONF_DEVICE_ADDRESS = "device_address"
CONF_IS_GROUP = "is_group"
CONF_MEMBER_ENTRY_IDS = "member_entry_ids"
CONF_PAIR_WITH_ENTRY_ID = "pair_with_entry_id"
CONF_PAIR_CALIBRATE = "pair_calibrate"
CONF_CALIBRATE_ON_ADD = "calibrate_on_add"
CONF_GROUP_OPTIONS = "group_options"
CONF_FULL_TRAVEL_SECONDS = "full_travel_seconds"
CONF_HEAD_FULL_TRAVEL_SECONDS = "head_full_travel_seconds"
CONF_FEET_FULL_TRAVEL_SECONDS = "feet_full_travel_seconds"
CONF_SHOW_CALIBRATION_BUTTONS = "show_calibration_buttons"
CONF_SOFT_PRESETS = "soft_presets"
# Pin the bed's BLE connection to a specific Bluetooth proxy/adapter (scanner
# source MAC). PROXY_SOURCE_AUTO leaves proxy selection to Home Assistant
# (best RSSI / load balancing), which is the default behaviour.
CONF_PROXY_SOURCE = "proxy_source"
PROXY_SOURCE_AUTO = "auto"
DEFAULT_FULL_TRAVEL_SECONDS = 30
SOFT_PRESET_SLOTS = 3

# The bed drops the BLE connection after ~30 s without PIN re-authentication,
# so refresh well within that window.
PIN_KEEPALIVE_SECONDS = 25

# Movement commands (built with checksum + byte stuffing)
CMD_HEAD_UP = protocol.build_packet(protocol.CMD_MOTOR_UP, [protocol.MOTOR_HEAD])
CMD_HEAD_DOWN = protocol.build_packet(protocol.CMD_MOTOR_DOWN, [protocol.MOTOR_HEAD])
CMD_FEET_UP = protocol.build_packet(protocol.CMD_MOTOR_UP, [protocol.MOTOR_FEET])
CMD_FEET_DOWN = protocol.build_packet(protocol.CMD_MOTOR_DOWN, [protocol.MOTOR_FEET])
CMD_BOTH_UP = protocol.build_packet(protocol.CMD_MOTOR_UP, [protocol.MOTOR_BOTH])
CMD_BOTH_DOWN = protocol.build_packet(protocol.CMD_MOTOR_DOWN, [protocol.MOTOR_BOTH])
CMD_STOP = protocol.build_packet(protocol.CMD_MOTOR_STOP)

# Light control (capability write to CAP_LIGHT; values from packet captures)
CMD_LIGHT_ON = protocol.build_packet(
    protocol.CMD_SYSTEM_SET_CAPS, [0x00, 0x01, 0x02, 0x01, 0x01, 0x01, 0x01, 0x01]
)
CMD_LIGHT_OFF = protocol.build_packet(
    protocol.CMD_SYSTEM_SET_CAPS, [0x00, 0x01, 0x02, 0x01, 0x01, 0x01, 0x01, 0x00]
)

# Notifications that require PIN (keep-alive / re-auth)
# 40214400001b40 = periodic keep-alive request from bed
# 40217f0000e040 = initial "no PIN given" / auth required (from packet captures)
NOTIFY_PIN_REQUIRED = bytes.fromhex("40214400001b40")
NOTIFY_PIN_REQUIRED_ALT = bytes.fromhex("40217f0000e040")
NOTIFY_PIN_ACCEPTED = bytes.fromhex("40214300011a0140")
NOTIFY_PIN_REJECTED = bytes.fromhex("40214300011b0040")
