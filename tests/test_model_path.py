"""The language-model path of the agent, with a scripted stand-in model (no network)."""
import asyncio
import datetime as dt
import json
from pathlib import Path

from audient.live import LiveSession

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "scenarios" / "manifest_core.json").read_text(encoding="utf-8"))
FAST = {"latency": {"search_flights": 0.3, "book_flight": 0.6, "get_route": 0.3}}


def fake_model(replies: dict, delay: float = 0.05, seen: list | None = None):
    """Answers by utterance; a reply can be an Exception to raise."""
    async def understand(context: str) -> dict:
        ctx = json.loads(context)
        if seen is not None:
            seen.append(ctx)
        await asyncio.sleep(replies.get("_delay_" + ctx["utterance"], delay))
        r = replies[ctx["utterance"]]
        if isinstance(r, Exception):
            raise r
        return r
    return understand


async def _run(model, turns, settle=1.5):
    actions = []
    s = LiveSession(MANIFEST, on_action=lambda a: actions.append(json.loads(a)), session_date=dt.date(2026, 10, 2),
                    env_config=FAST, llm=model)
    await s.start()
    t_prev = 0.0
    for t, text in turns:
        await asyncio.sleep(t - t_prev)
        t_prev = t
        s.push({"type": "transcript", "text": text, "end_of_turn": True})
    await asyncio.sleep(settle)
    s.close()
    return actions, s.commits(), s.trace


def test_model_understanding_drives_the_tool_call():
    text = "I gotta get from Bombay over to the capital, day after tomorrow works"
    model = fake_model({text: {"act": "task", "tool": "search_flights", "reply": "Looking for flights, Mumbai to Delhi.",
                               "args": {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-04"}}})
    actions, _, _ = asyncio.run(_run(model, [(0.05, text)]))
    call = next(a for a in actions if a["type"] == "tool_call")
    assert call["args"] == {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-04"}
    assert any(a["type"] == "speak" and a["text"] == "Looking for flights, Mumbai to Delhi." for a in actions)
    assert actions[-1]["type"] == "final_response"


def test_correction_cancels_in_the_same_tick_while_the_model_thinks():
    first = "Book the cheapest flight from Pune to Chennai tomorrow for 2 passengers"
    fix = "wait, make it 3 passengers"
    model_first = fake_model({
        first: {"act": "task", "tool": "book_flight", "reply": "Booking for 2.", "prefer": "cheapest",
                "args": {"origin": "Pune", "destination": "Chennai", "date": "2026-10-03", "passengers": 2}},
        fix: {"act": "task", "tool": "book_flight", "args": {"passengers": 3}, "reply": "Okay, 3 passengers."},
        "_delay_" + fix: 0.5})
    actions, commits, trace = asyncio.run(_run(model_first, [(0.05, first), (0.75, fix)], settle=2.5))
    t_fix = next(r["t"] for r in trace if r["dir"] == "in" and r.get("text") == fix)
    cancels = [a for a in trace if a["dir"] == "out" and a["type"] == "cancel"]
    assert cancels and cancels[0]["t"] - t_fix < 0.015, (cancels, t_fix)  # before the model's 0.5 s reply
    assert [c["args"]["passengers"] for c in commits] == [3]


def test_a_turn_said_before_the_last_was_understood_is_understood_with_it():
    a, b = "find flights from Mumbai to Delhi tomorrow", "actually take me to the airport"
    seen = []
    model = fake_model({
        a: {"act": "task", "tool": "search_flights", "args": {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-03"}},
        "_delay_" + a: 0.8,
        f"{a} {b}": {"act": "task", "tool": "get_route", "args": {"destination": "airport"}, "reply": "Routing to the airport."}},
        seen=seen)
    actions, _, _ = asyncio.run(_run(model, [(0.05, a), (0.2, b)]))
    assert seen[-1]["utterance"] == f"{a} {b}"  # the model saw both, so "actually ..." has its context
    assert [c["tool"] for c in actions if c["type"] == "tool_call"] == ["get_route"]  # the stale call never ran


def test_model_failure_falls_back_to_the_rules():
    text = "Take me to the airport"
    actions, _, _ = asyncio.run(_run(fake_model({text: RuntimeError("provider down")}), [(0.05, text)]))
    call = next(a for a in actions if a["type"] == "tool_call")
    assert call["args"] == {"destination": "airport"}


def test_wait_keeps_the_floor_and_does_nothing():
    text = "hmm so I was thinking maybe"
    actions, _, _ = asyncio.run(_run(fake_model({text: {"act": "wait", "reply": ""}}), [(0.05, text)], settle=0.8))
    assert not [a for a in actions if a["type"] in ("tool_call", "clarify", "final_response")]


def test_an_invented_flight_id_is_never_booked():
    text = "Book flight XX-999 from Delhi to Goa tomorrow for 1"
    model = fake_model({text: {"act": "task", "tool": "book_flight", "reply": "Booking that.",
                               "args": {"flight_id": "XX-999", "passengers": 1, "origin": "Delhi",
                                        "destination": "Goa", "date": "2026-10-03"}}})
    actions, commits, _ = asyncio.run(_run(model, [(0.05, text)], settle=2.0))
    assert not any(c["args"].get("flight_id") == "XX-999" for c in commits)
    assert any(a["type"] == "tool_call" and a["tool"] == "search_flights" for a in actions)  # looked it up instead


def test_the_model_sees_state_results_and_conversation():
    a, b = "Find flights from Mumbai to Delhi tomorrow", "book the second one for two"
    seen = []
    model = fake_model({
        a: {"act": "task", "tool": "search_flights", "args": {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-03"}},
        b: {"act": "task", "tool": "book_flight", "args": {"passengers": 2}, "pick": 1, "reply": "Booking the second one."}},
        seen=seen)
    actions, commits, _ = asyncio.run(_run(model, [(0.05, a), (1.0, b)], settle=2.0))
    ctx = seen[-1]
    assert ctx["state"]["task"] == "search_flights" and len(ctx["results"][0]["items"]) == 3
    assert any("user" in m and m["user"] == a for m in ctx["conversation"])
    second = ctx["results"][0]["items"][1]["flight_id"]
    assert [c["args"] for c in commits] == [{"flight_id": second, "passengers": 2}]


def test_a_bare_yes_is_never_taken_as_consent_to_book():
    a = "Find flights from Delhi to Goa tomorrow"
    model = fake_model({  # a model that over-reads "yes" as "book it"
        a: {"act": "task", "tool": "search_flights", "args": {"origin": "Delhi", "destination": "Goa", "date": "2026-10-03"}},
        "yes": {"act": "task", "tool": "book_flight", "args": {"passengers": 1}, "prefer": "cheapest", "reply": "Booking it."}})
    actions, commits, _ = asyncio.run(_run(model, [(0.05, a), (1.0, "yes")], settle=1.5))
    assert not commits and not any(x["type"] == "tool_call" and x["tool"] == "book_flight" for x in actions)
