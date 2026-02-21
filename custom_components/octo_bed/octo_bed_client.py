"""BLE client for Octo Bed communication."""

from __future__ import annotations

import asyncio
import logging
import time
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
    NOTIFY_PIN_ACCEPTED,
    NOTIFY_PIN_REJECTED,
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
        self._active_movement_tasks: set[asyncio.Task[None]] = set()
        # Shared position state (0-100, where 0 = down, 100 = up)
        self._head_position: int = 0
        self._feet_position: int = 0
        self._position_callbacks: list[Callable[[str, int], None]] = []
        # Track active movements by part to prevent conflicts
        self._active_movements: dict[str, asyncio.Task[None]] = {}  # "head", "feet", "both"
        # Calibration: part being calibrated and when it started
        self._calibration_part: str | None = None
        self._calibration_start_time: float | None = None
        self._calibration_task: asyncio.Task[None] | None = None
        # True while move_part_down_for_seconds is running (after complete_calibration)
        self._calibration_completing: bool = False
        self._calibration_returning_part: str | None = None  # "head" or "feet" while returning
        self._calibration_state_callbacks: list[Callable[[], None]] = []

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

    async def connect_and_verify_pin(self) -> bool:
        """Connect to the bed, send PIN, and verify acceptance via notification.
        Returns True only if the bed sends PIN accepted; False on reject or timeout.
        """
        try:
            def _on_disconnect(client: BleakClient) -> None:
                self._client = None

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
                direct = BleakClient(
                    self._device, disconnected_callback=_on_disconnect
                )
                await direct.connect(timeout=15.0)
                self._client = direct

            _LOGGER.debug("Connected to Octo bed at %s", self._device.address)

            try:
                await self._client.get_services()
            except Exception:  # noqa: BLE001
                pass

            pin_result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

            def _verify_handler(_char: BleakGATTCharacteristic, data: bytearray) -> None:
                raw = bytes(data)
                if pin_result.done():
                    return
                if raw == NOTIFY_PIN_ACCEPTED:
                    pin_result.set_result(True)
                elif raw == NOTIFY_PIN_REJECTED:
                    pin_result.set_result(False)

            await self._client.start_notify(COMMAND_CHAR_UUID, _verify_handler)
            await self.send_pin()
            try:
                result = await asyncio.wait_for(pin_result, 8.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("PIN verification timed out waiting for bed response")
                result = False
            finally:
                try:
                    await self._client.stop_notify(COMMAND_CHAR_UUID)
                except Exception:  # noqa: BLE001
                    pass

            if not result:
                self._intentional_disconnect = True
                if self._client and self._client.is_connected:
                    await self._client.disconnect()
                self._client = None
                return False

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
        return await self._send_command(CMD_BOTH_DOWN)

    async def both_up(self) -> bool:
        """Send both sides up command."""
        return await self._send_command(CMD_BOTH_UP)

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

    def register_movement_task(self, task: asyncio.Task[None]) -> None:
        """Register a movement task so it can be cancelled when stop is called."""
        self._active_movement_tasks.add(task)
        # Remove task when it completes
        task.add_done_callback(self._active_movement_tasks.discard)

    def register_active_movement(self, part: str, task: asyncio.Task[None]) -> None:
        """Register an active movement for a specific part (head, feet, or both).
        This will cancel any conflicting movements.
        """
        # Cancel conflicting movements
        if part == "head":
            # Cancel feet and both if they're active
            self._cancel_movement("feet")
            self._cancel_movement("both")
        elif part == "feet":
            # Cancel head and both if they're active
            self._cancel_movement("head")
            self._cancel_movement("both")
        elif part == "both":
            # Cancel head and feet if they're active
            self._cancel_movement("head")
            self._cancel_movement("feet")
        
        # Register this movement
        if part in self._active_movements:
            old_task = self._active_movements[part]
            if not old_task.done():
                old_task.cancel()
        
        self._active_movements[part] = task
        
        # Remove from active movements when task completes
        def cleanup(task: asyncio.Task[None]) -> None:
            if self._active_movements.get(part) == task:
                self._active_movements.pop(part, None)
        task.add_done_callback(cleanup)

    def _cancel_movement(self, part: str) -> None:
        """Cancel an active movement for a specific part."""
        if part in self._active_movements:
            task = self._active_movements[part]
            if not task.done():
                task.cancel()
                try:
                    # Wait briefly for cancellation to complete
                    asyncio.create_task(self._wait_for_cancellation(task))
                except Exception:  # noqa: BLE001
                    pass

    async def _wait_for_cancellation(self, task: asyncio.Task[None]) -> None:
        """Wait for a task to be cancelled and send stop command."""
        try:
            await task
        except asyncio.CancelledError:
            # Send stop command to bed when movement is cancelled
            try:
                await self._send_command(CMD_STOP)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to send stop after cancelling movement", exc_info=True)

    def _notify_calibration_state(self) -> None:
        """Notify listeners that calibration active state may have changed."""
        for callback in self._calibration_state_callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Calibration state callback error", exc_info=True)

    def register_calibration_state_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for calibration active state changes."""
        self._calibration_state_callbacks.append(callback)

    def is_calibration_active(self) -> bool:
        """Return True if calibration is in progress (tracking time or moving part down)."""
        return self._calibration_part is not None or self._calibration_completing

    async def start_calibration(self, part: str) -> None:
        """Start calibration for head or feet: move that part up and start counting time."""
        if part not in ("head", "feet"):
            return
        # Cancel any existing calibration
        if self._calibration_task and not self._calibration_task.done():
            self._calibration_task.cancel()
            try:
                await self._calibration_task
            except asyncio.CancelledError:
                pass
            await self._send_command(CMD_STOP)
        self._calibration_part = part
        self._calibration_start_time = time.monotonic()
        method = self.head_up if part == "head" else self.feet_up
        self._calibration_task = asyncio.create_task(self._calibration_move_loop(method))
        self.register_movement_task(self._calibration_task)
        self.register_active_movement(part, self._calibration_task)
        self._notify_calibration_state()

    async def _calibration_move_loop(self, method: Callable[[], Coroutine[Any, Any, bool]]) -> None:
        """Send movement command repeatedly until calibration is completed or cancelled."""
        try:
            while True:
                await method()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    async def complete_calibration(self) -> tuple[str | None, float]:
        """Stop calibration movement and return (part, duration_seconds). Returns (None, 0) if no calibration active."""
        if self._calibration_part is None or self._calibration_start_time is None:
            return (None, 0.0)
        part = self._calibration_part
        duration = max(0.0, time.monotonic() - self._calibration_start_time)
        if self._calibration_task and not self._calibration_task.done():
            self._calibration_task.cancel()
            try:
                await self._calibration_task
            except asyncio.CancelledError:
                pass
        await self._send_command(CMD_STOP)
        self._calibration_part = None
        self._calibration_start_time = None
        self._calibration_task = None
        if part in self._active_movements:
            self._active_movements.pop(part, None)
        self._notify_calibration_state()
        _LOGGER.info("Calibration complete for %s: %.1f seconds (100% travel)", part, duration)
        return (part, duration)

    def is_calibrating(self) -> bool:
        """Return True if a calibration session is active."""
        return self._calibration_part is not None

    def get_calibration_elapsed_seconds(self) -> float:
        """Return seconds elapsed since calibration started, or 0 if not calibrating."""
        if self._calibration_start_time is None:
            return 0.0
        return time.monotonic() - self._calibration_start_time

    async def move_part_down_for_seconds(self, part: str, seconds: float) -> None:
        """Move the given part (head or feet) down for the given duration, then set position to 0%."""
        if part not in ("head", "feet") or seconds <= 0:
            return
        self._calibration_completing = True
        self._calibration_returning_part = part
        self._notify_calibration_state()
        method = self.head_down if part == "head" else self.feet_down
        setter = self.set_head_position if part == "head" else self.set_feet_position
        setter(100)  # We're at 100% after calibration
        end_time = time.monotonic() + seconds
        start_time = time.monotonic()
        current = asyncio.current_task()
        if current is not None:
            self.register_movement_task(current)
            self.register_active_movement(part, current)
        try:
            while time.monotonic() < end_time:
                await method()
                elapsed = time.monotonic() - start_time
                progress = min(1.0, elapsed / seconds)
                setter(int(round(100 * (1.0 - progress))))
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            elapsed = time.monotonic() - start_time
            progress = min(1.0, elapsed / seconds)
            setter(int(round(100 * (1.0 - progress))))
            raise
        finally:
            await self._send_command(CMD_STOP)
            setter(0)
            self._calibration_completing = False
            self._calibration_returning_part = None
            self._notify_calibration_state()

    def get_calibration_status(self) -> tuple[str, str | None]:
        """Return (state, part) for calibration. state: 'idle' | 'tracking' | 'returning'; part: 'head' | 'feet' | None."""
        if self._calibration_completing and self._calibration_returning_part:
            return ("returning", self._calibration_returning_part)
        if self._calibration_part is not None:
            return ("tracking", self._calibration_part)
        return ("idle", None)

    def is_connected(self) -> bool:
        """Return True if connected to the bed (Bluetooth proxy)."""
        return self._client is not None and self._client.is_connected

    def get_device_address(self) -> str:
        """Return the Bluetooth device MAC address."""
        return self._device.address

    def get_head_position(self) -> int:
        """Get current head position (0-100)."""
        return self._head_position

    def get_feet_position(self) -> int:
        """Get current feet position (0-100)."""
        return self._feet_position

    def get_both_position(self) -> int:
        """Get current 'both' position (average of head and feet)."""
        # Use average to represent the overall position when head and feet differ
        return int(round((self._head_position + self._feet_position) / 2.0))

    def set_head_position(self, position: int) -> None:
        """Set head position (0-100) and notify listeners."""
        position = max(0, min(100, position))
        if self._head_position != position:
            self._head_position = position
            self._notify_position_change("head", position)

    def set_feet_position(self, position: int) -> None:
        """Set feet position (0-100) and notify listeners."""
        position = max(0, min(100, position))
        if self._feet_position != position:
            self._feet_position = position
            self._notify_position_change("feet", position)

    def set_both_position(self, position: int) -> None:
        """Set both head and feet positions to the same value."""
        position = max(0, min(100, position))
        self.set_head_position(position)
        self.set_feet_position(position)

    def register_position_callback(self, callback: Callable[[str, int], None]) -> None:
        """Register a callback to be notified when position changes.
        Callback receives (part: str, position: int) where part is 'head', 'feet', or 'both'.
        """
        self._position_callbacks.append(callback)

    def _notify_position_change(self, part: str, position: int) -> None:
        """Notify all registered callbacks of a position change."""
        for callback in self._position_callbacks:
            try:
                callback(part, position)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Position callback failed", exc_info=True)

    async def stop(self) -> bool:
        """Send stop command and cancel all active movement tasks."""
        # Cancel all active movement tasks
        for task in list(self._active_movement_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Send stop command to the bed
        return await self._send_command(CMD_STOP)

    async def light_on(self) -> bool:
        """Turn bed light on."""
        # Light commands appear to require a fresh auth on some beds.
        await self.send_pin()
        await asyncio.sleep(0.2)
        ok = await self._send_command(CMD_LIGHT_ON)
        if ok:
            # Retry once for reliability (matches "write command" behaviour in captures).
            await asyncio.sleep(0.1)
            await self._send_command(CMD_LIGHT_ON)
        return ok

    async def light_off(self) -> bool:
        """Turn bed light off."""
        await self.send_pin()
        await asyncio.sleep(0.2)
        ok = await self._send_command(CMD_LIGHT_OFF)
        if ok:
            await asyncio.sleep(0.1)
            await self._send_command(CMD_LIGHT_OFF)
        return ok


class CombinedOctoBedClient:
    """Virtual client that forwards commands to two OctoBedClient instances (paired beds)."""

    def __init__(self, client1: OctoBedClient, client2: OctoBedClient) -> None:
        """Initialize with two bed clients."""
        self._clients = (client1, client2)
        self._position_callbacks: list[Callable[[str, int], None]] = []
        self._calibration_state_callbacks: list[Callable[[], None]] = []
        for c in self._clients:
            c.register_position_callback(self._on_position_changed)

    def _on_position_changed(self, part: str, _position: int) -> None:
        """Notify our listeners with aggregated position when either bed changes."""
        pos = (
            self.get_head_position()
            if part == "head"
            else self.get_feet_position()
            if part == "feet"
            else self.get_both_position()
        )
        for cb in self._position_callbacks:
            try:
                cb(part, pos)
            except Exception:  # noqa: BLE001
                pass

    async def _both(self, coro_getter: Callable[[OctoBedClient], Coroutine[Any, Any, Any]]) -> None:
        """Run a coroutine on both clients concurrently."""
        await asyncio.gather(coro_getter(self._clients[0]), coro_getter(self._clients[1]))

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return self._clients[0].is_connected() and self._clients[1].is_connected()

    def get_device_address(self) -> str:
        return "combined"

    def get_head_position(self) -> int:
        return int(round((self._clients[0].get_head_position() + self._clients[1].get_head_position()) / 2.0))

    def get_feet_position(self) -> int:
        return int(round((self._clients[0].get_feet_position() + self._clients[1].get_feet_position()) / 2.0))

    def get_both_position(self) -> int:
        return int(round((self._clients[0].get_both_position() + self._clients[1].get_both_position()) / 2.0))

    def set_head_position(self, position: int) -> None:
        for c in self._clients:
            c.set_head_position(position)
        for cb in self._position_callbacks:
            try:
                cb("head", position)
            except Exception:  # noqa: BLE001
                pass

    def set_feet_position(self, position: int) -> None:
        for c in self._clients:
            c.set_feet_position(position)
        for cb in self._position_callbacks:
            try:
                cb("feet", position)
            except Exception:  # noqa: BLE001
                pass

    def set_both_position(self, position: int) -> None:
        for c in self._clients:
            c.set_both_position(position)
        for cb in self._position_callbacks:
            try:
                cb("both", position)
            except Exception:  # noqa: BLE001
                pass

    def register_position_callback(self, callback: Callable[[str, int], None]) -> None:
        self._position_callbacks.append(callback)

    def register_calibration_state_callback(self, callback: Callable[[], None]) -> None:
        self._calibration_state_callbacks.append(callback)

    def is_calibration_active(self) -> bool:
        return False

    def is_calibrating(self) -> bool:
        return False

    def get_calibration_status(self) -> tuple[str, str | None]:
        return ("idle", None)

    def get_calibration_elapsed_seconds(self) -> float:
        return 0.0

    async def start_calibration(self, part: str) -> None:
        pass

    async def complete_calibration(self) -> tuple[str | None, float]:
        return (None, 0.0)

    async def move_part_down_for_seconds(self, part: str, seconds: float) -> None:
        pass

    def register_movement_task(self, task: asyncio.Task[None]) -> None:
        for c in self._clients:
            c.register_movement_task(task)

    def register_active_movement(self, part: str, task: asyncio.Task[None]) -> None:
        for c in self._clients:
            c.register_active_movement(part, task)

    async def _send_command(self, data: bytes) -> bool:
        results = await asyncio.gather(
            self._clients[0]._send_command(data),
            self._clients[1]._send_command(data),
        )
        return bool(results[0] and results[1])

    async def stop(self) -> bool:
        results = await asyncio.gather(self._clients[0].stop(), self._clients[1].stop())
        return bool(results[0] and results[1])

    async def head_up(self) -> bool:
        results = await asyncio.gather(self._clients[0].head_up(), self._clients[1].head_up())
        return bool(results[0] and results[1])

    async def head_down(self) -> bool:
        results = await asyncio.gather(self._clients[0].head_down(), self._clients[1].head_down())
        return bool(results[0] and results[1])

    async def feet_up(self) -> bool:
        results = await asyncio.gather(self._clients[0].feet_up(), self._clients[1].feet_up())
        return bool(results[0] and results[1])

    async def feet_down(self) -> bool:
        results = await asyncio.gather(self._clients[0].feet_down(), self._clients[1].feet_down())
        return bool(results[0] and results[1])

    async def both_up(self) -> bool:
        results = await asyncio.gather(self._clients[0].both_up(), self._clients[1].both_up())
        return bool(results[0] and results[1])

    async def both_down(self) -> bool:
        results = await asyncio.gather(self._clients[0].both_down(), self._clients[1].both_down())
        return bool(results[0] and results[1])

    async def head_up_continuous(self) -> bool:
        results = await asyncio.gather(
            self._clients[0].head_up_continuous(),
            self._clients[1].head_up_continuous(),
        )
        return bool(results[0] and results[1])

    async def light_on(self) -> bool:
        results = await asyncio.gather(self._clients[0].light_on(), self._clients[1].light_on())
        return bool(results[0] and results[1])

    async def light_off(self) -> bool:
        results = await asyncio.gather(self._clients[0].light_off(), self._clients[1].light_off())
        return bool(results[0] and results[1])
