"""Real-time session: the agent, the mock tools and a trace on the *running* event loop.

The benchmark harness replays scenarios on a virtual clock; a live session instead runs in
wall-clock time and takes events as they happen (typed text, streaming speech, uploaded
audio, camera frames). The web app runs this inside the browser (Pyodide), so the exact
agent that is benchmarked is the one the user talks to.

    session = LiveSession(manifest, on_action=print)
    await session.start()
    session.push({"type": "transcript", "text": "Take me to the airport", "end_of_turn": True})
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, Callable

from .agent import RealtimeAgent
from .harness.mock_env import MockEnv
from .protocol import adapt_event


class BridgePerception:
    """Adapts asynchronous perception callables (e.g. JavaScript in the browser) to the agent.

    Each callable takes a reference (file path or browser blob id) and returns a dict or a
    JSON string with the same fields as ``perception.Perception``.
    """

    def __init__(self, analyze_frame: Callable[[str], Any] | None = None,
                 transcribe: Callable[[str], Any] | None = None) -> None:
        self._frame, self._asr = analyze_frame, transcribe

    async def warmup(self) -> None:
        return None

    @staticmethod
    async def _call(fn: Callable[[str], Any] | None, ref: str, missing: str) -> dict:
        if fn is None:
            return {"text": "", "ambiguous": missing}
        res = fn(ref)
        if hasattr(res, "__await__"):
            res = await res
        if hasattr(res, "to_py"):
            res = res.to_py()
        return json.loads(res) if isinstance(res, str) else dict(res)

    async def analyze_frame(self, ref: str) -> dict:
        return await self._call(self._frame, ref, "vision_unavailable")

    async def transcribe(self, ref: str) -> dict:
        return await self._call(self._asr, ref, "asr_unavailable")


class LiveSession:
    def __init__(self, manifest: list[dict], on_action: Callable[[str], Any],
                 on_event: Callable[[str], Any] | None = None, perception: Any = None,
                 session_date: dt.date | None = None, env_config: dict | None = None) -> None:
        self.manifest = manifest
        self.on_action, self.on_event = on_action, on_event
        self.perception, self.session_date, self.env_config = perception, session_date, env_config or {}
        self.trace: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self.t0 = 0.0

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.t0 = self.loop.time()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.env = MockEnv(self.manifest, self.env_config, self._deliver)
        self.agent = RealtimeAgent(perception=self.perception, session_date=self.session_date or dt.date.today())
        await self.agent.warmup()
        self._tasks = [asyncio.ensure_future(self.agent.run(self.inbox, self.outbox)),
                       asyncio.ensure_future(self._pump())]
        self._deliver({"type": "tool_manifest", "tools": self.manifest})

    def now(self) -> float:
        return round(self.loop.time() - self.t0, 4)

    def push(self, ev: Any) -> dict:
        """Feed one user/environment event (dict or JSON string); returns it time-stamped."""
        if hasattr(ev, "to_py"):
            ev = ev.to_py()
        ev = adapt_event(json.loads(ev) if isinstance(ev, str) else dict(ev))
        self._deliver(ev)
        return ev

    def _deliver(self, ev: dict) -> None:
        ev["t"] = self.now()  # tool results arrive stamped with raw loop time; re-stamp relative to start
        self.trace.append({"dir": "in", **ev})
        self.inbox.put_nowait(ev)
        if self.on_event is not None:
            self.on_event(json.dumps(ev, default=str))

    async def _pump(self) -> None:
        while True:
            act = await self.outbox.get()
            act = {"t": self.now(), **act}
            self.trace.append({"dir": "out", **act})
            if act["type"] == "tool_call":
                self.env.start_call(act)
            elif act["type"] == "cancel":
                act["env"] = self.env.cancel(act["call_id"])  # "aborted" or "too_late"
            self.on_action(json.dumps(act, default=str))

    def snapshot(self) -> str:
        """Live view for UIs: the agent's state snapshot plus every call it has made (JSON)."""
        calls = [{"call_id": cid, "tool": r["tool"], "args": r["args"], "status": r["status"], "state_modifying": r["sm"],
                  "idempotency_key": r["key"], "attempt": r["attempt"], "speculative": r.get("speculative", False),
                  "t0": round(r["t0"] - self.t0, 4), "result": r.get("result")}
                 for cid, r in self.agent.calls.items()]
        return json.dumps({"snapshot": self.agent.snapshot(), "calls": calls, "commits": self.commits(),
                           "holding_floor": self.agent.hold_task is not None and not self.agent.hold_task.done()},
                          default=str)

    def commits(self) -> list[dict]:
        return [{**c, "t": round(c["t"] - self.t0, 4)} for c in self.env.commits]

    def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for h in list(self.env.pending.values()):
            h.cancel()
        self.env.pending.clear()
