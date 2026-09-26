"""BLE client for Octo Bed communication.

Connection model
----------------
Every bed has exactly one *connection manager* task (``_manager_loop``). It is
the only code path that opens a BLE connection; commands that need the bed ask
the manager for a connection instead of connecting themselves. That rules out
the races the earlier design suffered from, where the initial connect, the
reconnect loop and every command could all try to connect at the same time
(and could deadlock on the connect lock).

The manager:

* only tries to connect while the bed is advertising and otherwise waits for
  its next advertisement, so an unreachable bed never ties up the limited
  connection slots of a Bluetooth proxy with futile attempts;
* sets up one connection at a time across all beds (``connect_gate``), because
  simultaneous GATT discovery on a single ESP32 proxy is a known cause of
  "GATT error 133";
* backs off between failed attempts, but tries again immediately when the bed
  reappears or a command needs it;
* keeps the PIN session alive and treats a link whose bed stops acknowledging
  the keep-alive as dead, so a half-open connection cannot stay "connected".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bleak import BleakClient
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
    MOVEMENT_COMMAND_INTERVAL,
    NOTIFY_PIN_ACCEPTED,
    NOTIFY_PIN_REJECTED,
    NOTIFY_PIN_REQUIRED,
    NOTIFY_PIN_REQUIRED_ALT,
    PIN_KEEPALIVE_SECONDS,
)
from .protocol import encode_pin

_LOGGER = logging.getLogger(__name__)

# Waits between failed connection attempts while the bed is advertising.
RECONNECT_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
# While the bed is not advertising, still try this often in case presence
# tracking missed it.
ABSENT_RETRY_SECONDS = 300.0
CONNECT_TIMEOUT = 15.0
# Attempts inside bleak-retry-connector per manager attempt; the manager does
# its own retrying, so keep this low to release the connect gate quickly.
CONNECT_MAX_ATTEMPTS = 2
# Hold the connect gate briefly after connecting so the proxy finishes setting
# up this link before the next bed starts its own GATT discovery.
CONNECT_SETTLE_SECONDS = 1.0
ON_DEMAND_CONNECT_TIMEOUT = 20.0
WRITE_TIMEOUT = 5.0
DISCONNECT_TIMEOUT = 5.0
# A bed that acknowledged keep-alives before but stays silent this long is
# considered gone (three missed keep-alives).
LIVENESS_TIMEOUT = PIN_KEEPALIVE_SECONDS * 3 + 5
FEATURE_DISCOVERY_TIMEOUT = 5.0
PIN_VERIFY_TIMEOUT = 8.0
# Calibration limits: travel times are clamped to this range when saved, and
# a measuring session that is never completed aborts itself.
MIN_TRAVEL_SECONDS = 5.0
MAX_TRAVEL_SECONDS = 120.0
MAX_CALIBRATION_TRACKING_SECONDS = 180.0

# Capabilities that are persisted between restarts (runtime state such as the
# current drive mode is deliberately left out).
CAPABILITY_KEYS = (
    "motor_count",
    "memory_slots",
    "has_light",
    "has_rgbwi_light",
    "rgbwi_value_type",
    "has_synchro",
)

_MOTION_COMMANDS: dict[tuple[frozenset[str], bool], bytes] = {
    (frozenset({"head"}), True): CMD_HEAD_UP,
    (frozenset({"head"}), False): CMD_HEAD_DOWN,
    (frozenset({"feet"}), True): CMD_FEET_UP,
    (frozenset({"feet"}), False): CMD_FEET_DOWN,
    (frozenset({"head", "feet"}), True): CMD_BOTH_UP,
    (frozenset({"head", "feet"}), False): CMD_BOTH_DOWN,
}


class _BedUnreachableError(Exception):
    """Raised inside a movement when the bed stops accepting commands."""


@dataclass
class _PartMove:
    """One motor's share of a movement."""

    part: str
    start: int
    target: int
    duration: float  # seconds until the estimated position reaches target
    run_for: float  # seconds the motor keeps being driven (>= duration)

    def position_at(self, elapsed: float) -> int:
        if self.duration <= 0:
            return self.target
        frac = min(1.0, max(0.0, elapsed / self.duration))
        return round(self.start + (self.target - self.start) * frac)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _describe(err: BaseException) -> str:
    text = str(err).strip()
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


def _add_listener(listeners: list[Any], listener: Any) -> Callable[[], None]:
    """Append a listener and return a function that removes it again."""
    listeners.append(listener)

    def _remove() -> None:
        try:
            listeners.remove(listener)
        except ValueError:
            pass

    return _remove


