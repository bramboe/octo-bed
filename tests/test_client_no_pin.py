"""Receivers that work without a PIN (e.g. some RC2 units)."""

from __future__ import annotations

import pytest

from custom_components.octo_bed import octo_bed_client, protocol
from custom_components.octo_bed.octo_bed_client import OctoBedClient

from .fake_bed import FakeBed
from .test_client_connection import Presence, wait_for


def make_client(bed: FakeBed, pin: str = "") -> OctoBedClient:
    return OctoBedClient(
        bed.device,  # type: ignore[arg-type]
        pin,
        address=bed.address,
        device_resolver=lambda: bed.device,  # type: ignore[return-value]
        presence_checker=Presence(True),
    )


@pytest.fixture(autouse=True)
def short_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(octo_bed_client, "NO_PIN_PROBE_SECONDS", 0.1)


async def test_verify_without_pin_succeeds_when_bed_does_not_ask(bed: FakeBed) -> None:
    bed.ack_pin = False
    assert await make_client(bed).connect_and_verify_pin()
    assert not any(protocol.is_pin_packet(w) for w in bed.writes)


async def test_verify_without_pin_fails_when_bed_asks_for_one(bed: FakeBed) -> None:
    bed.asks_for_pin = True
    assert not await make_client(bed).connect_and_verify_pin()


async def test_runs_without_pin_and_never_sends_one(bed: FakeBed) -> None:
    bed.ack_pin = False
    client = make_client(bed)
    client.start()
    try:
        await wait_for(client.is_connected)
        assert await client.head_up() is not False
        await wait_for(lambda: bool(bed.motion_writes()))
        assert not any(protocol.is_pin_packet(w) for w in bed.writes)
    finally:
        await client.async_close()
