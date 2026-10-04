#!/usr/bin/env bash
# Start a full 100-recording run in the background (it keeps going if the terminal closes), then print how to
# follow it. Results go to reports/fdb/audient_<tag>/.
#
#   bash fdb_agent/full_run.sh            # tag "final"
#   bash fdb_agent/full_run.sh mytag
TAG="${1:-final}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TAG="$TAG" nohup bash "$REPO/fdb_agent/run_benchmark.sh" audient > "/tmp/run_$TAG.log" 2>&1 &
echo "Started (about an hour). Check with:  tail -1 /tmp/run_$TAG.log"
echo "When it says Done:                    bash fdb_agent/summary.sh audient_$TAG"
