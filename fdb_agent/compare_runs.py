"""Compare two FDB-v3 runs scenario by scenario (standard library only).

    python3 fdb_agent/compare_runs.py reports/fdb/audient reports/fdb/gemini2_5

A scenario can have several recordings (different speakers), so each line shows how many of its recordings
each run passed ("A 1/2  B 2/2"). For the first run's failures it shows why, the argument mismatches, and, for
the Audient agent, what its coordination layer did in each recording (calls run / reused / deferred /
dropped) and when it joined the room relative to the start of the recording.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def load(run: Path):
    passes, calls = defaultdict(list), defaultdict(list)
    for r in json.load(open(run / "evaluate_pass_rate.json"))["scenario_results"]:
        passes[r["scenario_id"]].append(r)
    for r in json.load(open(run / "evaluate_tool_calls.json"))["scenario_results"]:
        calls[r["scenario_id"]].append(r)
    recordings = defaultdict(list)   # scenario -> [(folder, inference sidecar, result)]
    for d in sorted((run / "results").glob("*")):
        sid = re.sub(r"_[0-9a-f]{24}$", "", d.name)
        side = next((json.load(open(f)) for f in d.glob("inference_*.json")), {})
        res = next((json.load(open(f)) for f in d.glob("result_*.json")), {})
        recordings[sid].append((d.name, side, res))
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
    return passes, calls, recordings, gates


def short(x, n=110):
    s = json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
    return s if len(s) <= n else s[: n - 1] + "…"


def main(a: Path, b: Path):
    pa, ca, reca, ga = load(a)
    pb, _, _, _ = load(b)
    ids = sorted(set(pa) | set(pb))
    na = sum(r["passed"] for v in pa.values() for r in v)
    nb = sum(r["passed"] for v in pb.values() for r in v)
    ta = sum(len(v) for v in pa.values())
    tb = sum(len(v) for v in pb.values())
    better = [i for i in ids if sum(r["passed"] for r in pa.get(i, [])) > sum(r["passed"] for r in pb.get(i, []))]
    worse = [i for i in ids if sum(r["passed"] for r in pa.get(i, [])) < sum(r["passed"] for r in pb.get(i, []))]
    print(f"A = {a.name}: {na}/{ta} recordings pass   B = {b.name}: {nb}/{tb} recordings pass")
    print(f"scenarios where A passes more recordings: {len(better)} {better}")
    print(f"scenarios where B passes more recordings: {len(worse)} {worse}\n")
    for i in ids:
        ra, rb = pa.get(i, []), pb.get(i, [])
        sa, sb = sum(r["passed"] for r in ra), sum(r["passed"] for r in rb)
        feats = ",".join((ra or rb)[0].get("disfluency", [])) + (" rollback" if (ra or rb)[0].get("state_rollback") else "")
        mark = "+" if sa > sb else "-" if sa < sb else " "
        print(f"{mark} A {sa}/{len(ra)}  B {sb}/{len(rb)}  {i:<16} [{feats}]")
        for r in ra:
            if not r["passed"]:
                print(f"      A: {r.get('failure_reason', '?')}")
        for m in ca.get(i, []):
            for d in m.get("metrics", {}).get("argument_acc", {}).get("details", []):
                if d.get("score", 1) < 1:
                    print(f"         {d['function']}: expected {short(d.get('expected_args'))} got {short(d.get('actual_args'))}")
        if sa < len(ra):
            for folder, side, res in reca.get(i, []):
                att = side.get("attempts", [])
                ready = att[-1].get("agent_ready_after_start_s") if att else None
                said = res.get("transcript") or "(nothing)"
                print(f"         {folder[-24:-16]}: ready {ready}s, layer {ga.get(side.get('room_name'), '?')}, said: {short(said, 80)}")
        for r in rb:
            if not r["passed"]:
                print(f"      B: {r.get('failure_reason', '?')}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
