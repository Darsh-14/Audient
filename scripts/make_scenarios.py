"""Generate the public-style scenario suite (text + visual), FDB-v3 disfluency classes included."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "scenarios"
CORE = json.loads((S / "manifest_core.json").read_text())
RESERVE = {"name": "reserve_table", "description": "Reserve a restaurant table", "side_effect": "state_modifying",
           "parameters": {"type": "object", "properties": {
               "restaurant": {"type": "string", "description": "restaurant name"},
               "party_size": {"type": "integer", "description": "number of guests"},
               "date": {"type": "string", "format": "date"},
               "time": {"type": "string", "description": "HH:MM"}},
               "required": ["restaurant", "party_size", "time"]}}


def T(t, text, eot=True, **kw):
    return {"t": t, "type": "transcript", "text": text, "end_of_turn": eot, **kw}


def I(t):
    return {"t": t, "type": "interruption"}


def F(t, name):
    return {"t": t, "type": "frame", "path": f"assets/frames/{name}"}


SC = [
    dict(id="T01_basic_search", modality="text", tags=["baseline_flow"],
         events=[T(0.5, "Find me flights from Mumbai to Delhi tomorrow for two people")],
         expect={"calls": [{"tool": "search_flights", "args": {"origin": "Mumbai", "destination": "Delhi",
                                                               "date": "2026-10-02", "passengers": 2}}],
                 "snapshot": {"intent": "search_flights", "slots": {"destination": "Delhi"}, "status": "completed"},
                 "response_contains": ["Delhi"]}),
    dict(id="T02_barge_in_self_correction", modality="text", tags=["self_correction", "interruption"],
         events=[T(0.5, "Search flights from Mumbai to Delhi on Friday"), I(1.2), T(1.3, "Actually, make it Bangalore")],
         expect={"cancel": [{"tool": "search_flights", "args": {"destination": "Delhi"}, "by": 1.3}],
                 "no_calls": [{"tool": "search_flights", "args": {"destination": "Delhi"}, "after": 1.3}],
                 "calls": [{"tool": "search_flights", "args": {"origin": "Mumbai", "destination": "Bangalore",
                                                               "date": "2026-10-02"}}],
                 "snapshot": {"intent": "search_flights", "slots": {"destination": "Bangalore"}, "status": "completed"},
                 "response_contains": ["Bangalore"]}),
    dict(id="T03_chained_booking", modality="text", tags=["chained_calls"],
         events=[T(0.5, "Book the cheapest flight from Delhi to Goa on Saturday for 2 passengers")],
         expect={"calls": [{"tool": "search_flights", "args": {"origin": "Delhi", "destination": "Goa", "date": "2026-10-03"}},
                           {"tool": "book_flight", "args": {"passengers": 2}}],
                 "commits": {"book_flight": 1}, "snapshot": {"intent": "book_flight", "status": "completed"},
                 "response_contains": ["BK"]}),
    dict(id="T04_mid_booking_adjust", modality="text", tags=["interruption", "no_double_booking"],
         events=[T(0.5, "Book the cheapest flight from Pune to Chennai tomorrow for 2 passengers"), I(2.5),
                 T(2.6, "wait, make it 3 passengers")],
         expect={"cancel": [{"tool": "book_flight", "args": {"passengers": 2}, "by": 2.6}],
                 "no_calls": [{"tool": "book_flight", "args": {"passengers": 2}, "after": 2.6}],
                 "commits": {"book_flight": 1}, "commit_args": [{"tool": "book_flight", "args": {"passengers": 3}}],
                 "snapshot": {"intent": "book_flight", "slots": {"passengers": 3}, "status": "completed"},
                 "response_contains": ["BK"]}),
    dict(id="T05_clarify_missing_slot", modality="text", tags=["clarification", "slot_tracking"],
         events=[T(0.5, "I need a flight to Kolkata"), T(3.0, "From Hyderabad, on Monday")],
         expect={"clarify": True, "no_tool_before": 3.0,
                 "calls": [{"tool": "search_flights", "args": {"origin": "Hyderabad", "destination": "Kolkata",
                                                               "date": "2026-10-05"}}],
                 "snapshot": {"intent": "search_flights", "slots": {"origin": "Hyderabad", "destination": "Kolkata"},
                              "status": "completed"},
                 "response_contains": ["Kolkata"]}),
    dict(id="T06_retry_after_fault", modality="text", tags=["retries", "fault_injection"],
         env={"faults": {"search_flights": [1]}},
         events=[T(0.5, "Show me flights from Chennai to Kochi on Sunday")],
         expect={"calls": [{"tool": "search_flights", "args": {"origin": "Chennai", "destination": "Kochi",
                                                               "date": "2026-10-04"}}],
                 "snapshot": {"intent": "search_flights", "status": "completed"}, "response_contains": ["Kochi"]}),
    dict(id="T07_unseen_tool", modality="text", tags=["unseen_tool", "schema_driven"], extra_tools=[RESERVE],
         events=[T(0.5, "Reserve a table for four at Olive Garden at 8 pm tonight")],
         expect={"calls": [{"tool": "reserve_table", "args": {"restaurant": "Olive Garden", "party_size": 4, "time": "20:00"}}],
                 "commits": {"reserve_table": 1}, "snapshot": {"intent": "reserve_table", "status": "completed"},
                 "response_contains": ["RSV"]}),
    dict(id="T08_user_cancel", modality="text", tags=["interruption", "cancellation"],
         events=[T(0.5, "Take me to the airport"), I(0.9), T(1.0, "never mind, cancel that")],
         expect={"cancel": [{"tool": "get_route", "args": {"destination": "airport"}, "by": 1.0}],
                 "no_calls": [{"tool": "get_route", "args": {}, "after": 1.0}],
                 "snapshot": {"status": "cancelled"}}),
    dict(id="T09_acoustic_pause_filler", modality="text", tags=["acoustic_pause", "filler"],
         events=[T(0.5, "I want to fly from Mumbai to, um...", measure=False), T(1.1, "Chennai on Sunday")],
         expect={"no_tool_before": 1.1, "no_clarify_before": 1.1,
                 "calls": [{"tool": "search_flights", "args": {"origin": "Mumbai", "destination": "Chennai",
                                                               "date": "2026-10-04"}}],
                 "snapshot": {"intent": "search_flights", "status": "completed"}, "response_contains": ["Chennai"]}),
    dict(id="T10_stutter_false_start", modality="text", tags=["stutter", "false_start"],
         events=[T(0.5, "Can you, uh, I- I need to b- book, no, just f-f-find flights from Pune to Goa tomorrow")],
         expect={"calls": [{"tool": "search_flights", "args": {"origin": "Pune", "destination": "Goa", "date": "2026-10-02"}}],
                 "commits": {"book_flight": 0}, "snapshot": {"intent": "search_flights", "status": "completed"},
                 "response_contains": ["Goa"]}),
    dict(id="T11_speculative_prefetch", modality="text", tags=["speculative_execution", "latency_hiding"],
         events=[T(0.3, "Find flights from Mumbai to Delhi tomorrow", eot=False), T(1.0, "in the evening if possible")],
         expect={"calls": [{"tool": "search_flights", "args": {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-02"}}],
                 "final_by": 2.2, "snapshot": {"intent": "search_flights", "status": "completed"},
                 "response_contains": ["Delhi"]}),
    dict(id="T12_in_car_destination_change", modality="text", tags=["interruption", "self_correction", "in_car"],
         events=[T(0.5, "Navigate to the office"), I(0.9), T(1.0, "no wait, take me to the airport instead")],
         expect={"cancel": [{"tool": "get_route", "args": {"destination": "office"}, "by": 1.0}],
                 "no_calls": [{"tool": "get_route", "args": {"destination": "office"}, "after": 1.0}],
                 "calls": [{"tool": "get_route", "args": {"destination": "airport"}}],
                 "snapshot": {"intent": "get_route", "slots": {"destination": "airport"}, "status": "completed"},
                 "response_contains": ["airport"]}),
    dict(id="V01_frame_error_code", modality="visual", tags=["multimodal", "ocr"],
         events=[F(0.2, "wm3000_e42.png"), T(1.0, "What does this error on my washing machine mean?")],
         expect={"calls": [{"tool": "lookup_manual", "args": {"device_model": "WM-3000", "error_code": "E42"}}],
                 "snapshot": {"intent": "lookup_manual", "status": "completed"}, "response_contains": ["filter"]}),
    dict(id="V02_frame_too_dark", modality="visual", tags=["multimodal", "ambiguous_perception"],
         events=[F(0.2, "wm3000_dark.png"), T(1.0, "What does the error on the display mean?")],
         expect={"clarify": True, "no_tool_before": 99, "snapshot": {"intent": "lookup_manual", "status": "clarifying"}}),
    dict(id="V03_multimodal_self_repair", modality="visual", tags=["multimodal", "self_correction", "interruption"],
         events=[F(0.2, "wm3000_e20_red.png"), T(1.0, "My washer shows error E-20, what should I do?"), I(1.5),
                 F(1.5, "wm3000_blue_led.png"),
                 T(1.6, "wait, no, the red light stopped, now it's blinking blue twice")],
         expect={"cancel": [{"tool": "lookup_manual", "args": {"error_code": "E20"}, "by": 1.6}],
                 "no_calls": [{"tool": "lookup_manual", "args": {"error_code": "E20"}, "after": 1.6}],
                 "calls": [{"tool": "lookup_manual", "args": {"device_model": "WM-3000", "indicator": "blue_blinking_2"}}],
                 "snapshot": {"intent": "lookup_manual", "slots": {"indicator": "blue_blinking_2"}, "status": "completed"},
                 "response_contains": ["valve"]}),
]

if __name__ == "__main__":
    for sc in SC:
        sc["session_date"] = "2026-10-01"
        sc["manifest"] = CORE + sc.pop("extra_tools", [])
        (S / f"{sc['id']}.json").write_text(json.dumps(sc, indent=1))
    print(len(SC), "scenarios:", ", ".join(s["id"] for s in SC))
