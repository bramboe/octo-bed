"""Octo bed BLE packet protocol (standard variant).

Packet format: [0x40, escaped(cmd0, cmd1, len_hi, len_lo, checksum, ...data), 0x40]

The checksum is ((sum of all unescaped bytes except the checksum itself) XOR 0xFF) + 1.
Response packets from the bed use 0x80 instead of the leading 0x40 in the
checksum calculation. Payload bytes 0x40/0x3C/0x4F/0x41 are escaped as
0x3C followed by a mapped value (byte stuffing).

Protocol reverse-engineered by the Home Assistant community:
https://community.home-assistant.io/t/540790
"""

from __future__ import annotations

PACKET_CHAR = 0x40
ESCAPE_CHAR = 0x3C
_ESCAPE_MAP: dict[int, int] = {
    0x40: 0x01,
    0x3C: 0x02,
    0x4F: 0x03,
    0x41: 0x04,
}
_UNESCAPE_MAP: dict[int, int] = {v: k for k, v in _ESCAPE_MAP.items()}

# Command identifiers (cmd0, cmd1)
CMD_MOTOR_UP = (0x02, 0x70)
CMD_MOTOR_DOWN = (0x02, 0x71)
CMD_MOTOR_MEMPOS = (0x02, 0x72)  # recall memory preset, data=[slot]
CMD_MOTOR_STOP = (0x02, 0x73)
CMD_CONFIG_SAVE_MOTORPOS = (0x10, 0x70)  # save memory preset, data=[slot]
CMD_CONFIG_SET_DRIVEMODE = (0x10, 0x71)  # data=[0]=single, [1]=sync
CMD_CONFIG_GET_DRIVEMODE = (0x10, 0x72)
CMD_SYSTEM_PIN = (0x20, 0x43)  # data=[d1, d2, d3, d4]
CMD_SYSTEM_GET_CAPS = (0x20, 0x71)  # feature discovery
CMD_SYSTEM_SET_CAPS = (0x20, 0x72)  # write capability value (light etc.)

# Motor bit masks
MOTOR_HEAD = 0x02
MOTOR_FEET = 0x04
MOTOR_BOTH = MOTOR_HEAD | MOTOR_FEET

# Feature/capability IDs (3-byte big-endian in responses)
FEATURE_MOTORCOUNT = 0x000001
FEATURE_MEMCOUNT = 0x000002
FEATURE_PIN = 0x000003
FEATURE_SYNCHRO = 0x000101
FEATURE_LIGHT = 0x000102
FEATURE_LIGHT_RGBWI = 0x000104
FEATURE_END = 0xFFFFFF  # end-of-feature-list sentinel

DRIVEMODE_SINGLE = 0x00
DRIVEMODE_SYNC = 0x01


def calculate_checksum(packet: list[int]) -> int:
    """Return the Octo checksum over the given (unescaped) bytes."""
    total = sum(packet) & 0xFF
    return ((total ^ 0xFF) + 1) & 0xFF


def _escape(data: list[int]) -> list[int]:
    """Apply byte stuffing to payload bytes."""
    result: list[int] = []
    for byte in data:
        if byte in _ESCAPE_MAP:
            result.append(ESCAPE_CHAR)
            result.append(_ESCAPE_MAP[byte])
        else:
            result.append(byte)
    return result


def _unescape(data: list[int]) -> list[int]:
    """Remove byte stuffing from payload bytes."""
    result: list[int] = []
    i = 0
    while i < len(data):
        if data[i] == ESCAPE_CHAR and i + 1 < len(data):
            mapped = _UNESCAPE_MAP.get(data[i + 1])
            if mapped is not None:
                result.append(mapped)
                i += 2
                continue
        result.append(data[i])
        i += 1
    return result


def build_packet(command: tuple[int, int], data: list[int] | None = None) -> bytes:
    """Build a command packet with checksum and byte stuffing."""
    data = data or []
    data_len = len(data)
    unescaped = [
        PACKET_CHAR,
        command[0],
        command[1],
        (data_len >> 8) & 0xFF,
        data_len & 0xFF,
        0x00,  # checksum placeholder
        *data,
        PACKET_CHAR,
    ]
    unescaped[5] = calculate_checksum(unescaped[:5] + unescaped[6:])
    payload = _escape(unescaped[1:-1])
    return bytes([PACKET_CHAR, *payload, PACKET_CHAR])


def parse_packet(message: bytes) -> tuple[tuple[int, int], list[int]] | None:
    """Parse a response packet; return ((cmd0, cmd1), data) or None.

    A checksum mismatch is logged by the caller but does not reject the
    packet: some control boxes send packets with a zeroed checksum.
    """
    if len(message) < 7:
        return None
    if message[0] != PACKET_CHAR or message[-1] != PACKET_CHAR:
        return None
    payload = _unescape(list(message[1:-1]))
    if len(payload) < 5:
        return None
    command = (payload[0], payload[1])
    data_len = (payload[2] << 8) + payload[3]
    data = payload[5:]
    if len(data) != data_len:
        return None
    return (command, data)


def verify_response_checksum(message: bytes) -> bool:
    """Return True if the response checksum matches (0x80-based)."""
    if len(message) < 7:
        return False
    payload = _unescape(list(message[1:-1]))
    if len(payload) < 5:
        return False
    check_data = [0x80, payload[0], payload[1], payload[2], payload[3], *payload[5:]]
    return payload[4] == calculate_checksum(check_data)


def extract_feature(data: list[int]) -> tuple[int, list[int], int | None] | None:
    """Extract (feature_id, value_bytes, value_type) from a 0x21 0x71 response.

    Data layout: 3-byte feature ID, 1 flag byte, 1 skip-length byte,
    skipped bytes, 1 value-type byte, value bytes.
    """
    if len(data) < 6:
        return None
    feature_id = (data[0] << 16) + (data[1] << 8) + data[2]
    skip_length = data[4]
    value_type_index = 5 + skip_length
    value_type = data[value_type_index] if value_type_index < len(data) else None
    value_start = value_type_index + 1
    if value_start > len(data):
        return None
    return (feature_id, data[value_start:], value_type)


def encode_pin(pin: str) -> bytes:
    """Encode a 4-digit PIN into an authentication packet."""
    if len(pin) != 4 or not pin.isdigit():
        raise ValueError("PIN must be 4 digits")
    return build_packet(CMD_SYSTEM_PIN, [int(d) for d in pin])


def is_pin_packet(data: bytes) -> bool:
    """Return True if this outgoing packet contains the PIN (for log masking)."""
    return len(data) >= 3 and data[1] == CMD_SYSTEM_PIN[0] and data[2] == CMD_SYSTEM_PIN[1]
