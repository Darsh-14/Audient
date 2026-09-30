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

from typing import Any

ACTION_TYPES = {"speak", "tool_call", "cancel", "clarify", "final_response"}
SPEAK_KINDS = {"filler", "ack", "progress"}
SNAPSHOT_STATUS = {"idle", "clarifying", "in_progress", "completed", "cancelled", "failed"}

_ALIASES = {"chunk": "transcript", "text_chunk": "transcript", "audio_clip": "audio",
            "video_frame": "frame", "image": "frame", "interrupt": "interruption",
            "manifest": "tool_manifest"}


def adapt_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Normalise alternative field names (kept in one place on purpose)."""
    ev = dict(ev)
    ev["type"] = _ALIASES.get(ev.get("type", ""), ev.get("type"))
    if "timestamp" in ev and "t" not in ev:
        ev["t"] = ev["timestamp"]
    if "eot" in ev and "end_of_turn" not in ev:
        ev["end_of_turn"] = ev["eot"]
    if ev["type"] == "tool_manifest" and "tools" not in ev and "manifest" in ev:
        ev["tools"] = ev["manifest"]
    return ev


def is_state_modifying(spec: dict[str, Any]) -> bool:
    se = str(spec.get("side_effect", spec.get("kind", ""))).lower()
    if se in {"state_modifying", "write", "mutating", "side_effect"}:
        return True
    return bool(spec.get("state_modifying") or spec.get("mutating"))


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
