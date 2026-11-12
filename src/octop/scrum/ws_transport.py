"""The real Octop transport: dashboard WebSocket, one connection per wake.

Three behaviours are copied from Octop's own source rather than assumed, because
each of them has a silent failure mode that costs a whole night of debugging:

1. ``delivery="follow_up"`` while a turn is active is *rejected* — the server
   sends ``steer.ack {status: "rejected", reason: "turn_active"}`` and does not
   start the turn. That is what we want (retry later), but it means a rejected
   wake must never be counted as delivered.
2. ``delivery="auto"`` while busy *injects* the text into the running turn.
   Using it would merge two tasks into one execution, so it is never used here.
3. A disconnect does not cancel the turn ("Disconnect must not cancel an
   in-flight turn — clients may reconnect and subscribe"), so the dispatcher
   closes the socket right after the ack and observes progress elsewhere.

``X-Octop-Access-Token`` on any response is the sliding renewal token; capturing
it is what keeps a long-running dispatcher alive across the 24h TTL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from .observe import OctopObserver
from .transport import Probe, WakeResult

logger = logging.getLogger("octop.scrum.ws")

_DECISION_FRAMES = frozenset({"steer.ack", "turn.busy", "error", "done"})


class OctopAuthError(RuntimeError):
    """Login failed or the token was rejected."""


class OctopWS:
    """Push a pointer into an agent's dashboard session and observe progress."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8088",
        username: str = "admin",
        password: str = "",
        ack_timeout_s: float = 10.0,
        observer: OctopObserver | None = None,
        conversation_mode: str = "craft",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.ack_timeout_s = ack_timeout_s
        self.conversation_mode = conversation_mode
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self.observer = observer or OctopObserver(_default_db_path())

    # ── auth ────────────────────────────────────────────────────────────────
    def _ws_base(self) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, "", "", "", ""))

    async def token(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._token_expires_at:
                return self._token
            import httpx

            async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": self.username, "password": self.password},
                )
            if resp.status_code != 200:
                raise OctopAuthError(f"login failed: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            if "access_token" not in data:
                # MFA-enabled accounts return a challenge instead of a token.
                raise OctopAuthError(f"login requires interactive step: {sorted(data)}")
            self._token = str(data["access_token"])
            ttl = float(data.get("expires_in") or 3600)
            # Refresh at 90% of the TTL: the expiry is absolute and there is no
            # refresh endpoint, so a late renewal means a hard 401 storm.
            self._token_expires_at = time.time() + ttl * 0.9
            return self._token

    def note_renewed(self, header_value: str | None) -> None:
        """Adopt a sliding-renewal token handed back by any response."""
        if not header_value:
            return
        self._token = header_value
        self._token_expires_at = time.time() + 3600 * 0.9

    # ── wake ────────────────────────────────────────────────────────────────
    async def send_pointer(
        self, agent_id: str, text: str, thread_id: str | None
    ) -> WakeResult:
        try:
            token = await self.token()
        except Exception as exc:  # noqa: BLE001 - transport must never raise upward
            logger.warning("scrum: cannot authenticate to octop: %s", exc)
            return WakeResult(status="error", reason=f"auth:{exc}")

        frame: dict[str, Any] = {
            "type": "user_turn",
            "text": text,
            "delivery": "follow_up",  # idle-only: rejected (not injected) when busy
            "conversation_mode": self.conversation_mode,
        }
        if thread_id:
            frame["thread_id"] = thread_id
        url = f"{self._ws_base()}/api/agents/{agent_id}/chat/ws?token={token}"

        try:
            return await asyncio.wait_for(
                self._send_and_ack(url, frame), timeout=self.ack_timeout_s
            )
        except TimeoutError:
            return WakeResult(status="timeout", reason="ack_timeout")
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrum: wake failed agent=%s: %s", agent_id, exc)
            return WakeResult(status="error", reason=str(exc)[:200])

    async def _send_and_ack(self, url: str, frame: dict[str, Any]) -> WakeResult:
        from websockets.asyncio.client import connect

        async with connect(url, open_timeout=self.ack_timeout_s, max_size=2**22) as ws:
            await ws.send(json.dumps(frame, ensure_ascii=False))
            resolved_thread: str | None = frame.get("thread_id")
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = str(payload.get("type") or "")
                if payload.get("thread_id"):
                    resolved_thread = str(payload["thread_id"])
                if kind == "pong":
                    continue
                if kind == "steer.ack":
                    status = str(payload.get("status") or "")
                    if status == "rejected":
                        return WakeResult(
                            status="rejected",
                            reason=str(payload.get("reason") or "rejected"),
                            thread_id=resolved_thread,
                            raw=payload,
                        )
                    return WakeResult(
                        status="accepted", reason=status, thread_id=resolved_thread, raw=payload
                    )
                if kind == "turn.busy":
                    return WakeResult(
                        status="busy",
                        reason=str(payload.get("reason") or "busy"),
                        thread_id=resolved_thread,
                        raw=payload,
                    )
                if kind == "error":
                    return WakeResult(
                        status="error",
                        reason=str(payload.get("message") or "error"),
                        thread_id=resolved_thread,
                        raw=payload,
                    )
                if kind == "done":
                    # The turn ran and finished before we saw an ack frame.
                    return WakeResult(
                        status="accepted", reason="done", thread_id=resolved_thread, raw=payload
                    )
                if kind not in _DECISION_FRAMES:
                    # Any content frame proves the turn started; closing the
                    # socket does not cancel it.
                    return WakeResult(
                        status="accepted", reason=kind, thread_id=resolved_thread, raw=payload
                    )
        return WakeResult(status="error", reason="socket_closed")

    # ── observe / cancel ────────────────────────────────────────────────────
    async def probe(self, thread_id: str | None) -> Probe:
        return await asyncio.to_thread(
            self.observer.probe,
            thread_id,
            stall_window_s=600.0,
            now=time.time(),
        )

    async def cancel(self, thread_id: str) -> bool:
        try:
            token = await self.token()
        except Exception:  # noqa: BLE001
            return False
        agent_id = thread_id.split(":", 1)[0] if ":" in thread_id else None
        if agent_id is None:
            return False
        url = f"{self._ws_base()}/api/agents/{agent_id}/chat/ws?token={token}"
        try:
            from websockets.asyncio.client import connect

            async with connect(url, open_timeout=5.0) as ws:
                await ws.send(json.dumps({"type": "cancel", "thread_id": thread_id}))
                await asyncio.sleep(0.1)
            return True
        except Exception:  # noqa: BLE001
            return False


def _default_db_path() -> str:
    """Mirror Octop's own resolution: ``~/.octop/octop.db``."""
    from pathlib import Path

    return str(Path.home() / ".octop" / "octop.db")
