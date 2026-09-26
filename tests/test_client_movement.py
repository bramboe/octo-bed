"""Movement and position tracking of OctoBedClient and the group client."""

from __future__ import annotations

import asyncio

from custom_components.octo_bed.const import (
    CMD_BOTH_UP,
    CMD_FEET_DOWN,
    CMD_FEET_UP,
    CMD_HEAD_UP,
    CMD_STOP,
)
from custom_components.octo_bed.group_client import GroupOctoBedClient
from custom_components.octo_bed.octo_bed_client import OctoBedClient

from .fake_bed import FakeBed
from .test_client_connection import make_client, wait_for


async def connected_client(bed: FakeBed) -> OctoBedClient:
    client = make_client(bed)
    client.start()
    await wait_for(client.is_connected)
    await wait_for(lambda: client._features_discovered)
    bed.writes.clear()
    return client


def runs(writes: list[bytes]) -> list[bytes]:
    """Collapse consecutive identical commands into one entry."""
    result: list[bytes] = []
    for write in writes:
        if not result or result[-1] != write:
            result.append(write)
    return result


async def test_parts_moving_the_same_way_move_together(bed: FakeBed) -> None:
    client = await connected_client(bed)
    ok = await client.run_to_position(100, 50, 0.3, 0.3)

    assert ok is True
    assert (client.get_head_position(), client.get_feet_position()) == (100, 50)
    # Both motors run together until feet reach 50%, then only the head.
    assert runs(bed.motion_writes()) == [CMD_BOTH_UP, CMD_HEAD_UP]
    assert bed.writes[-1] == CMD_STOP
    await client.async_close()


async def test_parts_moving_opposite_ways_move_one_after_the_other(bed: FakeBed) -> None:
    client = await connected_client(bed)
    client.set_feet_position(80)
    ok = await client.run_to_position(60, 20, 0.2, 0.2)

    assert ok is True
    assert (client.get_head_position(), client.get_feet_position()) == (60, 20)
    assert runs(bed.motion_writes()) == [CMD_HEAD_UP, CMD_FEET_DOWN]
    await client.async_close()


async def test_none_target_keeps_that_part(bed: FakeBed) -> None:
    client = await connected_client(bed)
    client.set_head_position(30)
    assert await client.run_to_position(None, 40, 0.2, 0.2) is True
    assert client.get_head_position() == 30
    assert client.get_feet_position() == 40
    assert runs(bed.motion_writes()) == [CMD_FEET_UP]
    await client.async_close()


async def test_movement_ends_early_when_the_bed_drops(bed: FakeBed) -> None:
    client = await connected_client(bed)
    task = asyncio.create_task(client.run_to_position(100, None, 1.0, 1.0))
    await asyncio.sleep(0.2)
    bed.drop()

    assert await asyncio.wait_for(task, 1.0) is False
    # Position reflects how far the bed got, not the target.
    assert 5 <= client.get_head_position() <= 60
    await wait_for(client.is_connected)  # and the manager reconnects
    await client.async_close()


async def test_stop_cancels_a_running_movement(bed: FakeBed) -> None:
    client = await connected_client(bed)
    task = asyncio.create_task(client.run_to_position(100, None, 1.0, 1.0))
    client.register_movement_task(task)
    client.register_active_movement("head", task)
    await asyncio.sleep(0.2)

    assert await client.stop() is True
    assert task.cancelled()
    assert bed.writes[-1] == CMD_STOP
    assert 5 <= client.get_head_position() <= 60
    await client.async_close()


async def test_hold_tracks_each_part_at_its_own_speed(bed: FakeBed) -> None:
    client = await connected_client(bed)
    client.set_head_position(50)
    positions: list[tuple[str, int]] = []
    client.register_position_callback(lambda part, pos: positions.append((part, pos)))

    assert await client.run_hold(True, ("head", "feet"), 0.2, 0.4) is True
    assert (client.get_head_position(), client.get_feet_position()) == (100, 100)
    # Head reaches its end stop long before the feet: feet are still below
    # 100% when the head reports 100%.
    head_done = positions.index(("head", 100))
    feet_before = [pos for part, pos in positions[:head_done] if part == "feet"]
    assert feet_before and max(feet_before) < 100
    # Motors are driven for their full travel time; the feet alone at the end.
    assert runs(bed.motion_writes()) == [CMD_BOTH_UP, CMD_FEET_UP]
    await client.async_close()


async def test_group_moves_each_bed_from_its_own_position(
    bed: FakeBed, second_bed: FakeBed
) -> None:
    first = await connected_client(bed)
    second = await connected_client(second_bed)
    first.set_head_position(20)
    second.set_head_position(60)
    group = GroupOctoBedClient([first, second])

    assert await group.run_to_position(40, None, 0.2, 0.2) is True
    assert first.get_head_position() == 40
    assert second.get_head_position() == 40
    assert group.get_head_position() == 40
    await first.async_close()
    await second.async_close()


async def test_group_command_reaches_every_bed_when_one_fails(
    bed: FakeBed, second_bed: FakeBed
) -> None:
    first = await connected_client(bed)
    second = await connected_client(second_bed)
    group = GroupOctoBedClient([first, second])
    await first.async_close()  # one bed is gone

    assert await group.send_stop() is False
    assert second_bed.writes[-1] == CMD_STOP
    await second.async_close()


async def test_group_callbacks_unregister_on_every_member(
    bed: FakeBed, second_bed: FakeBed
) -> None:
    first = make_client(bed)
    second = make_client(second_bed)
    group = GroupOctoBedClient([first, second])
    seen: list[tuple[str, int]] = []
    unregister = group.register_position_callback(lambda p, v: seen.append((p, v)))
    first.set_head_position(10)
    unregister()
    second.set_head_position(10)
    assert seen == [("head", 10)]
    await first.async_close()
    await second.async_close()


async def test_movement_does_not_continue_on_a_new_link(bed: FakeBed) -> None:
    """Link drops and comes back within one tick: the motor stopped meanwhile."""
    client = await connected_client(bed)
    task = asyncio.create_task(client.run_to_position(100, None, 2.0, 2.0))
    await asyncio.sleep(0.1)
    bed.drop()
    await wait_for(lambda: len(bed.clients) == 2 and client.is_connected())

    assert await asyncio.wait_for(task, 1.0) is False
    # No motion command went out on the new link.
    assert CMD_HEAD_UP in bed.clients[0].writes
    assert CMD_HEAD_UP not in bed.clients[1].writes
    assert client.get_head_position() < 50
    await client.async_close()
