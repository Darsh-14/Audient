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


def build() -> dict:
    manifest = json.loads((ROOT / "scenarios" / "manifest_core.json").read_text(encoding="utf-8"))
    unseen = json.loads((ROOT / "scenarios" / "T07_unseen_tool.json").read_text(encoding="utf-8"))["manifest"]
    manifest += [t for t in unseen if t["name"] not in {m["name"] for m in manifest}]
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
