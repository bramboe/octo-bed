"""Octo Remote Star2 receivers (e.g. DA1458X)."""

from __future__ import annotations

from custom_components.octo_bed.const import (
    CMD_HEAD_UP,
    STAR2_CHAR_UUID,
    STAR2_COMMANDS,
    VARIANT_STAR2,
)
from custom_components.octo_bed.octo_bed_client import OctoBedClient

from .fake_bed import FakeBed
from .test_client_connection import Presence, wait_for


def make_client(bed: FakeBed) -> OctoBedClient:
    return OctoBedClient(
        bed.device,  # type: ignore[arg-type]
        "1234",  # ignored for Star2
        address=bed.address,
        device_resolver=lambda: bed.device,  # type: ignore[return-value]
        presence_checker=Presence(True),
        variant=VARIANT_STAR2,
    )


async def test_verify_checks_the_star2_characteristic(bed: FakeBed) -> None:
    bed.characteristics = {STAR2_CHAR_UUID}
    assert await make_client(bed).connect_and_verify_pin()
    bed.characteristics = set()
    assert not await make_client(bed).connect_and_verify_pin()


async def test_moves_with_star2_frames_only(bed: FakeBed) -> None:
    bed.characteristics = {STAR2_CHAR_UUID}
    client = make_client(bed)
    client.start()
    try:
        await wait_for(client.is_connected)
        await client.head_up()
        await client.stop()
        assert bed.writes == [STAR2_COMMANDS[CMD_HEAD_UP]]
        assert set(bed.current.write_uuids) == {STAR2_CHAR_UUID}  # type: ignore[union-attr]
        assert not client.has_rgbwi_light and client.memory_slot_count == 0
    finally:
        await client.async_close()
