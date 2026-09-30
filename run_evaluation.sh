#!/usr/bin/env bash
# One command: install pinned deps, regenerate fixtures, run unit tests and the full benchmark.
set -euo pipefail
cd "$(dirname "$0")"
python -m pip install -q -r requirements.txt
python scripts/make_frames.py
python scripts/make_scenarios.py
python -m pytest -q tests
python run_eval.py "$@"
