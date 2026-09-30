import asyncio, time
from audient.harness.runner import run_scenario
from audient.protocol import validate_action

MANIFEST = [{"name": "search_flights", "description": "Search flights", "side_effect": "read_only",
             "parameters": {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "date": {"type": "string"}},
                            "required": ["origin", "destination", "date"]}}]

class Dummy:
    async def run(self, inbox, outbox):
        while True:
            ev = await inbox.get()
            if ev["type"] == "transcript" and ev.get("end_of_turn"):
                outbox.put_nowait({"type": "tool_call", "call_id": "c1", "tool": "search_flights",
                                   "args": {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-02"}, "idempotency_key": None})
            if ev["type"] == "tool_result":
                outbox.put_nowait({"type": "final_response", "text": "found", "state_snapshot": {"version": 1, "intent": "search_flights", "slots": {}, "status": "completed"}})

def test_virtual_clock_and_env():
    scn = {"id": "smoke", "manifest": MANIFEST, "events": [{"t": 0.5, "type": "transcript", "text": "flights", "end_of_turn": True}]}
    w = time.perf_counter()
    r = run_scenario(scn, Dummy)
    wall = time.perf_counter() - w
    outs = [e for e in r["trace"] if e["dir"] == "out"]
    res = [e for e in r["trace"] if e["type"] == "tool_result"][0]
    print("trace:", [(e["t"], e["dir"], e["type"]) for e in r["trace"]], "wall", round(wall, 3))
    assert abs(outs[0]["t"] - 0.5) < 0.05          # call issued at event time
    assert abs(res["t"] - 2.1) < 0.05               # 1.6 s mock latency on virtual clock
    assert wall < 1.0                                # idle time skipped
    seen = set()
    assert validate_action({k: v for k, v in outs[0].items() if k not in ("t", "dir")}, {t["name"]: t for t in MANIFEST}, seen) == []

if __name__ == "__main__":
    test_virtual_clock_and_env(); print("PHASE1 OK")
