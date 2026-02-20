"""BLE client for Octo Bed communication."""

from __future__ import annotations

import logging
from typing import Callable, Coroutine, Any

from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakConnectionError,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

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
    NOTIFY_PIN_REQUIRED_ALT,
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
        device_resolver: Callable[[], Coroutine[Any, Any, BLEDevice | None]] | None = None,
    ) -> None:
        """Initialize the Octo Bed client."""
        self._device = device
        self._pin = pin
        self._client: BleakClient | None = None
        self._disconnect_callback = disconnect_callback
        self._device_resolver = device_resolver
        self._pin_sent = False
        self._intentional_disconnect = False

    async def connect(self) -> bool:
        """Connect to the bed and authenticate with PIN."""
        try:
            def _on_disconnect(client: BleakClient) -> None:
                self._client = None
                if not self._intentional_disconnect and self._disconnect_callback:
                    self._disconnect_callback()

            self._intentional_disconnect = False
            # Use longer timeout for proxy/ESP32; more attempts for flaky discovery
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                "Octo Bed",
                disconnected_callback=_on_disconnect,
                timeout=25.0,
                max_attempts=6,
            )
            _LOGGER.debug("Connected to Octo bed at %s", self._device.address)

            # Enable notifications - commands and PIN keep-alive use handle 0x0011
            # Find characteristic by UUID (most reliable) or handle
            notified = False
            for service in self._client.services:
                for char in service.characteristics:
                    is_cmd_char = (
                        (char.uuid and str(char.uuid).lower() == COMMAND_CHAR_UUID.lower())
                        or char.handle == COMMAND_HANDLE
                        or getattr(char, "value_handle", None) == COMMAND_HANDLE
                    )
                    if is_cmd_char and "notify" in char.properties:
                        await self._client.start_notify(
                            char.uuid, self._notification_handler
                        )
                        _LOGGER.debug(
                            "Enabled notifications on char handle %s", char.handle
                        )
                        notified = True
                        break
                if notified:
                    break
            if not notified:
                # Fallback: enable on first characteristic with notify
                for service in self._client.services:
                    for char in service.characteristics:
                        if "notify" in char.properties:
                            await self._client.start_notify(
                                char.uuid, self._notification_handler
                            )
                            _LOGGER.debug(
                                "Enabled notifications (fallback) on handle %s",
                                char.handle,
                            )
                            notified = True
                            break
                    if notified:
                        break
            if not notified:
                # Discovery may be incomplete (e.g. proxy/bed returns ATT errors);
                # try enabling notify by known command UUID anyway
                try:
                    await self._client.start_notify(
                        COMMAND_CHAR_UUID, self._notification_handler
                    )
                    _LOGGER.debug(
                        "Enabled notifications by UUID %s (discovery fallback)",
                        COMMAND_CHAR_UUID,
                    )
                    notified = True
                except BleakError as e:
                    _LOGGER.warning(
                        "Could not enable notifications (UUID fallback failed): %s", e
                    )
            if not notified:
                _LOGGER.warning(
                    "Could not enable notifications; PIN keep-alive may not work"
                )

            # Send PIN immediately after connection (bed may require it)
            await self.send_pin()

            return True
        except BleakNotFoundError as err:
            _LOGGER.error(
                "Octo bed not found at %s (out of range or not advertising). "
                "Ensure ESPHome Bluetooth proxy is in range and press a button on the remote to wake the bed: %s",
                self._device.address,
                err,
            )
            return False
        except BleakOutOfConnectionSlotsError as err:
            _LOGGER.error(
                "No free BLE connection slots (proxy/adapter busy). "
                "Disconnect other BLE devices or add another proxy: %s",
                err,
            )
            return False
        except BleakConnectionError as err:
            _LOGGER.error("Failed to connect to Octo bed: %s", err)
            return False
        except BleakError as err:
            _LOGGER.error("Failed to connect to Octo bed: %s", err)
            return False

    async def disconnect(self) -> None:
        """Disconnect from the bed."""
        self._intentional_disconnect = True
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def ensure_connected(self) -> bool:
        """Ensure we are connected; reconnect if needed."""
        if self._client and self._client.is_connected:
            return True
        if self._intentional_disconnect:
            return False
        # Refresh device if resolver available (e.g. after disconnect)
        if self._device_resolver:
            fresh = await self._device_resolver()
            if fresh:
                self._device = fresh
        _LOGGER.info("Reconnecting to Octo bed at %s", self._device.address)
        return await self.connect()

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notifications from the bed."""
        _LOGGER.debug("Notification: %s", data.hex())
        raw = bytes(data)
        # Check if PIN is required (keep-alive / re-auth)
        # 40214400001b40 = periodic keep-alive, 40217f0000e040 = initial auth
        pin_required = (
            (len(raw) >= 7 and raw[:7] == NOTIFY_PIN_REQUIRED[:7])
            or raw == NOTIFY_PIN_REQUIRED_ALT
        )
        if pin_required:
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
        if not await self.ensure_connected():
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
