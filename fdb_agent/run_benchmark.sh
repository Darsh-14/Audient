#!/usr/bin/env bash
# Run Full-Duplex-Bench v3 end to end for one agent and save the scores and logs under reports/fdb/<agent>/.
#
#   bash fdb_agent/run_benchmark.sh audient              # Audient (this repo's agent), all 100 recordings
#   bash fdb_agent/run_benchmark.sh gemini2_5            # FDB-v3's own Gemini 2.5 template, unchanged (baseline)
#   bash fdb_agent/run_benchmark.sh audient travel_01    # one scenario only (smoke test)
#
# Steps: start the agent and wait until LiveKit registers it, stream every recording (fdb_runner.py infer),
# stop the agent, run FDB-v3's ASR/latency/tool-call extraction (fdb_runner.py score), then FDB-v3's two
# scorers. The GPT-4o judge (--use-llm) is used when OPENAI_API_KEY is set; otherwise arguments are compared
# exactly (stricter than the organisers' judged run). Run fdb_agent/setup.sh once first.
set -euo pipefail

AGENT="${1:?usage: run_benchmark.sh <audient|gemini2_5|gemini3_1> [example_id]}"
EXAMPLE="${2:-}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
V3="$REPO/external/Full-Duplex-Bench/v3"
PY="$REPO/external/fdb-venv/bin/python"
[ -x "$PY" ] || PY="$REPO/external/fdb-venv/Scripts/python.exe"   # Windows layout
OUT="$REPO/reports/fdb/$AGENT${EXAMPLE:+_$EXAMPLE}"
LOG="/tmp/agent_${AGENT}.log"
export PYTHONUTF8=1 FDB_V3_DIR="$V3"
mkdir -p "$OUT" /tmp
cd "$V3"

echo "== starting agent: $AGENT"
if [ "$AGENT" = audient ]; then
  "$PY" "$REPO/fdb_agent/audient_agent.py" start > "$LOG" 2>&1 &
else
  LK_PROVIDER="$AGENT" "$PY" "$REPO/fdb_agent/run_baseline_agent.py" start > "$LOG" 2>&1 &
fi
AGENT_PID=$!
trap 'kill $AGENT_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 90); do grep -q "registered worker" "$LOG" 2>/dev/null && break; sleep 2; done
grep -q "registered worker" "$LOG" || { echo "agent did not register; see $LOG"; tail -20 "$LOG"; exit 1; }
echo "   registered with LiveKit"

echo "== streaming recordings"
"$PY" "$REPO/fdb_agent/fdb_runner.py" infer --provider "$AGENT" ${EXAMPLE:+--example "$EXAMPLE"} --force
kill $AGENT_PID 2>/dev/null || true; sleep 5

echo "== FDB-v3 ASR, latency and tool-call extraction"
"$PY" "$REPO/fdb_agent/fdb_runner.py" score --provider "$AGENT" ${EXAMPLE:+--example "$EXAMPLE"}

echo "== FDB-v3 scorers"
JUDGE=(); [ -n "${OPENAI_API_KEY:-}" ] && JUDGE=(--use-llm)
for s in evaluate_tool_calls evaluate_pass_rate; do
  "$PY" "$s.py" --benchmark benchmark_data_v2.json --results-dir fdb_v3_data_released --provider "$AGENT" \
    --output "$OUT/${s}.json" "${JUDGE[@]}" | tee "$OUT/${s}.txt"
done
"$PY" analyze_tool_latency.py --results-dir fdb_v3_data_released --provider "$AGENT" > "$OUT/latency.txt" 2>&1 || true

echo "== saving per-recording results and logs to $OUT"
mkdir -p "$OUT/results"
for d in fdb_v3_data_released/*/; do
  name=$(basename "$d")
  [ -z "$EXAMPLE" ] || [[ "$name" == "${EXAMPLE}_"* ]] || continue
  for f in "result_$AGENT.json" "inference_$AGENT.json"; do
    [ -f "$d$f" ] && { mkdir -p "$OUT/results/$name"; cp "$d$f" "$OUT/results/$name/"; }
  done
done
cp /tmp/agent_tool_calls.log "$OUT/agent_tool_calls.log" 2>/dev/null || true
cp "$LOG" "$OUT/agent.log"
"$PY" - "$OUT" "$AGENT" "$EXAMPLE" "${#JUDGE[@]}" <<'EOF'
import json, platform, subprocess, sys, time
out, agent, example, judged = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
cfg = {"agent": agent, "example": example or "all", "judge": "gpt-4o (--use-llm)" if judged else "exact match (no judge)",
       "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "platform": platform.platform(),
       "python": platform.python_version(), "fdb_v3_commit": git("-C", "../", "rev-parse", "HEAD"),
       "audient_commit": git("-C", out, "rev-parse", "HEAD")}
json.dump(cfg, open(f"{out}/run_config.json", "w"), indent=1)
print(json.dumps(cfg, indent=1))
EOF
echo "Done: $OUT"
