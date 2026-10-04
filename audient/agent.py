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

from .nlu import (ANAPHOR_RE, BACKCHANNEL_RE, GREET_RE, HELP_RE, PLACE_KINDS, PLACES, QUESTION_RE, REJECT_RE, THINK_RE, Parse,
                  _stem,
                  extract,
                  fill_slots, off_topic, param_type, route_intent, split_tasks, tool_tokens, turn_incomplete)
from .protocol import adapt_event, is_state_modifying
from .vclock import compute

PROGRESS_AFTER_S = 2.5
GIL_SWITCH_S = 0.0005  # perception threads must not starve the fast path (default 5 ms)
MAX_ATTEMPTS = 3
AUDIO_ACKS = ("Mm-hm, one moment.", "Got it, one sec.", "Okay, listening.")
RETRY_BACKOFF_S = 0.3

VERB = {"search": "searching", "find": "finding", "book": "booking", "create": "creating", "get": "getting",
        "lookup": "looking up", "reserve": "reserving", "cancel": "cancelling", "update": "updating",
        "check": "checking", "set": "setting", "order": "ordering", "send": "sending", "schedule": "scheduling",
        "control": "controlling", "play": "playing", "make": "making", "take": "taking", "add": "adding"}
UNCOUNTABLE = {"music", "weather", "news", "information"}


HOLD_S = 0.9  # how long to hold the floor after a pause that ends mid-phrase
MODEL_TIMEOUT_S = 4.5      # a language-model reply slower than this falls back to the rule parser
MODEL_HOLD_ACK_S = 1.5     # if the model hasn't answered by then, say a short "one moment"
HOLD_ACKS = ["One moment.", "Let me see.", "Okay, one second.", "Mm-hm, give me a second."]


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
    if kl.endswith("_c"):
        return f"{kl[:-2].replace('_', ' ')} {v}°C"           # high_c -> "high 31°C"
    if kl.endswith("_pct"):
        return f"{kl[:-4].replace('_', ' ')} {v}%"            # rain_chance_pct -> "rain chance 40%"
    return str(v)


