"""BLE client for Octo Bed communication."""

from __future__ import annotations

import logging
from typing import Callable

from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import (
    CMD_BOTH_DOWN,
    CMD_BOTH_UP,
    CMD_BOTH_UP_CONTINUOUS,
    CMD_FEET_DOWN,
    CMD_FEET_UP,
    CMD_HEAD_DOWN,
    CMD_HEAD_UP,
    CMD_HEAD_UP_CONTINUOUS,
    CMD_LIGHT_OFF,
    CMD_LIGHT_ON,
    CMD_PIN_PREFIX,
    CMD_PIN_SUFFIX,
    CMD_STOP,
    COMMAND_CHAR_UUID,
    COMMAND_HANDLE,
    NOTIFY_HANDLE,
    NOTIFY_PIN_REQUIRED,
)

_LOGGER = logging.getLogger(__name__)


def encode_pin(pin: str) -> bytes:
    """Encode 4-digit PIN into command format.
    Format from capture: 40204300040001 + XX XX XX XX + 40
    PIN digits as bytes: 0->0x00, 1->0x01, ..., 9->0x09
    """
    if len(pin) != 4 or not pin.isdigit():
        raise ValueError("PIN must be 4 digits")
    pin_bytes = bytes(int(d) for d in pin)
    return CMD_PIN_PREFIX + pin_bytes + CMD_PIN_SUFFIX


class OctoBedClient:
    """Client for communicating with Octo Bed via BLE."""

    def __init__(
        self,
        device: BLEDevice,
        pin: str,
        disconnect_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the Octo Bed client."""
        self._device = device
        self._pin = pin
        self._client: BleakClient | None = None
        self._disconnect_callback = disconnect_callback
        self._pin_sent = False

    async def connect(self) -> bool:
        """Connect to the bed and authenticate with PIN."""
        try:
            def _on_disconnect(client: BleakClient) -> None:
                if self._disconnect_callback:
                    self._disconnect_callback()

            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                "Octo Bed",
                disconnected_callback=_on_disconnect,
                timeout=15.0,
            )
            _LOGGER.debug("Connected to Octo bed at %s", self._device.address)

            # Enable notifications on handle 0x0012 (for PIN keep-alive)
            for service in self._client.services:
                for char in service.characteristics:
                    if char.handle == NOTIFY_HANDLE or "notify" in char.properties:
                        await self._client.start_notify(
                            char.uuid, self._notification_handler
                        )
                        _LOGGER.debug("Enabled notifications on handle %s", char.handle)
                        break

            # Send PIN immediately after connection (bed may require it)
            await self.send_pin()

            return True
        except BleakError as err:
            _LOGGER.error("Failed to connect to Octo bed: %s", err)
            return False

    async def disconnect(self) -> None:
        """Disconnect from the bed."""
        if self._client and self._client.is_connected:
            await self._client.disconnect()
            self._client = None

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notifications from the bed."""
        _LOGGER.debug("Notification: %s", data.hex())
        # Check if PIN is required (keep-alive / re-auth)
        if bytes(data[:7]) == NOTIFY_PIN_REQUIRED[:7]:
            _LOGGER.debug("PIN required, sending authentication")
            self._send_pin_async()

    def _send_pin_async(self) -> None:
        """Send PIN asynchronously - called from notification handler."""
        # Schedule on the event loop
        import asyncio

        if self._client and self._client.is_connected:
            asyncio.create_task(self._send_command(encode_pin(self._pin)))

    async def _send_command(self, data: bytes) -> bool:
        """Send raw command to the bed."""
        if not self._client or not self._client.is_connected:
            _LOGGER.warning("Not connected to Octo bed")
            return False

        try:
            # Find command characteristic - Handle 0x0011 from packet captures
            command_char = None
            for service in self._client.services:
                for char in service.characteristics:
                    if char.handle == COMMAND_HANDLE:
                        command_char = char
                        break
                if command_char:
                    break

            if command_char:
                await self._client.write_gatt_char(
                    command_char, data, response=False
                )
            else:
                # Fallback: use UUID (handle may not work on all Bleak backends)
                await self._client.write_gatt_char(
                    COMMAND_CHAR_UUID, data, response=False
                )
            _LOGGER.debug("Sent command: %s", data.hex())
            return True
        except BleakError as err:
            _LOGGER.error("Failed to send command: %s", err)
            return False

    async def send_pin(self) -> bool:
        """Send PIN authentication."""
        return await self._send_command(encode_pin(self._pin))

    async def both_down(self) -> bool:
        """Send both sides down command."""
        return await self._send_command(CMD_BOTH_DOWN)

    async def both_up(self) -> bool:
        """Send both sides up command."""
        return await self._send_command(CMD_BOTH_UP)

    async def both_up_continuous(self) -> bool:
        """Send both sides up continuously."""
        return await self._send_command(CMD_BOTH_UP_CONTINUOUS)

    async def feet_down(self) -> bool:
        """Send feet down command."""
        return await self._send_command(CMD_FEET_DOWN)

    async def feet_up(self) -> bool:
        """Send feet up command."""
        return await self._send_command(CMD_FEET_UP)

    async def head_down(self) -> bool:
        """Send head down command."""
        return await self._send_command(CMD_HEAD_DOWN)

    async def head_up(self) -> bool:
        """Send head up command."""
        return await self._send_command(CMD_HEAD_UP)

    async def head_up_continuous(self) -> bool:
        """Send head up continuously."""
        return await self._send_command(CMD_HEAD_UP_CONTINUOUS)

    async def stop(self) -> bool:
        """Send stop command."""
        return await self._send_command(CMD_STOP)

    async def light_on(self) -> bool:
        """Turn bed light on."""
        return await self._send_command(CMD_LIGHT_ON)

    async def light_off(self) -> bool:
        """Turn bed light off."""
        return await self._send_command(CMD_LIGHT_OFF)
