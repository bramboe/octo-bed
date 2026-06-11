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

from . import protocol
from .const import (
    CMD_BOTH_DOWN,
    CMD_BOTH_UP,
    CMD_FEET_DOWN,
    CMD_FEET_UP,
    CMD_HEAD_DOWN,
    CMD_HEAD_UP,
    CMD_LIGHT_OFF,
    CMD_LIGHT_ON,
    CMD_STOP,
    COMMAND_CHAR_UUID,
    NOTIFY_PIN_ACCEPTED,
    NOTIFY_PIN_REJECTED,
    NOTIFY_PIN_REQUIRED,
    NOTIFY_PIN_REQUIRED_ALT,
    PIN_KEEPALIVE_SECONDS,
)
from .protocol import encode_pin

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAYS = (5, 10, 20, 30, 60)
FEATURE_DISCOVERY_TIMEOUT = 5.0
# Calibration limits: travel times are clamped to this range when saved, and
# a measuring session that is never completed aborts itself.
MIN_TRAVEL_SECONDS = 5.0
MAX_TRAVEL_SECONDS = 120.0
MAX_CALIBRATION_TRACKING_SECONDS = 180.0


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
        self._intentional_disconnect = False
        self._keepalive_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._pin_task: asyncio.Task[bool] | None = None
        self._connect_lock = asyncio.Lock()
        self._active_movement_tasks: set[asyncio.Task[None]] = set()
        # Shared position state (0-100, where 0 = down, 100 = up)
        self._head_position: int = 0
        self._feet_position: int = 0
        self._position_callbacks: list[Callable[[str, int], None]] = []
        self._connection_callbacks: list[Callable[[bool], None]] = []
        # Track active movements by part to prevent conflicts
        self._active_movements: dict[str, asyncio.Task[None]] = {}  # "head", "feet", "both"
        # Calibration: part being calibrated, phase and when measuring started
        self._calibration_part: str | None = None
        self._calibration_phase: str | None = None  # "preparing" | "tracking"
        self._calibration_start_time: float | None = None
        self._calibration_task: asyncio.Task[None] | None = None
        # True while move_part_down_for_seconds is running (after complete_calibration)
        self._calibration_completing: bool = False
        self._calibration_returning_part: str | None = None  # "head" or "feet" while returning
        self._calibration_state_callbacks: list[Callable[[], None]] = []
        # Bed capabilities (filled by discover_features; None = unknown)
        self._features_complete = asyncio.Event()
        self._motor_count: int | None = None
        self._memory_count: int | None = None
        self._has_light: bool | None = None
        self._has_rgbwi: bool = False
        self._rgbwi_value_type: int | None = None
        self._has_synchro: bool | None = None
        self._synchro_active: bool | None = None

    # ---------------------------------------------------------------- features

    @property
    def memory_slot_count(self) -> int:
        """Number of hardware memory preset slots (0 if unsupported/unknown)."""
        return self._memory_count or 0

    @property
    def has_synchro(self) -> bool:
        """True if the bed supports linked/synchro drive mode."""
        return bool(self._has_synchro)

    @property
    def synchro_active(self) -> bool | None:
        """Current drive mode (True=sync, False=single, None=unknown)."""
        return self._synchro_active

    @property
    def has_rgbwi_light(self) -> bool:
        """True if the bed reported RGBW+intensity light control."""
        return self._has_rgbwi

    def get_feature_summary(self) -> dict[str, Any]:
        """Return discovered capabilities (for diagnostics)."""
        return {
            "motor_count": self._motor_count,
            "memory_slots": self._memory_count,
            "has_light": self._has_light,
            "has_rgbwi_light": self._has_rgbwi,
            "has_synchro": self._has_synchro,
            "synchro_active": self._synchro_active,
        }

    async def discover_features(self) -> bool:
        """Query bed capabilities; returns True when the full list was received.

        Best effort: beds that do not answer keep their defaults and the
        integration behaves as before discovery existed.
        """
        if not self.is_connected():
            return False
        self._features_complete.clear()
        try:
            await self._send_command(
                protocol.build_packet(protocol.CMD_SYSTEM_GET_CAPS)
            )
            await asyncio.wait_for(
                self._features_complete.wait(), FEATURE_DISCOVERY_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.debug("Feature discovery timed out; using defaults")
            return False
        except BleakError as err:
            _LOGGER.debug("Feature discovery failed: %s", err)
            return False
        _LOGGER.info("Octo bed features: %s", self.get_feature_summary())
        if self._has_synchro:
            await self._send_command(
                protocol.build_packet(protocol.CMD_CONFIG_GET_DRIVEMODE)
            )
        return True

    def _handle_feature_response(self, data: list[int]) -> None:
        """Process one capability entry from a feature discovery response."""
        result = protocol.extract_feature(data)
        if result is None:
            return
        feature_id, value, value_type = result
        if feature_id == protocol.FEATURE_END:
            self._features_complete.set()
        elif feature_id == protocol.FEATURE_MOTORCOUNT:
            self._motor_count = value[0] if value else None
        elif feature_id == protocol.FEATURE_MEMCOUNT:
            self._memory_count = value[0] if value else 0
        elif feature_id == protocol.FEATURE_SYNCHRO:
            self._has_synchro = True
        elif feature_id == protocol.FEATURE_LIGHT:
            self._has_light = True
        elif feature_id == protocol.FEATURE_LIGHT_RGBWI:
            self._has_rgbwi = True
            self._rgbwi_value_type = value_type

    # ------------------------------------------------------------- connection

    def register_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Register a callback invoked with the new connected state."""
        self._connection_callbacks.append(callback)

    def _notify_connection_change(self, connected: bool) -> None:
        for callback in self._connection_callbacks:
            try:
                callback(connected)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Connection callback failed", exc_info=True)

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
        """Background task: refresh PIN authentication periodically.

        Transient write failures are tolerated; the loop only ends when the
        connection is gone (the reconnect logic restarts it on reconnect).
        """
        while True:
            await asyncio.sleep(PIN_KEEPALIVE_SECONDS)
            client = self._client
            if self._intentional_disconnect or not client or not client.is_connected:
                return
            try:
                await client.write_gatt_char(
                    COMMAND_CHAR_UUID, encode_pin(self._pin), response=False
                )
                _LOGGER.debug("Keep-alive PIN pulse sent")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Keep-alive write failed (will retry): %s", err)

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Handle an unexpected or intentional disconnect."""
        self._client = None
        self._notify_connection_change(False)
        if self._intentional_disconnect:
            return
        if self._disconnect_callback:
            try:
                self._disconnect_callback()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Disconnect callback failed", exc_info=True)
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Reconnect with backoff after an unexpected disconnect."""
        for attempt, delay in enumerate(
            list(RECONNECT_DELAYS) + [RECONNECT_DELAYS[-1]] * 1000, start=1
        ):
            await asyncio.sleep(delay)
            if self._intentional_disconnect:
                return
            if self.is_connected():
                return
            _LOGGER.debug("Reconnect attempt %d to %s", attempt, self._device.address)
            try:
                if await self.connect():
                    _LOGGER.info("Reconnected to Octo bed at %s", self._device.address)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Reconnect attempt failed: %s", err)

    async def _establish(self) -> None:
        """Create the BLE connection and subscribe to notifications."""
        if self._device_resolver:
            fresh = await self._device_resolver()
            if fresh:
                self._device = fresh
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                "Octo Bed",
                disconnected_callback=self._on_disconnect,
                timeout=15.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "establish_connection failed, trying direct BleakClient: %s", err
            )
            direct = BleakClient(self._device, disconnected_callback=self._on_disconnect)
            await direct.connect(timeout=15.0)
            self._client = direct
        _LOGGER.debug("Connected to Octo bed at %s", self._device.address)
        try:
            await self._client.start_notify(
                COMMAND_CHAR_UUID, self._notification_handler
            )
        except Exception as err:  # noqa: BLE001
            # Notifications drive re-auth requests and feature discovery but
            # the bed can still be controlled without them.
            _LOGGER.debug("Could not subscribe to notifications: %s", err)

    async def connect(self) -> bool:
        """Connect to the bed and authenticate with PIN."""
        async with self._connect_lock:
            if self.is_connected():
                return True
            try:
                self._intentional_disconnect = False
                await self._establish()
                await self.send_pin()
                self._start_keepalive()
                self._notify_connection_change(True)
                return True
            except asyncio.CancelledError:
                raise  # do not treat task cancellation as connection failure
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to connect to Octo bed: %s", err)
                return False

    async def connect_and_verify_pin(self) -> bool:
        """Connect, send PIN, and verify acceptance via notification.

        Returns True only if the bed sends PIN accepted; False on reject or
        timeout. Used by the config flow.
        """
        try:
            self._intentional_disconnect = False
            await self._establish()

            pin_result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._pin_verify_future = pin_result
            try:
                await self.send_pin()
                result = await asyncio.wait_for(pin_result, 8.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("PIN verification timed out waiting for bed response")
                result = False
            finally:
                self._pin_verify_future = None

            if not result:
                await self.disconnect()
                return False

            self._start_keepalive()
            self._notify_connection_change(True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to connect to Octo bed: %s", err)
            return False

    _pin_verify_future: asyncio.Future[bool] | None = None

    async def disconnect(self) -> None:
        """Disconnect from the bed."""
        self._intentional_disconnect = True
        await self._stop_keepalive()
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def ensure_connected(self) -> bool:
        """Ensure we are connected; reconnect if needed."""
        if self._client and self._client.is_connected:
            return True
        if self._intentional_disconnect:
            return False
        _LOGGER.info("Reconnecting to Octo bed at %s", self._device.address)
        return await self.connect()

    # ------------------------------------------------------------ notifications

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notifications from the bed."""
        raw = bytes(data)
        _LOGGER.debug("Notification: %s", raw.hex())

        # PIN verification result (config flow)
        future = self._pin_verify_future
        if future is not None and not future.done():
            if raw == NOTIFY_PIN_ACCEPTED:
                future.set_result(True)
                return
            if raw == NOTIFY_PIN_REJECTED:
                future.set_result(False)
                return

        # PIN required (keep-alive request / initial auth)
        pin_required = (
            len(raw) >= 7 and raw[:7] == NOTIFY_PIN_REQUIRED[:7]
        ) or raw == NOTIFY_PIN_REQUIRED_ALT
        if pin_required:
            _LOGGER.debug("PIN required, sending authentication")
            self._send_pin_async()
            return

        parsed = protocol.parse_packet(raw)
        if parsed is None:
            return
        command, packet_data = parsed
        if command == (0x21, 0x71):
            self._handle_feature_response(packet_data)
        elif command[0] == 0x11 and command[1] in (0x71, 0x72) and packet_data:
            # CONFIG_SET/GET_DRIVEMODE response
            self._synchro_active = packet_data[0] == protocol.DRIVEMODE_SYNC

    def _send_pin_async(self) -> None:
        """Send PIN asynchronously - called from notification handler."""
        if self._client and self._client.is_connected:
            # Keep a reference so the task is not garbage collected mid-flight
            self._pin_task = asyncio.create_task(self.send_pin())

    # ----------------------------------------------------------------- commands

    async def _send_command(self, data: bytes) -> bool:
        """Send raw command to the bed."""
        if not await self.ensure_connected():
            _LOGGER.warning("Not connected to Octo bed")
            return False

        try:
            await self._client.write_gatt_char(COMMAND_CHAR_UUID, data, response=False)
            if protocol.is_pin_packet(data):
                _LOGGER.debug("Sent command: PIN authentication (masked)")
            else:
                _LOGGER.debug("Sent command: %s", data.hex())
            return True
        except BleakError as err:
            _LOGGER.error("Failed to send command: %s", err)
            return False

    async def send_pin(self) -> bool:
        """Send PIN authentication."""
        return await self._send_command(encode_pin(self._pin))

    async def send_stop(self) -> bool:
        """Send a single stop command to the bed (no task bookkeeping)."""
        return await self._send_command(CMD_STOP)

    async def both_down(self) -> bool:
        """Send both sides down command."""
        return await self._send_command(CMD_BOTH_DOWN)

    async def both_up(self) -> bool:
        """Send both sides up command."""
        return await self._send_command(CMD_BOTH_UP)

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

    async def recall_memory_preset(self, slot: int) -> bool:
        """Recall a hardware memory preset (0-based slot)."""
        if slot < 0 or slot >= self.memory_slot_count:
            _LOGGER.warning("Invalid memory slot %d", slot)
            return False
        return await self._send_command(
            protocol.build_packet(protocol.CMD_MOTOR_MEMPOS, [slot])
        )

    async def save_memory_preset(self, slot: int) -> bool:
        """Save the current position to a hardware memory slot (0-based)."""
        if slot < 0 or slot >= self.memory_slot_count:
            _LOGGER.warning("Invalid memory slot %d", slot)
            return False
        return await self._send_command(
            protocol.build_packet(protocol.CMD_CONFIG_SAVE_MOTORPOS, [slot])
        )

    async def set_synchro_mode(self, enabled: bool) -> bool:
        """Set linked (sync) or independent (single) drive mode."""
        mode = protocol.DRIVEMODE_SYNC if enabled else protocol.DRIVEMODE_SINGLE
        ok = await self._send_command(
            protocol.build_packet(protocol.CMD_CONFIG_SET_DRIVEMODE, [mode])
        )
        if ok:
            self._synchro_active = enabled
        return ok

    # ------------------------------------------------------------- movement state

    def register_movement_task(self, task: asyncio.Task[None]) -> None:
        """Register a movement task so it can be cancelled when stop is called."""
        self._active_movement_tasks.add(task)
        task.add_done_callback(self._active_movement_tasks.discard)

    def register_active_movement(self, part: str, task: asyncio.Task[None]) -> None:
        """Register an active movement for a specific part (head, feet, or both).

        This will cancel any conflicting movements.
        """
        if part == "head":
            self._cancel_movement("feet")
            self._cancel_movement("both")
        elif part == "feet":
            self._cancel_movement("head")
            self._cancel_movement("both")
        elif part == "both":
            self._cancel_movement("head")
            self._cancel_movement("feet")

        if part in self._active_movements:
            old_task = self._active_movements[part]
            if not old_task.done():
                old_task.cancel()

        self._active_movements[part] = task

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
                asyncio.create_task(self._wait_for_cancellation(task))

    async def _wait_for_cancellation(self, task: asyncio.Task[None]) -> None:
        """Wait for a task to be cancelled and send stop command."""
        try:
            await task
        except asyncio.CancelledError:
            try:
                await self.send_stop()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to send stop after cancelling movement", exc_info=True)

    # --------------------------------------------------------------- calibration

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
        """Return True if calibration is in progress (any phase)."""
        return self._calibration_phase is not None or self._calibration_completing

    def _reset_calibration_tracking(self) -> None:
        """Clear the preparing/tracking session state and notify listeners."""
        self._calibration_part = None
        self._calibration_phase = None
        self._calibration_start_time = None
        self._calibration_task = None
        self._notify_calibration_state()

    async def start_calibration(
        self, part: str, down_seconds: float = 30.0
    ) -> None:
        """Start calibration for head or feet.

        First drives the part fully down (current travel time + margin) so the
        measurement starts from 0%, then moves up while counting time.
        """
        if part not in ("head", "feet"):
            return
        await self.cancel_calibration()
        self._calibration_part = part
        self._calibration_phase = "preparing"
        self._calibration_start_time = None
        self._calibration_task = asyncio.create_task(
            self._calibration_session(part, down_seconds)
        )
        self.register_movement_task(self._calibration_task)
        self.register_active_movement(part, self._calibration_task)
        self._notify_calibration_state()

    async def _calibration_session(self, part: str, down_seconds: float) -> None:
        """Drive the part to 0%, then move up while measuring until completed."""
        up = self.head_up if part == "head" else self.feet_up
        down = self.head_down if part == "head" else self.feet_down
        setter = self.set_head_position if part == "head" else self.set_feet_position
        cancelled = False
        try:
            # Phase 1: ensure the part is at 0% (down for full travel + margin)
            down_for = max(MIN_TRAVEL_SECONDS, min(MAX_TRAVEL_SECONDS, float(down_seconds))) + 2.0
            end = time.monotonic() + down_for
            while time.monotonic() < end:
                if not await down():
                    _LOGGER.warning("Calibration for %s aborted: bed not reachable", part)
                    return
                await asyncio.sleep(0.1)
            await self.send_stop()
            setter(0)

            # Phase 2: move up and measure until complete_calibration is called
            self._calibration_phase = "tracking"
            self._calibration_start_time = time.monotonic()
            self._notify_calibration_state()
            end = time.monotonic() + MAX_CALIBRATION_TRACKING_SECONDS
            while time.monotonic() < end:
                if not await up():
                    _LOGGER.warning("Calibration for %s aborted: bed not reachable", part)
                    return
                await asyncio.sleep(0.1)
            _LOGGER.warning(
                "Calibration session for %s not completed within %d s; aborted without saving",
                part,
                int(MAX_CALIBRATION_TRACKING_SECONDS),
            )
        except asyncio.CancelledError:
            # Cancellers (complete/cancel_calibration, stop, conflicting moves)
            # send the stop command themselves.
            cancelled = True
            raise
        finally:
            if not cancelled:
                await self.send_stop()
            # Always clear session state so a cancellation from any source
            # (e.g. a conflicting movement) can never wedge the entities.
            self._reset_calibration_tracking()

    async def cancel_calibration(self) -> bool:
        """Abort any calibration session without saving. Returns True if one was active."""
        active = self._calibration_phase is not None
        task = self._calibration_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await self.send_stop()
        if active:
            self._reset_calibration_tracking()
        return active

    async def complete_calibration(self) -> tuple[str | None, float]:
        """Stop measuring and return (part, duration_seconds).

        Returns (None, 0) when no measuring session is active (e.g. still in
        the preparing phase).
        """
        if self._calibration_phase != "tracking" or self._calibration_start_time is None:
            return (None, 0.0)
        part = self._calibration_part
        duration = max(0.0, time.monotonic() - self._calibration_start_time)
        task = self._calibration_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.send_stop()
        self._reset_calibration_tracking()
        _LOGGER.info("Calibration complete for %s: %.1f seconds (100%% travel)", part, duration)
        return (part, duration)

    def is_calibrating(self) -> bool:
        """Return True while the measuring (tracking) phase is active."""
        return self._calibration_phase == "tracking"

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
            await self.send_stop()
            setter(0)
            self._calibration_completing = False
            self._calibration_returning_part = None
            self._notify_calibration_state()

    def get_calibration_status(self) -> tuple[str, str | None]:
        """Return (state, part). state: 'idle' | 'preparing' | 'tracking' | 'returning'."""
        if self._calibration_completing and self._calibration_returning_part:
            return ("returning", self._calibration_returning_part)
        if self._calibration_phase is not None:
            return (self._calibration_phase, self._calibration_part)
        return ("idle", None)

    # ------------------------------------------------------------------ position

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
        return int(round((self._head_position + self._feet_position) / 2.0))

    async def run_to_position(
        self,
        head_target: int,
        feet_target: int,
        head_travel_seconds: float,
        feet_travel_seconds: float,
    ) -> None:
        """Move head and feet to target positions (0-100). Uses travel seconds for full range."""
        head_target = max(0, min(100, head_target))
        feet_target = max(0, min(100, feet_target))
        head_current = self.get_head_position()
        feet_current = self.get_feet_position()
        interval = 0.375

        # Move head
        if head_target != head_current and head_travel_seconds > 0:
            duration = abs(head_target - head_current) / 100.0 * head_travel_seconds
            method = self.head_up if head_target > head_current else self.head_down
            start = time.monotonic()
            end = start + duration
            try:
                while time.monotonic() < end:
                    await method()
                    elapsed = time.monotonic() - start
                    frac = min(1.0, elapsed / duration) if duration > 0 else 1.0
                    self.set_head_position(
                        int(round(head_current + (head_target - head_current) * frac))
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                # Cancellers (stop, conflicting moves) send the stop command;
                # record how far we actually got instead of snapping to target.
                frac = min(1.0, (time.monotonic() - start) / duration) if duration > 0 else 1.0
                self.set_head_position(
                    int(round(head_current + (head_target - head_current) * frac))
                )
                raise
            await self.send_stop()
            self.set_head_position(head_target)

        # Move feet
        if feet_target != feet_current and feet_travel_seconds > 0:
            duration = abs(feet_target - feet_current) / 100.0 * feet_travel_seconds
            method = self.feet_up if feet_target > feet_current else self.feet_down
            start = time.monotonic()
            end = start + duration
            try:
                while time.monotonic() < end:
                    await method()
                    elapsed = time.monotonic() - start
                    frac = min(1.0, elapsed / duration) if duration > 0 else 1.0
                    self.set_feet_position(
                        int(round(feet_current + (feet_target - feet_current) * frac))
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                frac = min(1.0, (time.monotonic() - start) / duration) if duration > 0 else 1.0
                self.set_feet_position(
                    int(round(feet_current + (feet_target - feet_current) * frac))
                )
                raise
            await self.send_stop()
            self.set_feet_position(feet_target)

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

        Callback receives (part: str, position: int) where part is 'head' or 'feet'.
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
        """Send stop command and cancel all active movement tasks.

        Also aborts a running calibration session (without saving) so a stop
        from any source can never leave the calibration state wedged.
        """
        await self.cancel_calibration()
        for task in list(self._active_movement_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return await self.send_stop()

    # --------------------------------------------------------------------- light

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

    async def set_light_color_rgbw(self, rgbw: tuple[int, int, int, int]) -> bool:
        """Set RGBW light color (beds with CAP_LIGHT_RGBWI only)."""
        r, g, b, w = (max(0, min(255, v)) for v in rgbw)
        value_type = self._rgbwi_value_type if self._rgbwi_value_type is not None else 0x05
        return await self._send_command(
            protocol.build_packet(
                protocol.CMD_SYSTEM_SET_CAPS,
                [0x00, 0x01, 0x04, 0x00, 0x01, 0x01, value_type, r, g, b, w, 0xFF],
            )
        )
