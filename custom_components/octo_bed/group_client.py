"""Wrapper that delegates to multiple Octo Bed clients for a paired 'both beds' device."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

from .octo_bed_client import OctoBedClient

_LOGGER = logging.getLogger(__name__)


def _all_true(results: list[Any]) -> bool:
    """Return True only when every gathered result is exactly True.

    Results come from asyncio.gather(..., return_exceptions=True), so a member
    that raised shows up as an Exception instance; it is logged and counts as a
    failure, but never propagates and never aborts the other members.
    """
    ok = True
    for result in results:
        if isinstance(result, BaseException):
            _LOGGER.debug("Group member command failed: %s", result)
            ok = False
        elif result is not True:
            ok = False
    return ok


def _log_member_errors(results: list[Any], action: str) -> None:
    """Log any exceptions returned by a gather() over member clients."""
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError
        ):
            _LOGGER.warning("Group %s failed on a member bed: %s", action, result)


def _combine(unsubs: list[Callable[[], None]]) -> Callable[[], None]:
    def _remove_all() -> None:
        for unsub in unsubs:
            unsub()

    return _remove_all


class GroupOctoBedClient:
    """Makes several OctoBedClient instances behave as one.

    Positions are averaged, commands go to every bed. The member beds own
    their connections; the group never opens or closes one itself.
    """

    def __init__(self, clients: list[OctoBedClient]) -> None:
        self._clients = list(clients)

    def has_member(self, client: OctoBedClient) -> bool:
        """True if this exact client object is one of the members."""
        return any(c is client for c in self._clients)

    async def _gather(self, coros: Iterable[Any]) -> list[Any]:
        return await asyncio.gather(*coros, return_exceptions=True)

    # -------------------------------------------------------------- connection

    def start(self) -> None:
        """Members manage their own connections."""

    async def connect(self) -> bool:
        return _all_true(await self._gather(c.connect() for c in self._clients))

    async def async_close(self) -> None:
        """Member clients belong to their own config entries: nothing to close."""

    async def disconnect(self) -> None:
        """Member clients belong to their own config entries: nothing to close."""

    async def ensure_connected(self) -> bool:
        return _all_true(await self._gather(c.ensure_connected() for c in self._clients))

    def is_connected(self) -> bool:
        return bool(self._clients) and all(c.is_connected() for c in self._clients)

    def get_device_address(self) -> str:
        return ",".join(c.get_device_address() for c in self._clients)

    @property
    def connection_info(self) -> dict[str, Any]:
        return {
            "members": {c.get_device_address(): c.connection_info for c in self._clients}
        }

    # ------------------------------------------------------------------ features

    @property
    def memory_slot_count(self) -> int:
        """Memory presets only when every member bed supports them."""
        if not self._clients:
            return 0
        return min(c.memory_slot_count for c in self._clients)

    @property
    def has_synchro(self) -> bool:
        """Synchro mode is configured per bed, never on the group."""
        return False

    @property
    def has_rgbwi_light(self) -> bool:
        return bool(self._clients) and all(c.has_rgbwi_light for c in self._clients)

    def get_feature_summary(self) -> dict[str, Any]:
        return {"members": [c.get_feature_summary() for c in self._clients]}

    async def recall_memory_preset(self, slot: int) -> bool:
        return _all_true(
            await self._gather(c.recall_memory_preset(slot) for c in self._clients)
        )

    async def save_memory_preset(self, slot: int) -> bool:
        return _all_true(
            await self._gather(c.save_memory_preset(slot) for c in self._clients)
        )

    # ------------------------------------------------------------------ position

    def _average(self, values: list[int]) -> int:
        return round(sum(values) / len(values)) if values else 0

    def get_head_position(self) -> int:
        return self._average([c.get_head_position() for c in self._clients])

    def get_feet_position(self) -> int:
        return self._average([c.get_feet_position() for c in self._clients])

    def get_both_position(self) -> int:
        return self._average([c.get_both_position() for c in self._clients])

    def get_min_head_position(self) -> int:
        return min((c.get_head_position() for c in self._clients), default=0)

    def get_max_head_position(self) -> int:
        return max((c.get_head_position() for c in self._clients), default=0)

    def get_min_feet_position(self) -> int:
        return min((c.get_feet_position() for c in self._clients), default=0)

    def get_max_feet_position(self) -> int:
        return max((c.get_feet_position() for c in self._clients), default=0)

    def set_head_position(self, position: int) -> None:
        for c in self._clients:
            c.set_head_position(position)

    def set_feet_position(self, position: int) -> None:
        for c in self._clients:
            c.set_feet_position(position)

    def set_both_position(self, position: int) -> None:
        for c in self._clients:
            c.set_both_position(position)

    def register_position_callback(
        self, callback: Callable[[str, int], None]
    ) -> Callable[[], None]:
        return _combine([c.register_position_callback(callback) for c in self._clients])

    def register_calibration_state_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        return _combine(
            [c.register_calibration_state_callback(callback) for c in self._clients]
        )

    def register_connection_callback(
        self, callback: Callable[[bool], None]
    ) -> Callable[[], None]:
        return _combine([c.register_connection_callback(callback) for c in self._clients])

    # ------------------------------------------------------------- calibration

    def is_calibration_active(self) -> bool:
        return any(c.is_calibration_active() for c in self._clients)

    def is_calibrating(self) -> bool:
        return any(c.is_calibrating() for c in self._clients)

    def get_calibration_status(self) -> tuple[str, str | None]:
        for c in self._clients:
            state, part = c.get_calibration_status()
            if state != "idle":
                return (state, part)
        return ("idle", None)

    def get_calibration_elapsed_seconds(self) -> float:
        return max(
            (c.get_calibration_elapsed_seconds() for c in self._clients), default=0.0
        )

    async def start_calibration(self, part: str, down_seconds: float = 30.0) -> None:
        """Start calibration for this part on all beds."""
        _log_member_errors(
            await self._gather(
                c.start_calibration(part, down_seconds) for c in self._clients
            ),
            "start_calibration",
        )

    async def cancel_calibration(self) -> bool:
        """Abort calibration on all beds without saving."""
        results = await self._gather(c.cancel_calibration() for c in self._clients)
        return any(r is True for r in results)

    async def complete_calibration(self) -> tuple[str | None, float]:
        """Complete calibration on all beds; use max duration for return movement."""
        results = await self._gather(c.complete_calibration() for c in self._clients)
        part = None
        duration = 0.0
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.warning(
                    "Group complete_calibration failed on a member bed: %s", result
                )
                continue
            p, d = result
            if p is not None and d > 0:
                part = p
                duration = max(duration, d)
        return (part, duration)

    async def move_part_down_for_seconds(self, part: str, seconds: float) -> None:
        """Move this part down on all beds for the given duration."""
        _log_member_errors(
            await self._gather(
                c.move_part_down_for_seconds(part, seconds) for c in self._clients
            ),
            "move_part_down_for_seconds",
        )

    # ------------------------------------------------------------------ movement

    async def head_up(self) -> bool:
        return _all_true(await self._gather(c.head_up() for c in self._clients))

    async def head_down(self) -> bool:
        return _all_true(await self._gather(c.head_down() for c in self._clients))

    async def feet_up(self) -> bool:
        return _all_true(await self._gather(c.feet_up() for c in self._clients))

    async def feet_down(self) -> bool:
        return _all_true(await self._gather(c.feet_down() for c in self._clients))

    async def both_up(self) -> bool:
        return _all_true(await self._gather(c.both_up() for c in self._clients))

    async def both_down(self) -> bool:
        return _all_true(await self._gather(c.both_down() for c in self._clients))

    async def stop(self) -> bool:
        # Stop must reach every bed even if one errors, so failures are isolated.
        return _all_true(await self._gather(c.stop() for c in self._clients))

    async def send_stop(self) -> bool:
        return _all_true(await self._gather(c.send_stop() for c in self._clients))

    def register_movement_task(self, task: asyncio.Task[Any]) -> None:
        for c in self._clients:
            c.register_movement_task(task)

    def register_active_movement(self, part: str, task: asyncio.Task[Any]) -> None:
        for c in self._clients:
            c.register_active_movement(part, task)

    async def run_to_position(
        self,
        head_target: int | None,
        feet_target: int | None,
        head_travel_seconds: float,
        feet_travel_seconds: float,
    ) -> bool:
        """Move every bed to the same head/feet targets.

        Each bed moves from its own current position, so both beds end up at
        the target even when they started at different heights.
        """
        results = await self._gather(
            c.run_to_position(
                head_target, feet_target, head_travel_seconds, feet_travel_seconds
            )
            for c in self._clients
        )
        _log_member_errors(results, "run_to_position")
        return _all_true(results)

    async def run_hold(
        self,
        up: bool,
        parts: Iterable[str],
        head_travel_seconds: float,
        feet_travel_seconds: float,
    ) -> bool:
        """Hold-to-run on every bed; each bed tracks its own position."""
        parts = tuple(parts)
        results = await self._gather(
            c.run_hold(up, parts, head_travel_seconds, feet_travel_seconds)
            for c in self._clients
        )
        _log_member_errors(results, "run_hold")
        return _all_true(results)

    # --------------------------------------------------------------------- light

    async def light_on(self) -> bool:
        return _all_true(await self._gather(c.light_on() for c in self._clients))

    async def light_off(self) -> bool:
        return _all_true(await self._gather(c.light_off() for c in self._clients))

    async def set_light_color_rgbw(self, rgbw: tuple[int, int, int, int]) -> bool:
        return _all_true(
            await self._gather(c.set_light_color_rgbw(rgbw) for c in self._clients)
        )
