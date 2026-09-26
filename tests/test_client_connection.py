"""Connection manager behaviour of OctoBedClient against simulated beds."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import pytest

from custom_components.octo_bed import octo_bed_client
from custom_components.octo_bed.octo_bed_client import OctoBedClient

from .fake_bed import FakeBed, FakeBleakBackend, memory_slots_feature

PIN = "1234"


class Presence:
    """Mutable presence flag handed to the client as presence checker."""

    def __init__(self, present: bool = True) -> None:
        self.present = present

    def __call__(self) -> bool:
        return self.present


def make_client(
    bed: FakeBed,
    presence: Presence | None = None,
    gate: asyncio.Lock | None = None,
    features: list[dict] | None = None,
) -> OctoBedClient:
    return OctoBedClient(
        bed.device,  # type: ignore[arg-type]
        PIN,
        address=bed.address,
        device_resolver=lambda: bed.device,  # type: ignore[return-value]
        presence_checker=presence or Presence(True),
        connect_gate=gate,
        features_callback=features.append if features is not None else None,
    )


async def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def test_connects_and_discovers_features(bed: FakeBed) -> None:
    features: list[dict] = []
    client = make_client(bed, features=features)
    client.start()
    await wait_for(client.is_connected)
    await wait_for(lambda: bool(features))

    assert features[0]["has_light"] is True
    assert features[0]["memory_slots"] is None
    assert client.memory_slot_count == 0
    assert client.connection_info["via"] == "24:6F:28:5F:03:7E"
    assert client.connection_info["connections"] == 1
    await client.async_close()


async def test_feature_discovery_reports_memory_slots(bed: FakeBed) -> None:
    bed.capabilities.insert(0, memory_slots_feature(3))
    features: list[dict] = []
    client = make_client(bed, features=features)
    client.start()
    await wait_for(lambda: bool(features))
    assert features[0]["memory_slots"] == 3
    assert client.memory_slot_count == 3
    await client.async_close()


async def test_capabilities_can_be_preloaded(bed: FakeBed) -> None:
    client = make_client(bed)
    client.load_capabilities({"memory_slots": 4, "has_rgbwi_light": True})
    assert client.memory_slot_count == 4
    assert client.has_rgbwi_light is True
    await client.async_close()


async def test_waits_for_advertisement_while_bed_is_absent(bed: FakeBed) -> None:
    presence = Presence(False)
    client = make_client(bed, presence)
    client.start()
    await asyncio.sleep(0.1)
    assert bed.connect_calls == 0
    assert client.connection_info["state"] == "waiting_for_bed"

    presence.present = True
    client.async_on_advertisement()
    await wait_for(client.is_connected, timeout=1.0)
    assert bed.connect_calls == 1
    await client.async_close()


async def test_gatt_error_on_notify_is_retried_without_deadlock(bed: FakeBed) -> None:
    """Regression: a failed notify subscribe used to deadlock the connect lock."""
    bed.notify_failures = 1
    client = make_client(bed)
    client.start()
    await wait_for(client.is_connected)
    assert bed.connect_calls == 2
    assert client.connection_info["last_error"] is None
    # The failed link was closed again, only the second one is up.
    assert [c.is_connected for c in bed.clients] == [False, True]
    await client.async_close()


async def test_reconnects_after_the_bed_drops_the_link(bed: FakeBed) -> None:
    client = make_client(bed)
    states: list[bool] = []
    client.register_connection_callback(states.append)
    client.start()
    await wait_for(client.is_connected)

    bed.drop()
    await wait_for(lambda: states == [True, False, True])
    assert client.is_connected()
    assert client.connection_info["connections"] == 2
    await client.async_close()


async def test_late_disconnect_callback_of_old_link_is_ignored(bed: FakeBed) -> None:
    client = make_client(bed)
    client.start()
    await wait_for(client.is_connected)
    first = bed.clients[0]

    bed.drop()
    await wait_for(lambda: len(bed.clients) == 2 and client.is_connected())
    # The old link's callback arrives (again) after the new link is up.
    client._on_disconnect(first)
    await asyncio.sleep(0.05)
    assert client.is_connected()
    assert client.connection_info["connections"] == 2
    await client.async_close()


async def test_silent_link_is_detected_by_keepalive_watchdog(
    bed: FakeBed, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(bed)
    client.start()
    await wait_for(client.is_connected)
    await wait_for(lambda: client._ack_seen)

    bed.ack_pin = False  # the link stays "up" but the bed stops answering
    await wait_for(lambda: client.connection_info["connections"] == 2, timeout=3.0)
    assert "did not answer" in caplog.text
    # The new link never had an acknowledged keep-alive, so the watchdog must
    # not keep tearing it down (no reconnect storm on beds that never ack).
    await asyncio.sleep(0.5)
    assert client.connection_info["connections"] == 2
    assert client.is_connected()
    await client.async_close()


async def test_link_that_reports_down_without_callback_is_noticed(bed: FakeBed) -> None:
    client = make_client(bed)
    client.start()
    await wait_for(client.is_connected)

    bed.clients[0].is_connected = False  # no disconnected_callback
    assert await client.send_stop() is False
    await wait_for(lambda: client.connection_info["connections"] == 2)
    assert client.is_connected()
    await client.async_close()


async def test_close_is_terminal(bed: FakeBed) -> None:
    client = make_client(bed)
    client.start()
    await wait_for(client.is_connected)
    await client.async_close()

    assert not client.is_connected()
    calls = bed.connect_calls
    await asyncio.sleep(0.1)
    assert bed.connect_calls == calls
    assert await client.ensure_connected() is False
    assert await client.head_up() is False
    client.start()
    await asyncio.sleep(0.05)
    assert bed.connect_calls == calls


async def test_close_during_a_connection_attempt(bed: FakeBed) -> None:
    bed.connect_hang = asyncio.Event()
    client = make_client(bed)
    client.start()
    await wait_for(lambda: bed.connect_calls == 1)

    started = time.monotonic()
    await client.async_close()
    assert time.monotonic() - started < 1.0
    bed.connect_hang.set()
    await asyncio.sleep(0.05)
    assert bed.current is None
    assert not client.is_connected()


async def test_ensure_connected_fails_fast_when_bed_is_absent(bed: FakeBed) -> None:
    client = make_client(bed, Presence(False))
    started = time.monotonic()
    assert await client.ensure_connected() is False
    assert time.monotonic() - started < 0.1
    assert bed.connect_calls == 0
    await client.async_close()


async def test_command_wakes_manager_during_backoff(
    bed: FakeBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(octo_bed_client, "RECONNECT_DELAYS", (30.0,))
    bed.connect_failures = 1
    client = make_client(bed)
    client.start()
    await wait_for(lambda: client.connection_info["state"] == "retry_wait")

    started = time.monotonic()
    assert await client.head_up() is True
    assert time.monotonic() - started < 1.0
    await client.async_close()


async def test_beds_connect_one_at_a_time(
    backend: FakeBleakBackend, bed: FakeBed, second_bed: FakeBed
) -> None:
    """Simultaneous GATT setup on one proxy causes GATT error 133."""
    bed.connect_delay = second_bed.connect_delay = 0.05
    gate = asyncio.Lock()
    first = make_client(bed, gate=gate)
    second = make_client(second_bed, gate=gate)
    first.start()
    second.start()
    await wait_for(lambda: first.is_connected() and second.is_connected())
    assert backend.max_concurrent_connects == 1
    await first.async_close()
    await second.async_close()


async def test_unreachable_bed_does_not_log_errors(
    bed: FakeBed, caplog: pytest.LogCaptureFixture
) -> None:
    bed.connect_failures = 3
    client = make_client(bed)
    with caplog.at_level(logging.DEBUG):
        client.start()
        await wait_for(client.is_connected, timeout=3.0)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    await client.async_close()


async def test_callbacks_can_be_unregistered(bed: FakeBed) -> None:
    client = make_client(bed)
    seen: list[bool] = []
    unregister = client.register_connection_callback(seen.append)
    unregister()
    unregister()  # removing twice is harmless
    client.start()
    await wait_for(client.is_connected)
    assert seen == []
    await client.async_close()


async def test_config_flow_pin_verification(bed: FakeBed, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OctoBedClient(bed.device, PIN)  # type: ignore[arg-type]
    assert await client.connect_and_verify_pin() is True
    assert bed.current is None  # the verification link is closed again

    monkeypatch.setattr(octo_bed_client, "PIN_VERIFY_TIMEOUT", 0.1)
    bed.ack_pin = False
    assert await client.connect_and_verify_pin() is False
    assert bed.current is None
    await client.async_close()
