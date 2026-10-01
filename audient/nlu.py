"""Fast-path, schema-driven NLU (no LLM, sub-millisecond, deterministic).

* disfluency removal and self-repair resolution ("Delhi, no wait, Bangalore")
* generic entity extraction (city, date, time, count, code, model, names, enums)
* parameter typing from the tool manifest (name / description / JSON schema), so
  unseen tools are handled without tool-specific code
* intent routing by lexical overlap with tool names and descriptions
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

CITIES = {
    "mumbai": ("Mumbai", "BOM"), "bombay": ("Mumbai", "BOM"), "delhi": ("Delhi", "DEL"),
    "new delhi": ("Delhi", "DEL"), "bangalore": ("Bangalore", "BLR"), "bengaluru": ("Bangalore", "BLR"),
    "chennai": ("Chennai", "MAA"), "kolkata": ("Kolkata", "CCU"), "hyderabad": ("Hyderabad", "HYD"),
    "pune": ("Pune", "PNQ"), "goa": ("Goa", "GOI"), "ahmedabad": ("Ahmedabad", "AMD"),
    "jaipur": ("Jaipur", "JAI"), "kochi": ("Kochi", "COK"), "lucknow": ("Lucknow", "LKO"),
    "noida": ("Noida", "DEL"), "london": ("London", "LHR"), "new york": ("New York", "JFK"),
    "nyc": ("New York", "JFK"),
    "dubai": ("Dubai", "DXB"), "singapore": ("Singapore", "SIN"), "paris": ("Paris", "CDG"),
    "boston": ("Boston", "BOS"), "chicago": ("Chicago", "ORD"), "seattle": ("Seattle", "SEA"),
    "san francisco": ("San Francisco", "SFO"), "tokyo": ("Tokyo", "NRT"),
}
PLACES = ["airport", "railway station", "station", "home", "office", "work", "hospital", "mall", "hotel"]
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "a couple of": 2, "couple of": 2}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
STOP = set("a an the to for of on in at me my i we you please can could would will it is this that and or "
           "with from by be do does need want some any about what so get".split())
SYNONYMS = {
    "search": {"find", "look", "show", "check", "search", "available", "options", "list"},
    "book": {"book", "reserve", "buy", "purchase", "confirm"},
    "reserve": {"reserve", "book"},
    "create": {"create", "open", "file", "raise", "log", "report", "register", "submit"},
    "ticket": {"ticket", "complaint", "support", "broken", "leaking", "fault", "not"},
    "route": {"route", "navigate", "navigation", "directions", "drive", "take", "way", "head"},
    "lookup": {"mean", "means", "meaning", "what", "lookup", "look", "explain"},
    "manual": {"manual", "error", "code", "display", "showing", "light", "blinking", "troubleshoot", "fix"},
    "flight": {"flight", "flights", "fly", "flying", "plane"},
    "table": {"table", "dinner", "lunch", "restaurant"},
}
FILLER_RE = re.compile(r"\b(?:uh+|um+|umm+|erm+|er|hmm+|you know)\b[,.]?\s*", re.I)
REPAIR_RE = re.compile(r"(?:,|\.|\s)\s*(?:no,?\s*wait|wait,?\s*no|no,?\s*sorry|sorry|i mean|actually|hold on|"
                       r"correction|scratch that|rather|wait,?\s*make (?:it|that)|make (?:it|that)|"
                       r"change (?:it|that) to|instead)\b[,]?", re.I)
# FDB-v3 disfluency classes handled before parsing
STUTTER_RE = re.compile(r"\b(\w{1,3})-(?:\1-)*(?=\1)", re.I)          # f-f-fly -> fly
FRAGMENT_RE = re.compile(r"\b(\w{1,5})-\s+(?=\1)", re.I)               # Bo- Boston -> Boston
REPEAT_RE = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.I)                   # to to -> to
ELONG_RE = re.compile(r"([a-z])\1{2,}", re.I)                          # sooo -> so
DANGLING = {"to", "from", "for", "and", "or", "the", "a", "an", "on", "at", "in", "with", "my", "is", "but",
            "uh", "um", "umm", "er", "erm", "like", "so", "of", "then", "because"}
CANCEL_RE = re.compile(r"\b(never ?mind|forget (?:it|that|about it)|cancel (?:that|it|this|everything)|"
                       r"stop (?:that|it|searching)|don'?t bother|abort)\b", re.I)
CONFIRM_RE = re.compile(r"^\s*(yes|yeah|yep|sure|ok(?:ay)?|go ahead|do it|please do|confirm)\b", re.I)


@dataclass
class Mention:
    etype: str          # city | place | date | time | count | code | model | name | pref
    value: Any
    start: int
    end: int
    role: str | None = None  # origin | destination (for locations)


@dataclass
class Parse:
    text: str
    clean: str
    mentions: list[Mention] = field(default_factory=list)
    cancel: bool = False
    confirm: bool = False
    tokens: set[str] = field(default_factory=set)


# Frequent speech-to-text mishearings in this domain (observed with Whisper tiny.en on real audio).
ASR_FIXES = [(re.compile(r"\bno weight\b", re.I), "no wait"), (re.compile(r"\bdeli\b", re.I), "Delhi"),
             (re.compile(r"\bbangaluru\b", re.I), "Bengaluru"), (re.compile(r"\bchenai\b", re.I), "Chennai")]


def clean_text(text: str) -> str:
    t = text
    for pat, fix in ASR_FIXES:
        t = pat.sub(fix, t)
    t = STUTTER_RE.sub("", t)
    t = FRAGMENT_RE.sub("", t)
    t = ELONG_RE.sub(r"\1", t)
    t = FILLER_RE.sub("", t)
    t = REPEAT_RE.sub(r"\1", t)
    t = re.sub(r"\s*(?:\.\.\.|…|—|--)\s*", ", ", t)                     # pauses / false-start dashes
    return re.sub(r"\s+", " ", t).strip(" ,")


def turn_incomplete(text: str) -> bool:
    """Semantic end-of-turn check: a VAD pause after 'fly to, um...' is not a finished request."""
    raw = text.strip().lower()
    if raw.endswith(("...", "…", "-", "—", ",")):
        return True
    words = re.findall(r"[a-z']+", raw)
    return bool(words) and words[-1] in DANGLING


def _stem(w: str) -> str:
    for suf in ("ing", "es", "ed", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP}


def _date_from(m_day: int, m_mon: int, ref: dt.date) -> dt.date:
    d = dt.date(ref.year, m_mon, m_day)
    return d if d >= ref else dt.date(ref.year + 1, m_mon, m_day)


def extract(text: str, ref_date: dt.date) -> Parse:
    clean = clean_text(text)
    low = clean.lower()
    ms: list[Mention] = []

    for name in sorted(CITIES, key=len, reverse=True):
        for m in re.finditer(rf"\b{re.escape(name)}\b", low):
            if any(x.start <= m.start() < x.end for x in ms):
                continue
            pre = low[max(0, m.start() - 12): m.start()]
            role = "origin" if re.search(r"\bfrom[\s,]+$", pre) else (
                "destination" if re.search(r"\b(to|for|towards|into)[\s,]+$", pre) else None)
            ms.append(Mention("city", CITIES[name][0], m.start(), m.end(), role))
    for p in PLACES:
        for m in re.finditer(rf"\b{p}\b", low):
            if not any(x.start <= m.start() < x.end for x in ms):
                ms.append(Mention("place", p, m.start(), m.end(), "destination"))

    # dates
    for m in re.finditer(r"\b(today|tonight|tomorrow|day after tomorrow)\b", low):
        off = {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2}[m.group(1)]
        if m.group(1) == "tomorrow" and low[max(0, m.start() - 10): m.start()].endswith("after "):
            continue
        ms.append(Mention("date", (ref_date + dt.timedelta(days=off)).isoformat(), m.start(), m.end()))
    for m in re.finditer(r"\b(?:(this|next|coming)\s+)?(" + "|".join(WEEKDAYS) + r")\b", low):
        wd = WEEKDAYS.index(m.group(2))
        ahead = (wd - ref_date.weekday()) % 7
        if ahead == 0:
            ahead = 7
        if m.group(1) == "next" and ahead < 7 and False:
            ahead += 7
        ms.append(Mention("date", (ref_date + dt.timedelta(days=ahead)).isoformat(), m.start(), m.end()))
    mon = "|".join(MONTHS)
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({mon})\b|\b({mon})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", low):
        day = int(m.group(1) or m.group(4))
        month = MONTHS.index(m.group(2) or m.group(3)) + 1
        try:
            ms.append(Mention("date", _date_from(day, month, ref_date).isoformat(), m.start(), m.end()))
        except ValueError:
            pass
    for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)\b", low):
        ms.append(Mention("date", m.group(0), m.start(), m.end()))

    # times
    time_spans = []
    for m in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?=\W|$)", low):
        h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3).startswith("p"):
            h += 12
        ms.append(Mention("time", f"{h:02d}:{mi:02d}", m.start(), m.end()))
        time_spans.append((m.start(), m.end()))
    for m in re.finditer(r"\bat (\d{1,2})(?::(\d{2}))?\b(?!\s*(?:people|persons|passengers|guests|a\.?m|p\.?m|:))", low):
        if any(s <= m.start(1) < e for s, e in time_spans):
            continue
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h < 12 and (h < 7 or re.search(r"tonight|evening|dinner|pm", low)):
            h += 12
        ms.append(Mention("time", f"{h:02d}:{mi:02d}", m.start(1), m.end()))
    for word, hm in (("noon", "12:00"), ("midnight", "00:00")):
        for m in re.finditer(rf"\b{word}\b", low):
            ms.append(Mention("time", hm, m.start(), m.end()))

    # counts
    num = r"(\d{1,2}|" + "|".join(sorted(NUMWORDS, key=len, reverse=True)) + r")"
    for m in re.finditer(num + r"\s+(?:people|persons|passengers|adults|guests|tickets|seats|travellers|travelers|of us)\b", low):
        ms.append(Mention("count", int(NUMWORDS.get(m.group(1), m.group(1))), m.start(), m.end()))
    for m in re.finditer(r"\b(?:party of|table for|for)\s+" + num + r"\b(?!\s*(?:a\.?m|p\.?m|:|o'?clock|pm|am|people|persons|passengers|guests|adults|tickets|seats))", low):
        if not any(x.etype == "count" and x.start <= m.start() < x.end for x in ms):
            ms.append(Mention("count", int(NUMWORDS.get(m.group(1), m.group(1))), m.start(), m.end()))
    for m in re.finditer(r"\bjust me\b|\bmyself\b|\bonly me\b", low):
        ms.append(Mention("count", 1, m.start(), m.end()))

    # error codes and model ids (keep original casing positions: `clean` and `low` align)
    for m in re.finditer(r"\b(?:err(?:or)?\s*)?([a-z]{1,2})[\s-]?(\d{1,3})\b", low):
        pre = low[max(0, m.start() - 12): m.start()]
        if m.group(0).startswith("err") or re.search(r"(code|error|showing|shows|says)\s+$", pre) or clean[m.start(1)].isupper():
            if m.group(1) in {"at", "for", "to", "of", "on", "in", "is"}:
                continue
            ms.append(Mention("code", f"{m.group(1).upper()}{m.group(2)}", m.start(), m.end()))
    for m in re.finditer(r"\b([A-Z]{2,}[- ]?\d{2,}[A-Z]?)\b", clean):
        ms.append(Mention("model", m.group(1).replace(" ", "-"), m.start(), m.end()))

    # LED / indicator states ("blinking blue twice", "solid red light")
    colors = r"(red|green|blue|amber|orange|yellow|white)"
    times_w = {"once": 1, "twice": 2, "three times": 3, "thrice": 3, "four times": 4}
    for m in re.finditer(rf"\b(?:(blinking|flashing|solid|steady)\s+{colors}|{colors}\s+(?:light\s+)?(?:is\s+)?(blinking|flashing|solid|steady))"
                         r"(?:\s+(once|twice|thrice|three times|four times))?", low):
        state = (m.group(1) or m.group(4)).replace("flashing", "blinking").replace("steady", "solid")
        color = m.group(2) or m.group(3)
        n = times_w.get(m.group(5) or "", 0)
        ms.append(Mention("indicator", f"{color}_{state}" + (f"_{n}" if n and state == "blinking" else ""), m.start(), m.end()))
    for m in re.finditer(rf"\b{colors}\s+light\b", low):
        if not any(x.etype == "indicator" and x.start <= m.start() < x.end for x in ms):
            ms.append(Mention("indicator", f"{m.group(1)}_solid", m.start(), m.end()))

    # preferences over prior results
    for pat, val in ((r"\bcheapest|lowest price|least expensive\b", ("min", "price")),
                     (r"\bearliest|first flight|morning one\b", ("min", "time")),
                     (r"\blatest|last one\b", ("max", "time")),
                     (r"\b(?:the )?first one\b|\boption one\b", ("index", 0)),
                     (r"\b(?:the )?second one\b|\boption two\b", ("index", 1)),
                     (r"\b(?:the )?third one\b|\boption three\b", ("index", 2))):
        for m in re.finditer(pat, low):
            ms.append(Mention("pref", val, m.start(), m.end()))

    # capitalised names after a preposition (restaurants, venues, people)
    for m in re.finditer(r"\b(?:at|called|named|to)\s+((?:[A-Z][\w'&]+)(?:\s+[A-Z][\w'&]+)*)", clean):
        val = m.group(1)
        if val.lower() in CITIES or val.lower() in WEEKDAYS or val.lower() in MONTHS or re.match(r"[A-Z]+[- ]?\d", val):
            continue
        ms.append(Mention("name", val, m.start(1), m.end(1)))

    ms = _apply_repairs(ms, low)
    return Parse(text=text, clean=clean, mentions=ms, cancel=bool(CANCEL_RE.search(low)),
                 confirm=bool(CONFIRM_RE.search(low)), tokens=tokens(clean))


def _apply_repairs(ms: list[Mention], low: str) -> list[Mention]:
    """A mention right after a repair marker replaces the closest earlier one of the same type."""
    ms = sorted(ms, key=lambda x: x.start)
    drop: set[int] = set()
    for rm in REPAIR_RE.finditer(low):
        after = [x for x in ms if x.start >= rm.end()]
        before = [x for x in ms if x.end <= rm.start() + 1]
        seen_types = set()
        for new in after:
            if new.etype in seen_types:
                continue
            seen_types.add(new.etype)
            prev = [x for x in before if x.etype == new.etype and id(x) not in drop]
            if prev:
                old = prev[-1]
                drop.add(id(old))
                if new.role is None:
                    new.role = old.role
    for m in re.finditer(r"\bnot\s+(?:on\s+)?(\w+)", low):
        for x in ms:
            if x.start == m.start(1):
                drop.add(id(x))
    return [x for x in ms if id(x) not in drop]


# ---------------------------------------------------------------- param typing
def param_type(name: str, schema: dict) -> str:
    n = name.lower()
    desc = str(schema.get("description", "")).lower()
    if "enum" in schema:
        return "enum"
    if n in {"origin", "source", "from", "from_city", "departure_city", "start", "start_location"}:
        return "origin"
    if n in {"destination", "to", "dest", "to_city", "arrival_city", "end_location"} or "destination" in n:
        return "destination"
    if schema.get("type") in ("integer", "number"):
        return "count"
    if "date" in n or schema.get("format") == "date" or n == "day":
        return "date"
    if "time" in n or schema.get("format") == "time":
        return "time"
    if "code" in n or "error" in n:
        return "code"
    if set(n.split("_")) & {"indicator", "led", "light", "lamp"}:  # whole tokens: 'flight_id' is not a light
        return "indicator"
    if "model" in n or n in {"device_id", "serial"}:
        return "model"
    if n.endswith("_id") or n.endswith("_ref") or n in {"id", "ref", "reference"}:
        return "ref"
    if "city" in n or "location" in n or "place" in n or "address" in n:
        return "location"
    if any(k in n for k in ("description", "issue", "problem", "summary", "details", "notes", "message", "query")):
        return "freetext"
    if any(k in desc for k in ("free text", "describe", "description of")):
        return "freetext"
    if n in {"device", "appliance", "product", "item"}:
        return "device"
    return "name"


DEVICE_WORDS = ["washing machine", "dishwasher", "refrigerator", "fridge", "router", "modem", "air conditioner",
                "ac", "tv", "television", "laptop", "phone", "printer", "microwave", "oven", "thermostat"]


def fill_slots(p: Parse, spec: dict, perception: dict | None = None) -> dict[str, Any]:
    """Map mentions to the tool's parameters. Returns only confidently extracted slots."""
    props = spec.get("parameters", {}).get("properties", {})
    out: dict[str, Any] = {}
    locs = [m for m in p.mentions if m.etype in ("city", "place")]
    used: set[int] = set()
    types = {k: param_type(k, s) for k, s in props.items()}
    iata = {k: "iata" in str(s.get("description", "")).lower() or "airport code" in str(s.get("description", "")).lower()
            for k, s in props.items()}

    def loc_value(m: Mention, k: str) -> str:
        if m.etype == "city" and iata[k]:
            return next(code for n, code in CITIES.values() if n == m.value)
        return m.value

    for role in ("origin", "destination"):
        keys = [k for k, t in types.items() if t == role]
        if not keys:
            continue
        cand = [m for m in locs if m.role == role and id(m) not in used]
        if cand:
            out[keys[0]] = loc_value(cand[-1], keys[0])
            used.add(id(cand[-1]))
    # unlabeled locations: destination-like params first (corrections usually change the destination)
    free_locs = [m for m in locs if id(m) not in used and m.role is None]
    for k in sorted([k for k, t in types.items() if t in ("destination", "origin", "location") and k not in out],
                    key=lambda k: {"destination": 0, "location": 1, "origin": 2}[types[k]]):
        if free_locs:
            m = free_locs.pop(-1 if types[k] != "origin" else 0)
            out[k] = loc_value(m, k)

    by_type: dict[str, list[Mention]] = {}
    for m in p.mentions:
        by_type.setdefault(m.etype, []).append(m)
    perception = perception or {}
    for k, t in types.items():
        if k in out:
            continue
        s = props[k]
        if t == "enum":
            v = _match_enum(p.clean, s["enum"])
            if v is not None:
                out[k] = v
        elif t in ("date", "time", "count", "code", "model", "indicator") and by_type.get(t):
            out[k] = by_type[t][-1].value
        elif t in ("code", "model", "indicator") and perception.get(t):
            out[k] = perception[t]
        elif t == "freetext" and len(p.clean.split()) >= 3:
            out[k] = p.clean
        elif t == "device":
            low = p.clean.lower()
            devs = [d for d in DEVICE_WORDS if re.search(rf"\b{d}\b", low)]
            if devs:
                last = max(devs, key=lambda d: low.rfind(d))
                out[k] = last
        elif t == "name" and by_type.get("name"):
            out[k] = by_type["name"][-1].value
        if k in out and s.get("type") == "integer" and not isinstance(out[k], int):
            out.pop(k)
    return out


