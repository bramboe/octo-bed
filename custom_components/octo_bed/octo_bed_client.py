"""BLE client for Octo Bed communication."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine, Any

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
    NOTIFY_PIN_REQUIRED_ALT,
)

_LOGGER = logging.getLogger(__name__)


def encode_pin(pin: str) -> bytes:
    """Encode 4-digit PIN into command format.
    Format from working script: 402043000400 + XX XX XX XX + 40
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
        self._keepalive_task: asyncio.Task[None] | None = None

    def _start_keepalive(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keep_alive_loop())

    async def _stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _keep_alive_loop(self) -> None:
        """Background task: refresh PIN authentication periodically."""
        while True:
            await asyncio.sleep(30)
            client = self._client
            if (
                self._intentional_disconnect
                or not client
                or not client.is_connected
            ):
                return
            try:
                await client.write_gatt_char(
                    COMMAND_CHAR_UUID, encode_pin(self._pin), response=False
                )
                _LOGGER.debug("Keep-alive PIN pulse sent")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Keep-alive failed: %s", err)
                return

    async def connect(self) -> bool:
        """Connect to the bed and authenticate with PIN."""
        try:
            def _on_disconnect(client: BleakClient) -> None:
                self._client = None
                if not self._intentional_disconnect and self._disconnect_callback:
                    self._disconnect_callback()

            self._intentional_disconnect = False
            try:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._device,
                    "Octo Bed",
                    disconnected_callback=_on_disconnect,
                    timeout=15.0,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "establish_connection failed, trying direct BleakClient: %s", err
                )
                direct = BleakClient(self._device, disconnected_callback=_on_disconnect)
                await direct.connect(timeout=15.0)
                self._client = direct

            _LOGGER.debug("Connected to Octo bed at %s", self._device.address)

            # Ensure services are discovered on all backends
            try:
                await self._client.get_services()
            except Exception:  # noqa: BLE001
                pass

            # Send PIN immediately after connection (bed may require it)
            await self.send_pin()
            self._start_keepalive()

            return True
        except BleakError as err:
            _LOGGER.error("Failed to connect to Octo bed: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to connect to Octo bed: %s", err)
            return False

    async def disconnect(self) -> None:
        """Disconnect from the bed."""
        self._intentional_disconnect = True
        await self._stop_keepalive()
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
        if self._client and self._client.is_connected:
            asyncio.create_task(self._send_command(encode_pin(self._pin)))

    async def _send_command(self, data: bytes) -> bool:
        """Send raw command to the bed."""
        if not await self.ensure_connected():
            _LOGGER.warning("Not connected to Octo bed")
            return False

        try:
            # Prefer UUID writes (most reliable across backends)
            await self._client.write_gatt_char(COMMAND_CHAR_UUID, data, response=False)
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
        ok1 = await self.head_down()
        ok2 = await self.feet_down()
        return ok1 and ok2

    async def both_up(self) -> bool:
        """Send both sides up command."""
        ok1 = await self.head_up()
        ok2 = await self.feet_up()
        return ok1 and ok2

    async def both_up_continuous(self) -> bool:
        """Send both sides up continuously."""
        ok1 = await self.head_up_continuous()
        ok2 = await self.feet_up()
        return ok1 and ok2

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
