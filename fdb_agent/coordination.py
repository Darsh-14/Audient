"""Audient's coordination layer for tool calls in a live voice conversation (no LiveKit dependency).

A realtime speech model decides to call a tool as soon as it thinks the user has finished, which is often
during a pause in the middle of a sentence ("flights to Paris... actually, no, make that Berlin"). Two rules
keep the actions that reach the backend correct:

  * hold, then commit: a call is held until the user has been quiet for a short settle time. If the user
    starts speaking again before it runs, the call is cancelled (never executed) and the model is told to
    issue it again with the final details if it is still needed.
  * never repeat: a call identical to one already executed in this conversation (same tool, same
    arguments) is not executed again; the earlier result is returned. A second identical call that arrives
    while the first is still held or running waits for the first and shares its result.

Only calls that actually run are logged (the benchmark scores exactly those).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

SETTLE_S = 0.8      # quiet time required before a held call runs
MAX_HOLD_S = 6.0    # safety valve: a call never waits longer than this unless the user re-started speaking
POLL_S = 0.05

CANCELLED = {"status": "cancelled",
             "message": "Not run: the user kept talking after this was requested. Use the user's final words; "
                        "call the tool again with the corrected details if it is still needed."}


def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return " ".join(v.split()).lower()
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return str(v)


def call_key(name: str, args: dict) -> str:
    return json.dumps([name, _norm({k: v for k, v in args.items() if v is not None})], sort_keys=True)


class ToolGate:
    def __init__(self, execute: Callable[[str, dict], Any], log: Callable[[str, dict, float, float], None],
                 settle_s: float = SETTLE_S, max_hold_s: float = MAX_HOLD_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.execute, self.log = execute, log
        self.settle_s, self.max_hold_s = settle_s, max_hold_s
        self.clock, self.sleep = clock, sleep
        self.speaking = False
        self.last_start = float("-inf")
        self.last_stop = float("-inf")
        self.done: dict[str, Any] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.stats = {"executed": 0, "reused": 0, "cancelled": 0}

    # ---- the user's speech, from the voice pipeline's voice-activity events
    def user_started(self) -> None:
        self.speaking = True
        self.last_start = self.clock()

    def user_stopped(self) -> None:
        self.speaking = False
        self.last_stop = self.clock()

    # ---- a tool call from the model
    async def call(self, name: str, args: dict) -> Any:
        key = call_key(name, args)
        if key in self.done:
            self.stats["reused"] += 1
            return self.done[key]
        if key in self.pending:
            self.stats["reused"] += 1
            return await asyncio.shield(self.pending[key])
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        try:
            result = await self._hold_then_run(name, args, key)
            fut.set_result(result)
            return result
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            self.pending.pop(key, None)

    async def _hold_then_run(self, name: str, args: dict, key: str) -> Any:
        issued = self.clock()
        while True:
            now = self.clock()
            if self.last_start > issued:  # the user went on talking: this call was premature
                self.stats["cancelled"] += 1
                return dict(CANCELLED)
            quiet_since = max(issued, self.last_stop)
            if not self.speaking and now - quiet_since >= self.settle_s:
                break
            if now - issued >= self.max_hold_s:
                break
            await self.sleep(POLL_S)
        t0 = time.time()
        result = await asyncio.to_thread(self.execute, name, args)
        t1 = time.time()
        self.log(name, args, t0, t1)
        self.done[key] = result
        self.stats["executed"] += 1
        return result
