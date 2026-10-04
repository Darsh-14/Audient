"""Start FDB-v3's own template agent (lk_agent_tool.py, e.g. LK_PROVIDER=gemini2_5) unchanged, for the baseline.

The provider plugins are imported here first, on the main thread: on Windows LiveKit runs jobs in threads and
refuses to register a plugin from one ("Plugins must be registered on the main thread"), which is where the
template imports it. On Linux this changes nothing.
Usage (cwd: FDB-v3's v3/ directory):  LK_PROVIDER=gemini2_5 python <repo>/fdb_agent/run_baseline_agent.py start
"""
import os
import runpy
import sys
from pathlib import Path

import livekit.plugins.google  # noqa: F401
import livekit.plugins.openai  # noqa: F401
import livekit.plugins.silero  # noqa: F401
import livekit.plugins.xai  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fdb_agent.crt_quiet import quiet_native_asserts  # noqa: E402

quiet_native_asserts()
V3 = Path(os.getenv("FDB_V3_DIR", Path(__file__).resolve().parents[1] / "external" / "Full-Duplex-Bench" / "v3"))
sys.path.insert(0, str(V3))
sys.argv = ["lk_agent_tool.py"] + sys.argv[1:]
runpy.run_path(str(V3 / "lk_agent_tool.py"), run_name="__main__")
