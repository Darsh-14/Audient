"""Half-duplex (cascaded) baseline: listen -> think -> call tool -> wait -> speak.

Uses the *same* NLU and perception as Audient so the comparison isolates the
architecture: no acknowledgements, no speculation, no cancellation, no retries,
no idempotency keys. Input that arrives while it is busy is queued and handled
afterwards as a fresh request (the classic source of stale re-runs).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from .agent import _items, summarize_item
from .nlu import extract, fill_slots, param_type, route_intent
from .protocol import adapt_event, is_state_modifying


class HalfDuplexAgent:
    def __init__(self, perception: Any = None, session_date: dt.date | None = None) -> None:
        self.perception = perception
        self.ref_date = session_date or dt.date.today()
        self.tools: dict[str, dict] = {}
        self.intent: str | None = None
        self.slots: dict[str, Any] = {}
        self.frame: str | None = None
        self.n = 0
        self.backlog: list[dict] = []
        self.buffer: list[str] = []

    async def warmup(self) -> None:
        if self.perception is not None:
            self.perception.warmup()

    async def run(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self.inbox, self.out = inbox, outbox
        while True:
            ev = self.backlog.pop(0) if self.backlog else adapt_event(await inbox.get())
            t = ev.get("type")
            if t == "tool_manifest":
                self.tools = {x["name"]: x for x in ev["tools"]}
            elif t == "frame":
                self.frame = ev["path"]
            elif t == "audio":  # same speech recognition as Audient, but blocking, on the conversation thread
                res = self.perception.transcribe(ev["path"]) if self.perception is not None else {}
                if res.get("text") and not res.get("ambiguous"):
                    self.backlog.insert(0, {"type": "transcript", "text": res["text"], "end_of_turn": ev.get("end_of_turn", True)})
                else:
                    self.out.put_nowait({"type": "clarify", "text": "Sorry?", "slot": None, "state_snapshot": self.snap("clarifying")})
            elif t == "transcript":
                self.buffer.append(ev["text"])
                if ev.get("end_of_turn", True):
                    text, self.buffer = " ".join(self.buffer), []
                    await self.turn(text)

    async def wait_result(self, cid: str) -> dict:
        while True:
            ev = adapt_event(await self.inbox.get())
            if ev.get("type") == "tool_result" and ev.get("call_id") == cid:
                return ev
            if ev.get("type") != "tool_result":
                self.backlog.append(ev)  # half-duplex: user speech waits until we're done

    async def call(self, tool: str, args: dict) -> dict:
        self.n += 1
        cid = f"b_{self.n:03d}"
        self.out.put_nowait({"type": "tool_call", "call_id": cid, "tool": tool, "args": args, "idempotency_key": None})
        return await self.wait_result(cid)

    def snap(self, status: str) -> dict:
        return {"version": self.n, "intent": self.intent, "slots": dict(self.slots), "status": status}

    async def turn(self, text: str) -> None:
        p = extract(text, self.ref_date)
        intent, _ = route_intent(p, self.tools)
        if p.cancel and intent is None:
            self.out.put_nowait({"type": "final_response", "text": "Okay.", "state_snapshot": self.snap("cancelled")})
            return
        if intent is not None and intent != self.intent:
            self.intent, self.slots = intent, {}
        if self.intent is None:
            self.out.put_nowait({"type": "clarify", "text": "Sorry?", "slot": None, "state_snapshot": self.snap("clarifying")})
            return
        spec = self.tools[self.intent]
        props = spec["parameters"]["properties"]
        vis = {}
        if self.frame and self.perception is not None:
            vis = self.perception.analyze_frame(self.frame)  # blocking, on the conversation thread
        prov = next((n for n, s in self.tools.items() if not is_state_modifying(s) and n != self.intent
                     and set(n.split("_")[1:]) & {w.rstrip("s") + "s" for w in self.intent.split("_")[1:]}), None)
        for s in [spec] + ([self.tools[prov]] if prov else []):
            self.slots.update(fill_slots(p, s, vis))
        refs = [k for k, s in props.items() if param_type(k, s) == "ref" and k not in self.slots]
        if refs and prov:
            pargs = {k: v for k, v in self.slots.items() if k in self.tools[prov]["parameters"]["properties"]}
            miss = [r for r in self.tools[prov]["parameters"]["required"] if r not in pargs]
            if miss:
                self.out.put_nowait({"type": "clarify", "text": f"What {miss[0]}?", "slot": miss[0],
                                     "state_snapshot": self.snap("clarifying")})
                return
            res = await self.call(prov, pargs)
            items = _items(res.get("result", {})) or []
            if res.get("status") != "ok" or not items:
                self.out.put_nowait({"type": "final_response", "text": "Sorry, that failed.", "state_snapshot": self.snap("failed")})
                return
            best = min(items, key=lambda it: it.get("price_inr", 0))
            self.slots[refs[0]] = best.get(refs[0])
        args = {k: v for k, v in self.slots.items() if k in props}
        miss = [r for r in spec["parameters"]["required"] if r not in args]
        if miss:
            self.out.put_nowait({"type": "clarify", "text": f"What {miss[0]}?", "slot": miss[0],
                                 "state_snapshot": self.snap("clarifying")})
            return
        res = await self.call(self.intent, args)
        if res.get("status") != "ok":
            self.out.put_nowait({"type": "final_response", "text": "Sorry, that failed.", "state_snapshot": self.snap("failed")})
            return
        r = res["result"]
        text = r.get("instructions") or ("Here's what I found: " + "; ".join(
            summarize_item(i) for i in (_items(r) or [r])[:3]) + f" ({', '.join(str(v) for v in args.values())})")
        self.out.put_nowait({"type": "final_response", "text": text, "state_snapshot": self.snap("completed")})
