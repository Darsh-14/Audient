"""Run a Python script with native assertion dialogs turned off (Windows; a plain run elsewhere).
Usage: python quiet_run.py <script.py> [args...]   (see crt_quiet.py)"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fdb_agent.crt_quiet import quiet_native_asserts  # noqa: E402

quiet_native_asserts()
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(script).resolve().parent))
runpy.run_path(script, run_name="__main__")
