"""Shared fixtures for the Octo Bed tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

from custom_components.octo_bed import octo_bed_client

from .fake_bed import FakeBed, FakeBleakBackend


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow Home Assistant to load the integration under test."""


@pytest.fixture
def fast_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink every delay so connection behaviour can be tested in real time."""
    monkeypatch.setattr(octo_bed_client, "RECONNECT_DELAYS", (0.02, 0.05, 0.1))
    monkeypatch.setattr(octo_bed_client, "ABSENT_RETRY_SECONDS", 5.0)
    monkeypatch.setattr(octo_bed_client, "CONNECT_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(octo_bed_client, "ON_DEMAND_CONNECT_TIMEOUT", 2.0)
    monkeypatch.setattr(octo_bed_client, "PIN_KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(octo_bed_client, "LIVENESS_TIMEOUT", 0.2)
    monkeypatch.setattr(octo_bed_client, "FEATURE_DISCOVERY_TIMEOUT", 1.0)
    monkeypatch.setattr(octo_bed_client, "MOVEMENT_COMMAND_INTERVAL", 0.01)


@pytest.fixture
def backend(fast_timings: None) -> Generator[FakeBleakBackend]:
    """Route all BLE connections to simulated beds."""
    fake = FakeBleakBackend()
    with patch.object(octo_bed_client, "establish_connection", fake.establish_connection):
        yield fake


@pytest.fixture
def bed(backend: FakeBleakBackend) -> FakeBed:
    """One simulated bed ("Bram")."""
    return backend.add(FakeBed("F6:21:DD:DD:6F:19"))


@pytest.fixture
def second_bed(backend: FakeBleakBackend) -> FakeBed:
    """A second simulated bed ("Ianthe")."""
    return backend.add(FakeBed("C3:E7:63:36:0C:0C"))
