"""Wire protocol: timestamped input events and output actions (plain JSON dicts).

The official evaluation kit was not released when this was written, so this
schema is reconstructed from the Theme 05 guide. ``adapt_event`` is the single
place to map a different field naming onto the internal one.

Input events (inbox):
  {"t", "type": "tool_manifest", "tools": [ToolSpec...]}
  {"t", "type": "transcript", "text", "end_of_turn": bool}
  {"t", "type": "audio", "path", "end_of_turn": bool}
  {"t", "type": "frame", "path"}
  {"t", "type": "interruption"}                      # user barge-in signal
  {"t", "type": "tool_result", "call_id", "status": "ok"|"error", "result"|"error"}
  {"t", "type": "session_end"}

Output actions (outbox; the harness stamps "t"):
  {"type": "speak", "kind": "filler"|"ack"|"progress", "text"}
  {"type": "tool_call", "call_id", "tool", "args", "idempotency_key"|None}
  {"type": "cancel", "call_id", "reason"}
  {"type": "clarify", "text", "slot", "state_snapshot"}
  {"type": "final_response", "text", "state_snapshot"}

ToolSpec: {"name", "description", "side_effect": "read_only"|"state_modifying",
           "parameters": {"type": "object", "properties": {...}, "required": [...]}}
StateSnapshot: {"version": int, "intent": str|None, "slots": {..}, "status": str}
"""
from __future__ import annotations

import re
from typing import Any

ACTION_TYPES = {"speak", "tool_call", "cancel", "clarify", "final_response"}
SPEAK_KINDS = {"filler", "ack", "progress"}
SNAPSHOT_STATUS = {"idle", "clarifying", "in_progress", "completed", "cancelled", "failed"}

_ALIASES = {"chunk": "transcript", "text_chunk": "transcript", "audio_clip": "audio",
            "video_frame": "frame", "image": "frame", "interrupt": "interruption",
            "manifest": "tool_manifest", "tool_response": "tool_result", "tool_output": "tool_result"}
_OK = {"ok", "success", "succeeded", "completed", "complete", "done"}


def adapt_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Normalise alternative field names (kept in one place on purpose)."""
    ev = dict(ev)
    ev["type"] = _ALIASES.get(ev.get("type", ""), ev.get("type"))
    if "timestamp" in ev and "t" not in ev:
        ev["t"] = ev["timestamp"]
    if "eot" in ev and "end_of_turn" not in ev:
        ev["end_of_turn"] = ev["eot"]
    if ev["type"] == "tool_manifest":
        tools = next((ev[k] for k in ("tools", "manifest", "functions") if k in ev), [])
        ev["tools"] = [normalize_tool(t) for t in tools]
    elif ev["type"] == "tool_result":
        if "call_id" not in ev:
            ev["call_id"] = next((ev[k] for k in ("tool_call_id", "callId", "id") if k in ev), None)
        if "result" not in ev:
            for k in ("output", "data", "content", "response"):
                if k in ev:
                    ev["result"] = ev[k]
                    break
        if isinstance(ev.get("ok"), bool) and "status" not in ev:
            ev["status"] = "ok" if ev["ok"] else "error"
        st = str(ev.get("status", "ok" if "error" not in ev else "error")).lower()
        ev["status"] = "ok" if st in _OK else "error"
    return ev


# ---- tool specs: any common manifest format -> {"name", "description", "side_effect", "parameters"}
_WRITE = {"state_modifying", "state-modifying", "state_changing", "state-changing", "write", "writes", "read_write",
          "mutating", "mutation", "mutates", "modify", "modifying", "side_effect", "side_effects", "action", "command"}
_READ = {"read_only", "read-only", "readonly", "read", "query", "safe", "none", "pure", "idempotent_read"}
_EFFECT_KEYS = ("side_effect", "side_effects", "sideEffect", "sideEffects", "effect", "effects", "kind",
                "access", "mode", "category", "type")
_WRITE_VERBS = {"book", "create", "cancel", "delete", "remove", "update", "set", "send", "make", "place", "reserve",
                "order", "pay", "purchase", "buy", "submit", "add", "schedule", "post", "transfer", "play", "control",
                "start", "stop", "call", "reset", "open", "close", "turn", "change", "modify", "edit", "register",
                "subscribe", "unsubscribe", "confirm", "approve", "assign", "upload", "save", "write", "log", "take",
                "enable", "disable", "lock", "unlock", "rate", "refund", "rebook", "reschedule", "file", "raise"}
_READ_VERBS = {"get", "find", "search", "lookup", "look", "list", "check", "fetch", "read", "show", "query", "view",
               "estimate", "track", "describe", "detect", "recognize", "recognise", "analyze", "analyse", "compare",
               "translate", "calculate", "convert", "count", "locate", "browse", "identify", "summarize", "verify"}


def _declared_effect(spec: dict[str, Any]) -> bool | None:
    """What the manifest itself says about side effects, in any of the usual spellings (None if silent)."""
    for k in ("state_modifying", "mutating", "modifies_state", "is_mutating", "stateful", "writes", "destructive"):
        if isinstance(spec.get(k), bool):
            return spec[k]
    for k in ("read_only", "readOnly", "readonly", "is_read_only", "safe"):
        if isinstance(spec.get(k), bool):
            return not spec[k]
    ann = spec.get("annotations")
    if isinstance(ann, dict):  # MCP tool annotations
        if isinstance(ann.get("readOnlyHint"), bool):
            return not ann["readOnlyHint"]
        if isinstance(ann.get("destructiveHint"), bool) and ann["destructiveHint"]:
            return True
    for k in _EFFECT_KEYS:
        v = spec.get(k)
        if isinstance(v, bool) and k.startswith("side"):
            return v
        if isinstance(v, str):
            s = v.strip().lower().replace(" ", "_")
            if s in _WRITE:
                return True
            if s in _READ:
                return False
    desc = str(spec.get("description", "")).lower()
    if re.search(r"\bread[- ]only\b|\bno side[- ]effects?\b|\bdoes not (?:modify|change)\b", desc):
        return False
    if re.search(r"\bstate[- ](?:modifying|changing)\b|\bside[- ]effects?\b|\bmodifies\b", desc):
        return True
    return None


def is_state_modifying(spec: dict[str, Any]) -> bool:
    declared = _declared_effect(spec)
    if declared is not None:
        return declared
    # undeclared: judge by the verb in the tool's name; an unknown verb is treated as state-changing,
    # the safe side for "never repeat a state-changing call"
    verb = re.split(r"[_\-\s.]+|(?<=[a-z])(?=[A-Z])", str(spec.get("name", "")).strip())[0].lower()
    if verb in _READ_VERBS:
        return False
    return bool(verb)


def normalize_tool(spec: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-style {"type": "function", "function": {...}}, Anthropic/MCP input_schema, missing pieces -> the
    internal ToolSpec, with an explicit side_effect so every later check agrees."""
    s = dict(spec)
    if isinstance(s.get("function"), dict):  # OpenAI wrapper
        s = {**{k: v for k, v in s.items() if k not in ("function", "type")}, **s["function"]}
    params = next((s[k] for k in ("parameters", "input_schema", "inputSchema", "schema", "args_schema", "arguments")
                   if isinstance(s.get(k), dict)), {})
    params = {"type": "object", **params}
    params.setdefault("properties", {})
    params.setdefault("required", [])
    s["parameters"] = params
    s.setdefault("description", "")
    s["side_effect"] = "state_modifying" if is_state_modifying(s) else "read_only"
    return s


