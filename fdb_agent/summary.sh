#!/usr/bin/env bash
# Print the headline numbers of a finished run and compare it with the baseline run.
#
#   bash fdb_agent/summary.sh audient_final            # a folder under reports/fdb/
#   bash fdb_agent/summary.sh audient_final gemini2_5  # compare with another run (default: gemini2_5)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
A="$REPO/reports/fdb/${1:?usage: summary.sh <run folder> [baseline folder]}"
B="$REPO/reports/fdb/${2:-gemini2_5}"
grep -E "Passed|Pass Rate|hard:|medium:|easy:|SELF_CORRECTION" "$A/evaluate_pass_rate.txt"
grep -E "Turn-Take|No Response|Tool Selection Acc|Argument Acc|Avg latency" "$A/evaluate_tool_calls.txt"
echo
[ -d "$B" ] && python3 "$REPO/fdb_agent/compare_runs.py" "$A" "$B" | head -3
[ -d "$B" ] && python3 "$REPO/fdb_agent/silent_report.py" "$A" "$B" | head -2
echo "reply nudges fired: $(grep -c 'reply nudge' "$A/agent.log" 2>/dev/null || echo 0)"
