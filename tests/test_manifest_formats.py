"""The agent reads tool manifests and tool results in other common formats, not only our own.

The official evaluation kit's schema is unknown, so the scenarios with state-changing tools are replayed
with the manifest rewritten three ways (OpenAI function wrappers with no side-effect field at all, MCP-style
inputSchema + readOnlyHint, and read_only flags). Each must score the same and commit exactly once.
"""
import json
from pathlib import Path

import pytest

from audient.agent import RealtimeAgent
from audient.harness.runner import run_scenario
from audient.harness.scorer import score
from audient.protocol import adapt_event, is_state_modifying, normalize_tool

ROOT = Path(__file__).resolve().parents[1]
IDS = ["T02_barge_in_self_correction", "T03_chained_booking", "T04_mid_booking_adjust", "T07_unseen_tool"]


def openai_style(t):  # no side-effect declaration: the agent has to judge by the tool's verb
    return {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}


def mcp_style(t):
    return {"name": t["name"], "description": t["description"], "inputSchema": t["parameters"],
            "annotations": {"readOnlyHint": not is_state_modifying(t)}}


def flag_style(t):
    return {"name": t["name"], "description": t["description"], "parameters": t["parameters"],
            "read_only": not is_state_modifying(t)}


def _run(scn):
    run = run_scenario(scn, lambda: RealtimeAgent(perception=None))
    return score(scn, run), sorted(json.dumps([c["tool"], c["args"]], sort_keys=True) for c in run["commits"])


@pytest.mark.parametrize("sid", IDS)
@pytest.mark.parametrize("fmt", [openai_style, mcp_style, flag_style], ids=["openai", "mcp", "read_only_flag"])
def test_same_outcome_in_other_manifest_formats(sid, fmt):
    scn = json.loads((ROOT / "scenarios" / f"{sid}.json").read_text(encoding="utf-8"))
    want_score, want_commits = _run(scn)
    alt = {**scn, "manifest": [fmt(t) for t in scn["manifest"]]}
    got_score, got_commits = _run(alt)
    assert got_score["score"] == want_score["score"] == 100.0, got_score["checks"]
    assert got_commits == want_commits


def test_state_changing_detection():
    assert is_state_modifying({"name": "book_flight"}) and not is_state_modifying({"name": "search_flights"})
    assert is_state_modifying({"name": "frobnicate_widget"})          # unknown verb: the safe side
    assert not is_state_modifying({"name": "book_flight", "read_only": True})  # a declaration wins over the verb
    assert is_state_modifying({"name": "get_x", "side_effect": True})
    assert is_state_modifying({"name": "x", "annotations": {"readOnlyHint": False}})
    assert not is_state_modifying({"name": "x", "effect": "read-only"})
    assert is_state_modifying({"name": "x", "description": "State-changing: places an order."})
    t = normalize_tool({"type": "function", "function": {"name": "lookupOrder", "parameters": {"properties": {"id": {"type": "string"}}}}})
    assert t["name"] == "lookupOrder" and t["side_effect"] == "read_only"
    assert t["parameters"] == {"type": "object", "properties": {"id": {"type": "string"}}, "required": []}


def test_tool_result_variants():
    ok = adapt_event({"type": "tool_response", "tool_call_id": "c1", "output": {"status": "confirmed"}, "ok": True})
    assert (ok["type"], ok["call_id"], ok["status"], ok["result"]) == ("tool_result", "c1", "ok", {"status": "confirmed"})
    err = adapt_event({"type": "tool_result", "id": "c2", "status": "failed", "error": "timeout"})
    assert (err["call_id"], err["status"]) == ("c2", "error")
    assert adapt_event({"type": "tool_result", "call_id": "c3", "status": "success", "result": {}})["status"] == "ok"