def _type_ok(value: Any, schema: dict[str, Any]) -> bool:
    t = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        return False
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(t, True)


def validate_snapshot(snap: Any, tools: dict[str, dict]) -> list[str]:
    errs = []
    if not isinstance(snap, dict):
        return ["snapshot not an object"]
    for k in ("version", "intent", "slots", "status"):
        if k not in snap:
            errs.append(f"snapshot missing {k}")
    if snap.get("status") not in SNAPSHOT_STATUS:
        errs.append(f"bad snapshot status {snap.get('status')!r}")
    intent = snap.get("intent")
    if intent is not None and intent not in tools:
        errs.append(f"snapshot intent {intent!r} not in manifest")
    slots = snap.get("slots")
    if not isinstance(slots, dict):
        errs.append("snapshot slots not an object")
    elif intent in tools:
        props = tools[intent].get("parameters", {}).get("properties", {})
        for k, v in slots.items():
            if k in props and not _type_ok(v, props[k]):
                errs.append(f"slot {k}={v!r} violates schema")
    return errs


def validate_action(a: Any, tools: dict[str, dict], seen_calls: set[str]) -> list[str]:
    """Protocol check for one output action. ``seen_calls`` is updated in place."""
    if not isinstance(a, dict):
        return ["action is not an object"]
    typ = a.get("type")
    if typ not in ACTION_TYPES:
        return [f"unknown action type {typ!r}"]
    errs: list[str] = []
    if typ == "speak":
        if a.get("kind") not in SPEAK_KINDS or not isinstance(a.get("text"), str) or not a["text"]:
            errs.append("malformed speak")
    elif typ == "tool_call":
        cid = a.get("call_id")
        if not isinstance(cid, str) or not cid:
            errs.append("tool_call without call_id")
        elif cid in seen_calls:
            errs.append(f"duplicate call_id {cid}")
        else:
            seen_calls.add(cid)
        spec = tools.get(a.get("tool"))
        if spec is None:
            errs.append(f"unknown tool {a.get('tool')!r}")
        else:
            params = spec.get("parameters", {})
            props, req = params.get("properties", {}), params.get("required", [])
            args = a.get("args")
            if not isinstance(args, dict):
                errs.append("args not an object")
            else:
                errs += [f"missing required arg {r}" for r in req if r not in args]
                errs += [f"unknown arg {k}" for k in args if k not in props]
                errs += [f"arg {k}={v!r} violates schema" for k, v in args.items()
                         if k in props and not _type_ok(v, props[k])]
    elif typ == "cancel":
        if a.get("call_id") not in seen_calls:
            errs.append(f"cancel of unknown call {a.get('call_id')!r}")
    elif typ in ("clarify", "final_response"):
        if not isinstance(a.get("text"), str) or not a["text"]:
            errs.append(f"{typ} without text")
        errs += validate_snapshot(a.get("state_snapshot"), tools)
    return errs
