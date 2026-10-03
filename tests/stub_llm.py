"""A stand-in OpenAI-compatible chat-completions server for tests (no real model, no network).

It answers a few fixed utterances with the JSON a real model would be asked for, so the whole path
browser -> /api/understand -> provider -> agent can be tested without an API key.

    python tests/stub_llm.py 8790          # then AUDIENT_LLM_BASE_URL=http://127.0.0.1:8790/v1
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLIES = {
    "i gotta get from bombay over to the capital tomorrow": {
        "act": "task", "tool": "search_flights", "reply": "Looking at flights from Mumbai to Delhi. Tell me if anything's off.",
        "args": {"origin": "Mumbai", "destination": "Delhi", "date": "{tomorrow}"}},
    "grab me the second one, it's for two of us": {
        "act": "task", "tool": "book_flight", "args": {"passengers": 2}, "pick": 1, "reply": "Booking the second one for two."},
    "what's the weather like": {"act": "unsupported", "reply": "I can't check the weather, but I can find flights or get you a route."},
}


class Stub(BaseHTTPRequestHandler):
    reject_json_mode = False
    fenced = False
    calls: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        Stub.calls.append(body)
        if Stub.reject_json_mode and "response_format" in body:
            self.send_response(400)
            self.end_headers()
            return
        ctx = json.loads(body["messages"][-1]["content"])
        key = ctx["utterance"].lower().strip(" .?!")
        reply = REPLIES.get(key, {"act": "chat", "reply": "Sorry, could you say that another way?"})
        tomorrow = ctx["today"].split()[0]
        import datetime as dt
        tomorrow = (dt.date.fromisoformat(tomorrow) + dt.timedelta(days=1)).isoformat()
        content = json.dumps(reply).replace("{tomorrow}", tomorrow)
        if Stub.fenced:
            content = "```json\n" + content + "\n```"
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    print(f"stub model on http://127.0.0.1:{port}/v1")
    ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
