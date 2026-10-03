"""Free-form evaluation: unscripted requests through the live agent, checked against expected outcomes.

The benchmark (run_eval.py) replays the 15 scenarios the agent was built around. This suite asks the
question a user would: what happens with requests nobody wrote a scenario for?

    python scripts/eval_freeform.py                  # dev, held-out and blind sets
    python scripts/eval_freeform.py --set dev -v     # one set, print every reply

Each case runs in its own LiveSession (the same agent, mock tools and manifest as the web app),
with the session date fixed to 2026-10-02 so dates in the expectations are stable.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audient.live import LiveSession  # noqa: E402
from audient.protocol import validate_action  # noqa: E402

SESSION_DATE = dt.date(2026, 10, 2)
LATENCY = {t: 0.6 for t in ("search_flights", "book_flight", "create_ticket", "lookup_manual", "get_route", "reserve_table",
                             "get_weather", "set_alarm", "set_timer", "set_reminder", "add_calendar_event", "send_message",
                             "make_call", "play_music", "control_device", "book_cab", "take_note")}


def web_manifest() -> list[dict]:
    """The tool manifest the web app loads (core tools + the 'unseen' tools from T07)."""
    core = json.loads((ROOT / "scenarios" / "manifest_core.json").read_text(encoding="utf-8"))
    unseen = json.loads((ROOT / "scenarios" / "T07_unseen_tool.json").read_text(encoding="utf-8"))["manifest"]
    return core + [t for t in unseen if t["name"] not in {m["name"] for m in core}]


def _match(args: dict, want: dict) -> bool:
    return all(str(args.get(k, "")).lower() == str(v).lower() for k, v in want.items())


def real_model(stats: dict):
    """The language model from .env / environment (api/understand.py), with every call and failure counted."""
    sys.path.insert(0, str(ROOT / "api"))
    import understand

    async def call(context: str) -> dict:
        try:
            out = await asyncio.to_thread(understand.understand, json.loads(context))
        except asyncio.CancelledError:
            stats["cancelled"].append("timed out or replaced by a newer turn")
            raise
        except Exception as e:
            stats["failed"].append(f"{type(e).__name__}: {str(e)[:80]}")
            raise  # the agent falls back to its rules for this turn
        stats["ms"].append(out["ms"])
        return out["result"]
    return call


async def run_case(case: dict, manifest: list[dict], model: bool = False) -> dict:
    stats = {"ms": [], "failed": [], "cancelled": []}
    s = LiveSession(manifest, on_action=lambda a: None, session_date=SESSION_DATE, env_config={"latency": LATENCY},
                    llm=real_model(stats) if model else None)
    await s.start()
    await asyncio.sleep(0.02)
    gap, settle = (case.get("gap", 5.0), 6.0) if model else (case.get("gap", 2.0), 2.5)
    for i, text in enumerate(case["turns"]):
        s.push({"type": "transcript", "text": text, "end_of_turn": True})
        await asyncio.sleep(gap if i < len(case["turns"]) - 1 else settle)
    s.close()
    trace, commits = s.trace, s.commits()
    outs = [r for r in trace if r["dir"] == "out"]
    calls = {r["call_id"]: r for r in outs if r["type"] == "tool_call"}
    cancelled = {r["call_id"] for r in outs if r["type"] == "cancel"}
    ok_results = {r["call_id"] for r in trace if r["dir"] == "in" and r["type"] == "tool_result" and r.get("status") == "ok"}
    spoken = [r for r in outs if r["type"] in ("speak", "final_response", "clarify")]  # the last thing it said
    last = spoken[-1]["text"] if spoken else ""
    exp, fails = case["expect"], []

    for w in exp.get("calls", []):
        if not any(c["tool"] == w["tool"] and _match(c["args"], w["args"]) and cid not in cancelled and cid in ok_results
                   for cid, c in calls.items()):
            fails.append(f"no effective call {w['tool']} {w['args']}")
    for tool, n in exp.get("commits", {}).items():
        got = sum(1 for c in commits if c["tool"] == tool)
        if got != n:
            fails.append(f"{tool} committed {got}x, expected {n}")
    for w in exp.get("commit_args", []):
        if not any(c["tool"] == w["tool"] and _match(c["args"], w["args"]) for c in commits):
            fails.append(f"no commit {w['tool']} {w['args']}")
    for w in exp.get("no_call_args", []):
        if any(c["tool"] == w["tool"] and _match(c["args"], w["args"]) for c in calls.values()):
            fails.append(f"unwanted call {w['tool']} {w['args']}")
    if exp.get("no_tool") and calls:
        fails.append(f"called {[c['tool'] for c in calls.values()]} for an out-of-scope request")
    if "max_calls" in exp and len(calls) > exp["max_calls"]:
        fails.append(f"{len(calls)} tool calls, expected at most {exp['max_calls']}")
    for sub in exp.get("contains", []):
        if sub.lower() not in last.lower():
            fails.append(f"last reply lacks '{sub}'")
    for sub in exp.get("not_contains", []):
        if sub.lower() in last.lower():
            fails.append(f"last reply contains '{sub}'")
    said = [r["text"] for r in outs if r["type"] in ("speak", "clarify", "final_response")]
    if exp.get("no_repeat") and len(said) != len(set(said)):
        fails.append("repeated an identical reply")
    seen: set[str] = set()
    tools = {t["name"]: t for t in manifest}
    perr = [e for r in outs for e in validate_action({k: v for k, v in r.items() if k not in ("t", "dir", "env")}, tools, seen)]
    if perr:
        fails.append(f"protocol errors {perr[:2]}")

    lines = []
    for r in trace:
        if r["dir"] == "in" and r["type"] == "transcript":
            lines.append(f"USER  {r['text']}")
        elif r["dir"] == "out" and r["type"] in ("speak", "clarify", "final_response"):
            lines.append(f"{r['type'][:5].upper():5} {r['text']}")
        elif r["dir"] == "out" and r["type"] == "tool_call":
            lines.append(f"CALL  {r['tool']} {json.dumps(r['args'], ensure_ascii=False)}")
        elif r["dir"] == "out" and r["type"] == "cancel":
            lines.append(f"CANCL {r['call_id']} ({r['reason']})")
    return {"id": case["id"], "pass": not fails, "fails": fails, "transcript": lines,
            "model_ms": stats["ms"], "model_failed": stats["failed"], "model_cancelled": stats["cancelled"]}


async def run_set(path: Path, model: bool = False, parallel: int = 1) -> dict:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("manifest") == "full" or os.environ.get("FREEFORM_MANIFEST") == "full":  # the web app's full tool set
        from build_web import full_manifest
        manifest = full_manifest()
    else:
        manifest = web_manifest()
    gate = asyncio.Semaphore(parallel if model else 1000)  # stay under the provider's rate limit

    async def one(c):
        async with gate:
            return await run_case(c, manifest, model)
    results = await asyncio.gather(*(one(c) for c in spec["cases"]))
    out = {"set": spec["name"], "passed": sum(r["pass"] for r in results), "total": len(results), "cases": results}
    if model:
        ms = sorted(x for r in results for x in r["model_ms"])
        out["model"] = {"calls": len(ms), "failed_calls": sum(len(r["model_failed"]) for r in results),
                        "cases_with_a_fallback": sum(1 for r in results if r["model_failed"]),
                        "calls_cut_off": sum(len(r["model_cancelled"]) for r in results),
                        "median_ms": ms[len(ms) // 2] if ms else None, "p90_ms": ms[int(len(ms) * 0.9)] if ms else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="dev,heldout,blind", help="comma-separated: dev, heldout, blind")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--model", action="store_true", help="understand with the language model from .env (real API calls)")
    ap.add_argument("--out", default=str(ROOT / "reports" / "freeform.json"))
    a = ap.parse_args()
    names = [n.strip() for n in a.set.split(",") if n.strip()]
    report = {}
    for name in names:
        path = Path(name) if name.endswith(".json") else ROOT / "tests" / "freeform" / f"{name}.json"
        res = asyncio.run(run_set(path, a.model))
        report[name] = res
        print(f"\n== {name}: {res['passed']}/{res['total']} passed" + (f"  | model: {res['model']}" if a.model else ""))
        for c in res["cases"]:
            fb = f"  [model failed {len(c['model_failed'])}x: rules took over]" if c.get("model_failed") else ""
            fb += f"  [model cut off {len(c['model_cancelled'])}x]" if c.get("model_cancelled") else ""
            print(f"  {'PASS' if c['pass'] else 'FAIL'}  {c['id']:<4} {'; '.join(c['fails'])}{fb}")
            if a.verbose or not c["pass"]:
                for line in c["transcript"]:
                    print(f"          {line[:170]}")
    Path(a.out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
