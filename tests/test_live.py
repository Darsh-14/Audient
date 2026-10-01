import asyncio
import datetime as dt
import json
from pathlib import Path

from audient.live import BridgePerception, LiveSession

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "scenarios" / "manifest_core.json").read_text())
FAST = {"latency": {"search_flights": 0.3, "book_flight": 0.4, "lookup_manual": 0.2}}


async def _session(events, perception=None, settle=1.5):
    actions = []
    s = LiveSession(MANIFEST, on_action=lambda a: actions.append(json.loads(a)), perception=perception,
                    session_date=dt.date(2026, 10, 1), env_config=FAST)
    await s.start()
    t_prev = 0.0
    for t, ev in events:
        await asyncio.sleep(t - t_prev)
        t_prev = t
        s.push(ev)
    await asyncio.sleep(settle)
    s.close()
    return actions, s.commits()


def test_live_mid_booking_correction_single_commit():
    events = [(0.1, {"type": "transcript", "text": "Book the cheapest flight from Pune to Chennai tomorrow for 2 passengers",
                     "end_of_turn": True}),
              (0.55, {"type": "interruption"}),
              (0.6, json.dumps({"type": "transcript", "text": "wait, make it 3 passengers", "end_of_turn": True}))]
    actions, commits = asyncio.run(_session(events))
    kinds = [a["type"] for a in actions]
    assert "cancel" in kinds and kinds[-1] == "final_response"
    assert [c["args"]["passengers"] for c in commits] == [3]


def test_streaming_hypotheses_replace_not_append():
    hyp = lambda text, final=False: {"type": "transcript", "text": text, "end_of_turn": final, "hypothesis": True}
    events = [(0.05, hyp("take me")), (0.1, hyp("take me to the")), (0.15, hyp("take me to the airport")),
              (0.2, hyp("take me to the airport", final=True))]
    actions, _ = asyncio.run(_session(events, settle=0.6))
    calls = [a for a in actions if a["type"] == "tool_call"]
    assert [c["args"] for c in calls] == [{"destination": "airport"}]


def test_bridge_perception_frame_json():
    async def analyze(ref):  # stands in for the browser's OCR bridge
        return json.dumps({"model": "WM-3000", "code": "E42"})
    events = [(0.05, {"type": "frame", "path": "blob:1"}),
              (0.1, {"type": "transcript", "text": "What does this error on my washing machine mean?", "end_of_turn": True})]
    actions, _ = asyncio.run(_session(events, perception=BridgePerception(analyze_frame=analyze), settle=0.8))
    call = next(a for a in actions if a["type"] == "tool_call")
    assert call["args"] == {"device_model": "WM-3000", "error_code": "E42"}
    assert "filter" in actions[-1]["text"]
