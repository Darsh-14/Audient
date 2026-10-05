#!/usr/bin/env bash
# Start full 100-recording runs in the background (they keep going if the terminal closes), then print how to
# follow them. Results go to reports/fdb/<agent>_<tag>/.
#
#   bash fdb_agent/full_run.sh              # Audient, tag "final"
#   bash fdb_agent/full_run.sh final both   # Audient, then FDB-v3's Gemini baseline, one after the other (~2 h)
TAG="${1:-final}"
WHAT="${2:-audient}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="audient"; [ "$WHAT" = both ] && AGENTS="audient gemini2_5"
nohup bash -c "for a in $AGENTS; do TAG=$TAG bash '$REPO/fdb_agent/run_benchmark.sh' \$a; done" > "/tmp/run_$TAG.log" 2>&1 &
echo "Started: $AGENTS (about an hour each). Check with:  tail -1 /tmp/run_$TAG.log"
echo "When the last one says Done:  bash fdb_agent/summary.sh audient_$TAG$([ "$WHAT" = both ] && echo " gemini2_5_$TAG")"
