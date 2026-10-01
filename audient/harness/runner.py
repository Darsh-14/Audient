"""Streaming harness: replays a scenario on a virtual clock and records a full trace."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from ..protocol import adapt_event
from ..vclock import VirtualEventLoop
from .mock_env import MockEnv

ROOT = Path(__file__).resolve().parents[2]
WALL_CAP_S = 120.0


def load_scenario(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_paths(ev: dict) -> dict:
    if "path" in ev and not Path(ev["path"]).is_absolute():
        ev = {**ev, "path": str(ROOT / ev["path"])}
    return ev


async def _run(scn: dict, agent: Any) -> dict:
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    trace: list[dict] = []

    def deliver(ev: dict) -> None:
        # t = when the scenario says it happens; t_recv = when the agent can actually see it. They differ
        # when the harness waits in real time (perception running) and the OS timer wakes it late
        # (~15.6 ms timer tick on Windows) -- latency is measured from t_recv, the lag reported separately.
        trace.append({"dir": "in", **ev, "t_recv": round(asyncio.get_running_loop().time(), 6)})
        inbox.put_nowait(ev)

    env = MockEnv(scn["manifest"], scn.get("env", {}), deliver)
    events = [{"t": 0.0, "type": "tool_manifest", "tools": scn["manifest"]}] + scn["events"]
    for ev in events:
        ev = _resolve_paths(adapt_event(ev))
        loop.call_at(float(ev["t"]), deliver, ev)
    last_t = max(float(e["t"]) for e in events)

    agent_task = asyncio.ensure_future(agent.run(inbox, outbox))
    wall0 = time.perf_counter()
    last_final_t = -1.0
    while True:
        try:
            act = await asyncio.wait_for(outbox.get(), timeout=1.0)
        except asyncio.TimeoutError:
            act = None
        now = loop.time()
        if act is not None:
            act = {"t": round(now, 4), **act}
            trace.append({"dir": "out", **act})
            if act["type"] == "tool_call":
                env.start_call(act)
            elif act["type"] == "cancel":
                env.cancel(act["call_id"])
            elif act["type"] in ("final_response", "clarify"):
                last_final_t = now
        done = now > last_t and not env.pending and last_final_t > last_t and outbox.empty()
        quiet = now > last_t + 15.0 and not env.pending
        if done and now > last_final_t + 0.5 or quiet or time.perf_counter() - wall0 > WALL_CAP_S:
            break
    agent_task.cancel()
    try:
        await agent_task
    except (asyncio.CancelledError, Exception):
        pass
    return {"scenario": scn["id"], "trace": sorted(trace, key=lambda r: r["t"]),
            "commits": env.commits, "env_log": env.log, "wall_s": round(time.perf_counter() - wall0, 3)}


def run_scenario(scn: dict, agent_factory: Callable[[], Any]) -> dict:
    loop = VirtualEventLoop()
    asyncio.set_event_loop(loop)
    try:
        agent = agent_factory()
        if hasattr(agent, "warmup"):
            loop.run_until_complete(agent.warmup())
        loop.clock.t = 0.0  # warm-up time (setup hook) is not part of the scenario timeline
        return loop.run_until_complete(_run(scn, agent))
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)
