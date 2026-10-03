"""Package the agent for the browser web app (run after changing anything in audient/).

Writes public/py/bundle.json (the Python sources Pyodide loads + the tool manifest) and copies
the sample camera frames to public/frames/. The web app runs these exact sources, so the
agent you talk to in the browser is the one the benchmark scores.
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["audient/__init__.py", "audient/agent.py", "audient/nlu.py", "audient/protocol.py",
         "audient/vclock.py", "audient/live.py", "audient/harness/__init__.py", "audient/harness/mock_env.py"]


# areas the agent describes itself by (the benchmark manifests carry no categories and are unaffected)
CORE_CATEGORIES = {"search_flights": "travel and directions", "book_flight": "travel and directions",
                   "get_route": "travel and directions", "reserve_table": "restaurant bookings",
                   "create_ticket": "device help", "lookup_manual": "device help"}


def full_manifest() -> list[dict]:
    """The web app's tools: the benchmark's core + unseen tools, plus everyday assistant tools."""
    manifest = json.loads((ROOT / "scenarios" / "manifest_core.json").read_text(encoding="utf-8"))
    unseen = json.loads((ROOT / "scenarios" / "T07_unseen_tool.json").read_text(encoding="utf-8"))["manifest"]
    manifest += [t for t in unseen if t["name"] not in {m["name"] for m in manifest}]
    for t in manifest:
        t.setdefault("category", CORE_CATEGORIES.get(t["name"], "other tasks"))
    order = ["search_flights", "book_flight", "get_route"]  # travel first in the intro
    extra = json.loads((ROOT / "scenarios" / "manifest_assistant.json").read_text(encoding="utf-8"))
    manifest = sorted(manifest, key=lambda t: (t["name"] not in order, order.index(t["name"]) if t["name"] in order else 0))
    return manifest + [t for t in extra if t["name"] not in {m["name"] for m in manifest}]


def build() -> dict:
    manifest = full_manifest()
    bundle = {"files": {f: (ROOT / f).read_text(encoding="utf-8") for f in FILES}, "manifest": manifest}
    out = ROOT / "public" / "py" / "bundle.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle), encoding="utf-8")
    frames = ROOT / "public" / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    for png in sorted((ROOT / "assets" / "frames").glob("*.png")):
        shutil.copyfile(png, frames / png.name)
    return bundle


if __name__ == "__main__":
    b = build()
    print(f"bundled {len(b['files'])} files, {len(b['manifest'])} tools -> public/py/bundle.json; frames -> public/frames/")