PRIORITY_SYN = {"high": ["urgent", "asap", "critical", "high", "emergency", "immediately"],
                "low": ["low", "whenever", "not urgent", "no rush"], "medium": ["medium", "normal"]}


def _match_enum(text: str, enum: list) -> Any:
    low = text.lower()
    hits = []
    for v in enum:
        vs = str(v).lower().replace("_", " ")
        words = [vs] + PRIORITY_SYN.get(vs, [])
        for w in words:
            for m in re.finditer(rf"\b{re.escape(w)}\b", low):
                hits.append((m.start(), v))
    if not hits:
        return None
    if any(re.search(r"\bnot urgent\b|\bno rush\b", low) for _ in [0]):
        hits = [h for h in hits if h[1] != "high"] or hits
    return max(hits)[1]  # last mentioned wins (self-repair friendly)


# ---------------------------------------------------------------- intent routing
def tool_tokens(spec: dict) -> tuple[set[str], set[str]]:
    name_t = {_stem(w) for w in spec["name"].lower().split("_") if w}
    desc_t = tokens(spec.get("description", "")) - name_t
    return name_t, desc_t


def route_intent(p: Parse, tools: dict[str, dict]) -> tuple[str | None, float]:
    utt = set(p.tokens)
    expanded = set(utt)
    for key, syns in SYNONYMS.items():
        if utt & {_stem(s) for s in syns} or key in utt:
            expanded.add(key)
    best, best_s = None, 0.0
    for name, spec in tools.items():
        name_t, desc_t = tool_tokens(spec)
        s = 2.0 * len(name_t & expanded) + 0.5 * len(desc_t & utt)
        props = spec.get("parameters", {}).get("properties", {})
        if props:
            s += 1.0 * len(fill_slots(p, spec)) / len(props)
        if s > best_s:
            best, best_s = name, s
    return (best, best_s) if best_s >= 2.0 else (None, best_s)