def summarize_item(d: dict) -> str:
    scalars = [(k, v) for k, v in d.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    idk = [kv for kv in scalars if kv[0].endswith("id") or kv[0].endswith("ref")]
    head = idk[0][1] if idk else ""
    bits = []
    for k, v in [kv for kv in scalars if kv not in idk[:1]][:5]:
        kl = k.lower()
        if kl == "status" and str(v).lower() in ("confirmed", "ok", "success") or kl in ("source", "found"):
            continue  # already said by "is confirmed" / bookkeeping, not news
        if "price" in kl:
            bits.append(f"for {_fmt(k, v)}")
        elif kl in ("depart", "departure", "time") or kl.endswith("_time"):
            bits.append(f"at {v}")
        elif kl == "via":
            bits.append(f"via {v}")
        elif isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            d_ = dt.date.fromisoformat(v)
            bits.append(f"{d_.day} {d_.strftime('%B')}")
        elif kl.endswith(("_min", "_km", "_c", "_pct")):
            bits.append(_fmt(k, v))
        elif isinstance(v, int):
            bits.append(f"{v} {kl[:-1] if v == 1 else kl}" if kl.endswith("s")
                        else f"{kl.replace('_', ' ')} {v}")  # 2 passengers, 1 passenger, party size 4
        elif kl in ("priority", "level", "tier", "class"):
            bits.append(f"{v} {kl}")
        else:
            bits.append(_fmt(k, v))
    if not head:
        return ", ".join(bits)
    return f"{head} " + ("(" + ", ".join(bits) + ")" if bits else "")


# words that ask for a field of a result ("what time does it leave" -> depart)
ATTR_WORDS = {"depart": {"time", "leave", "leaves", "depart", "departure", "departs", "when"},
              "price": {"price", "cost", "costs", "much", "fare", "pay"}, "airline": {"airline", "carrier", "operator"},
              "eta": {"eta", "long", "arrive", "arrival", "minutes"}, "distance": {"far", "distance", "km", "kilometres"},
              "via": {"via", "route"}, "ref": {"reference", "ref", "confirmation", "pnr", "number"},
              "id": {"id", "number", "reference", "code"}, "status": {"status"},
              "instructions": {"fix", "instructions", "steps", "should", "solve"}}
PREF_LEAD = {("min", "price"): "The cheapest one", ("min", "time"): "The earliest one", ("max", "time"): "The latest one"}


def _asked_key(p: Parse, item: dict) -> str | None:
    q = {_stem(w) for w in re.findall(r"[a-z]+", p.clean.lower())}
    best, best_s = None, 0
    for k in item:
        parts = [x for x in k.lower().split("_") if x not in ("inr", "min", "km")]
        words = set(parts)
        for part in parts:
            words |= ATTR_WORDS.get(part, set())
        sc = len({_stem(w) for w in words} & q)
        if sc > best_s:
            best, best_s = k, sc
    return best


def _attr(k: str, v: Any) -> str:
    kl = k.lower()
    if kl in ("depart", "departure", "time") or kl.endswith("_time"):
        return f"leaves at {v}"
    if "price" in kl or "cost" in kl or "fare" in kl:
        return f"costs {_fmt(k, v)}"
    if kl.startswith("eta"):
        return f"takes about {_fmt(k, v)}"
    if "distance" in kl:
        return f"is {_fmt(k, v)} away"
    if kl == "via":
        return f"goes via {v}"
    if kl.endswith("_ref") or kl.endswith("_id"):
        return f"{' '.join(kl.split('_')[:-1])} reference is {v}"
    return f"{kl.replace('_', ' ')} is {_fmt(k, v)}"


def _leaves_of(x: Any):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves_of(v)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves_of(v)
    else:
        yield x


def _items(result: dict) -> list[dict] | None:
    for v in result.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    return None


class RealtimeAgent:
    def __init__(self, perception: Any = None, session_date: dt.date | None = None,
                 speculative: bool = True, llm: Any = None) -> None:
        self.perception = perception
        # optional language model (async callable: context JSON -> decision dict). It replaces the rule
        # parser for understanding; the fast path (acks, cancellation, idempotent commits) stays here.
        self.llm = llm
        self.audio_n = 0
        self.llm_task: asyncio.Task | None = None
        self.llm_n = 0
        self.dialog: list[dict] = []           # what was said this session, for the model's context
        self._ack_override: str | None = None
        self._llm_text = ""                    # the turn the pending model call is about
        self.n_hold_acks = 0
        self.ref_date = session_date or dt.date.today()
        self.speculative = speculative and llm is None  # with a model, act only on what the user finished saying
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
        self.queue: list[str] = []            # further requests from the same utterance, run one after another
        self.history: list[dict] = []         # earlier tasks of this session (session-scoped only)

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
            if action.get("kind") != "filler":
                self.dialog = (self.dialog + [{"agent": action["text"]}])[-12:]
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
        if self.queue and status in ("completed", "failed"):
            asyncio.get_running_loop().call_soon(self._next_task)

    def _next_task(self) -> None:
        if self.queue and not self._busy() and self.status in ("completed", "failed"):
            self.handle_turn(self.queue.pop(0), queued=True)

    def _busy(self) -> bool:
        return any(r["status"] in ("inflight", "retry_pending") for r in self.calls.values()) or (
            self.hold_task is not None and not self.hold_task.done())

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
            # a short acknowledgment while the clip is transcribed; varied, so a second clip doesn't get an echo
            self.say(AUDIO_ACKS[self.audio_n % len(AUDIO_ACKS)], "filler")
            self.audio_n += 1
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

    def handle_turn(self, text: str, queued: bool = False, rules: bool = False) -> None:
        if not queued and not rules:
            self.dialog = (self.dialog + [{"user": text}])[-12:]
        if self.llm is not None and not rules:
            self._turn_with_model(text)
            return
        p = extract(text, self.ref_date)
        if self.intent is None and self.spec_intent is not None:
            self.intent = self.spec_intent  # speculation becomes the working hypothesis...
        self.spec_intent, self.tentative = None, {}  # ...and tier-1 slots are re-derived from the full turn
        # "okay" / "thanks" with nothing being asked is a backchannel: keep working, don't re-plan
        if self.intent is not None and self.awaiting is None and not p.mentions and BACKCHANNEL_RE.match(p.clean):
            return  # nothing to add: an unprompted "anything else?" would only talk over the user
        if p.cancel and not (route_intent(p, self.tools)[0] and p.mentions):
            self.queue.clear()
            self._user_cancel()
            return
        if not p.mentions:
            if THINK_RE.search(p.clean):  # "let me think": the user keeps the floor; nothing to do yet
                self.say("Sure, take your time.", "ack")
                return
            if self.intent is not None and REJECT_RE.match(p.clean):  # "no, that's not what I wanted"
                self.clarify("Sorry about that. What should I change?", None)
                return
        # several requests in one breath ("book a table ... and find flights ..."): one task at a time
        if not queued and self.awaiting is None:
            parts = split_tasks(p.clean, self.tools, self.ref_date)
            if len(parts) > 1:
                self.queue = parts[1:]
                p = extract(parts[0], self.ref_date)
        intent, _ = route_intent(p, self.tools)
        off = intent is not None and off_topic(p, self.tools[intent])
        if off:
            intent = None
        social = bool(GREET_RE.match(p.clean) or HELP_RE.search(p.clean))
        if intent is None and (self.intent is None or off or social):
            self._scope_reply(social and not off)
            return
        if intent is not None and self.intent is not None and intent != self.intent and self._escalates(intent):
            self.intent, self.awaiting = intent, None  # "book the second one" after a search: the next step of the task
        same_task = intent is None or intent == self.intent or (
            self.intent is not None and intent in (self._provider(self.intent), self._related(self.intent)))
        self.epoch += 1
        if self.intent is None or not same_task:
            if self.intent is not None:  # new task supersedes the old one
                self.cancel_calls([c for c, r in self.calls.items() if r["status"] == "inflight"], "superseded_by_new_request")
                self._remember()
                if not queued:
                    self.queue.clear()
            self.intent, self.slots, self.pref, self.awaiting = intent, {}, None, None
            self.results = {}
            if ANAPHOR_RE.search(p.clean):
                self._recall(intent)  # "book the cheapest of those flights" after a detour
        prefs = [m.value for m in p.mentions if m.etype == "pref"]
        if prefs:
            self.pref = prefs[-1]
        if self.awaiting == "choice" and self._choose_from_text(p) is not None:
            return
        new = self._extract_slots(p, self._specs())
        # a question about results we already have is answered from them, with no new tool call
        if QUESTION_RE.search(p.clean) and not any(self.slots.get(k) != v for k, v in new.items()):
            answer = self._answer(p)
            if answer:
                self.final(answer, "completed")
                return
        # nothing in the turn changes the finished task: say so, rather than re-running it as if it did
        if (intent in (None, self.intent) and not p.mentions and not new and self.awaiting is None
                and self.status in ("completed", "failed", "cancelled", "clarifying") and not ANAPHOR_RE.search(p.clean)):
            self.clarify("Sorry, I don't have that information." if QUESTION_RE.search(p.clean)
                         else "Sorry, I didn't get that. What would you like to do?", None)
            return
        self._apply_slots(new)

    def _apply_slots(self, new: dict[str, Any]) -> None:
        """Merge new slot values into the task, cancel what they invalidate, and (re)plan."""
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
        self.spawn(self._plan(self.epoch, None, corrected))

    # ------------------------------------------------------------------ language-model understanding
    def _turn_with_model(self, text: str) -> None:
        """Fast path in the same tick (cancel / contradicting correction); the model call is slow-path work."""
        p = extract(text, self.ref_date)
        self.spec_intent, self.tentative = None, {}
        if p.cancel and not (route_intent(p, self.tools)[0] and p.mentions):
            self.queue.clear()
            self._user_cancel()
            return
        # "yes" / "okay" that answers no question is a backchannel: never let a model read it as consent
        asked = any("agent" in m and m["agent"].rstrip().endswith("?") for m in self.dialog[-2:])
        if self.awaiting is None and not asked and not p.mentions and BACKCHANNEL_RE.match(p.clean):
            return
        # two requests in one breath: the model gets them one at a time
        if self.awaiting is None:
            parts = split_tasks(p.clean, self.tools, self.ref_date)
            if len(parts) > 1:
                self.queue = parts[1:]
                text, p = parts[0], extract(parts[0], self.ref_date)
        if self.intent is not None:
            new = self._extract_slots(p, self._specs())
            stale = self._conflicts({**self.slots, **new}) if new else []
            if stale:  # "make it 3" while booking for 2: stop the stale call now, not after the model replies
                self.cancel_calls(stale, "superseded_by_user_correction")
        if self.llm_task is not None and not self.llm_task.done():
            self.llm_task.cancel()  # the user said more before the last turn was understood: understand both together
            text = f"{self._llm_text} {text}"
        self._llm_text = text
        self.llm_n += 1
        self.llm_task = self.spawn(self._understand(text, self.llm_n))

    async def _hold_ack(self, n: int) -> None:
        await asyncio.sleep(MODEL_HOLD_ACK_S)
        if n == self.llm_n:
            self.say(HOLD_ACKS[self.n_hold_acks % len(HOLD_ACKS)], "ack")
            self.n_hold_acks += 1

    async def _understand(self, text: str, n: int) -> None:
        hold = self.spawn(self._hold_ack(n))
        try:
            res = await asyncio.wait_for(self.llm(json.dumps(self._model_context(text), default=str)), MODEL_TIMEOUT_S)
        except asyncio.CancelledError:
            hold.cancel()
            raise
        except Exception:
            res = None
        hold.cancel()
        if n != self.llm_n:
            return
        if not self._apply_model(res, text):
            self.handle_turn(text, rules=True)  # model unavailable or unusable: the rule parser takes over

    def _model_context(self, text: str) -> dict:
        results = []
        for tool, result in self._recent_results()[:2]:
            items = _items(result)
            results.append({"tool": tool, "items": [{"index": i, **it} for i, it in enumerate(items[:6])]}
                           if items else {"tool": tool, "result": result})
        return {"today": f"{self.ref_date.isoformat()} ({self.ref_date.strftime('%A')})", "utterance": text,
                "tools": [{"name": t["name"], "description": t.get("description", ""),
                           "side_effect": "state_modifying" if is_state_modifying(t) else "read_only",
                           "parameters": t.get("parameters", {})} for t in self.tools.values()],
                "state": {"task": self.intent, "slots": self.slots, "status": self.status,
                          "waiting_for": self.awaiting, "working": self._busy()},
                "results": results, "conversation": self.dialog[-10:]}

    def _clean_args(self, tool: str, args: Any) -> dict[str, Any]:
        """Keep only arguments the tool schemas accept, in the right types; never an invented id."""
        if not isinstance(args, dict):
            return {}
        specs = [self.tools[tool]] + ([self.tools[pv]] if (pv := self._provider(tool)) else [])
        props = {k: sc for sp in specs for k, sc in sp.get("parameters", {}).get("properties", {}).items()}
        known = {str(v) for _, r in self._recent_results() for v in _leaves_of(r)}
        out: dict[str, Any] = {}
        for k, v in args.items():
            sc = props.get(k)
            if sc is None or v is None or v == "":
                continue
            try:
                if sc.get("type") == "integer":
                    v = int(v)
                elif sc.get("type") == "number":
                    v = float(v)
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                continue
            if "enum" in sc:
                v = next((e for e in sc["enum"] if str(e).lower() == str(v).lower()), None)
                if v is None:
                    continue
            t = param_type(k, sc)
            if t == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                continue
            if t == "time" and not re.fullmatch(r"\d{2}:\d{2}", str(v)):
                continue
            if t == "ref" and str(v) not in known:
                continue  # an id the model made up: let the agent look it up instead
            if t == "kind":  # "movie theatre" / "Petrol pumps" -> the same kinds the rules use ("cinema", "fuel")
                w = str(v).lower().strip()
                v = next((PLACE_KINDS[x] for x in (w, w[:-1], w[:-2]) if x in PLACE_KINDS), w)
            out[k] = v
        return out

    def _apply_model(self, res: Any, text: str) -> bool:
        """Act on the model's decision. False means it was unusable and the rules should handle the turn."""
        if not isinstance(res, dict):
            return False
        act, reply = res.get("act"), str(res.get("reply") or "").strip()
        if act == "wait":  # the user hasn't finished, or is thinking: keep the floor with them
            if reply:
                self.say(reply, "ack")
            return True
        if act == "cancel":
            self.queue.clear()
            self._user_cancel()
            return True
        if act == "unsupported":  # a model refusing what a tool plainly covers ("take me to the airport"): trust the rules
            rp = extract(text, self.ref_date)
            rule_intent, score = route_intent(rp, self.tools)
            if rule_intent and score >= 3.0 and not off_topic(rp, self.tools[rule_intent]):
                return False
        if act in ("chat", "unsupported") or (act == "answer" and self.intent is None):
            if not reply:
                return False
            self.say(reply, "ack") if self._busy() else self.clarify(reply, None)
            return True
        if act == "answer":
            if not reply:
                return False
            self.final(reply, "completed")
            return True
        tool = res.get("tool")
        if act != "task" or tool not in self.tools:
            return False
        args = self._clean_args(tool, res.get("args"))
        # exact codes ("red_solid") and dates are read by the rules, which apply one convention every time
        p = extract(text, self.ref_date)
        for spec in [self.tools[tool]] + ([self.tools[pv]] if (pv := self._provider(tool)) else []):
            props = spec.get("parameters", {}).get("properties", {})
            for k, v in fill_slots(p, spec).items():
                if param_type(k, props.get(k, {})) in ("code", "indicator", "date"):
                    args[k] = v
        pick = res.get("pick")
        pick = pick if isinstance(pick, int) and not isinstance(pick, bool) and pick >= 0 else None
        nxt = str(res.get("next") or "").strip()
        if nxt and not self.queue and len(nxt) < len(text) and not text.lower().startswith(nxt.lower()):
            self.queue = [nxt]  # a separate second request; the sentence itself repeated would book twice
        prefer = {"cheapest": ("min", "price"), "earliest": ("min", "time"), "latest": ("max", "time")}.get(
            str(res.get("prefer") or "").lower())
        self._model_task(tool, args, pick, reply, text, prefer)
        return True

    def _model_task(self, intent: str, args: dict, pick: int | None, reply: str, text: str,
                    prefer: tuple | None = None) -> None:
        if self.intent is not None and intent != self.intent and self._escalates(intent):
            self.intent, self.awaiting = intent, None
        same_task = intent == self.intent or (
            self.intent is not None and intent in (self._provider(self.intent), self._related(self.intent)))
        self.epoch += 1
        if self.intent is None or not same_task:
            if self.intent is not None:
                self.cancel_calls([c for c, r in self.calls.items() if r["status"] == "inflight"], "superseded_by_new_request")
                self._remember()
            self.intent, self.slots, self.pref, self.awaiting = intent, {}, None, None
            self.results = {}
            if pick is not None or prefer is not None or ANAPHOR_RE.search(text):
                self._recall(intent)
        if prefer is not None and pick is None:
            self.pref = prefer
            items = getattr(self, "_pending_items", None)
            if self.awaiting == "choice" and items and (choice := self._select(items, prefer)) is not None:
                self.slots.update(args)
                self._pending_items, self.pref = None, None
                self._commit_choice(self.intent, choice)
                return
        if pick is not None:
            self.pref = ("index", pick)
            items = getattr(self, "_pending_items", None)
            if self.awaiting == "choice" and items and pick < len(items):
                self.slots.update(args)
                self._pending_items, self.pref = None, None
                self._commit_choice(self.intent, items[pick])
                return
        self._ack_override = reply or None
        self._apply_slots(args)

    def _capabilities(self) -> str:
        cats = list(dict.fromkeys(t["category"] for t in self.tools.values() if t.get("category")))
        if cats:  # a manifest with categories: describe areas, not every tool
            if self.llm is not None:
                cats.append("general questions")
            return cats[0] if len(cats) == 1 else ", ".join(cats[:-1]) + " and " + cats[-1]
        out = []
        for name in self.tools:
            parts = name.lower().split("_")
            verb, noun = {"lookup": "look up"}.get(parts[0], parts[0]), " ".join(parts[1:])
            if noun and not noun.endswith("s"):
                noun = ("an " if noun[0] in "aeiou" else "a ") + noun
            out.append(f"{verb} {noun}".strip())
        return out[0] if len(out) == 1 else ", ".join(out[:-1]) + " and " + out[-1] if out else "nothing yet"

    def _scope_reply(self, social: bool) -> None:
        """Greeting / "what can you do" / a request none of the tools can serve: say what is possible."""
        caps = self._capabilities()
        caps = ("help with " if any(t.get("category") for t in self.tools.values()) else "") + caps
        text = (f"Hi, I'm Audient. I can {caps}, and you can interrupt me any time. What would you like to do?"
                if social else f"Sorry, I can't do that with the tools I have. I can {caps}.")
        if self._busy():
            self.say(text, "ack")  # don't disturb the task in flight
        else:
            self.clarify(text, None)

    def _escalates(self, intent: str) -> bool:
        """Read-only step -> the state-modifying step it feeds (search_flights -> book_flight)."""
        cur = self.intent
        return (cur in self.tools and intent in self.tools and is_state_modifying(self.tools[intent])
                and not is_state_modifying(self.tools[cur]) and self._provider(intent) == cur)

    def _remember(self) -> None:
        self.history = (self.history + [{"intent": self.intent, "slots": dict(self.slots), "results": dict(self.results)}])[-5:]

    def _recall(self, intent: str) -> None:
        for h in reversed(self.history):
            if h["intent"] in (intent, self._provider(intent)):
                self.slots, self.results = dict(h["slots"]), dict(h["results"])
                return

    def _recent_results(self) -> list[tuple[str, dict]]:
        fam = {self.intent, self._provider(self.intent)} - {None}
        return [(r["tool"], r["result"]) for r in reversed(list(self.calls.values()))
                if r["status"] == "done" and r["tool"] in fam and isinstance(r.get("result"), dict)
                and not self._conflicts_with_slots(r)]

    def _answer(self, p: Parse) -> str | None:
        """Answer "what time does the cheapest one leave?" from results the agent already has."""
        pref = next((m.value for m in reversed(p.mentions) if m.etype == "pref"), None)
        for tool, result in self._recent_results():
            items = _items(result)
            pool = items or [result]
            low = p.clean.lower()
            named = [it for it in items or [] if any(isinstance(v, str) and len(v) > 3 and v.lower() in low for v in it.values())]
            if items and pref:
                pick = self._select(items, pref)
                pool = [pick] if pick is not None else pool
            elif named and len(named) < len(items):
                pool = named  # "tell me about the IndiGo one"
            key = _asked_key(p, pool[0])
            if key is None and items and named and not pref:
                return "; ".join(summarize_item(it).strip() for it in pool) + "."
            if key is None and not (items and pref):
                continue
            if not items:
                return f"Your {_human(tool)[1]} {_attr(key, result[key])}."
            if len(pool) == 1 and pref:
                it = pool[0]
                lead = PREF_LEAD.get(tuple(pref), f"Option {pref[1] + 1}" if pref[0] == "index" else "That one")
                if key is None:
                    return f"{lead} is {summarize_item(it).strip()}."
                head = next((str(it[k]) for k in it if k.endswith("id") or k.endswith("ref")), "")
                return f"{lead} is {head}" + (f" ({it['airline']})" if "airline" in it else "") + f", and it {_attr(key, it[key])}."
            return "; ".join(f"{next((str(it[k]) for k in it if k.endswith('id')), '')} {_attr(key, it[key])}".strip()
                             for it in pool[:4]) + "."
        return None

    def _related(self, intent: str) -> str | None:
        """Tools sharing a noun with the current intent (e.g. search_flights ~ book_flight)."""
        n1, _ = tool_tokens(self.tools[intent])
        for name, spec in self.tools.items():
            if name != intent and (tool_tokens(spec)[0] & n1) - {"get", "create", "search", "book"}:
                return name
        return None

    def _committed_conflict(self, changed: dict) -> bool:
        for cid, rec in self.calls.items():
            # only this task's own commit: a flight search for the 3rd is not a change to a table booked for the 2nd
            if rec["status"] == "done" and rec["sm"] and rec["tool"] == self.intent and any(
                    k in rec["args"] and rec["args"][k] != v for k, v in changed.items()):
                ref = summarize_item(rec["result"]) if isinstance(rec.get("result"), dict) else ""
                self.final(f"That {_human(rec['tool'])[1]} is already confirmed ({ref.strip()}), so I haven't "
                           f"made a second one. I can't change it with the tools I have.", "completed")
                return True
        return False

    def _user_cancel(self) -> None:
        self.epoch += 1
        self.queue.clear()
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
                "model": "What's the model number printed on the device?", "device": "Which device?",
                "location": "For which city?"}.get(
            t, f"What {s.get('description', slot.replace('_', ' ')).split(',')[0]} should I use?")

    def _spoken_date(self, v: Any) -> str:
        """2026-10-09 -> "on Friday 9 October" (slots keep ISO dates; only speech changes)."""
        try:
            d = dt.date.fromisoformat(str(v))
        except ValueError:
            return f"on {v}"
        delta = (d - self.ref_date).days
        return "today" if delta == 0 else "tomorrow" if delta == 1 else f"on {d.strftime('%A')} {d.day} {d.strftime('%B')}"

    @staticmethod
    def _place(v: Any) -> str:
        return f"the {v}" if v in PLACES and v not in ("home", "work") else str(v)

    def _ack_text(self, tool: str, corrected: bool) -> str:
        verb, noun = _human(tool)
        spec = self.tools[tool]
        props = spec.get("parameters", {}).get("properties", {})
        art = "" if noun.endswith("s") or noun in UNCOUNTABLE else (
            ("an " if noun[0] in "aeiou" else "a ") if is_state_modifying(spec) else "the ")
        head = "Got it, updating that. " if corrected else "Sure. "
        sl = self.slots
        if "kind" in props and "kind" in sl:  # nearby search: "Looking for the nearest cinema near Koregaon Park."
            kind = str(sl["kind"]).upper() if len(str(sl["kind"])) <= 3 else sl["kind"]
            return f"{head}Looking for the nearest {kind}" + (f" near {sl['near']}" if sl.get("near") else "") + "."
        if {"device", "action"} <= set(props) and "device" in sl and "action" in sl:  # smart-home phrasing
            dev = str(sl["device"]).upper() if len(str(sl["device"])) <= 2 else sl["device"]
            what = f"setting your {dev} to {sl['value']}" if sl["action"] == "set" and "value" in sl else f"turning your {dev} {sl['action']}"
            return f"{head}{what[0].upper()}{what[1:]}."
        bits = []
        for k in props:
            if k in self.slots and param_type(k, props[k]) not in ("freetext", "ref"):
                t = param_type(k, props[k])
                v = self.slots[k]
                venue = any(w in k.lower() for w in ("restaurant", "venue", "hotel", "shop", "store", "salon"))
                person = k.lower() in ("contact", "recipient", "person", "to", "contact_name")
                bits.append({"origin": f"from {v}", "destination": f"to {self._place(v)}", "date": self._spoken_date(v),
                             "time": f"at {v}",
                             "count": f"for {v} {k}" if k.lower() in ("minutes", "hours", "seconds", "days", "nights") else f"for {v}", "device": f"for your {str(v).upper() if len(str(v)) <= 2 else v}", "code": f"error {v}",
                             "model": f"for {v}", "indicator": f"({str(v).replace('_', ' ')} light)",
                             "enum": str(v) if k.lower() in ("action", "mode", "state") else f"{v} {k.replace('_', ' ')}",
                             "location": f"for {v}",
                             "name": f"at {v}" if venue else f"to {v}" if person else str(v)}.get(t, str(v)))
        head = "Got it, updating that. " if corrected else "Sure. "
        return f"{head}{verb.capitalize()} {art}{noun} {' '.join(bits)}".rstrip() + "."

    async def _plan(self, epoch: int, p: Parse | None, corrected: bool) -> None:
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
            ack, self._ack_override = self._ack_override, None
            self.say(ack or self._ack_text(tool, corrected), "ack")
        refs = [k for k, s in props.items() if param_type(k, s) == "ref" and k not in self.slots]
        if refs and prov:
            pargs = self._args(prov)
            cached = self.results.get(prov)
            req = self.tools[prov].get("parameters", {}).get("required", [])
            if cached and (cached[0] == _canon(prov, pargs) or all(cached[2].get(r) == pargs.get(r) for r in req)):
                self._after_provider(tool, prov, cached[1], epoch)  # same query: choose from what the user saw
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
            self.results[rec["tool"]] = (_canon(rec["tool"], rec["args"]), ev["result"], dict(rec["args"]))
        if rec["epoch"] != self.epoch and not rec.get("speculative"):
            # result of a plan the user has since changed; keep only if still consistent
            if self._conflicts_with_slots(rec):
                return
        if rec["purpose"] == "provider":
            self.results[rec["tool"]] = (_canon(rec["tool"], rec["args"]), ev["result"], dict(rec["args"]))
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
            status = str(result.get("status", "")).lower()
            if "device" in result and "action" in result:  # smart home: say the new state
                dev = str(result["device"]).upper() if len(str(result["device"])) <= 2 else result["device"]
                be = "are" if str(dev).lower().endswith("s") else "is"
                return (f"Done. Your {dev} {be} set to {result['value']}." if result["action"] == "set" and "value" in result
                        else f"Done. Your {dev} {be} {result['action']}.")
            if status == "calling" and result.get("contact"):
                return f"Calling {result['contact']} now."
            if status == "playing" and result.get("query"):
                return f"Now playing {result['query']}."
            if status and status not in ("confirmed", "open", "ok"):  # "Done. Alarm set: ..." / "Message sent: ..."
                rest = summarize_item({k: v for k, v in result.items() if k != "status"}).strip()
                return f"Done. {noun.capitalize()} {status}: {rest}."
            return f"Done. Your {noun} is confirmed: {summarize_item(result).strip()}."
        if result.get("found") is False:
            if result.get("reason"):  # e.g. location access is off, no saved address
                return f"Sorry, {result['reason']}"
            where = "in the documentation" if "manual" in tool else f"the {noun}"
            return (f"I couldn't find {where} for "
                    f"{', '.join(str(v) for k, v in self._args(tool).items())}. Could you double-check it?")
        if "instructions" in result:
            return f"{result.get('section', 'From the manual')}: {result['instructions']}"
        items = _items(result)
        if items:
            pick = self._select(items, self.pref) if self.pref else None
            where = " ".join(self._spoken_date(self.slots[k]) if k == "date" else f"{p} {self.slots[k]}"
                             for k, p in (("origin", "from"), ("destination", "to"), ("date", "on")) if k in self.slots)
            if pick is None:  # no stated preference: list the options rather than call one "best"
                near = f" near {result['near']}" if result.get("near") else ""
                lead = f"I found {len(items)} {noun}{(' ' + where) if where else ''}{near}"
                kind = str(result.get("kind", noun))  # nearby places: nearest first
                kind = kind.upper() if len(kind) <= 3 else kind
                if "distance_km" in items[0]:
                    lead = f"The nearest {kind}{near} is {summarize_item(items[0]).strip()}"
                    rest = "; ".join(summarize_item(it).strip() for it in items[1:3])
                    return lead + (f". Also close: {rest}." if rest else ".")
                return f"{lead}: " + "; ".join(summarize_item(it).strip() for it in items[:5]) + "."
            lead = PREF_LEAD.get(tuple(self.pref), f"Option {self.pref[1] + 1}" if self.pref[0] == "index" else "My pick")
            others = "; ".join(summarize_item(it).strip() for it in items if it is not pick)
            return f"I found {len(items)} {noun} {where}. {lead} is {summarize_item(pick).strip()}. Others: {others}."
        return f"Here you go: {summarize_item(result).strip()}."
