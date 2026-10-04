#!/usr/bin/env bash
# A few-minute check on chosen scenarios, with the summary printed at the end.
#
#   bash fdb_agent/quick_check.sh                                # the two recordings the reply nudge targets
#   bash fdb_agent/quick_check.sh travel_19,housing_13 mytag     # any scenarios, results under <agent>_selected_<tag>
set -uo pipefail
SCENARIOS="${1:-ecommerce_25,housing_13}"
TAG="${2:-nudge}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ONE=$([[ "$SCENARIOS" == *,* ]] && echo selected || echo "$SCENARIOS")
OUT="$REPO/reports/fdb/audient_${ONE}_$TAG"

echo "Running $SCENARIOS (a few minutes; progress in /tmp/quick_check.log)..."
TAG="$TAG" bash "$REPO/fdb_agent/run_benchmark.sh" audient "$SCENARIOS" > /tmp/quick_check.log 2>&1
tail -1 /tmp/quick_check.log
echo
grep -E "Turn-Take|No Response" "$OUT/evaluate_tool_calls.txt"
grep -E "Passed|Pass Rate" "$OUT/evaluate_pass_rate.txt"
echo
python3 "$REPO/fdb_agent/silent_report.py" "$OUT" | head -30
grep -c "reply nudge" "$OUT/agent.log" | sed 's/^/reply nudges fired: /'
