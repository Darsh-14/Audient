"""Interruptible real-time agent: fast path / slow path / coordination layer.

Fast path   ``on_event`` is synchronous and never awaits: it acknowledges, cancels
            superseded calls and updates the state snapshot in the same tick an
            event arrives (0 ms on the virtual clock + CPU time).
Slow path   tool calls, perception (OCR / ASR) and multi-step plans run as tasks;
            every plan carries an epoch and aborts itself if the user re-planned.
Coordination
            call ledger with explicit call_ids, conflict-driven cancellation,
            idempotency keys + commit ledger for state-modifying tools, bounded
            retries, progress narration only when the floor has been silent.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
from typing import Any

from .nlu import (Parse, extract, fill_slots, param_type, route_intent, tool_tokens, turn_incomplete)
from .protocol import adapt_event, is_state_modifying
from .vclock import compute

PROGRESS_AFTER_S = 2.5
GIL_SWITCH_S = 0.0005  # perception threads must not starve the fast path (default 5 ms)
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.3

VERB = {"search": "searching", "find": "finding", "book": "booking", "create": "creating", "get": "getting",
        "lookup": "looking up", "reserve": "reserving", "cancel": "cancelling", "update": "updating",
        "check": "checking", "set": "setting", "order": "ordering", "send": "sending", "schedule": "scheduling"}


HOLD_S = 0.9  # how long to hold the floor after a pause that ends mid-phrase


def _canon(tool: str, args: dict) -> str:
    """SHA-256 idempotency key over the tool name and canonical (sorted) arguments."""
    return hashlib.sha256((tool + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()[:32]


class CancelGraph:
    """Dependency DAG over in-flight work (tool calls, retries, progress timers, perception).

    Invalidating a node cancels its asyncio task and every descendant, so a superseded
    call can never be revived by a pending retry timer or a dependent step.
    """

    def __init__(self) -> None:
        self.children: dict[str, set[str]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.dead: set[str] = set()

    def add(self, node: str, parent: str | None = None, task: asyncio.Task | None = None) -> None:
        self.children.setdefault(node, set())
        if parent is not None:
            self.children.setdefault(parent, set()).add(node)
        if task is not None:
            self.tasks[node] = task

    def invalidate(self, node: str) -> list[str]:
        """Cancel ``node`` and its descendants; returns every invalidated node id."""
        out, stack = [], [node]
        while stack:
            n = stack.pop()
            if n in self.dead:
                continue
            self.dead.add(n)
            out.append(n)
            t = self.tasks.pop(n, None)
            if t is not None and not t.done():
                t.cancel()
            stack.extend(self.children.get(n, ()))
        return out


def _human(tool: str) -> tuple[str, str]:
    parts = tool.lower().split("_")
    verb = VERB.get(parts[0], parts[0] + "ing")
    noun = " ".join(parts[1:]) or "request"
    return verb, noun


def _fmt(k: str, v: Any) -> str:
    kl = k.lower()
    if "price" in kl and isinstance(v, (int, float)):
        return f"₹{v:,}" if "inr" in kl else f"{v:,}"
    if kl.endswith("_min"):
        return f"{v} min"
    if kl.endswith("_km"):
        return f"{v} km"
    return str(v)


def summarize_item(d: dict) -> str:
    scalars = [(k, v) for k, v in d.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    idk = [kv for kv in scalars if kv[0].endswith("id") or kv[0].endswith("ref")]
    rest = [kv for kv in scalars if kv not in idk]
    head = idk[0][1] if idk else ""
    bits = []
    for k, v in rest[:4]:
        kl = k.lower()
        if "price" in kl:
            bits.append(f"for {_fmt(k, v)}")
        elif kl in ("depart", "departure", "time") or kl.endswith("_time"):
            bits.append(f"at {v}")
        else:
            bits.append(_fmt(k, v))
    return (f"{head} " if head else "") + ("(" + ", ".join(bits) + ")" if bits else "")


def _items(result: dict) -> list[dict] | None:
    for v in result.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    return None


class RealtimeAgent:
    def __init__(self, perception: Any = None, session_date: dt.date | None = None,
                 speculative: bool = True) -> None:
        self.perception = perception
        self.ref_date = session_date or dt.date.today()
        self.speculative = speculative
        self.tools: dict[str, dict] = {}
        # dialogue state (session-scoped only)
        self.intent: str | None = None
        self.slots: dict[str, Any] = {}        # tier 2: committed (end-of-turn) slots
        self.tentative: dict[str, Any] = {}    # tier 1: speculative slots from partial speech
        self.spec_intent: str | None = None
        self.graph = CancelGraph()
        self.hold_task: asyncio.Task | None = None
        self.status = "idle"
        self.version = 0
        self.pref: Any = None
        self.awaiting: str | None = None      # slot or "choice" we asked about
        self.results: dict[str, Any] = {}     # provider tool -> (args_key, result)
        self.buffer: list[str] = []
        self.hyp = ""                          # live ASR hypothesis for the utterance in progress
        self.epoch = 0
        # coordination
        self.calls: dict[str, dict] = {}
        self.committed: dict[str, dict] = {}  # idempotency key -> result
        self.n_calls = 0
        self.last_spoke = -1e9
        self.frame_task: asyncio.Task | None = None
        self.out: asyncio.Queue | None = None

    # ------------------------------------------------------------------ plumbing
    async def warmup(self) -> None:
        import sys
        sys.setswitchinterval(GIL_SWITCH_S)
        if self.perception is not None:
            await compute(self.perception.warmup)

    async def run(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self.out = outbox
        while True:
            ev = adapt_event(await inbox.get())
            if ev.get("type") == "session_end":
                return
            self.on_event(ev)

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    def emit(self, action: dict) -> None:
        assert self.out is not None
        if action["type"] in ("speak", "clarify", "final_response"):
            self.last_spoke = self.now()
        self.out.put_nowait(action)

    def snapshot(self) -> dict:
        return {"version": self.version, "intent": self.intent, "slots": dict(self.slots), "status": self.status,
                "tentative_slots": dict(self.tentative),
                "committed_actions": [{"tool": r["tool"], "call_id": c, "idempotency_key": r["key"]}
                                      for c, r in self.calls.items() if r["sm"] and r["status"] == "done"]}

    def say(self, text: str, kind: str = "ack") -> None:
        self.emit({"type": "speak", "kind": kind, "text": text})

    def final(self, text: str, status: str) -> None:
        self.status = status
        self.emit({"type": "final_response", "text": text, "state_snapshot": self.snapshot()})

    def clarify(self, text: str, slot: str | None) -> None:
        self.status = "clarifying"
        self.awaiting = slot
        self.emit({"type": "clarify", "text": text, "slot": slot, "state_snapshot": self.snapshot()})

    def spawn(self, coro) -> asyncio.Task:
        return asyncio.ensure_future(coro)

    # ------------------------------------------------------------------ fast path
    def on_event(self, ev: dict) -> None:
        typ = ev.get("type")
        if typ == "tool_manifest":
            self.tools = {t["name"]: t for t in ev.get("tools", [])}
        elif typ == "transcript":
            if self.hold_task is not None and not self.hold_task.done():
                self.hold_task.cancel()  # the user resumed after a pause: same turn continues
            self.hold_task = None
            if ev.get("hypothesis"):
                # streaming ASR: the text is the whole current utterance so far (it gets revised),
                # not a new chunk; it joins the buffer only once final
                self.hyp = "" if ev.get("end_of_turn", True) else ev.get("text", "")
                if ev.get("end_of_turn", True):
                    self.buffer.append(ev.get("text", ""))
            else:
                self.buffer.append(ev.get("text", ""))
            text = " ".join(self.buffer + ([self.hyp] if self.hyp else [])).strip()
            if ev.get("end_of_turn", True):
                if turn_incomplete(text):
                    # acoustic pause / trailing filler: keep the floor with the user, don't act yet
                    self.hold_task = self.spawn(self._hold_then_commit())
                    return
                self.buffer = []
                if text:
                    self.handle_turn(text)
            else:
                self.on_partial(text)
        elif typ == "interruption":
            self.last_spoke = -1e9  # floor yielded to the user
        elif typ == "frame":
            if self.perception is not None:
                self.frame_task = self.spawn(compute(self.perception.analyze_frame, ev["path"]))
        elif typ == "audio":
            self.say("Mm-hm, one moment.", "filler")
            self.spawn(self._asr(ev["path"], ev.get("end_of_turn", True), self.epoch))
        elif typ == "tool_result":
            self.on_result(ev)

    def _specs(self) -> list[dict]:
        specs = [self.tools[self.intent]] if self.intent in self.tools else []
        prov = self._provider(self.intent) if self.intent else None
        if prov:
            specs.append(self.tools[prov])
        return specs

    def _extract_slots(self, p: Parse, specs: list[dict]) -> dict[str, Any]:
        new: dict[str, Any] = {}
        for spec in specs:
            for k, v in fill_slots(p, spec).items():
                new.setdefault(k, v)
        return new

    async def _hold_then_commit(self) -> None:
        await asyncio.sleep(HOLD_S)
        text, self.buffer = " ".join(self.buffer).strip(), []
        self.hold_task = None
        if text:
            self.handle_turn(text)

    def _conflicts(self, slots: dict[str, Any]) -> list[str]:
        """Live calls (in flight or waiting to retry) whose arguments disagree with ``slots``."""
        return [cid for cid, rec in self.calls.items()
                if rec["status"] in ("inflight", "retry_pending")
                and any(slots.get(k) != v for k, v in rec["args"].items())]

    def cancel_calls(self, cids: list[str], reason: str) -> None:
        """Invalidate calls through the cancellation graph (descendants included)."""
        for cid in cids:
            for node in self.graph.invalidate(cid):
                rec = self.calls.get(node)
                if rec is None:
                    continue
                if rec["status"] == "inflight":
                    rec["status"] = "cancelled"
                    self.emit({"type": "cancel", "call_id": node, "reason": reason})
                elif rec["status"] == "retry_pending":
                    rec["status"] = "cancelled"  # retry timer killed; nothing was in flight

    def on_partial(self, text: str) -> None:
        """Speculative handling of an unfinished turn: early cancel / early read-only retrieval."""
        p = extract(text, self.ref_date)
        if self.intent and any(r["status"] == "inflight" for r in self.calls.values()):
            if p.cancel:
                return  # wait for end of turn to confirm a full cancel
            new = self._extract_slots(p, self._specs())
            self.tentative = {k: v for k, v in new.items() if self.slots.get(k) != v}
            if self.tentative:  # mid-utterance self-repair: abort stale work before the turn ends
                self.cancel_calls(self._conflicts({**self.slots, **self.tentative}), "superseded_by_user_correction")
            return
        if not self.speculative or self.intent or not self.tools:
            return
        intent, score = route_intent(p, self.tools)
        if not intent or score < 3.0 or is_state_modifying(self.tools[intent]):
            return  # never speculate a state-modifying action
        spec = self.tools[intent]
        slots = fill_slots(p, spec)
        req = spec.get("parameters", {}).get("required", [])
        if all(r in slots for r in req) and not self._inflight(intent, slots):
            self.cancel_calls([c for c, r in self.calls.items() if r.get("speculative") and r["status"] == "inflight"],
                              "speculation_revised")
            self.spec_intent, self.tentative, self.status = intent, slots, "in_progress"
            self.call(intent, slots, "goal", speculative=True)

    def handle_turn(self, text: str) -> None:
        p = extract(text, self.ref_date)
        if self.intent is None and self.spec_intent is not None:
            self.intent = self.spec_intent  # speculation becomes the working hypothesis...
        self.spec_intent, self.tentative = None, {}  # ...and tier-1 slots are re-derived from the full turn
        if p.cancel and not (route_intent(p, self.tools)[0] and p.mentions):
            self._user_cancel()
            return
        intent, _ = route_intent(p, self.tools)
        same_task = intent is None or intent == self.intent or (
            self.intent is not None and intent in (self._provider(self.intent), self._related(self.intent)))
        self.epoch += 1
        if self.intent is None or not same_task:
            if self.intent is not None:  # new task supersedes the old one
                self.cancel_calls([c for c, r in self.calls.items() if r["status"] == "inflight"], "superseded_by_new_request")
            if intent is None:
                self.clarify("Sorry, what would you like me to do?", None)
                return
            self.intent, self.slots, self.pref, self.awaiting = intent, {}, None, None
            self.results = {}
        prefs = [m.value for m in p.mentions if m.etype == "pref"]
        if prefs:
            self.pref = prefs[-1]
        if self.awaiting == "choice" and self._choose_from_text(p) is not None:
            return
        new = self._extract_slots(p, self._specs())
        # a newly reported symptom (error code vs. indicator light) replaces the old one
        props = {k: s for spec in self._specs() for k, s in spec.get("parameters", {}).get("properties", {}).items()}
        new_sym = {param_type(k, props[k]) for k in new if k in props} & {"code", "indicator"}
        if new_sym:
            for k in list(self.slots):
                if k in props and k not in new and param_type(k, props[k]) in {"code", "indicator"} - new_sym:
                    self.slots.pop(k)
                    new_sym.add("_removed")
        changed = {k: v for k, v in new.items() if self.slots.get(k) != v}
        corrected = any(k in self.slots for k in changed) or "_removed" in new_sym
        self.slots.update(new)
        if changed or corrected or self.version == 0:
            self.version += 1
        # 1) cancel anything the correction invalidated (same tick as the event)
        stale = self._conflicts(self.slots)
        self.cancel_calls(stale, "superseded_by_user_correction")
        # 2) a committed state change cannot be silently redone
        if changed and self._committed_conflict(changed):
            return
        self.status = "in_progress"
        self.spawn(self._plan(self.epoch, p, corrected))

    def _related(self, intent: str) -> str | None:
        """Tools sharing a noun with the current intent (e.g. search_flights ~ book_flight)."""
        n1, _ = tool_tokens(self.tools[intent])
        for name, spec in self.tools.items():
            if name != intent and (tool_tokens(spec)[0] & n1) - {"get", "create", "search", "book"}:
                return name
        return None

    def _committed_conflict(self, changed: dict) -> bool:
        for cid, rec in self.calls.items():
            if rec["status"] == "done" and rec["sm"] and any(k in rec["args"] and rec["args"][k] != v for k, v in changed.items()):
                ref = summarize_item(rec["result"]) if isinstance(rec.get("result"), dict) else ""
                self.final(f"That {_human(rec['tool'])[1]} is already confirmed ({ref.strip()}), so I haven't "
                           f"made a second one. I can't change it with the tools I have.", "completed")
                return True
        return False

    def _user_cancel(self) -> None:
        self.epoch += 1
        live = [c for c, r in self.calls.items() if r["status"] == "inflight"]
        self.cancel_calls(live, "user_cancelled")
        done_sm = [r for r in self.calls.values() if r["status"] == "done" and r["sm"]]
        self.status = "cancelled"
        if done_sm:
            self.final("Okay, I've stopped. Note that the earlier " + _human(done_sm[-1]["tool"])[1] +
                       " was already confirmed before you asked.", "cancelled")
        else:
            self.final("Okay, I've cancelled that. Nothing was changed.", "cancelled")

    # ------------------------------------------------------------------ slow path
    def _provider(self, intent: str | None) -> str | None:
        """Read-only tool that can supply a missing reference id for ``intent``."""
        if intent is None or intent not in self.tools:
            return None
        spec = self.tools[intent]
        props = spec.get("parameters", {}).get("properties", {})
        refs = [k for k, s in props.items() if param_type(k, s) == "ref"]
        if not refs:
            return None
        n1, _ = tool_tokens(spec)
        best, best_s = None, 0
        for name, other in self.tools.items():
            if name == intent or is_state_modifying(other):
                continue
            n2, _ = tool_tokens(other)
            s = 2 * len({w.rstrip("s") for w in n1} & {w.rstrip("s") for w in n2}) + \
                ("search" in n2) + (refs[0] in json.dumps(other))
            if s > best_s:
                best, best_s = name, s
        return best if best_s >= 2 else None

    def _missing(self, tool: str) -> list[str]:
        spec = self.tools[tool]
        props = spec.get("parameters", {}).get("properties", {})
        return [r for r in spec.get("parameters", {}).get("required", [])
                if r not in self.slots and param_type(r, props.get(r, {})) != "ref"]

    def _question(self, tool: str, slot: str) -> str:
        s = self.tools[tool]["parameters"]["properties"].get(slot, {})
        t = param_type(slot, s)
        return {"origin": "Where will you be departing from?", "destination": "Where would you like to go?",
                "date": "What date should I use?", "time": "What time would you like?",
                "count": f"How many {slot.replace('_', ' ')}?".replace("party size", "people"),
                "code": "What error code is shown on the display?",
                "model": "What's the model number printed on the device?"}.get(
            t, f"What {s.get('description', slot.replace('_', ' '))} should I use?")

    def _ack_text(self, tool: str, corrected: bool) -> str:
        verb, noun = _human(tool)
        props = self.tools[tool].get("parameters", {}).get("properties", {})
        bits = []
        for k in props:
            if k in self.slots and param_type(k, props[k]) not in ("freetext", "ref"):
                t = param_type(k, props[k])
                v = self.slots[k]
                bits.append({"origin": f"from {v}", "destination": f"to {v}", "date": f"on {v}", "time": f"at {v}",
                             "count": f"for {v}"}.get(t, str(v)))
        head = "Got it, updating that. " if corrected else "Sure. "
        return f"{head}{verb.capitalize()} {noun} {' '.join(bits)}".rstrip() + "."

    async def _plan(self, epoch: int, p: Parse, corrected: bool) -> None:
        tool = self.intent
        if tool is None:
            return
        spec = self.tools[tool]
        props = spec.get("parameters", {}).get("properties", {})
        # perception-backed slots (model / error code / indicator light read from the camera frame)
        needs_vis = [k for k in props if param_type(k, props[k]) in ("code", "model", "indicator")
                     and k not in self.slots]
        acked = False
        if needs_vis and self.frame_task is not None:
            if not self.frame_task.done():
                self.say(("Got it, updating that. " if corrected else "") + "Let me take a look at the camera feed.", "ack")
                acked = True
            try:
                vis = await asyncio.shield(self.frame_task)
            except Exception:
                vis = {"ambiguous": "perception_error"}
            if epoch != self.epoch:
                return
            has_symptom = any(param_type(k, props[k]) in ("code", "indicator") for k in self.slots if k in props)
            for k in needs_vis:
                t = param_type(k, props[k])
                if vis.get(t) and not (t in ("code", "indicator") and has_symptom):
                    self.slots[k] = vis[t]
                    has_symptom = has_symptom or t in ("code", "indicator")
            self.version += 1
            still = [k for k in self._missing(tool) if k in needs_vis]
            if still or (vis.get("ambiguous") and not has_symptom):
                reason = (vis.get("ambiguous") or "nothing readable").replace("_", " ")
                self.clarify(f"I can't read the device clearly ({reason}). Could you hold the camera closer, "
                             "or tell me the code or light you see?", (still or needs_vis)[0])
                return
        missing = self._missing(tool)
        prov = self._provider(tool)
        if prov:
            missing = [m for m in missing] + [r for r in self._missing(prov) if r not in missing]
        if missing:
            self.clarify(self._question(prov if (prov and missing[0] in self.tools[prov]["parameters"]["properties"]
                                                 and missing[0] not in props) else tool, missing[0]), missing[0])
            return
        if not acked:
            self.say(self._ack_text(tool, corrected), "ack")
        refs = [k for k, s in props.items() if param_type(k, s) == "ref" and k not in self.slots]
        if refs and prov:
            pargs = self._args(prov)
            cached = self.results.get(prov)
            if cached and cached[0] == _canon(prov, pargs):
                self._after_provider(tool, prov, cached[1], epoch)
            elif not self._inflight(prov, pargs):
                self.call(prov, pargs, "provider")
            return
        args = self._args(tool)
        cached = self.results.get(tool)
        if cached and cached[0] == _canon(tool, args) and not is_state_modifying(spec):
            self.final(self._result_text(tool, cached[1]), "completed")  # speculative prefetch paid off
        elif not self._inflight(tool, args):
            self.call(tool, args, "goal")

    def _args(self, tool: str) -> dict:
        props = self.tools[tool].get("parameters", {}).get("properties", {})
        return {k: self.slots[k] for k in props if k in self.slots}

    def _inflight(self, tool: str, args: dict) -> bool:
        return any(r["status"] == "inflight" and r["tool"] == tool and r["args"] == args for r in self.calls.values())

    def call(self, tool: str, args: dict, purpose: str, speculative: bool = False, key: str | None = None,
             parent: str | None = None) -> str | None:
        sm = is_state_modifying(self.tools[tool])
        key = key or (_canon(tool, args) if sm else None)
        if sm and key in self.committed:  # never repeat a committed state change
            self.final(self._result_text(tool, self.committed[key]), "completed")
            return None
        if sm and any(r["status"] == "inflight" and r.get("key") == key for r in self.calls.values()):
            return None
        self.n_calls += 1
        cid = f"call_{self.n_calls:03d}"
        self.calls[cid] = {"tool": tool, "args": dict(args), "status": "inflight", "purpose": purpose, "sm": sm,
                           "key": key, "attempt": self.calls.get(cid, {}).get("attempt", 1), "t0": self.now(),
                           "epoch": self.epoch, "speculative": speculative}
        self.emit({"type": "tool_call", "call_id": cid, "tool": tool, "args": dict(args), "idempotency_key": key})
        self.graph.add(cid, parent)
        self.graph.add(f"{cid}:progress", cid, self.spawn(self._progress_watch(cid)))
        return cid

    async def _progress_watch(self, cid: str) -> None:
        await asyncio.sleep(PROGRESS_AFTER_S)
        rec = self.calls.get(cid)
        if rec and rec["status"] == "inflight" and self.now() - self.last_spoke >= PROGRESS_AFTER_S:
            self.say(f"Still {_human(rec['tool'])[0]} {_human(rec['tool'])[1]}, almost there.", "progress")

    def on_result(self, ev: dict) -> None:
        cid = ev.get("call_id")
        rec = self.calls.get(cid)
        if rec is None:
            return
        if rec["status"] == "cancelled":
            if ev.get("status") == "ok" and rec["sm"]:  # race: committed before the cancel landed
                self.committed[rec["key"]] = ev["result"]
                rec["status"], rec["result"] = "done", ev["result"]
                self.say(f"Heads up: the earlier {_human(rec['tool'])[1]} went through before I could stop it "
                         f"({summarize_item(ev['result']).strip()}).", "progress")
            return  # stale read-only result: discard
        if ev.get("status") != "ok":
            rec["status"] = "failed"
            if self._conflicts_with_slots(rec):
                return
            if ev.get("retryable", True) and rec["attempt"] < MAX_ATTEMPTS:
                if rec["attempt"] == 1:
                    self.say(f"The {_human(rec['tool'])[1]} service timed out, retrying.", "progress")
                rec["status"] = "retry_pending"
                self.graph.add(f"{cid}:retry", cid, self.spawn(self._retry(cid, rec)))
            else:
                self.final(f"Sorry, the {_human(rec['tool'])[1]} service failed after {rec['attempt']} attempts, "
                           f"so nothing was completed. Want me to try again?", "failed")
            return
        rec["status"], rec["result"] = "done", ev["result"]
        if rec["sm"]:
            self.committed[rec["key"]] = ev["result"]
        else:
            self.results[rec["tool"]] = (_canon(rec["tool"], rec["args"]), ev["result"])
        if rec["epoch"] != self.epoch and not rec.get("speculative"):
            # result of a plan the user has since changed; keep only if still consistent
            if self._conflicts_with_slots(rec):
                return
        if rec["purpose"] == "provider":
            self.results[rec["tool"]] = (_canon(rec["tool"], rec["args"]), ev["result"])
            self._after_provider(self.intent, rec["tool"], ev["result"], self.epoch)
        elif rec["tool"] == self.intent and not self._conflicts_with_slots(rec):
            self.final(self._result_text(rec["tool"], ev["result"]), "completed")

    def _conflicts_with_slots(self, rec: dict) -> bool:
        return any(k in self.slots and self.slots[k] != v for k, v in rec["args"].items())

    async def _retry(self, failed_cid: str, rec: dict) -> None:
        await asyncio.sleep(RETRY_BACKOFF_S * rec["attempt"])
        if rec["status"] != "retry_pending" or self._conflicts_with_slots(rec):
            return
        rec["status"] = "failed"
        # same idempotency key on retry: a state change can never be applied twice
        cid = self.call(rec["tool"], rec["args"], rec["purpose"], key=rec.get("key"), parent=failed_cid,
                        speculative=rec.get("speculative", False))
        if cid:
            self.calls[cid]["attempt"] = rec["attempt"] + 1

    def _after_provider(self, tool: str | None, prov: str, result: dict, epoch: int) -> None:
        if tool is None or epoch != self.epoch:
            return
        items = _items(result) or []
        if not items:
            self.final(f"I couldn't find any options for that, so nothing was {_human(tool)[0].replace('ing', 'ed')}.", "completed")
            return
        choice = self._select(items, self.pref)
        if choice is None:
            listing = "; ".join(summarize_item(it).strip() for it in items[:3])
            self.clarify(f"I found {len(items)} options: {listing}. Which one should I pick?", "choice")
            self._pending_items = items
            return
        self._commit_choice(tool, choice)

    def _commit_choice(self, tool: str, item: dict) -> None:
        props = self.tools[tool].get("parameters", {}).get("properties", {})
        for k, s in props.items():
            if param_type(k, s) == "ref" and k not in self.slots:
                v = item.get(k) or next((item[x] for x in item if x.endswith("id") or x.endswith("ref")), None)
                if v is not None:
                    self.slots[k] = v
        self.version += 1
        self.awaiting = None
        missing = self._missing(tool)
        if missing:
            self.clarify(self._question(tool, missing[0]), missing[0])
            return
        args = self._args(tool)
        if not self._inflight(tool, args):
            self.call(tool, args, "goal")

    def _select(self, items: list[dict], pref: Any) -> dict | None:
        if len(items) == 1:
            return items[0]
        if not pref:
            return None
        mode, what = pref
        if mode == "index":
            return items[what] if what < len(items) else None
        keyf = None
        for k in items[0]:
            if what == "price" and "price" in k.lower():
                keyf = k
            if what == "time" and k.lower() in ("depart", "departure", "time", "start"):
                keyf = k
        if keyf is None:
            return None
        return (min if mode == "min" else max)(items, key=lambda it: it[keyf])

    def _choose_from_text(self, p: Parse) -> dict | None:
        items = getattr(self, "_pending_items", None)
        if not items:
            return None
        prefs = [m.value for m in p.mentions if m.etype == "pref"]
        choice = self._select(items, prefs[-1]) if prefs else None
        if choice is None:
            low = p.clean.lower().replace(" ", "")
            for it in items:
                vals = [str(v).lower().replace(" ", "") for v in it.values()]
                times = [m.value for m in p.mentions if m.etype == "time"]
                if any(v and v in low for v in vals if len(v) > 3) or any(t in it.values() for t in times):
                    choice = it
                    break
        if choice is not None:
            self.pref = None
            self._pending_items = None
            self.epoch += 1
            self._commit_choice(self.intent, choice)
        return choice

    async def _asr(self, path: str, end_of_turn: bool, epoch: int) -> None:
        if self.perception is None:
            self.clarify("Sorry, I can't process audio right now. Could you type that?", None)
            return
        res = await compute(self.perception.transcribe, path)
        if not res.get("text") or res.get("ambiguous"):
            self.clarify("Sorry, I didn't catch that clearly. Could you say it again?", None)
            return
        self.on_event({"type": "transcript", "text": res["text"], "end_of_turn": end_of_turn})

    # ------------------------------------------------------------------ NLG
    def _result_text(self, tool: str, result: dict) -> str:
        verb, noun = _human(tool)
        if is_state_modifying(self.tools[tool]):
            return f"Done. Your {noun} is confirmed: {summarize_item(result).strip()}."
        if result.get("found") is False:
            return (f"I couldn't find that in the documentation for "
                    f"{', '.join(str(v) for k, v in self._args(tool).items())}. Could you double-check it?")
        if "instructions" in result:
            return f"{result.get('section', 'From the manual')}: {result['instructions']}"
        items = _items(result)
        if items:
            best = self._select(items, self.pref) or items[0]
            others = "; ".join(summarize_item(it).strip() for it in items if it is not best)[:200]
            where = " ".join(f"{p} {self.slots[k]}" for k, p in (("origin", "from"), ("destination", "to"), ("date", "on"))
                             if k in self.slots)
            return f"I found {len(items)} {noun} {where}. Best match: {summarize_item(best).strip()}. Others: {others}."
        return f"Here you go: {summarize_item(result).strip()}."
