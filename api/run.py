"""Web API for the Audient demo — a Vercel Python serverless function, also runnable locally.

GET  /api/run                     list the runnable (text) scenarios
GET  /api/run?scenario=T04        run one scenario for Audient and the half-duplex baseline
POST /api/run {"turns": [...]}    run your own timed turns against the core tool manifest

Local:  python api/run.py   ->   http://localhost:8000
Camera (V*) scenarios need OCR (numpy/onnxruntime) and are run with run_eval.py instead.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audient.agent import RealtimeAgent  # noqa: E402
from audient.baseline import HalfDuplexAgent  # noqa: E402
from audient.harness.runner import run_scenario  # noqa: E402
from audient.harness.scorer import score  # noqa: E402

SCEN = ROOT / "scenarios"
MANIFEST = json.loads((SCEN / "manifest_core.json").read_text(encoding="utf-8"))
MAX_TURNS, MAX_T, MAX_CHARS = 8, 60.0, 300


def text_scenarios() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCEN.glob("T*.json"))]


def _run_both(scn: dict) -> dict:
    date = dt.date.fromisoformat(scn.get("session_date") or dt.date.today().isoformat())
    out = {}
    for name, cls in (("audient", RealtimeAgent), ("baseline", HalfDuplexAgent)):
        run = run_scenario(scn, lambda: cls(perception=None, session_date=date))
        out[name] = {"trace": [e for e in run["trace"] if e["type"] != "tool_manifest"],
                     "commits": run["commits"], "score": score(scn, run)}
    return out


def run_both(scn: dict) -> dict:
    # a fresh thread guarantees no event loop is already running (the harness owns its own loop)
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(_run_both, scn).result(timeout=100)


def custom_scenario(body: dict) -> dict:
    turns = body.get("turns")
    if not isinstance(turns, list) or not 1 <= len(turns) <= MAX_TURNS:
        raise ValueError(f"send 1-{MAX_TURNS} turns")
    events = []
    for tr in turns:
        t, text = float(tr.get("t", 0)), str(tr.get("text", "")).strip()[:MAX_CHARS]
        if not (0 <= t <= MAX_T) or not text:
            raise ValueError("each turn needs 0 <= t <= 60 and some text")
        if tr.get("interrupt"):
            events.append({"t": max(0.0, t - 0.05), "type": "interruption"})
        events.append({"t": t, "type": "transcript", "text": text, "end_of_turn": True})
    return {"id": "custom", "modality": "text", "manifest": MANIFEST, "events": events, "expect": {},
            "session_date": body.get("session_date") or dt.date.today().isoformat()}


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):  # only reached when running locally
            return self._send(200, (ROOT / "public" / "index.html").read_bytes(), "text/html; charset=utf-8")
        sid = (parse_qs(u.query).get("scenario") or [""])[0]
        scns = text_scenarios()
        if not sid:
            return self._send(200, [{"id": s["id"], "tags": s.get("tags", []),
                                     "turns": [e for e in s["events"] if e["type"] in ("transcript", "interruption")]}
                                    for s in scns])
        match = [s for s in scns if s["id"] == sid or s["id"].split("_")[0] == sid]
        if not match:
            return self._send(404, {"error": f"unknown text scenario {sid!r}"})
        self._send(200, {"scenario": match[0], "results": run_both(match[0])})

    def do_POST(self) -> None:
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 20000)
            scn = custom_scenario(json.loads(self.rfile.read(n) or b"{}"))
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e)})
        self._send(200, {"scenario": scn, "results": run_both(scn)})


if __name__ == "__main__":
    print("Audient demo on http://localhost:8000")
    HTTPServer(("127.0.0.1", 8000), handler).serve_forever()