class OctoBedClient:
    """Client for communicating with an Octo Bed via BLE."""

    def __init__(
        self,
        device: BLEDevice | None,
        pin: str,
        *,
        address: str | None = None,
        name: str | None = None,
        disconnect_callback: Callable[[], None] | None = None,
        device_resolver: Callable[[], BLEDevice | None] | None = None,
        presence_checker: Callable[[], bool] | None = None,
        connect_gate: asyncio.Lock | None = None,
        features_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Initialize the Octo Bed client.

        ``device`` may be None when the bed has not been seen yet; the
        ``device_resolver`` is then asked for a fresh BLEDevice before every
        connection attempt.
        """
        if device is None and address is None:
            raise ValueError("Either a BLE device or an address is required")
        self._device = device
        self._address = (address or device.address).upper()  # type: ignore[union-attr]
        self._name = name or "Octo Bed"
        self._pin = pin
        self._disconnect_callback = disconnect_callback
        self._device_resolver = device_resolver
        self._presence_checker = presence_checker
        self._connect_gate = connect_gate
        self._features_callback = features_callback

        # Connection state
        self._client: BleakClient | None = None
        self._pending_client: BleakClient | None = None
        self._closed = False
        self._state = "idle"
        self._wakeup = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._demand = False
        self._waiting_for_presence = False
        self._failures = 0
        self._manager_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pin_verify_future: asyncio.Future[bool] | None = None
        self._last_rx: float | None = None
        self._ack_seen = False
        self._pin_rejected_logged = False
        # Connection statistics (diagnostics / connection sensor)
        self._last_connected: str | None = None
        self._last_disconnected: str | None = None
        self._last_error: str | None = None
        self._attempts_since_connect = 0
        self._connection_count = 0
        self._via: str | None = None

        # Shared position state (0-100, where 0 = down, 100 = up)
        self._head_position: int = 0
        self._feet_position: int = 0
        self._position_callbacks: list[Callable[[str, int], None]] = []
        self._connection_callbacks: list[Callable[[bool], None]] = []
        self._active_movement_tasks: set[asyncio.Task[Any]] = set()
        # Track active movements by part to prevent conflicts
        self._active_movements: dict[str, asyncio.Task[Any]] = {}

        # Calibration: part being calibrated, phase and when measuring started
        self._calibration_part: str | None = None
        self._calibration_phase: str | None = None  # "preparing" | "tracking"
        self._calibration_start_time: float | None = None
        self._calibration_task: asyncio.Task[None] | None = None
        # True while move_part_down_for_seconds is running (after complete_calibration)
        self._calibration_completing: bool = False
        self._calibration_returning_part: str | None = None
        self._calibration_state_callbacks: list[Callable[[], None]] = []

        # Bed capabilities (filled by discovery or persisted data; None = unknown)
        self._features_complete = asyncio.Event()
        self._features_discovered = False
        self._motor_count: int | None = None
        self._memory_count: int | None = None
        self._has_light: bool | None = None
        self._has_rgbwi: bool = False
        self._rgbwi_value_type: int | None = None
        self._has_synchro: bool | None = None
        self._synchro_active: bool | None = None

    # ------------------------------------------------------------ properties

    @property
    def address(self) -> str:
        """The bed's Bluetooth address."""
        return self._address

    def get_device_address(self) -> str:
        """Return the Bluetooth device MAC address."""
        return self._address

    def is_connected(self) -> bool:
        """Return True if an authenticated link to the bed is up."""
        client = self._client
        return client is not None and client.is_connected

    @property
    def connection_info(self) -> dict[str, Any]:
        """Connection details for diagnostics and the connection sensor."""
        return {
            "state": self._state,
            "via": self._via,
            "last_connected": self._last_connected,
            "last_disconnected": self._last_disconnected,
            "last_error": self._last_error,
            "attempts_since_last_connect": self._attempts_since_connect,
            "connections": self._connection_count,
        }

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

    def get_capabilities(self) -> dict[str, Any]:
        """Return the bed's capabilities in the form that is persisted."""
        return {
            "motor_count": self._motor_count,
            "memory_slots": self._memory_count,
            "has_light": self._has_light,
            "has_rgbwi_light": self._has_rgbwi,
            "rgbwi_value_type": self._rgbwi_value_type,
            "has_synchro": self._has_synchro,
        }

    def load_capabilities(self, capabilities: dict[str, Any] | None) -> None:
        """Preload capabilities stored from an earlier discovery.

        Lets entities that depend on capabilities (presets, RGBW light, synchro
        switch) be created correctly before the bed has even connected.
        """
        if not capabilities:
            return
        self._motor_count = capabilities.get("motor_count")
        self._memory_count = capabilities.get("memory_slots")
        self._has_light = capabilities.get("has_light")
        self._has_rgbwi = bool(capabilities.get("has_rgbwi_light"))
        self._rgbwi_value_type = capabilities.get("rgbwi_value_type")
        self._has_synchro = capabilities.get("has_synchro")

    def get_feature_summary(self) -> dict[str, Any]:
        """Return discovered capabilities plus runtime state (for diagnostics)."""
        return {**self.get_capabilities(), "synchro_active": self._synchro_active}

    async def discover_features(self) -> bool:
        """Query bed capabilities; returns True when the full list was received.

        Best effort: beds that do not answer keep their defaults.
        """
        if not self.is_connected():
            return False
        self._features_complete.clear()
        if not await self._write(
            protocol.build_packet(protocol.CMD_SYSTEM_GET_CAPS), quiet=True
        ):
            return False
        try:
            async with asyncio.timeout(FEATURE_DISCOVERY_TIMEOUT):
                await self._features_complete.wait()
        except TimeoutError:
            _LOGGER.debug("Feature discovery for %s timed out; using defaults", self._address)
            return False
        self._features_discovered = True
        _LOGGER.debug("Octo bed %s features: %s", self._address, self.get_feature_summary())
        if self._has_synchro:
            await self._write(
                protocol.build_packet(protocol.CMD_CONFIG_GET_DRIVEMODE), quiet=True
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

    def _maybe_discover_features(self) -> None:
        if self._features_discovered:
            return
        self._spawn(self._discover_features_task())

    async def _discover_features_task(self) -> None:
        if not await self.discover_features():
            return
        if self._features_callback is not None:
            try:
                self._features_callback(self.get_capabilities())
            except Exception:
                _LOGGER.exception("Handling discovered features of %s failed", self._address)

    # --------------------------------------------------------------- listeners

    def register_connection_callback(
        self, callback: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Register a callback invoked with the new connected state.

        Returns a function that unregisters the callback again.
        """
        return _add_listener(self._connection_callbacks, callback)

    def register_position_callback(
        self, callback: Callable[[str, int], None]
    ) -> Callable[[], None]:
        """Register a callback for position changes ('head'/'feet', 0-100).

        Returns a function that unregisters the callback again.
        """
        return _add_listener(self._position_callbacks, callback)

    def register_calibration_state_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback for calibration state changes.

        Returns a function that unregisters the callback again.
        """
        return _add_listener(self._calibration_state_callbacks, callback)

    def _notify_connection_change(self, connected: bool) -> None:
        for callback in list(self._connection_callbacks):
            try:
                callback(connected)
            except Exception:
                _LOGGER.debug("Connection callback failed", exc_info=True)

    def _notify_position_change(self, part: str, position: int) -> None:
        for callback in list(self._position_callbacks):
            try:
                callback(part, position)
            except Exception:
                _LOGGER.debug("Position callback failed", exc_info=True)

    def _notify_calibration_state(self) -> None:
        for callback in list(self._calibration_state_callbacks):
            try:
                callback()
            except Exception:
                _LOGGER.debug("Calibration state callback error", exc_info=True)

    # ------------------------------------------------------ connection manager

    def start(self) -> None:
        """Start the connection manager (idempotent)."""
        if self._closed:
            return
        if self._manager_task is not None and not self._manager_task.done():
            return
        self._manager_task = asyncio.create_task(
            self._manager_loop(), name=f"octo_bed_connection_{self._address}"
        )

    def async_on_advertisement(self) -> None:
        """Tell the manager the bed was just seen advertising.

        Only wakes the manager while it is waiting for the bed to come into
        range; during a back-off wait advertisements are ignored so a bed that
        advertises but refuses connections is not hammered.
        """
        if self._waiting_for_presence:
            self._wakeup.set()

    async def ensure_connected(self, timeout: float = ON_DEMAND_CONNECT_TIMEOUT) -> bool:
        """Return True once connected, asking the manager to connect if needed.

        Fails fast (False) when the bed is not in range, so a command never
        blocks on a bed that cannot be reached.
        """
        if self.is_connected():
            return True
        if self._closed or not self._is_present():
            return False
        self.start()
        self._demand = True
        self._wakeup.set()
        try:
            async with asyncio.timeout(timeout):
                await self._connected_event.wait()
        except TimeoutError:
            return False
        return self.is_connected()

    async def connect(self) -> bool:
        """Start the manager and wait for a connection (on-demand timeout)."""
        self.start()
        return await self.ensure_connected()

    def async_ensure_reconnecting(self) -> None:
        """Backwards-compatible alias of :meth:`start`."""
        self.start()

    def _is_present(self) -> bool:
        if self._presence_checker is None:
            return True
        try:
            return bool(self._presence_checker())
        except Exception:
            _LOGGER.debug("Presence check for %s failed", self._address, exc_info=True)
            return True

    def _resolve_device(self) -> BLEDevice | None:
        if self._device_resolver is not None:
            try:
                fresh = self._device_resolver()
            except Exception:
                _LOGGER.debug("Resolving BLE device for %s failed", self._address, exc_info=True)
                fresh = None
            if fresh is not None:
                return fresh
        return self._device

    async def _wait_for_wakeup(self, timeout: float | None) -> bool:
        """Wait until woken (or timeout). Returns True when woken."""
        if self._closed or self._demand:
            return True
        self._wakeup.clear()
        if timeout is None:
            await self._wakeup.wait()
            return True
        try:
            async with asyncio.timeout(timeout):
                await self._wakeup.wait()
        except TimeoutError:
            return False
        return True

    async def _manager_loop(self) -> None:
        """Own the bed's BLE connection for the lifetime of the client."""
        while not self._closed:
            try:
                await self._manager_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let an unexpected error end reconnecting for good.
                _LOGGER.exception("Connection manager for Octo bed %s failed", self._address)
                await asyncio.sleep(RECONNECT_DELAYS[-1])
        self._state = "closed"

    async def _manager_step(self) -> None:
        if self.is_connected():
            self._failures = 0
            self._demand = False
            self._state = "connected"
            await self._wait_for_wakeup(None)
            return

        if self._client is not None:
            # The link reports itself down without bleak having called us.
            self._handle_lost("link reported down")

        demanded = self._demand
        self._demand = False
        if not demanded and not self._is_present():
            self._state = "waiting_for_bed"
            self._waiting_for_presence = True
            try:
                await self._wait_for_wakeup(ABSENT_RETRY_SECONDS)
            finally:
                self._waiting_for_presence = False
            if self._closed:
                return
            self._demand = False

        self._state = "connecting"
        if await self._attempt_connect():
            self._failures = 0
            return
        if self._closed:
            return
        self._failures += 1
        delay = RECONNECT_DELAYS[min(self._failures, len(RECONNECT_DELAYS)) - 1]
        self._state = "retry_wait"
        _LOGGER.debug(
            "Octo bed %s not connected (attempt %d, %s); next try in %.0f s",
            self._address,
            self._attempts_since_connect,
            self._last_error,
            delay,
        )
        await self._wait_for_wakeup(delay)

    def _gate(self) -> asyncio.Lock:
        if self._connect_gate is None:
            self._connect_gate = asyncio.Lock()
        return self._connect_gate

    async def _attempt_connect(self) -> bool:
        """Open, subscribe and authenticate one connection. Never raises."""
        device = self._resolve_device()
        if device is None:
            self._last_error = "bed not seen by any Bluetooth adapter or proxy"
            return False
        self._attempts_since_connect += 1
        async with self._gate():
            if self._closed:
                return False
            # Re-resolve inside the gate: waiting may have taken a while.
            device = self._resolve_device() or device
            self._device = device
            client: BleakClient | None = None
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self._name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=CONNECT_MAX_ATTEMPTS,
                    ble_device_callback=lambda: self._resolve_device() or device,
                    timeout=CONNECT_TIMEOUT,
                )
                self._pending_client = client
                await client.start_notify(COMMAND_CHAR_UUID, self._notification_handler)
                await self._write_to(client, encode_pin(self._pin))
                self._require_usable(client)
            except asyncio.CancelledError:
                self._pending_client = None
                if client is not None:
                    await self._safe_disconnect(client)
                raise
            except Exception as err:
                # Covers e.g. "Insufficient authorization" or GATT error 133 on
                # the notify subscribe: the link is unusable, so drop it and let
                # the manager retry cleanly.
                self._pending_client = None
                self._last_error = _describe(err)
                _LOGGER.debug("Connecting to Octo bed %s failed: %s", self._address, err)
                if client is not None:
                    await self._safe_disconnect(client)
                return False

            self._pending_client = None
            self._client = client
            self._last_rx = time.monotonic()
            self._ack_seen = False
            self._last_error = None
            self._last_connected = _now_iso()
            self._attempts_since_connect = 0
            self._connection_count += 1
            details = getattr(device, "details", None)
            self._via = details.get("source") if isinstance(details, dict) else None
            self._state = "connected"
            self._demand = False
            self._connected_event.set()
            self._start_keepalive()
            _LOGGER.info(
                "Connected to Octo bed %s%s",
                self._address,
                f" via {self._via}" if self._via else "",
            )
            self._notify_connection_change(True)
            self._maybe_discover_features()
            await asyncio.sleep(CONNECT_SETTLE_SECONDS)
        return True

    def _require_usable(self, client: BleakClient) -> None:
        if self._closed or not client.is_connected:
            raise ConnectionError("link dropped during authentication")

    def _on_disconnect(self, client: BleakClient) -> None:
        """Bleak callback: a link went down."""
        if client is not self._client:
            # A link that is still being set up (the handshake fails and cleans
            # up) or an old one that was already replaced: it must not touch
            # the current connection.
            return
        self._handle_lost("connection lost")

    def _handle_lost(self, reason: str) -> None:
        """Bookkeeping for a lost current connection; wakes the manager."""
        self._client = None
        self._connected_event.clear()
        self._last_disconnected = _now_iso()
        self._last_error = reason
        keepalive = self._keepalive_task
        self._keepalive_task = None
        if keepalive is not None and keepalive is not asyncio.current_task():
            keepalive.cancel()
        self._notify_connection_change(False)
        if self._closed:
            return
        _LOGGER.warning(
            "Octo bed %s disconnected (%s); reconnecting in the background",
            self._address,
            reason,
        )
        if self._disconnect_callback is not None:
            try:
                self._disconnect_callback()
            except Exception:
                _LOGGER.debug("Disconnect callback failed", exc_info=True)
        self._wakeup.set()

    def _drop(self, client: BleakClient, reason: str) -> None:
        """Deliberately drop a link that stopped working."""
        if client is self._client:
            self._handle_lost(reason)
        self._spawn(self._safe_disconnect(client))

    async def _safe_disconnect(self, client: BleakClient) -> None:
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Disconnecting from %s failed: %s", self._address, err)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Run a background task that is cancelled when the client closes."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def async_close(self) -> None:
        """Close the client for good: stop reconnecting and drop the link."""
        if self._closed and self._manager_task is None and self._client is None:
            return
        self._closed = True
        self._state = "closed"
        self._wakeup.set()
        # Release anyone waiting in ensure_connected (they then see "closed").
        self._connected_event.set()
        current = asyncio.current_task()
        tasks = [
            t
            for t in (self._manager_task, self._keepalive_task, *self._tasks)
            if t is not None and t is not current and not t.done()
        ]
        self._manager_task = None
        self._keepalive_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                _LOGGER.debug("Tasks of Octo bed %s did not stop in time", self._address)
        was_connected = self._client is not None
        for client in (self._client, self._pending_client):
            if client is not None:
                await self._safe_disconnect(client)
        self._client = None
        self._pending_client = None
        if was_connected:
            self._notify_connection_change(False)

    async def disconnect(self) -> None:
        """Disconnect from the bed and stop reconnecting (alias of async_close)."""
        await self.async_close()

    # --------------------------------------------------------------- keep-alive

    def _start_keepalive(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keep_alive_loop())

    async def _keep_alive_loop(self) -> None:
        """Refresh PIN authentication and watch that the bed still answers."""
        while not self._closed:
            await asyncio.sleep(PIN_KEEPALIVE_SECONDS)
            client = self._client
            if client is None:
                return
            if not client.is_connected:
                # The link went down without bleak telling us.
                self._handle_lost("link reported down")
                return
            silent_for = (
                time.monotonic() - self._last_rx if self._last_rx is not None else 0.0
            )
            if self._ack_seen and silent_for > LIVENESS_TIMEOUT:
                _LOGGER.warning(
                    "Octo bed %s did not answer for %.0f s; reconnecting",
                    self._address,
                    silent_for,
                )
                self._drop(client, "no response to keep-alive")
                return
            try:
                await self._write_to(client, encode_pin(self._pin))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Keep-alive to %s failed (will retry): %s", self._address, err)

    # ------------------------------------------------------------ notifications

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notifications from the bed."""
        raw = bytes(data)
        self._last_rx = time.monotonic()
        future = self._pin_verify_future

        if raw == NOTIFY_PIN_ACCEPTED:
            # Also the bed's answer to every keep-alive: not worth a log line.
            self._ack_seen = True
            if future is not None and not future.done():
                future.set_result(True)
            return
        if raw == NOTIFY_PIN_REJECTED:
            if future is not None and not future.done():
                future.set_result(False)
            elif not self._pin_rejected_logged:
                self._pin_rejected_logged = True
                _LOGGER.error(
                    "Octo bed %s rejected the PIN; check the PIN configured for this bed",
                    self._address,
                )
            return

        _LOGGER.debug("Notification from %s: %s", self._address, raw.hex())

        pin_required = (
            len(raw) >= 7 and raw[:7] == NOTIFY_PIN_REQUIRED[:7]
        ) or raw == NOTIFY_PIN_REQUIRED_ALT
        if pin_required:
            self._spawn(self.send_pin())
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

    # ------------------------------------------------------------ config flow

    async def connect_and_verify_pin(self) -> bool:
        """Connect once, send the PIN and wait for the bed to accept it.

        Used by the config flow: returns True only when the bed confirms the
        PIN. The link is always closed again before returning.
        """
        device = self._resolve_device()
        if device is None:
            return False
        client: BleakClient | None = None
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pin_verify_future = future
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self._name,
                max_attempts=CONNECT_MAX_ATTEMPTS + 1,
                timeout=CONNECT_TIMEOUT,
            )
            await client.start_notify(COMMAND_CHAR_UUID, self._notification_handler)
            await self._write_to(client, encode_pin(self._pin))
            async with asyncio.timeout(PIN_VERIFY_TIMEOUT):
                return await future
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _LOGGER.warning("PIN verification timed out waiting for the bed's response")
            return False
        except Exception as err:
            _LOGGER.warning("Could not connect to Octo bed %s: %s", self._address, err)
            return False
        finally:
            self._pin_verify_future = None
            if client is not None:
                await self._safe_disconnect(client)

    # ----------------------------------------------------------------- commands

    async def _write_to(self, client: BleakClient, data: bytes) -> None:
        async with asyncio.timeout(WRITE_TIMEOUT):
            await client.write_gatt_char(COMMAND_CHAR_UUID, data, response=False)

    async def _write(self, data: bytes, *, quiet: bool = False) -> bool:
        """Write a packet on the current link; never connects, never raises."""
        client = self._client
        if client is None or not client.is_connected:
            return False
        try:
            await self._write_to(client, data)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log = _LOGGER.debug if quiet else _LOGGER.warning
            log("Sending a command to Octo bed %s failed: %s", self._address, _describe(err))
            return False
        if not quiet and _LOGGER.isEnabledFor(logging.DEBUG):
            shown = "PIN (masked)" if protocol.is_pin_packet(data) else data.hex()
            _LOGGER.debug("Sent to %s: %s", self._address, shown)
        return True

    async def _send_command(
        self, data: bytes, *, connect: bool = True, link: object | None = None
    ) -> bool:
        """Send a command, connecting on demand when ``connect`` is True.

        With ``link`` the command is only sent on that exact connection: a
        movement that started on one link must not silently continue on a new
        one after a reconnect, because the motor stopped in between.
        """
        if link is not None and self._client is not link:
            return False
        if not self.is_connected():
            if not connect:
                return False
            if not await self.ensure_connected():
                _LOGGER.warning(
                    "Octo bed %s is not reachable; command not sent", self._address
                )
                return False
        return await self._write(data)

    async def send_pin(self) -> bool:
        """Send PIN authentication on the current link."""
        return await self._write(encode_pin(self._pin), quiet=True)

    async def send_stop(self) -> bool:
        """Send a single stop command (only when connected; never connects)."""
        return await self._write(CMD_STOP)

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

    def register_movement_task(self, task: asyncio.Task[Any]) -> None:
        """Register a movement task so it can be cancelled when stop is called."""
        self._active_movement_tasks.add(task)
        task.add_done_callback(self._active_movement_tasks.discard)

    def register_active_movement(self, part: str, task: asyncio.Task[Any]) -> None:
        """Register an active movement for a part (head, feet or both).

        Cancels any conflicting movement.
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

        old_task = self._active_movements.get(part)
        if old_task is not None and old_task is not task and not old_task.done():
            old_task.cancel()

        self._active_movements[part] = task

        def cleanup(done: asyncio.Task[Any]) -> None:
            if self._active_movements.get(part) is done:
                self._active_movements.pop(part, None)

        task.add_done_callback(cleanup)

    def _cancel_movement(self, part: str) -> None:
        """Cancel an active movement for a specific part."""
        task = self._active_movements.get(part)
        if task is not None and not task.done():
            task.cancel()
            self._spawn(self._wait_for_cancellation(task))

    async def _wait_for_cancellation(self, task: asyncio.Task[Any]) -> None:
        """Wait for a cancelled movement to finish, then send stop."""
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Cancelled movement ended with an error", exc_info=True)
        await self.send_stop()

    # ------------------------------------------------------------------ position

    def get_head_position(self) -> int:
        """Get current head position (0-100)."""
        return self._head_position

    def get_feet_position(self) -> int:
        """Get current feet position (0-100)."""
        return self._feet_position

    def get_both_position(self) -> int:
        """Get current 'both' position (average of head and feet)."""
        return round((self._head_position + self._feet_position) / 2.0)

    def set_head_position(self, position: int) -> None:
        """Set head position (0-100) and notify listeners."""
        position = max(0, min(100, int(position)))
        if self._head_position != position:
            self._head_position = position
            self._notify_position_change("head", position)

    def set_feet_position(self, position: int) -> None:
        """Set feet position (0-100) and notify listeners."""
        position = max(0, min(100, int(position)))
        if self._feet_position != position:
            self._feet_position = position
            self._notify_position_change("feet", position)

    def set_both_position(self, position: int) -> None:
        """Set both head and feet positions to the same value."""
        self.set_head_position(position)
        self.set_feet_position(position)

    def _get_part_position(self, part: str) -> int:
        return self._head_position if part == "head" else self._feet_position

    def _set_part_position(self, part: str, position: int) -> None:
        if part == "head":
            self.set_head_position(position)
        else:
            self.set_feet_position(position)

    def _apply_moves(self, moves: Iterable[_PartMove], elapsed: float) -> None:
        for move in moves:
            self._set_part_position(move.part, move.position_at(elapsed))

    async def _motion_tick(self, command: bytes, link: object | None) -> object:
        """Send one motion command; returns the link it went out on."""
        if not await self._send_command(command, connect=link is None, link=link):
            raise _BedUnreachableError
        current = self._client
        if current is None:
            # Sent, but the link dropped right after: the motor stops.
            raise _BedUnreachableError
        return current

    async def _drive(self, moves: list[_PartMove], up: bool) -> bool:
        """Drive motors until every part's run time has elapsed.

        Sends the combined head+feet command while both parts still need to
        move and switches to the single-part command once one of them is done,
        so each part moves for exactly its own duration. Only the first command
        may connect on demand; if the bed stops accepting commands the movement
        ends early and the position reflects how far the bed actually got.
        Returns True when the movement completed.
        """
        started = False
        link: object | None = None
        start = time.monotonic()
        last_ok = 0.0
        try:
            while True:
                elapsed = time.monotonic() - start if started else 0.0
                active = frozenset(m.part for m in moves if elapsed < m.run_for)
                if not active:
                    break
                link = await self._motion_tick(_MOTION_COMMANDS[(active, up)], link)
                if not started:
                    started = True
                    start = time.monotonic()
                last_ok = time.monotonic() - start
                self._apply_moves(moves, last_ok)
                await asyncio.sleep(MOVEMENT_COMMAND_INTERVAL)
        except _BedUnreachableError:
            if started:
                self._apply_moves(moves, last_ok + MOVEMENT_COMMAND_INTERVAL)
                _LOGGER.warning(
                    "Octo bed %s stopped responding during a movement; "
                    "the shown position may be off",
                    self._address,
                )
            return False
        except asyncio.CancelledError:
            # Cancellers (stop, conflicting moves) send the stop command;
            # record how far we actually got instead of snapping to target.
            if started:
                self._apply_moves(moves, time.monotonic() - start)
            raise
        await self.send_stop()
        for move in moves:
            self._set_part_position(move.part, move.target)
        return True

    async def run_to_position(
        self,
        head_target: int | None,
        feet_target: int | None,
        head_travel_seconds: float,
        feet_travel_seconds: float,
    ) -> bool:
        """Move head and/or feet to target positions (0-100).

        A target of None keeps that part where it is. Parts moving in the same
        direction move simultaneously; opposite directions move one after the
        other (one packet cannot drive them in opposite directions).
        """
        moves: list[_PartMove] = []
        for part, target, travel in (
            ("head", head_target, head_travel_seconds),
            ("feet", feet_target, feet_travel_seconds),
        ):
            if target is None or travel is None or float(travel) <= 0:
                continue
            target = max(0, min(100, int(target)))
            start = self._get_part_position(part)
            if target == start:
                continue
            duration = abs(target - start) / 100.0 * float(travel)
            moves.append(_PartMove(part, start, target, duration, duration))
        if not moves:
            return True
        ups = [m for m in moves if m.target > m.start]
        downs = [m for m in moves if m.target < m.start]
        if ups and downs:
            for move in moves:
                if not await self._drive([move], move.target > move.start):
                    return False
            return True
        return await self._drive(moves, bool(ups))

    async def run_hold(
        self,
        up: bool,
        parts: Iterable[str],
        head_travel_seconds: float,
        feet_travel_seconds: float,
    ) -> bool:
        """Hold-to-run the given parts towards their end stop.

        The motors are driven for the full travel time so the bed really
        reaches its end stop even when the estimate had drifted, while each
        part's position advances at its own real speed.
        """
        target = 100 if up else 0
        moves: list[_PartMove] = []
        for part in parts:
            travel = float(head_travel_seconds if part == "head" else feet_travel_seconds)
            if travel <= 0:
                continue
            start = self._get_part_position(part)
            duration = abs(target - start) / 100.0 * travel
            moves.append(_PartMove(part, start, target, duration, travel))
        if not moves:
            return True
        return await self._drive(moves, up)

    async def stop(self) -> bool:
        """Send stop and cancel all active movement tasks.

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
                except Exception:
                    _LOGGER.debug("Movement ended with an error on stop", exc_info=True)
        return await self.send_stop()

    # --------------------------------------------------------------- calibration

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

    async def start_calibration(self, part: str, down_seconds: float = 30.0) -> None:
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
        up_cmd = CMD_HEAD_UP if part == "head" else CMD_FEET_UP
        down_cmd = CMD_HEAD_DOWN if part == "head" else CMD_FEET_DOWN
        cancelled = False
        try:
            # Phase 1: ensure the part is at 0% (down for full travel + margin).
            # Only the very first command may connect on demand; a link that
            # drops mid-way aborts the session instead of measuring garbage.
            down_for = max(MIN_TRAVEL_SECONDS, min(MAX_TRAVEL_SECONDS, float(down_seconds))) + 2.0
            link: object | None = None
            end = time.monotonic() + down_for
            while time.monotonic() < end:
                if not await self._send_command(down_cmd, connect=link is None, link=link):
                    _LOGGER.warning("Calibration for %s aborted: bed not reachable", part)
                    return
                if link is None:
                    link = self._client
                    end = time.monotonic() + down_for
                await asyncio.sleep(MOVEMENT_COMMAND_INTERVAL)
            await self.send_stop()
            self._set_part_position(part, 0)

            # Phase 2: move up and measure until complete_calibration is called
            self._calibration_phase = "tracking"
            self._calibration_start_time = time.monotonic()
            self._notify_calibration_state()
            end = time.monotonic() + MAX_CALIBRATION_TRACKING_SECONDS
            while time.monotonic() < end:
                if not await self._send_command(up_cmd, connect=False, link=link):
                    _LOGGER.warning("Calibration for %s aborted: bed not reachable", part)
                    return
                await asyncio.sleep(MOVEMENT_COMMAND_INTERVAL)
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
        """Move a part (head or feet) down for the given time, back to 0%."""
        if part not in ("head", "feet") or seconds <= 0:
            return
        self._calibration_completing = True
        self._calibration_returning_part = part
        self._notify_calibration_state()
        self._set_part_position(part, 100)  # we're at 100% after calibration
        current = asyncio.current_task()
        if current is not None:
            self.register_movement_task(current)
            self.register_active_movement(part, current)
        move = _PartMove(part, 100, 0, float(seconds), float(seconds))
        try:
            await self._drive([move], up=False)
        finally:
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

    # --------------------------------------------------------------------- light

    async def light_on(self) -> bool:
        """Turn bed light on."""
        # Light commands appear to require a fresh auth on some beds.
        if not await self.ensure_connected():
            return False
        await self.send_pin()
        await asyncio.sleep(0.2)
        ok = await self._send_command(CMD_LIGHT_ON)
        if ok:
            # Retry once for reliability (matches "write command" behaviour in captures).
            await asyncio.sleep(0.1)
            await self._write(CMD_LIGHT_ON, quiet=True)
        return ok

    async def light_off(self) -> bool:
        """Turn bed light off."""
        if not await self.ensure_connected():
            return False
        await self.send_pin()
        await asyncio.sleep(0.2)
        ok = await self._send_command(CMD_LIGHT_OFF)
        if ok:
            await asyncio.sleep(0.1)
            await self._write(CMD_LIGHT_OFF, quiet=True)
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
