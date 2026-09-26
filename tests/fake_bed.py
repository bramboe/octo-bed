"""A simulated Octo bed behind a fake bleak client.

The notifications are the ones real beds send (taken from Home Assistant logs
of two RC2 control boxes): every PIN write is acknowledged, and a capability
query is answered with a list that ends in the 0xFFFFFF sentinel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from bleak import BleakError

from custom_components.octo_bed import protocol
from custom_components.octo_bed.const import NOTIFY_PIN_ACCEPTED

CAPS_QUERY = protocol.build_packet(protocol.CMD_SYSTEM_GET_CAPS)

# Capability list of the RC2 boxes in the logs (no memory slots, plain light).
RC2_CAPABILITIES = [
    "4021710007e20000010101020040",
    "4021710008df000102010101010040",
    "4021710008de000103010101010040",
    "4021710008d2000010010101010040",
    "4021710009dc00000301010101010140",
    "4021710006eaffffff01000040",
]


def memory_slots_feature(slots: int) -> str:
    """A capability entry announcing hardware memory slots."""
    return protocol.build_packet(
        (0x21, 0x71), [0x00, 0x00, 0x02, 0x01, 0x01, 0x01, 0x01, slots]
    ).hex()


class FakeBleDevice:
    """Minimal stand-in for bleak's BLEDevice."""

    def __init__(self, address: str, source: str = "24:6F:28:5F:03:7E") -> None:
        self.address = address
        self.name = "RC2"
        self.details = {"source": source}


class FakeBed:
    """Behaviour of one bed as seen through establish_connection."""

    def __init__(self, address: str = "F6:21:DD:DD:6F:19") -> None:
        self.address = address
        self.device = FakeBleDevice(address)
        self.capabilities: list[str] = list(RC2_CAPABILITIES)
        self.ack_pin = True
        self.connect_failures = 0  # next N establish_connection calls fail
        self.notify_failures = 0  # next N start_notify calls fail (GATT 133)
        self.connect_hang: asyncio.Event | None = None  # block connects until set
        self.connect_delay = 0.0
        self.clients: list[FakeBleakClient] = []
        self.writes: list[bytes] = []
        self.connect_calls = 0

    @property
    def current(self) -> FakeBleakClient | None:
        for client in reversed(self.clients):
            if client.is_connected:
                return client
        return None

    def drop(self) -> None:
        """The bed drops the link (out of range, other central, ...)."""
        client = self.current
        if client is not None:
            client.remote_disconnect()

    def motion_writes(self) -> list[bytes]:
        """Writes except PIN/keep-alive, capability queries and stops."""
        from custom_components.octo_bed.const import CMD_STOP

        return [
            w
            for w in self.writes
            if not protocol.is_pin_packet(w) and w not in (CAPS_QUERY, CMD_STOP)
        ]


class FakeBleakClient:
    """Implements the part of BleakClient the integration uses."""

    def __init__(
        self, bed: FakeBed, disconnected_callback: Callable[[Any], None] | None
    ) -> None:
        self.bed = bed
        self._disconnected_callback = disconnected_callback
        self.is_connected = True
        self.writes: list[bytes] = []
        self._notify: Callable[[Any, bytearray], None] | None = None

    async def start_notify(self, _uuid: str, callback: Callable[..., None]) -> None:
        await asyncio.sleep(0)
        if self.bed.notify_failures > 0:
            self.bed.notify_failures -= 1
            raise BleakError(
                f"Bluetooth GATT Error address={self.bed.address} handle=18 error=133"
            )
        self._notify = callback

    async def write_gatt_char(self, _uuid: str, data: bytes, response: bool = False) -> None:
        await asyncio.sleep(0)
        if not self.is_connected:
            raise BleakError("Not connected")
        data = bytes(data)
        self.writes.append(data)
        self.bed.writes.append(data)
        loop = asyncio.get_running_loop()
        if protocol.is_pin_packet(data) and self.bed.ack_pin and self._notify:
            loop.call_soon(self._send, NOTIFY_PIN_ACCEPTED)
        elif data == CAPS_QUERY and self._notify:
            for packet in self.bed.capabilities:
                loop.call_soon(self._send, bytes.fromhex(packet))

    def _send(self, data: bytes) -> None:
        if self.is_connected and self._notify is not None:
            self._notify(None, bytearray(data))

    async def disconnect(self) -> bool:
        await asyncio.sleep(0)
        self.remote_disconnect()
        return True

    def remote_disconnect(self) -> None:
        if not self.is_connected:
            return
        self.is_connected = False
        if self._disconnected_callback is not None:
            asyncio.get_running_loop().call_soon(self._disconnected_callback, self)


class FakeBleakBackend:
    """Replacement for bleak_retry_connector.establish_connection."""

    def __init__(self) -> None:
        self.beds: dict[str, FakeBed] = {}
        self.active_connects = 0
        self.max_concurrent_connects = 0

    def add(self, bed: FakeBed) -> FakeBed:
        self.beds[bed.address.upper()] = bed
        return bed

    async def establish_connection(
        self,
        _client_class: Any,
        device: Any,
        _name: str,
        disconnected_callback: Callable[[Any], None] | None = None,
        **_kwargs: Any,
    ) -> FakeBleakClient:
        bed = self.beds[device.address.upper()]
        bed.connect_calls += 1
        self.active_connects += 1
        self.max_concurrent_connects = max(self.max_concurrent_connects, self.active_connects)
        try:
            if bed.connect_hang is not None:
                await bed.connect_hang.wait()
            await asyncio.sleep(bed.connect_delay)
            if bed.connect_failures > 0:
                bed.connect_failures -= 1
                raise BleakError(f"Timeout waiting for connect response to {bed.address}")
            client = FakeBleakClient(bed, disconnected_callback)
            bed.clients.append(client)
            return client
        finally:
            self.active_connects -= 1
