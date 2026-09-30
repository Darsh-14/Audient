"""One-command benchmark: replays every scenario for Audient and the half-duplex baseline.

    python run_eval.py                 # full suite, both agents
    python run_eval.py --only T02      # scenarios whose id contains T02
    python run_eval.py --agent audient --show T04   # print the timestamped trace of one scenario
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from audient.agent import RealtimeAgent
from audient.baseline import HalfDuplexAgent
from audient.harness.runner import run_scenario
from audient.harness.scorer import aggregate, score
from audient.perception import Perception

ROOT = Path(__file__).resolve().parent


def load_suite(only: str | None) -> list[dict]:
    files = sorted(p for p in (ROOT / "scenarios").glob("*.json") if not p.name.startswith("manifest"))
    suite = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    return [s for s in suite if not only or only in s["id"]]


def fmt_trace(run: dict) -> str:
    lines = []
    for r in run["trace"]:
        body = {k: v for k, v in r.items() if k not in ("t", "dir", "type", "state_snapshot")}
        if r["type"] == "tool_manifest":
            body = {"tools": [t["name"] for t in r["tools"]]}
        snap = r.get("state_snapshot")
        extra = f"  snapshot={{intent:{snap['intent']}, slots:{snap['slots']}, status:{snap['status']}}}" if snap else ""
        lines.append(f"{r['t']:8.4f}s  {'USER/ENV ->' if r['dir'] == 'in' else '<- AGENT  '} {r['type']:<15} "
                     f"{json.dumps(body, ensure_ascii=False)[:230]}{extra}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--agent", choices=["audient", "baseline", "both"], default="both")
    ap.add_argument("--show")
    ap.add_argument("--out", default=str(ROOT / "reports"))
    a = ap.parse_args()
    suite = load_suite(a.show or a.only)
    perception = Perception()
    t0 = time.perf_counter()
    perception.warmup()  # setup/warm-up hook (not part of any scenario timeline)
    print(f"warm-up: {time.perf_counter() - t0:.1f}s (ASR model available: {perception._asr is not None})")
    agents = {"audient": RealtimeAgent, "baseline": HalfDuplexAgent}
    names = ["audient", "baseline"] if a.agent == "both" else [a.agent]
    out = Path(a.out)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    report = {}
    for name in names:
        rows = []
        for scn in suite:
            date = dt.date.fromisoformat(scn.get("session_date", dt.date.today().isoformat()))
            run = run_scenario(scn, lambda: agents[name](perception=perception, session_date=date))
            s = score(scn, run)
            rows.append(s)
            (out / "traces" / f"{name}_{scn['id']}.json").write_text(json.dumps(run, indent=1, default=str), encoding="utf-8")
            if a.show:
                print(f"\n=== {name} :: {scn['id']} ===\n{fmt_trace(run)}")
                for cat, items in s["checks"].items():
                    for n, v in items:
                        print(f"   [{cat:9}] {'PASS' if v is True or v == 1.0 else ('FAIL' if not v else f'{v:.2f}')}  {n}")
        agg = aggregate(rows)
        report[name] = {"aggregate": agg, "scenarios": rows}
        print(f"\n## {name}: weighted score {agg['weighted_score']}/100 (before quality multiplier: "
              f"{agg['weighted_base_score']}) | all checks pass: {agg['scenarios_all_checks_pass']}/{agg['scenarios']} | "
              f"categories % {agg['category_pct']} | "
              f"mean first response {agg['mean_first_response_ms']} ms")
        print(f"{'scenario':34} {'score':>6} {'task':>5} {'intr':>5} {'lat':>5} {'safe':>5} {'q':>4}  first-resp ms")
        for s in rows:
            c = s["categories"]
            f = lambda k: f"{c[k]:.2f}" if k in c else "  n/a"
            print(f"{s['id']:34} {s['score']:6.1f} {f('task'):>5} {f('interrupt'):>5} {f('latency'):>5} "
                  f"{f('safety'):>5} {s['quality']:4.2f}  {s['first_response_ms']}")
    if not a.show:
        (out / "results.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"\nwrote {out / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
