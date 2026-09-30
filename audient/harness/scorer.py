"""Trace-based scorer mirroring the Theme 05 framework (our reconstruction, not the official kit).

Task completion 40 | Interruption recovery 35 | Response latency 15 | Safety & protocol 10,
times a 0.8-1.2 quality multiplier; multimodal scenarios weigh 1.5x in the aggregate.
Categories with no applicable checks are dropped and the weights renormalised.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..protocol import is_state_modifying, validate_action

WEIGHTS = {"task": 40, "interrupt": 35, "latency": 15, "safety": 10}
GRACE_S = 0.015        # cancellation grace period
FLOOR_S = 0.250        # floor-management target for the first substantive response
LATENCY_ZERO_S = 2.0   # linear decay to zero credit
SUBSTANTIVE = {"clarify", "final_response"}
CLAIM_RE = re.compile(r"\b(booked|confirmed|reserved|created|done)\b", re.I)


def _match(args: dict, want: dict) -> bool:
    return all(str(args.get(k, "")).lower() == str(v).lower() for k, v in want.items())


def score(scn: dict, run: dict) -> dict[str, Any]:
    exp = scn.get("expect", {})
    tools = {t["name"]: t for t in scn["manifest"]}
    trace = run["trace"]
    outs = [r for r in trace if r["dir"] == "out"]
    calls = {r["call_id"]: r for r in outs if r["type"] == "tool_call"}
    cancels = {r["call_id"]: r["t"] for r in outs if r["type"] == "cancel"}
    results = {r["call_id"]: r for r in trace if r["dir"] == "in" and r["type"] == "tool_result"}
    finals = [r for r in outs if r["type"] in ("final_response", "clarify")]
    checks: dict[str, list[tuple[str, bool]]] = {k: [] for k in WEIGHTS}

    # ---- task completion
    for w in exp.get("calls", []):
        ok = any(c["tool"] == w["tool"] and _match(c["args"], w["args"]) and cid not in cancels
                 and results.get(cid, {}).get("status") == "ok" for cid, c in calls.items())
        checks["task"].append((f"effective call {w['tool']} {w['args']}", ok))
    for tool, n in exp.get("commits", {}).items():
        got = sum(1 for c in run["commits"] if c["tool"] == tool)
        checks["task"].append((f"{tool} committed exactly {n}x (got {got})", got == n))
    for w in exp.get("commit_args", []):
        checks["task"].append((f"commit {w['tool']} {w['args']}",
                               any(c["tool"] == w["tool"] and _match(c["args"], w["args"]) for c in run["commits"])))
    last = finals[-1] if finals else None
    snap = (last or {}).get("state_snapshot") or {}
    if "snapshot" in exp:
        want = exp["snapshot"]
        ok = last is not None and all(snap.get(k) == v for k, v in want.items() if k != "slots") and \
            _match(snap.get("slots", {}), want.get("slots", {}))
        checks["task"].append((f"final snapshot {want}", ok))
    for s in exp.get("response_contains", []):
        checks["task"].append((f"response grounded ('{s}')", last is not None and s.lower() in last["text"].lower()))
    if exp.get("clarify"):
        checks["task"].append(("asked a clarification", any(r["type"] == "clarify" for r in outs)))
    if "final_by" in exp:
        fr = [r["t"] for r in outs if r["type"] == "final_response"]
        checks["task"].append((f"final answer by t={exp['final_by']}s", bool(fr) and fr[0] <= exp["final_by"]))

    # ---- interruption recovery
    for w in exp.get("cancel", []):
        hit = [cid for cid, c in calls.items() if c["tool"] == w["tool"] and _match(c["args"], w["args"])
               and c["t"] <= w["by"] + 1e-9]
        lat = [cancels[cid] - w["by"] for cid in hit if cid in cancels]
        ok = bool(lat) and all(-5.0 <= x <= GRACE_S for x in lat)
        checks["interrupt"].append((f"cancelled {w['tool']} {w['args']} within {GRACE_S * 1000:.0f} ms "
                                    f"(got {[round(x * 1000, 2) for x in lat]} ms)", ok))
    for w in exp.get("no_calls", []):
        stale = [cid for cid, c in calls.items() if c["tool"] == w["tool"] and _match(c["args"], w["args"])
                 and c["t"] > w["after"] + 1e-9]
        checks["interrupt"].append((f"no stale re-run of {w['tool']} {w['args']}", not stale))
    if "no_tool_before" in exp:
        early = [c for c in calls.values() if c["t"] < exp["no_tool_before"] - 1e-9]
        checks["interrupt"].append((f"no premature tool call before t={exp['no_tool_before']}", not early))
    if "no_clarify_before" in exp:
        early = [r for r in outs if r["type"] == "clarify" and r["t"] < exp["no_clarify_before"]]
        checks["interrupt"].append(("did not cut the user off during the pause", not early))
    if exp.get("cancel") and "snapshot" in exp:
        checks["interrupt"].append(("snapshot reflects the correction", checks["task"] and
                                    any(n.startswith("final snapshot") and ok for n, ok in checks["task"])))

    # ---- latency: first substantive spoken action after each user turn end / interruption
    lats = []
    inputs = [r for r in trace if r["dir"] == "in" and (
        (r["type"] == "transcript" and r.get("end_of_turn", True) and r.get("measure", True)))]
    for ev in inputs:
        nxt = [r["t"] for r in outs if r["t"] >= ev["t"] - 1e-9 and (
            r["type"] in SUBSTANTIVE or (r["type"] == "speak" and r["kind"] in ("ack", "progress")))]
        lats.append(nxt[0] - ev["t"] if nxt else None)
    for lat in lats:
        credit = 0.0 if lat is None else (1.0 if lat <= FLOOR_S else max(0.0, 1 - (lat - FLOOR_S) / (LATENCY_ZERO_S - FLOOR_S)))
        checks["latency"].append((f"first response {'none' if lat is None else f'{lat * 1000:.1f} ms'}", credit))

    # ---- safety & protocol
    seen: set[str] = set()
    errs = []
    for r in outs:
        a = {k: v for k, v in r.items() if k not in ("t", "dir")}
        errs += validate_action(a, tools, seen)
    checks["safety"].append((f"protocol errors: {errs[:3]}", not errs))
    keys = [json.dumps([c["tool"], c["args"]], sort_keys=True) for c in run["commits"]]
    checks["safety"].append(("zero duplicate state-changing commits", len(keys) == len(set(keys))))
    sm_live = [c for cid, c in calls.items() if is_state_modifying(tools.get(c["tool"], {}))]
    dup_calls = [k for k in {json.dumps([c["tool"], c["args"]], sort_keys=True) for c in sm_live}
                 if sum(1 for c in sm_live if json.dumps([c["tool"], c["args"]], sort_keys=True) == k
                        and results.get(c["call_id"], {}).get("status") == "ok") > 1]
    checks["safety"].append(("no duplicate state-changing calls", not dup_calls))
    for tool, n in exp.get("commits", {}).items():  # double-booking: more commits than the user asked for
        got = sum(1 for c in run["commits"] if c["tool"] == tool)
        checks["safety"].append((f"no double {tool} ({got} commits, expected {n})", got <= n))

    # ---- quality multiplier (naturalness, truthfulness, relevance)
    q = 1.0
    first_commit = min((c["t"] for c in run["commits"]), default=None)
    spoken = [r for r in outs if r["type"] in ("speak", "final_response", "clarify")]
    false_claims = [r for r in spoken if CLAIM_RE.search(r["text"]) and (first_commit is None or r["t"] < first_commit)
                    and any(is_state_modifying(t) for t in tools.values()) and r.get("state_snapshot", {}).get("status") != "cancelled"
                    and "nothing" not in r["text"].lower() and "haven't" not in r["text"].lower()]
    fillers = sum(1 for r in outs if r["type"] == "speak" and r["kind"] == "filler")
    texts = [r["text"] for r in spoken]
    q -= 0.2 * bool(false_claims)
    q -= 0.1 * (fillers > max(1, len(inputs)))
    q -= 0.1 * (len(texts) != len(set(texts)))
    grounded = last is not None and any(
        str(v).lower() in last["text"].lower() for res in results.values() if res.get("status") == "ok"
        for v in _leaves(res.get("result", {})) if isinstance(v, str) and len(str(v)) > 3)
    q += 0.1 * grounded + 0.1 * (not false_claims and fillers <= 1)
    q = min(1.2, max(0.8, q))

    cats = {}
    for k, items in checks.items():
        if items:
            cats[k] = sum(float(v) for _, v in items) / len(items)
    wsum = sum(WEIGHTS[k] for k in cats)
    base = 100.0 * sum(WEIGHTS[k] * v for k, v in cats.items()) / wsum
    valid_lats = [x for x in lats if x is not None]
    return {"id": scn["id"], "modality": scn.get("modality", "text"), "score": round(min(100.0, base * q), 2),
            "base": round(base, 2), "quality": round(q, 2), "categories": {k: round(v, 3) for k, v in cats.items()},
            "checks": {k: [(n, v) for n, v in items] for k, items in checks.items()},
            "first_response_ms": [None if x is None else round(x * 1000, 2) for x in lats],
            "mean_first_response_ms": round(1000 * sum(valid_lats) / len(valid_lats), 2) if valid_lats else None,
            "cancel_latency_ms": [round((cancels[c] - calls[c]["t"]) * 1000, 2) for c in cancels if c in calls],
            "commits": len(run["commits"]), "false_claims": len(false_claims), "wall_s": run["wall_s"]}


def _leaves(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves(v)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves(v)
    else:
        yield x


def aggregate(scores: list[dict]) -> dict:
    w = [1.5 if s["modality"] in ("visual", "audio") else 1.0 for s in scores]
    total = sum(s["score"] * wi for s, wi in zip(scores, w)) / sum(w)
    cats = {}
    for k in WEIGHTS:
        vals = [s["categories"][k] for s in scores if k in s["categories"]]
        cats[k] = round(100 * sum(vals) / len(vals), 1) if vals else None
    lat = [s["mean_first_response_ms"] for s in scores if s["mean_first_response_ms"] is not None]
    base = sum(s["base"] * wi for s, wi in zip(scores, w)) / sum(w)
    fully = sum(1 for s in scores if all((v is True or v == 1.0) for items in s["checks"].values() for _, v in items))
    return {"weighted_score": round(total, 2), "weighted_base_score": round(base, 2), "scenarios_all_checks_pass": fully,
            "category_pct": cats, "scenarios": len(scores),
            "mean_first_response_ms": round(sum(lat) / len(lat), 2) if lat else None,
            "duplicate_commit_scenarios": sum(1 for s in scores if not all(v for n, v in s["checks"]["safety"][1:])),
            "false_claim_scenarios": sum(1 for s in scores if s["false_claims"])}
