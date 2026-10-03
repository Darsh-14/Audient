"""Deterministic mock tool environment with latency and fault injection.

Results are pure functions of the arguments (hash-seeded), latency is fixed per
tool, and faults are injected on configured attempt numbers. A cancel that
arrives before a call completes aborts it with no side effect; a cancel after
completion is "too_late" (the side effect stands).
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable

from ..protocol import is_state_modifying

DEFAULT_LATENCY = {"search_flights": 1.6, "book_flight": 1.8, "create_ticket": 1.2,
                   "lookup_manual": 1.0, "get_route": 1.4, "reserve_table": 1.5,
                   "get_weather": 0.9, "set_alarm": 0.4, "set_timer": 0.3, "set_reminder": 0.5, "add_calendar_event": 0.7,
                   "send_message": 0.8, "make_call": 0.6, "play_music": 0.7, "control_device": 0.6, "book_cab": 1.4,
                   "take_note": 0.4, "find_places": 0.8}

MANUAL = {
    ("WM3000", "E42"): "Drain pump blocked. Unplug the machine, open the filter hatch at the "
                       "bottom-right and clear the drain filter, then run a Rinse & Spin cycle.",
    ("WM3000", "E17"): "Water inlet fault. Check that the tap is open and the inlet hose is not kinked.",
    ("WM3000", "E20"): "Drain pump fault. Check the drain hose for kinks and clean the pump filter.",
    ("WM3000", "BLUEBLINKING2"): "Water inlet valve not opening. Turn off the supply, check the valve "
                                 "screen for debris and verify the valve coil (manual section 7.3).",
    ("WM3000", "REDSOLID"): "Door lock engaged during a fault. Wait 2 minutes for the lock to release.",
    ("AC900", "F3"): "Outdoor unit overheating. Clean the condenser coil and ensure 50 cm clearance.",
}


def _h(*parts: Any) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _norm(s: Any) -> str:
    return "".join(ch for ch in str(s).upper() if ch.isalnum())


def search_flights(a: dict) -> dict:
    o, d, date = a.get("origin"), a.get("destination"), a.get("date")
    base = _h(o, d, date)
    airlines = ["IndiGo", "Air India", "Vistara", "Akasa Air"]
    flights = []
    for i in range(3):
        k = (base >> (i * 5)) % 97
        dep_h = [6, 9, 13, 18, 21][(k + i) % 5]
        airline = airlines[(k + i) % 4]
        code = {"IndiGo": "6E", "Air India": "AI", "Vistara": "UK", "Akasa Air": "QP"}[airline]
        flights.append({"flight_id": f"{code}-{100 + (k * 7 + i * 13) % 900}", "airline": airline,
                        "depart": f"{dep_h:02d}:{(k * 5) % 60:02d}",
                        "price_inr": 3500 + ((k * 131 + i * 977) % 6000)})
    return {"origin": o, "destination": d, "date": date, "flights": flights}


def book_flight(a: dict) -> dict:
    return {"status": "confirmed", "booking_ref": f"BK{_h(a.get('flight_id'), a.get('passengers')) % 10**6:06d}",
            "flight_id": a.get("flight_id"), "passengers": a.get("passengers")}


def create_ticket(a: dict) -> dict:
    return {"status": "open", "ticket_id": f"TCK-{_h(a.get('issue'), a.get('device')) % 10**5:05d}",
            "priority": a.get("priority", "medium")}


def lookup_manual(a: dict) -> dict:
    model = _norm(a.get("device_model"))
    model = next((m for m, _ in MANUAL if model.endswith(m)), model)  # OCR may merge brand + model
    key = (model, _norm(a.get("error_code") or a.get("indicator") or ""))
    text = MANUAL.get(key)
    if text is None:
        return {"found": False, "device_model": a.get("device_model"), "error_code": a.get("error_code")}
    return {"found": True, "device_model": a.get("device_model"), "error_code": a.get("error_code"),
            "section": f"Manual section for {a.get('error_code') or str(a.get('indicator', '')).replace('_', ' ')}", "instructions": text}


def get_route(a: dict) -> dict:
    k = _h(a.get("destination"))
    return {"destination": a.get("destination"), "eta_min": 12 + k % 40,
            "distance_km": round(4 + (k % 300) / 10, 1), "via": ["Ring Road", "Airport Expressway", "MG Road"][k % 3]}


def reserve_table(a: dict) -> dict:
    return {"status": "confirmed", "reservation_id": f"RSV-{_h(*sorted(a.items())) % 10**5:05d}",
            **{k: v for k, v in a.items()}}


def _ref(prefix: str, a: dict) -> str:
    return f"{prefix}-{_h(prefix, *sorted(a.items())) % 10**5:05d}"


CONDITIONS = ["clear sky", "partly cloudy", "overcast", "light rain", "thunderstorms", "haze"]


def get_weather(a: dict) -> dict:  # simulated; the web app replaces it with live Open-Meteo data
    k = _h(a.get("location"), a.get("date"))
    return {"location": a.get("location"), "date": a.get("date"), "condition": CONDITIONS[k % len(CONDITIONS)],
            "high_c": 24 + k % 12, "low_c": 16 + k % 8, "rain_chance_pct": (k >> 3) % 90, "source": "simulated"}


def find_places(a: dict) -> dict:  # simulated; the web app searches OpenStreetMap near the user's location
    kind = str(a.get("kind") or "place")
    k = _h(kind, a.get("near"))
    places = [{"name": f"{['City', 'Central', 'Lakeside'][i]} {kind.title()}", "distance_km": round(0.6 + ((k >> (3 * i)) % 40) / 10, 1)}
              for i in range(3)]
    out = {"kind": kind, "places": sorted(places, key=lambda x: x["distance_km"]), "source": "simulated"}
    if a.get("near"):
        out["near"] = a["near"]
    return out


def _done(status: str, prefix: str):
    return lambda a: {"status": status, "id": _ref(prefix, a), **a}


HANDLERS: dict[str, Callable[[dict], dict]] = {
    "get_weather": get_weather, "find_places": find_places, "set_alarm": _done("set", "ALM"), "set_timer": _done("running", "TMR"),
    "set_reminder": _done("set", "REM"), "add_calendar_event": _done("added", "EVT"), "send_message": _done("sent", "MSG"),
    "make_call": _done("calling", "CALL"), "play_music": _done("playing", "MUS"), "control_device": _done("done", "DEV"),
    "book_cab": lambda a: {"status": "confirmed", "ride_id": _ref("RIDE", a), **a, "eta_min": 4 + _h(a.get("destination")) % 9},
    "take_note": _done("saved", "NOTE"),
    "search_flights": search_flights, "book_flight": book_flight, "create_ticket": create_ticket,
    "lookup_manual": lookup_manual, "get_route": get_route, "reserve_table": reserve_table,
}


class MockEnv:
    def __init__(self, manifest: list[dict], config: dict | None = None,
                 deliver: Callable[[dict], None] | None = None, live: dict | None = None) -> None:
        cfg = config or {}
        self.live = live or {}  # tool -> async callable(args) -> result: real services (e.g. weather in the browser)
        self.tools = {t["name"]: t for t in manifest}
        self.latency = {**DEFAULT_LATENCY, **cfg.get("latency", {})}
        self.faults = cfg.get("faults", {})  # tool -> [attempt numbers that fail]
        self.deliver = deliver or (lambda ev: None)
        self.attempts: dict[str, int] = {}
        self.pending: dict[str, asyncio.TimerHandle] = {}
        self.commits: list[dict] = []  # committed side effects
        self.idem_seen: dict[str, dict] = {}
        self.log: list[dict] = []

    def start_call(self, action: dict) -> None:
        loop = asyncio.get_running_loop()
        tool, cid = action["tool"], action["call_id"]
        self.attempts[tool] = self.attempts.get(tool, 0) + 1
        fail = self.attempts[tool] in self.faults.get(tool, [])
        if tool in self.live:  # a real service: the call is a task, so a cancel aborts it the same way
            self.pending[cid] = asyncio.ensure_future(self._live_call(action))
            return
        h = loop.call_later(self.latency.get(tool, 1.0), self._complete, action, fail)
        self.pending[cid] = h

    def cancel(self, call_id: str) -> str:
        h = self.pending.pop(call_id, None)
        if h is None:
            self.log.append({"call_id": call_id, "cancel": "too_late_or_unknown"})
            return "too_late"
        h.cancel()
        self.log.append({"call_id": call_id, "cancel": "aborted"})
        return "aborted"

    async def _live_call(self, action: dict) -> None:
        cid = action["call_id"]
        try:
            result = await self.live[action["tool"]](action.get("args", {}))
            ev = {"type": "tool_result", "call_id": cid, "status": "ok", "result": result}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network or service error: reported, and retried by the agent
            ev = {"type": "tool_result", "call_id": cid, "status": "error", "error": f"{type(e).__name__}: {e}"[:200],
                  "retryable": True}
        self.pending.pop(cid, None)
        ev["t"] = asyncio.get_running_loop().time()
        self.deliver(ev)

    def _complete(self, action: dict, fail: bool) -> None:
        cid, tool, args = action["call_id"], action["tool"], action.get("args", {})
        self.pending.pop(cid, None)
        loop = asyncio.get_running_loop()
        if fail:
            ev = {"type": "tool_result", "call_id": cid, "status": "error",
                  "error": "upstream_timeout", "retryable": True}
        else:
            key = action.get("idempotency_key")
            if key and key in self.idem_seen:  # idempotent replay: same result, no new commit
                result = self.idem_seen[key]
            else:
                handler = HANDLERS.get(tool, lambda a: {"status": "ok", "id": f"OK-{_h(tool, sorted(a.items())) % 10**5:05d}", **a})
                result = handler(args)
                if is_state_modifying(self.tools.get(tool, {})):
                    self.commits.append({"t": loop.time(), "tool": tool, "args": args, "call_id": cid})
                    if key:
                        self.idem_seen[key] = result
            ev = {"type": "tool_result", "call_id": cid, "status": "ok", "result": result}
        ev["t"] = loop.time()
        self.deliver(ev)
