"""Compare two FDB-v3 runs recording by recording (standard library only).

    python3 fdb_agent/compare_runs.py reports/fdb/audient_sample30 reports/fdb/gemini2_5_sample30

For each scenario: pass/fail for both runs and why; for the first run's failures, the calls made versus the
calls expected (with argument mismatches), and for the Audient agent its coordination-layer counts (calls run,
reused, cancelled) and when it joined the room relative to the start of the recording.
"""
import json
import re
import sys
from pathlib import Path


def load(run: Path):
    passes = {r["scenario_id"]: r for r in json.load(open(run / "evaluate_pass_rate.json"))["scenario_results"]}
    calls = {r["scenario_id"]: r for r in json.load(open(run / "evaluate_tool_calls.json"))["scenario_results"]}
    rooms, results = {}, {}
    for d in (run / "results").glob("*"):
        sid = re.sub(r"_[0-9a-f]{24}$", "", d.name)
        for f in d.glob("inference_*.json"):
            rooms[sid] = json.load(open(f))
        for f in d.glob("result_*.json"):
            results[sid] = json.load(open(f))
    gates = {}
    log = run / "agent.log"
    if log.exists():
        for line in open(log, encoding="utf-8", errors="ignore"):
            try:  # the agent logs JSON lines; the counters are inside the message text
                line = json.loads(line).get("message", "")
            except ValueError:
                pass
            m = re.search(r"gate stats room=(eval-[0-9a-f]+) (\{[^}]*\})", line)
            if m:
                try:
                    gates[m.group(1)] = json.loads(m.group(2))
                except ValueError:
                    gates[m.group(1)] = m.group(2)
    return passes, calls, rooms, results, gates


def short(x, n=110):
    s = json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
    return s if len(s) <= n else s[: n - 1] + "…"


def main(a: Path, b: Path):
    pa, ca, ra, resa, ga = load(a)
    pb, _, _, _, _ = load(b)
    ids = sorted(set(pa) | set(pb))
    both = sum(1 for i in ids if pa.get(i, {}).get("passed") and pb.get(i, {}).get("passed"))
    only_a = [i for i in ids if pa.get(i, {}).get("passed") and not pb.get(i, {}).get("passed")]
    only_b = [i for i in ids if pb.get(i, {}).get("passed") and not pa.get(i, {}).get("passed")]
    print(f"A = {a.name}   B = {b.name}")
    print(f"both pass: {both} | only A passes: {len(only_a)} {only_a} | only B passes: {len(only_b)} {only_b}\n")
    for i in ids:
        ra_, rb_ = pa.get(i, {}), pb.get(i, {})
        tag = "".join(["A✓" if ra_.get("passed") else "A✗", " ", "B✓" if rb_.get("passed") else "B✗"])
        feats = ",".join(ra_.get("disfluency", [])) + (" rollback" if ra_.get("state_rollback") else "")
        print(f"{tag}  {i:<16} [{feats}]")
        if not ra_.get("passed"):
            print(f"      A: {ra_.get('failure_reason', '?')}")
            m = ca.get(i, {}).get("metrics", {})
            for d in m.get("argument_acc", {}).get("details", []):
                if d.get("score", 1) < 1:
                    print(f"         {d['function']}: expected {short(d.get('expected_args'))} got {short(d.get('actual_args'))}")
            res = resa.get(i, {})
            got = [c["function"] for c in res.get("actual_tool_calls", [])]
            if not got:
                print(f"         no tool calls; agent said: {short(res.get('transcript') or '(nothing)')}")
            side = ra.get(i, {})
            room = side.get("room_name")
            att = side.get("attempts", [])
            ready = att[-1].get("agent_ready_after_start_s") if att else None
            print(f"         room {room}: ready {ready}s after start, attempts {len(att)}, layer {ga.get(room, '?')}")
        if not rb_.get("passed"):
            print(f"      B: {rb_.get('failure_reason', '?')}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
