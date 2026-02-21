"""Wrapper that delegates to multiple Octo Bed clients for a paired 'both beds' device."""

from __future__ import annotations

import asyncio
from typing import Callable

from .octo_bed_client import OctoBedClient


class GroupOctoBedClient:
    """Makes multiple OctoBedClient instances behave as one (average position, commands to all)."""

    def __init__(self, clients: list[OctoBedClient]) -> None:
        self._clients = list(clients)

    def _first(self) -> OctoBedClient:
        return self._clients[0]

    async def connect(self) -> bool:
        results = await asyncio.gather(*[c.connect() for c in self._clients])
        return all(results)

    async def disconnect(self) -> None:
        await asyncio.gather(*[c.disconnect() for c in self._clients])

    async def ensure_connected(self) -> bool:
        results = await asyncio.gather(*[c.ensure_connected() for c in self._clients])
        return all(results)

    def is_connected(self) -> bool:
        return all(c.is_connected() for c in self._clients)

    def get_device_address(self) -> str:
        return ",".join(c.get_device_address() for c in self._clients)

    def get_head_position(self) -> int:
        if not self._clients:
            return 0
        return int(round(sum(c.get_head_position() for c in self._clients) / len(self._clients)))

    def get_feet_position(self) -> int:
        if not self._clients:
            return 0
        return int(round(sum(c.get_feet_position() for c in self._clients) / len(self._clients)))

    def get_both_position(self) -> int:
        if not self._clients:
            return 0
        return int(round(sum(c.get_both_position() for c in self._clients) / len(self._clients)))

    def register_position_callback(self, callback: Callable[[str, int], None]) -> None:
        for c in self._clients:
            c.register_position_callback(callback)

    def register_calibration_state_callback(self, callback: Callable[[], None]) -> None:
        for c in self._clients:
            c.register_calibration_state_callback(callback)

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

    async def head_up(self) -> bool:
        results = await asyncio.gather(*[c.head_up() for c in self._clients])
        return all(results)

    async def head_down(self) -> bool:
        results = await asyncio.gather(*[c.head_down() for c in self._clients])
        return all(results)

    async def feet_up(self) -> bool:
        results = await asyncio.gather(*[c.feet_up() for c in self._clients])
        return all(results)

    async def feet_down(self) -> bool:
        results = await asyncio.gather(*[c.feet_down() for c in self._clients])
        return all(results)

    async def both_up(self) -> bool:
        results = await asyncio.gather(*[c.both_up() for c in self._clients])
        return all(results)

    async def both_up_continuous(self) -> bool:
        results = await asyncio.gather(*[c.both_up_continuous() for c in self._clients])
        return all(results)

    async def head_up_continuous(self) -> bool:
        results = await asyncio.gather(*[c.head_up_continuous() for c in self._clients])
        return all(results)

    async def both_down(self) -> bool:
        results = await asyncio.gather(*[c.both_down() for c in self._clients])
        return all(results)

    async def stop(self) -> bool:
        results = await asyncio.gather(*[c.stop() for c in self._clients])
        return all(results)

    def register_movement_task(self, task: asyncio.Task[None]) -> None:
        for c in self._clients:
            c.register_movement_task(task)

    def register_active_movement(self, part: str, task: asyncio.Task[None]) -> None:
        for c in self._clients:
            c.register_active_movement(part, task)

    async def light_on(self) -> bool:
        results = await asyncio.gather(*[c.light_on() for c in self._clients])
        return all(results)

    async def light_off(self) -> bool:
        results = await asyncio.gather(*[c.light_off() for c in self._clients])
        return all(results)

    # Calibration on group: run same calibration on all beds in parallel
    async def start_calibration(self, part: str) -> None:
        await asyncio.gather(*[c.start_calibration(part) for c in self._clients])

    async def complete_calibration(self) -> tuple[str | None, float]:
        results = await asyncio.gather(*[c.complete_calibration() for c in self._clients])
        part: str | None = None
        duration: float = 0.0
        for p, d in results:
            if p is not None and d > duration:
                part = p
                duration = d
        return (part, duration)

    async def move_part_down_for_seconds(self, part: str, seconds: float) -> None:
        await asyncio.gather(*[c.move_part_down_for_seconds(part, seconds) for c in self._clients])

    def set_head_position(self, position: int) -> None:
        for c in self._clients:
            c.set_head_position(position)

    def set_feet_position(self, position: int) -> None:
        for c in self._clients:
            c.set_feet_position(position)

    def set_both_position(self, position: int) -> None:
        for c in self._clients:
            c.set_both_position(position)

    async def _send_command(self, data: bytes) -> bool:
        results = await asyncio.gather(*[c._send_command(data) for c in self._clients])
        return all(results)
