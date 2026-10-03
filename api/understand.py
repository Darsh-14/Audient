"""Language-model understanding for the live agent (a Vercel Python function, also used by api/run.py).

GET  /api/understand   -> {"configured": bool, "model": str|null}
POST /api/understand   body: the agent's context (utterance, tools, task state, results, conversation)
                       -> {"result": {"act", "tool", "args", "pick", "prefer", "next", "reply"}, "ms": int}

Works with any provider that speaks the OpenAI chat-completions format (OpenAI, Google Gemini's
OpenAI-compatible endpoint, Groq, OpenRouter, a local Ollama, ...). Configure with environment variables;
the key never reaches the browser:

    AUDIENT_LLM_BASE_URL   e.g. https://api.openai.com/v1
    AUDIENT_LLM_API_KEY    your key (leave empty for a local server that needs none)
    AUDIENT_LLM_MODEL      the model name your provider lists
    AUDIENT_LLM_FALLBACK_MODELS   optional, comma-separated: tried in turn when the main one is busy or rate-limited

Only the standard library is used, so the function deploys without extra packages.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

MAX_BODY = 60_000
TIMEOUT_S = 12


def _load_dotenv() -> None:
    """Local runs: read KEY=VALUE lines from the project's .env (never committed) without overriding real env vars."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.environ.get("AUDIENT_DOTENV") == "0" or not os.path.isfile(path):  # tests switch it off
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_dotenv()

SYSTEM = """You are the understanding module of Audient, a real-time voice assistant that the user can interrupt.
You get JSON with: today's date, the user's latest utterance (speech-to-text: it may contain fillers, pauses,
self-corrections like "no wait", and Hindi-English), the tools the assistant can call, the current task state,
results already shown to the user, and the recent conversation. Decide what the user wants right now.

Reply with ONE JSON object and nothing else:
{"act": "task" | "answer" | "chat" | "unsupported" | "cancel" | "wait",
 "tool": "<a tool name from the list, for act=task>",
 "args": {"<parameter>": <value>, ...},
 "pick": <0-based index of the result the user chose, or null>,
 "prefer": "cheapest" | "earliest" | "latest" | null,
 "next": "<a second request in the same utterance, in the user's words, or null>",
 "reply": "<what the assistant says now>"}

How to choose "act":
- task: the user asks for something a tool can do, OR changes the current task ("make it 3", "actually Bangalore",
  "the second one", "book it"). For a change, give only the new or changed arguments. Choosing from shown results
  is a task with the tool that acts on them (e.g. book_flight) and "pick" or "prefer".
- answer: (1) a question about results already shown or the current task: answer ONLY from "results" and
  "state", and if it's not there say you don't have it; or (2) a general question you can answer from your own
  knowledge (facts, definitions, explanations, quick maths, everyday advice): answer briefly and correctly.
- chat: greetings, thanks, "what can you do?". Say briefly what you can help with (the tools' areas, plus general
  questions), and that the user can interrupt or correct you any time.
- unsupported: an action no tool can do (e.g. book a hotel, order food), or live information you don't have and no
  tool provides (news, live traffic, prices). Say so briefly and mention what you can do. Never make up live facts.
- cancel: the user abandons the task ("never mind", "forget it", "stop").
- wait: the utterance is unfinished or the user is thinking ("um", "let me think", "so I was..."). reply may be
  "" or a two-word "Take your time."

Arguments: use only parameter names from the chosen tool (or the search tool that feeds it). Places by their common
modern name (Bombay -> Mumbai, Bengaluru -> Bangalore, "the capital" of India -> Delhi). A weekday name
means its next occurrence after today ("on Friday" said on a Friday is a week later). Dates as YYYY-MM-DD
computed from "today"; times as HH:MM 24-hour (a dinner table "at 8" is 20:00); counts as integers. Use an id only
if it appears in "results". Never invent values the user did not give; leave them out and the assistant will ask.

The reply is spoken aloud: one or two short, natural sentences, no lists, no markdown. For a task, acknowledge
what you're doing in under 15 words, and it's good to invite a correction ("tell me if anything's off").
Never say a booking or change is done: the assistant confirms that itself when the tool finishes.

Read every tool's description before deciding a request is unsupported: "get me to", "take me to", "navigate",
"directions" are route requests if a route tool exists. When the user asks to BOOK or RESERVE something that
first needs a search (e.g. "book the cheapest flight from Pune to Goa"), choose the booking tool, give the search
arguments too, and set "prefer"/"pick"; the assistant runs the search itself. Only choose a state-changing tool
when the user clearly asks for that change: "yes", "ok" or "sounds good" on its own after results is NOT a
request to book. "next" is only for a second, different request in the same utterance.

Examples (tools: search_flights, book_flight, get_route, reserve_table):
"book the cheapest flight from pune to goa tomorrow for 2" ->
 {"act":"task","tool":"book_flight","args":{"origin":"Pune","destination":"Goa","date":"<tomorrow>","passengers":2},"prefer":"cheapest","pick":null,"next":null,"reply":"Booking the cheapest Pune to Goa flight for two."}
"get me to the railway station" -> {"act":"task","tool":"get_route","args":{"destination":"railway station"},"reply":"Getting directions to the railway station."}
after flight results: "the second one, for two of us" -> {"act":"task","tool":"book_flight","args":{"passengers":2},"pick":1,"reply":"Booking the second one for two."}
after flight results: "how much is the cheapest?" -> {"act":"answer","reply":"<price of the cheapest item in results>"}
"um so I was going to" -> {"act":"wait","reply":""}"""


def config() -> tuple[str, str, str]:
    return (os.environ.get("AUDIENT_LLM_BASE_URL", "").rstrip("/"), os.environ.get("AUDIENT_LLM_API_KEY", ""),
            os.environ.get("AUDIENT_LLM_MODEL", ""))


def status() -> dict:
    base, _, model = config()
    return {"configured": bool(base and model), "model": model or None}


def _post(url: str, key: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def _first_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("model reply had no JSON object")
        return json.loads(m.group(0))


def understand(context: dict) -> dict:
    base, key, model = config()
    if not (base and model):
        raise RuntimeError("no language model configured (set AUDIENT_LLM_BASE_URL and AUDIENT_LLM_MODEL)")
    body = {"model": model, "temperature": 0.1, "max_tokens": 400,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}}
    t0 = time.perf_counter()
    # busy or rate-limited (429/5xx): try the next model in AUDIENT_LLM_FALLBACK_MODELS (comma-separated), once each
    models = [model] + [m.strip() for m in os.environ.get("AUDIENT_LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
    i = 0
    while True:
        body["model"] = models[i]
        try:
            data = _post(base + "/chat/completions", key, body)
            break
        except urllib.error.HTTPError as e:
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")  # some providers or models don't accept JSON mode: ask without it
            elif e.code in (429, 500, 502, 503) and i + 1 < len(models):
                i += 1
            else:
                raise
    content = data["choices"][0]["message"]["content"] or ""
    return {"result": _first_json(content), "ms": round((time.perf_counter() - t0) * 1000), "model": models[i]}


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(200, status())

    def do_POST(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "context too large"})
            ctx = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, understand(ctx))
        except (ValueError, KeyError, RuntimeError, urllib.error.URLError, TimeoutError) as e:
            self._send(502, {"error": f"{type(e).__name__}: {e}"[:300]})
