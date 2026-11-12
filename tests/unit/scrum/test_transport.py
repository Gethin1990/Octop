"""Transport details that must not need a live Octop to verify."""

from __future__ import annotations

import asyncio

from octop.scrum.transport import FakeTransport, pointer_text
from octop.scrum.ws_transport import OctopWS


def test_ws_scheme_follows_http_scheme():
    assert OctopWS(base_url="http://127.0.0.1:8088")._ws_base() == "ws://127.0.0.1:8088"  # noqa: SLF001
    assert OctopWS(base_url="https://box.local:8443")._ws_base() == "wss://box.local:8443"  # noqa: SLF001


def test_pointer_carries_ids_and_the_stop_word():
    """A duplicate wake must be able to exit without re-implementing the task."""
    text = pointer_text("T-abc", "lease-1", 2, 3)
    assert "T-abc" in text
    assert "lease-1" in text
    assert "NOT_YOURS" in text
    assert "NOOP" in text
    assert len(text.splitlines()) <= 4  # a pointer, not a brief


def test_unreachable_octop_returns_error_instead_of_raising():
    """A dead server must degrade to a retryable result, never an exception."""
    transport = OctopWS(
        base_url="http://127.0.0.1:9",  # discard port: connection refused
        username="nobody",
        password="nobody",
        ack_timeout_s=2.0,
    )
    result = asyncio.run(transport.send_pointer("dev-1", "TASK T-1", None))
    assert result.status == "error"
    assert not result.accepted


def test_fake_transport_repeats_its_last_scripted_answer():
    fake = FakeTransport(results={"dev-1": ["rejected", "accepted"]})
    first = asyncio.run(fake.send_pointer("dev-1", "x", None))
    second = asyncio.run(fake.send_pointer("dev-1", "x", None))
    third = asyncio.run(fake.send_pointer("dev-1", "x", None))
    assert (first.status, second.status, third.status) == ("rejected", "accepted", "accepted")
    assert len(fake.wakes) == 3
