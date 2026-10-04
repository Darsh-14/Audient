"""List the recordings where an FDB-v3 run produced no agent speech ("No Response (silent)"), with timings.

    python3 fdb_agent/silent_report.py reports/fdb/audient_fix1 [reports/fdb/gemini2_5]

For each silent recording of the first run: when the user finished speaking and how long the recording is, when the
agent joined, which tool calls ran and when (seconds from the start of the recording), what Audient's coordination
layer did, and whether the second run was silent on the same recording. Standard library only.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def recordings(run: Path):
    out = {}
    for d in sorted((run / "results").glob("*")):
        res = next((json.load(open(f)) for f in d.glob("result_*.json")), {})
        side = next((json.load(open(f)) for f in d.glob("inference_*.json")), {})
        out[d.name] = (res, side)
    return out


def silent(res):
    return not (res.get("transcript") or "").strip() and not res.get("asr_chunks")


def tool_calls(run: Path):
    calls = defaultdict(list)
    log = run / "agent_tool_calls.log"
    if log.exists():
        for line in open(log, encoding="utf-8", errors="ignore"):
            try:
                e = json.loads(line)
                calls[e["room"]].append(e["call"])
            except (ValueError, KeyError):
                pass
    return calls


def when(entry):
    """Unix time of a JSON log line, if it has one."""
    t = entry.get("timestamp") or entry.get("time") if isinstance(entry, dict) else None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def agent_log(run: Path):
    """Per room: the layer's counters, and the agent's own events (calls issued, user speech start/end) and
    warnings/errors, each with its time when the log line carries one."""
    gates, events = {}, defaultdict(list)
    log = run / "agent.log"
    if log.exists():
        for raw in open(log, encoding="utf-8", errors="ignore"):
            try:
                entry = json.loads(raw)
                line, level = entry.get("message", ""), str(entry.get("level", ""))
            except ValueError:
                entry, line, level = None, raw, ""
            m = re.search(r"gate stats room=(eval-[0-9a-f]+) (\{[^}]*\})", line)
            if m:
                gates[m.group(1)] = m.group(2)
                continue
            m = re.search(r"AUDIENT (call issued|user speech start|user speech end) room=(eval-[0-9a-f]+) ?(\w*)", line)
            if m:
                events[m.group(2)].append((when(entry), f"{m.group(1)} {m.group(3)}".strip()))
                continue
            m = re.search(r"eval-[0-9a-f]+", raw)
            if m and (level.upper() in ("WARNING", "ERROR", "CRITICAL") or re.search(r"\b(ERROR|WARNING|Traceback)\b", raw)):
                events[m.group(0)].append((when(entry), "!! " + line.strip()[:150]))
    return gates, events


def main(a: Path, b: Path = None):
    ra, calls = recordings(a), tool_calls(a)
    gates, events = agent_log(a)
    rb = recordings(b) if b else {}
    quiet = [k for k, (res, _) in ra.items() if silent(res)]
    print(f"{a.name}: {len(quiet)} silent of {len(ra)}" +
          (f";  {b.name}: {sum(silent(r) for r, _ in rb.values())} silent of {len(rb)}" if b else ""))
    both = [k for k in quiet if k in rb and silent(rb[k][0])]
    if b:
        print(f"silent in both runs: {len(both)} {[k[:-25] for k in both]}")
    print()
    for k in quiet:
        res, side = ra[k]
        room, start = side.get("room_name") or res.get("room_name"), side.get("stream_start_time")
        att = side.get("attempts", [])
        lat = res.get("latency") or {}
        ran = calls.get(room, [])
        when = [f"{c['function']}@{c['timestamp_start'] - start:.1f}s" for c in ran if start] or ["none"]
        other = ("" if not b else " (other run silent too)" if k in both else
                 f" (other run spoke at {rb[k][0].get('audio_agent_speech_start')}s)" if k in rb else "")
        print(f"{k[:-25]:<14} user done {res.get('user_speech_end_rel')}s of {lat.get('input_duration_s')}s, "
              f"ready {att[-1].get('agent_ready_after_start_s') if att else '?'}s, attempts {len(att)}{other}")
        print(f"   calls run: {', '.join(when)}")
        print(f"   layer: {gates.get(room, '?')}")
        for t, what in events.get(room, []):
            at = f"{t - start:6.1f}s" if t and start else "     ?"
            if what.startswith("!!") or not what.startswith("user speech"):
                print(f"   {at} {what}")
        speech = [t - start for t, w in events.get(room, []) if w.startswith("user speech end") and t and start]
        n = sum(w.startswith("user speech") for _, w in events.get(room, []))
        print(f"   voice detector: {n} events" + (f", last end of speech at {speech[-1]:.1f}s" if speech else ""))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
