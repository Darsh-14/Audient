"""Audient's coordination layer for tool calls in a live voice conversation (no LiveKit dependency).

A realtime speech model decides to call a tool as soon as it thinks the user has finished, which is often
during a pause in the middle of a sentence ("flights to Milan on June 1st... well, wait, actually June 3rd").
Three rules keep the actions that reach the backend correct:

  * hold, then commit: a call runs once the user has been quiet for a settle time (2.0 s by default, counted
    from when they stopped speaking, so later steps in a chain run at once;
    pauses inside a sentence in FDB-v3's recordings last 1.6-3.2 s, and 0.8 s let premature calls through).
  * defer, don't drop: if the user starts speaking again before a held call runs, the call waits until they
    have finished. It is then dropped only if it was replaced: the user said a correction phrase ("no",
    "wait", "actually", "instead", "scratch that", "I mean", "make it"...) and the model has since issued a
    newer call to the same tool. Otherwise it runs as requested. (Cancelling every call the user talked over
    lost calls the model never re-issued.)
  * never repeat: a call identical to one already executed (same tool, same arguments) is not executed
    again; the earlier result is returned. Identical calls arriving together share one execution.

Only calls that actually run are logged (the benchmark scores exactly those).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

SETTLE_S = 2.0       # quiet time required before a held call runs
REPLACE_WAIT_S = 2.5  # after a correction phrase, how long to wait for the model's replacement call
MAX_HOLD_S = 20.0    # safety valve: a held call never waits longer than this
POLL_S = 0.05

CORRECTION = re.compile(
    r"\b(no|nope|wait|actually|instead|scratch that|i mean|make (?:it|that)|change (?:it|that)|not that|"
    r"rather|on second thought|correction|sorry)\b", re.I)

REPLACED = {"status": "cancelled",
            "message": "Not run: the user corrected this request and a newer request replaced it. "
                       "Use the result of the corrected request."}


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
                 settle_s: float = SETTLE_S, max_hold_s: float = MAX_HOLD_S, replace_wait_s: float = REPLACE_WAIT_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.execute, self.log = execute, log
        self.settle_s, self.max_hold_s, self.replace_wait_s = settle_s, max_hold_s, replace_wait_s
        self.clock, self.sleep = clock, sleep
        self.speaking = False
        self.last_start = float("-inf")
        self.last_stop = float("-inf")
        self.heard: list[tuple[float, str]] = []        # (when, words) from the user's transcribed speech
        self.issued: dict[str, list[float]] = {}         # tool name -> times calls to it were issued
        self.done: dict[str, Any] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.stats = {"executed": 0, "reused": 0, "cancelled": 0, "deferred": 0}

    # ---- the user's speech, from the voice pipeline
    def user_started(self) -> None:
        self.speaking = True
        self.last_start = self.clock()

    def user_stopped(self) -> None:
        self.speaking = False
        self.last_stop = self.clock()

    def user_said(self, text: str) -> None:
        if text and text.strip():
            self.heard.append((self.clock(), text))

    # ---- a tool call from the model
    async def call(self, name: str, args: dict) -> Any:
        key = call_key(name, args)
        if key in self.done:
            self.stats["reused"] += 1
            return self.done[key]
        if key in self.pending:
            self.stats["reused"] += 1
            return await asyncio.shield(self.pending[key])
        issued = self.clock()
        self.issued.setdefault(name, []).append(issued)
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        try:
            result = await self._hold_then_run(name, args, key, issued)
            fut.set_result(result)
            return result
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            self.pending.pop(key, None)

    def _corrected_since(self, t: float) -> bool:
        return any(when > t and CORRECTION.search(words) for when, words in self.heard)

    def _replaced_since(self, name: str, t: float) -> bool:
        return any(when > t for when in self.issued.get(name, []))

    async def _hold_then_run(self, name: str, args: dict, key: str, issued: float) -> Any:
        deferred = False
        while True:
            now = self.clock()
            if not deferred and self.last_start > issued:  # the user went on talking: wait until they finish
                deferred = True
                self.stats["deferred"] += 1
            if deferred and self._corrected_since(issued) and self._replaced_since(name, issued):
                self.stats["cancelled"] += 1
                return dict(REPLACED)
            # quiet is counted from when the user last stopped speaking, not from when the call was issued: a call
            # made long after the user finished (a later step in a chain) runs at once; one made in a pause waits
            quiet_since = self.last_stop
            quiet = not self.speaking and now - quiet_since >= self.settle_s
            if quiet and deferred and self._corrected_since(issued):
                # the user corrected something: give the model a moment to issue the replacement
                if now - quiet_since < self.settle_s + self.replace_wait_s:
                    await self.sleep(POLL_S)
                    continue
            if quiet or now - issued >= self.max_hold_s:
                break
            await self.sleep(POLL_S)
        t0 = time.time()
        result = await asyncio.to_thread(self.execute, name, args)
        t1 = time.time()
        self.log(name, args, t0, t1)
        self.done[key] = result
        self.stats["executed"] += 1
        return result
