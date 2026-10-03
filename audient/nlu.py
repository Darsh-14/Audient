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
    "indore": ("Indore", "IDR"), "bhopal": ("Bhopal", "BHO"), "nagpur": ("Nagpur", "NAG"), "patna": ("Patna", "PAT"),
    "chandigarh": ("Chandigarh", "IXC"), "srinagar": ("Srinagar", "SXR"), "amritsar": ("Amritsar", "ATQ"),
    "varanasi": ("Varanasi", "VNS"), "guwahati": ("Guwahati", "GAU"), "bhubaneswar": ("Bhubaneswar", "BBI"),
    "coimbatore": ("Coimbatore", "CJB"), "thiruvananthapuram": ("Thiruvananthapuram", "TRV"),
    "trivandrum": ("Thiruvananthapuram", "TRV"), "visakhapatnam": ("Visakhapatnam", "VTZ"), "vizag": ("Visakhapatnam", "VTZ"),
    "vadodara": ("Vadodara", "BDQ"), "surat": ("Surat", "STV"), "raipur": ("Raipur", "RPR"), "ranchi": ("Ranchi", "IXR"),
    "mangalore": ("Mangalore", "IXE"), "mangaluru": ("Mangalore", "IXE"), "madurai": ("Madurai", "IXM"),
    "udaipur": ("Udaipur", "UDR"), "jodhpur": ("Jodhpur", "JDH"), "dehradun": ("Dehradun", "DED"), "leh": ("Leh", "IXL"),
    "port blair": ("Port Blair", "IXZ"), "bagdogra": ("Bagdogra", "IXB"), "jammu": ("Jammu", "IXJ"),
    "tirupati": ("Tirupati", "TIR"), "mysore": ("Mysore", "MYQ"), "mysuru": ("Mysore", "MYQ"), "agra": ("Agra", "AGR"),
    "kozhikode": ("Kozhikode", "CCJ"), "calicut": ("Kozhikode", "CCJ"), "vijayawada": ("Vijayawada", "VGA"),
    "imphal": ("Imphal", "IMF"), "aurangabad": ("Aurangabad", "IXU"), "gurgaon": ("Gurgaon", "DEL"), "gurugram": ("Gurgaon", "DEL"),
    "bangkok": ("Bangkok", "BKK"), "kathmandu": ("Kathmandu", "KTM"), "colombo": ("Colombo", "CMB"), "dhaka": ("Dhaka", "DAC"),
    "doha": ("Doha", "DOH"), "abu dhabi": ("Abu Dhabi", "AUH"), "muscat": ("Muscat", "MCT"), "riyadh": ("Riyadh", "RUH"),
    "jeddah": ("Jeddah", "JED"), "istanbul": ("Istanbul", "IST"), "frankfurt": ("Frankfurt", "FRA"), "munich": ("Munich", "MUC"),
    "amsterdam": ("Amsterdam", "AMS"), "zurich": ("Zurich", "ZRH"), "rome": ("Rome", "FCO"), "madrid": ("Madrid", "MAD"),
    "barcelona": ("Barcelona", "BCN"), "vienna": ("Vienna", "VIE"), "hong kong": ("Hong Kong", "HKG"),
    "kuala lumpur": ("Kuala Lumpur", "KUL"), "seoul": ("Seoul", "ICN"), "shanghai": ("Shanghai", "PVG"),
    "sydney": ("Sydney", "SYD"), "melbourne": ("Melbourne", "MEL"), "toronto": ("Toronto", "YYZ"),
    "vancouver": ("Vancouver", "YVR"), "los angeles": ("Los Angeles", "LAX"), "phuket": ("Phuket", "HKT"),
    "bali": ("Bali", "DPS"), "auckland": ("Auckland", "AKL"), "nairobi": ("Nairobi", "NBO"), "cairo": ("Cairo", "CAI"),
}
PLACES = ["airport", "railway station", "station", "home", "office", "work", "hospital", "mall", "hotel", "bus stand",
          "bus station", "bus stop", "metro station", "petrol pump", "petrol station", "gas station", "school", "college",
          "university", "park", "beach", "temple", "market", "gym", "pharmacy", "clinic", "stadium", "museum", "cinema"]
PLACES = sorted(PLACES, key=len, reverse=True)  # "bus station" before "station"
HINDI_DAYS = {"aaj": 0, "kal": 1, "parso": 2, "parson": 2}
# kinds of places people look for nearby (surface words -> one canonical kind, shared with public/livetools.js)
PLACE_KINDS = {
    "movie theatre": "cinema", "movie theater": "cinema", "theatre": "cinema", "theater": "cinema", "cinema": "cinema",
    "multiplex": "cinema", "restaurant": "restaurant", "place to eat": "restaurant", "cafe": "cafe", "coffee shop": "cafe",
    "hospital": "hospital", "clinic": "clinic", "pharmacy": "pharmacy", "chemist": "pharmacy", "medical store": "pharmacy",
    "atm": "atm", "bank": "bank", "petrol pump": "fuel", "petrol station": "fuel", "gas station": "fuel", "fuel station": "fuel",
    "charging station": "charging station", "ev charger": "charging station", "police station": "police",
    "supermarket": "supermarket", "grocery store": "supermarket", "mall": "mall", "park": "park", "hotel": "hotel",
    "parking": "parking", "gym": "gym", "temple": "temple", "mosque": "mosque", "church": "church", "school": "school",
    "college": "college", "bus stop": "bus stop", "metro station": "metro station", "railway station": "railway station",
    "airport": "airport", "bakery": "bakery", "bar": "bar", "pub": "pub", "museum": "museum", "library": "library",
    "post office": "post office", "toilet": "toilet", "washroom": "toilet",
}
KIND_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(PLACE_KINDS, key=len, reverse=True)) + r")(?:e?s)?\b")
CITY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(CITIES, key=len, reverse=True)) + r")\b")
PLACE_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in PLACES) + r")\b")
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
    "ticket": {"ticket", "complaint", "support", "broken", "leaking", "fault", "not", "noise", "noisy", "problem",
               "issue", "repair", "damaged", "stopped", "overheating", "jammed", "sparking", "smell"},
    "route": {"route", "navigate", "navigation", "directions", "drive", "take", "way", "head", "reach"},
    "lookup": {"mean", "means", "meaning", "what", "lookup", "look", "explain"},
    "manual": {"manual", "error", "code", "display", "showing", "light", "blinking", "troubleshoot", "fix"},
    "flight": {"flight", "flights", "fly", "flying", "plane"},
    "table": {"table", "dinner", "lunch", "restaurant"},
    # everyday assistant tools (web app manifest)
    "place": {"nearest", "nearby", "closest", "near", "around", "cinema", "theatre", "theater", "restaurant", "cafe",
              "pharmacy", "chemist", "atm", "petrol", "gym", "supermarket", "multiplex"},
    "weather": {"weather", "rain", "raining", "temperature", "forecast", "sunny", "humid", "umbrella"},
    "alarm": {"alarm", "wake"},
    "timer": {"timer", "countdown"},
    "reminder": {"remind", "reminder"},
    "message": {"message", "text", "sms", "whatsapp", "texting"},
    "call": {"call", "phone", "dial", "ring"},
    "music": {"music", "song", "songs", "play", "playlist", "album"},
    "device": {"light", "lights", "lamp", "fan", "heater", "geyser", "turn", "switch", "dim", "brightness", "ac", "tv"},
    "cab": {"cab", "taxi", "uber", "ola", "ride"},
    "note": {"note", "notes", "jot"},
    "calendar": {"calendar", "meeting", "event", "appointment"},
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
                       r"stop (?:that|it|searching)|don'?t bother|do not bother|abort)\b", re.I)
CONFIRM_RE = re.compile(r"^\s*(yes|yeah|yep|sure|ok(?:ay)?|go ahead|do it|please do|confirm)\b", re.I)
# whole-utterance social turns (no task content)
GREET_RE = re.compile(r"^\s*(?:hi+|hello|hey|hiya|namaste|yo|good (?:morning|afternoon|evening|day))"
                      r"(?:\s+(?:there|audient))?[\s!.,]*$", re.I)
HELP_RE = re.compile(r"\b(?:what can you do|what (?:do|can) you help|how can you help|what are you|who are you|"
                     r"what do you do|your (?:skills|capabilities)|help me with|what can i ask)\b|^\s*help[\s!.?]*$", re.I)
BACKCHANNEL_RE = re.compile(r"^\s*(?:yes|yeah|yep|ok(?:ay)?|sure|thanks|thank you|great|cool|perfect|alright|all right|"
                            r"got it|fine|mm+-?hmm+|uh-huh|right|nice)[\s!.,]*$", re.I)
QUESTION_RE = re.compile(r"^\s*(?:what|what's|whats|which|when|where|how|is|are|does|do|did|tell me|"
                         r"can you tell|could you tell)\b|\?\s*$", re.I)
# a request that points back at earlier results ("those flights", "the cheapest one")
ANAPHOR_RE = re.compile(r"\b(?:those|these|them|that one|this one|the same|earlier|previous|again|"
                        r"(?:the )?(?:first|second|third|cheapest|earliest|latest|last) (?:one|of))\b", re.I)


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
def _deli(m: re.Match) -> str:
    """ASR hears "Delhi" as "deli", but "Smoke House Deli" is a name: keep it after a capitalised word."""
    prev = re.findall(r"(\w+)\W*$", m.string[: m.start()])
    keep = prev and prev[-1][:1].isupper() and prev[-1].lower() not in ("to", "from", "i", "for", "in")
    return m.group(0) if keep else "Delhi"


ASR_FIXES = [(re.compile(r"\bno weight\b", re.I), "no wait"), (re.compile(r"\bdeli\b", re.I), _deli),
             (re.compile(r"\bbangaluru\b", re.I), "Bengaluru"), (re.compile(r"\bchenai\b", re.I), "Chennai")]


def clean_text(text: str) -> str:
    t = text.replace("\u2019", "'")
    t = re.sub(r"\b([Ww])on't\b", r"\1ill not", t)
    t = re.sub(r"\b([Cc])an't\b", r"\1an not", t)
    t = re.sub(r"\b(\w+)n't\b", r"\1 not", t)                           # isn't -> is not
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
    if len(w) > 4 and w.endswith("es") and re.search(r"(?:s|x|z|ch|sh)es$", w):
        return w[:-2]                                   # buses -> bus, matches -> match
    for suf in ("ing", "ed", "s"):                      # places -> place (not "plac"), flights -> flight
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"\b[a-z]+\b", text.lower()) if w not in STOP}


def _date_from(m_day: int, m_mon: int, ref: dt.date) -> dt.date:
    d = dt.date(ref.year, m_mon, m_day)
    return d if d >= ref else dt.date(ref.year + 1, m_mon, m_day)


def extract(text: str, ref_date: dt.date) -> Parse:
    clean = clean_text(text)
    low = clean.lower()
    ms: list[Mention] = []

    for m in CITY_RE.finditer(low):  # one pass; the alternation tries longer names first ("new delhi" before "delhi")
        pre = low[max(0, m.start() - 12): m.start()]
        post = low[m.end(): m.end() + 10]
        role = "origin" if re.search(r"\bfrom[\s,]+$", pre) or re.match(r"\s+se\b", post) else (  # "Delhi se" (Hindi)
            "destination" if re.search(r"\b(to|for|towards|into)[\s,]+$", pre) or re.match(r"\s+(?:ko|tak)\b", post)
            else None)
        ms.append(Mention("city", CITIES[m.group(1)][0], m.start(), m.end(), role))
    for m in PLACE_RE.finditer(low):
        if not any(x.start <= m.start() < x.end for x in ms):
            ms.append(Mention("place", m.group(1), m.start(), m.end(), "destination"))
    # places we have no list for: a capitalised name after from / to ("to Phoenix Marketcity", "from Indore")
    for m in re.finditer(r"\b(from|to|towards|into)\s+(?:the\s+)?([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,3})", clean):
        val, st = m.group(2), m.start(2)
        if (val.lower() in CITIES or val.lower() in WEEKDAYS or val.lower() in MONTHS or val.lower() in HINDI_DAYS
                or val.lower() in PLACES or val == "I" or re.match(r"[A-Z]+[- ]?\d", val)
                or any(x.start <= st < x.end for x in ms)):
            continue
        ms = [x for x in ms if not (x.etype == "place" and st <= x.start and x.end <= m.end(2))]
        ms.append(Mention("loc", val, st, m.end(2), "origin" if m.group(1) == "from" else "destination"))
    for m in re.finditer(r"\b(?:near|around)\s+([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,3})", clean):
        val, st = m.group(1), m.start(1)
        if val.lower() in CITIES or any(x.start <= st < x.end for x in ms):
            continue
        ms = [x for x in ms if not (x.etype == "place" and st <= x.start and x.end <= m.end(1))]
        ms.append(Mention("loc", val, st, m.end(1), None))
    # a lower-case destination after an explicit navigation verb ("navigate to phoenix marketcity")
    for m in re.finditer(r"\b(?:navigate|directions?|drive|take (?:me|us)|get (?:me|us)|head|go|way|route)\s+(?:back\s+)?"
                         r"(?:to|towards)\s+(?:the\s+)?([a-z][a-z'&-]*(?:\s+[a-z][a-z'&-]*){0,3})", low):
        words, st = [], m.start(1)
        for w in m.group(1).split():
            if w in LOC_STOP or w in WEEKDAYS or w in STOP:
                break
            words.append(w)
        if words and not any(st <= x.start < m.end(1) or x.start <= st < x.end for x in ms):
            end = st + len(" ".join(words))
            ms.append(Mention("loc", clean[st:end], st, end, "destination"))

    # dates
    for m in re.finditer(r"\b(aaj|kal|parso|parson)\b", low):                       # Hindi: today / tomorrow / day after
        ms.append(Mention("date", (ref_date + dt.timedelta(days=HINDI_DAYS[m.group(1)])).isoformat(), m.start(), m.end()))
    for m in re.finditer(r"\bin (\d{1,2}|" + "|".join(NUMWORDS) + r") days\b", low):
        n = int(NUMWORDS.get(m.group(1), m.group(1)))
        ms.append(Mention("date", (ref_date + dt.timedelta(days=n)).isoformat(), m.start(), m.end()))
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
    # numeric dates, read day-first (12/10 = 12 October) unless only month-first is valid (10/25)
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2}|\d{4}))?\b", low):
        if any(x.etype == "date" and x.start <= m.start() < x.end for x in ms):
            continue
        a, b = int(m.group(1)), int(m.group(2))
        day, mon = (a, b) if b <= 12 else (b, a)
        try:
            if m.group(3):
                d = dt.date(int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0), mon, day)
            else:
                d = _date_from(day, mon, ref_date)
            ms.append(Mention("date", d.isoformat(), m.start(), m.end()))
        except ValueError:
            pass
    # a day of the month on its own ("on the 15th"): this month, or next month if it has passed
    for m in re.finditer(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low):
        if any(x.etype == "date" and x.start <= m.start(1) < x.end for x in ms):
            continue
        day, y, mo = int(m.group(1)), ref_date.year, ref_date.month
        if day < ref_date.day:
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        try:
            ms.append(Mention("date", dt.date(y, mo, day).isoformat(), m.start(), m.end()))
        except ValueError:
            pass

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
        meal = re.search(r"\b(?:table|restaurant|reserv\w*|dine|dining)\b", low) and not re.search(
            r"\b(?:breakfast|brunch|morning|am)\b", low)                     # "a table at 7" means 7 pm
        if h < 12 and (h < 7 or re.search(r"tonight|evening|dinner|night|pm", low) or (meal and 5 <= h <= 10)):
            h += 12
        ms.append(Mention("time", f"{h:02d}:{mi:02d}", m.start(1), m.end()))
    # a bare clock time ("make it 6:30"): as written, or afternoon if the sentence says so
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})\b", low):
        if any(x.etype == "time" and x.start <= m.start() < x.end for x in ms):
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 12 and re.search(r"\b(?:pm|evening|tonight|night)\b", low):
            h += 12
        if h < 24 and mi < 60:
            ms.append(Mention("time", f"{h:02d}:{mi:02d}", m.start(), m.end()))
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
        explicit = bool(m.group(0).startswith("err") or re.search(r"(code|error|showing|shows|says)\s+$", pre))
        if explicit or clean[m.start(1)].isupper():
            if m.group(1) in {"at", "for", "to", "of", "on", "in", "is"}:
                continue
            ms.append(Mention("code", f"{m.group(1).upper()}{m.group(2)}", m.start(), m.end(), "explicit" if explicit else None))
    for m in re.finditer(r"\b([A-Z]{2,}[- ]?\d{2,}[A-Z]?)\b", clean):
        ms.append(Mention("model", m.group(1).replace(" ", "-"), m.start(), m.end()))
    # "AC900" fits both patterns; without "error" / "code" in front it is the model, not an error code
    models = [x for x in ms if x.etype == "model"]
    ms = [x for x in ms if not (x.etype == "code" and x.role != "explicit" and any(y.start <= x.start < y.end for y in models))]

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

    # a contact right after call / text / message ("Call Mom", "Text Rahul that ...")
    for m in re.finditer(r"\b(?i:call|text|message|ring|phone|dial|whatsapp|email)\s+([A-Z][\w']+(?:\s+[A-Z][\w']+)?)", clean):
        val = m.group(1)
        if val.lower() not in CITIES and not re.match(r"[A-Z]+[- ]?\d", val) and val.split()[0].lower() not in STOP:
            ms.append(Mention("name", val, m.start(1), m.end(1)))
    for m in re.finditer(r"\b(?:call|text|message|ring|phone|dial|whatsapp)\s+(?:my\s+)?(mom|mum|mother|dad|father|wife|husband|"
                         r"brother|sister|boss|son|daughter|grandma|grandpa)\b", low):
        if not any(x.etype == "name" and x.start <= m.start(1) < x.end for x in ms):
            ms.append(Mention("name", m.group(1).capitalize(), m.start(1), m.end(1)))
    # a kind of place to look for ("the nearest movie theatre")
    for m in KIND_RE.finditer(low):
        if not any(x.etype in ("loc", "name", "city") and x.start <= m.start() < x.end for x in ms):  # not "Koregaon Park"
            ms.append(Mention("kind", PLACE_KINDS[m.group(1)], m.start(), m.end()))
    # a level to set ("set the AC to 24 degrees", "brightness to 60 percent")
    for m in re.finditer(r"\b(?:to|at)\s+(\d{1,3})\s*(?:degrees?|\u00b0c?|percent|%)", low):
        ms.append(Mention("count", int(m.group(1)), m.start(), m.end()))
    # capitalised names after a preposition (restaurants, venues, people)
    for m in re.finditer(r"\b(?:at|called|named|to)\s+((?:[A-Z][\w'&]+)(?:\s+[A-Z][\w'&]+)*)", clean):
        val = m.group(1)
        if val.lower() in CITIES or val.lower() in WEEKDAYS or val.lower() in MONTHS or re.match(r"[A-Z]+[- ]?\d", val):
            continue
        ms.append(Mention("name", val, m.start(1), m.end(1)))

    ms = _apply_repairs(ms, low)
    toks = tokens(clean)
    if ROUTE_PHRASE_RE.search(low):  # "get me to", "how do I get to": navigation, though "get" is a stop word
        toks.add("route")
    return Parse(text=text, clean=clean, mentions=ms, cancel=bool(CANCEL_RE.search(low)),
                 confirm=bool(CONFIRM_RE.search(low)), tokens=toks)


LOC_STOP = {"please", "now", "instead", "and", "then", "via", "by", "quickly", "asap", "today", "tonight", "tomorrow",
            "at", "on", "in", "for", "with", "from", "so", "because", "right", "immediately", "fast"}
ROUTE_PHRASE_RE = re.compile(
    r"\b(?:get|take|drive|drop|bring)\s+(?:me|us)\b(?!\s+(?:a |an |the |some )?(?:ticket|table|flight|seat|cab|taxi|reservation))"
    r"|\bhow (?:do|can|to) (?:i |we )?(?:get|go|reach)\b|\b(?:get|go) to\b|\bthe way to\b")


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
    if n in {"kind", "category", "place_type", "type_of_place"}:
        return "kind"
    if n == "near":
        return "location"
    if n.endswith("_id") or n.endswith("_ref") or n in {"id", "ref", "reference"}:
        return "ref"
    if "city" in n or "location" in n or "place" in n or "address" in n:
        return "location"
    free = ("description", "issue", "problem", "summary", "details", "notes", "message", "query")
    if any(k in n for k in free) or n in {"note", "text", "content", "body"}:
        return "freetext"
    if any(k in desc for k in ("free text", "describe", "description of")):
        return "freetext"
    if n in {"device", "appliance", "product", "item"}:
        return "device"
    return "name"


DEVICE_WORDS = ["washing machine", "dishwasher", "refrigerator", "fridge", "router", "modem", "air conditioner",
                "ac", "tv", "television", "laptop", "phone", "printer", "microwave", "oven", "thermostat",
                "lights", "light", "lamp", "fan", "heater", "geyser", "speaker"]


def fill_slots(p: Parse, spec: dict, perception: dict | None = None) -> dict[str, Any]:
    """Map mentions to the tool's parameters. Returns only confidently extracted slots."""
    props = spec.get("parameters", {}).get("properties", {})
    out: dict[str, Any] = {}
    locs = [m for m in p.mentions if m.etype in ("city", "place", "loc")]
    used: set[int] = set()
    types = {k: param_type(k, s) for k, s in props.items()}
    iata = {k: "iata" in str(s.get("description", "")).lower() or "airport code" in str(s.get("description", "")).lower()
            for k, s in props.items()}

    def loc_value(m: Mention, k: str) -> str:
        if m.etype == "city" and iata[k]:
            return next((code for n, code in CITIES.values() if n == m.value), m.value)
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
            body = re.search(r"(?:\bsaying\b|\bthat says\b|\btelling (?:him|her|them)\b|\bremind me (?:to|about)\b|:)\s*(.+)$",
                             p.clean, re.I) if re.search(r"message|note|text|content|body", k) else None
            out[k] = body.group(1).strip(" .") if body and len(body.group(1).split()) >= 1 else p.clean
            if k == "query":  # "play some Arijit Singh songs" -> "Arijit Singh songs"
                out[k] = re.sub(r"^(?:please\s+)?(?:play|put on|search for|find|look up)\s+(?:me\s+)?(?:some\s+)?", "", out[k], flags=re.I) or p.clean
        elif t == "kind" and by_type.get("kind"):
            out[k] = by_type["kind"][-1].value
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


NEAR_RE = re.compile(r"\b(?:nearest|nearby|closest|near me|near here|around here|from here|around me|close by)\b", re.I)
PROBLEM_RE = re.compile(r"\b(?:not|broken|stopped|leaking|faulty|keeps|noise|noisy|smell|sparking|overheating|jammed)\b"
                        r"|\bmaking (?:a |an )?(?:\w+ )?(?:noise|sound)\b", re.I)


def route_intent(p: Parse, tools: dict[str, dict]) -> tuple[str | None, float]:
    utt = set(p.tokens)
    expanded = set(utt)
    for key, syns in SYNONYMS.items():
        if utt & {_stem(s) for s in syns} or key in utt:
            expanded.add(key)
    best, best_s = None, 0.0
    for name, spec in tools.items():
        name_t, desc_t = tool_tokens(spec)
        # a tool's noun counts fully; a generic verb in its name less ("make it 3" is not make_call)
        s = 2.0 * len((name_t - GENERIC_VERBS) & expanded) + 1.5 * len(name_t & GENERIC_VERBS & expanded) \
            + 0.5 * len(desc_t & utt)
        props = spec.get("parameters", {}).get("properties", {})
        if props:
            s += 1.0 * len(fill_slots(p, spec)) / len(props)
            # (not when an error code / indicator light is named: that's the device's display, for a manual lookup)
            names_display = any(m.etype in ("code", "indicator", "model") for m in p.mentions)
            if PROBLEM_RE.search(p.clean) and not names_display and any(
                    re.search(r"issue|problem|complaint|fault", k) for k in props):
                s += 1.5
            # "nearest", "nearby", "from here": a search around the user beats booking or routing by name
            if any(param_type(k, sc) == "kind" for k, sc in props.items()):
                if "route" in expanded:
                    s -= 1.0  # "take me to the nearest hospital": go there (the route tool finds it), don't just list
                elif NEAR_RE.search(p.clean):
                    s += 1.0
        if s > best_s:
            best, best_s = name, s
    return (best, best_s) if best_s >= 2.0 else (None, best_s)


# ---------------------------------------------------------------- scope and multi-request turns
GENERIC_VERBS = {"search", "find", "book", "create", "get", "lookup", "look", "reserve", "check", "set", "send", "order",
                 "cancel", "update", "schedule", "make", "show", "list", "add", "open", "buy", "request", "start", "compute"}
OBJ_SKIP = {"a", "an", "the", "me", "us", "my", "our", "some", "any", "cheapest", "earliest", "latest", "first", "second",
            "third", "last", "one", "best", "next", "this", "that", "those", "these", "it", "them", "again", "now", "please",
            "another", "new", "same", "quick", "cheap", "good", "direct", "people", "persons", "passengers", "adults",
            "guests", "seats", "travellers", "travelers", "up"}
OBJ_STOP = {"for", "to", "from", "at", "on", "in", "with", "by", "of", "and", "tomorrow", "today", "tonight", "between"}
OBJ_VERB_RE = re.compile(r"\b(?:book|reserve|order|buy|find|search for|search|look for|get|create|open|raise|file|log|"
                         r"make|set|send|show|take)\s+(.*)", re.I)
# "let me think", "one sec": the user is keeping the floor, not asking for anything
THINK_RE = re.compile(r"\b(?:let me think|let me see|give me a (?:sec|second|minute|moment)|one (?:sec|second|moment|minute)|"
                      r"i'?ll think|not sure yet|thinking about it)\b", re.I)
# "no, that's not what I wanted": the last answer missed, but nothing says what to change
REJECT_RE = re.compile(r"^\s*(?:no|nope|nah|wrong|that'?s (?:not|wrong)|that is (?:not|wrong)|not (?:that|this|what))\b", re.I)
SPLIT_RE = re.compile(r"\s*,?\s*\b(?:and then|and also|and|then|also|plus)\b\s+", re.I)


CONTROL_RE = re.compile(r"\b(?:turn|switch)\s+(?:\w+\s+){0,3}?(?:on|off|up|down)\b|\b(?:dim|brighten)\b", re.I)


def off_topic(p: Parse, spec: dict) -> bool:
    """The request only shares a generic verb with the tool and names something else ("book a cab").

    Tools are matched on names and descriptions, so "book" alone would route a cab or hotel request to
    book_flight; here the object of the verb has to belong to the tool's vocabulary.
    """
    name_t, desc_t = tool_tokens(spec)
    if CONTROL_RE.search(p.clean) and not (name_t | desc_t) & {"turn", "switch", "control", "power", "toggle"}:
        return True
    nouns = name_t - GENERIC_VERBS
    vocab = set(nouns) | desc_t
    for key, syns in SYNONYMS.items():
        if key in nouns:
            vocab |= {_stem(w) for w in syns} | {key}
    # the object of the verb decides: "take a train" is not a route even though "take" is a route word
    m = OBJ_VERB_RE.search(p.clean)
    if not m:
        return False
    props = spec.get("parameters", {}).get("properties", {})
    has_loc = any(param_type(k, s) in ("origin", "destination", "location") for k, s in props.items())
    cities = {x.value.lower() for x in p.mentions if x.etype in ("city", "loc")}
    places = {x.value.lower() for x in p.mentions if has_loc and x.etype == "place"}
    after_me = False  # "take me home": a place right after me/us is where to go; "book a hotel": the hotel is the thing
    for w in re.findall(r"[A-Za-z][A-Za-z'-]*|\d+", m.group(1)):
        wl = w.lower()
        if wl in OBJ_STOP:
            return False
        if wl in OBJ_SKIP or w.isdigit() or wl in NUMWORDS:
            after_me = after_me or wl in ("me", "us")
            continue
        if w[0].isupper() and not (w.isupper() and len(w) <= 3):
            return False  # a proper name ("book Olive Garden"): can't judge it; "AC", "TV" are ordinary words
        return _stem(wl) not in vocab and wl not in cities and not (after_me and wl in places)
    return False


def split_tasks(text: str, tools: dict[str, dict], ref_date: dt.date) -> list[str]:
    """Split "book a table ... and find flights ..." into one request per tool (else return [text])."""
    parts = [x.strip(" ,") for x in SPLIT_RE.split(text) if x.strip(" ,")]
    if len(parts) < 2:
        return [text]
    groups: list[list] = []
    for part in parts:
        intent, _ = route_intent(extract(part, ref_date), tools)
        if intent is not None and (not groups or groups[-1][0] != intent):
            groups.append([intent, part])
        elif groups:
            groups[-1][1] += " and " + part   # a fragment ("Hyderabad on Monday") belongs to the request before it
        else:
            groups.append([None, part])
    if sum(1 for g in groups if g[0]) < 2:
        return [text]
    if groups[0][0] is None:                  # a leading fragment without a request joins the first one
        lead = groups.pop(0)[1]
        groups[0][1] = lead + " " + groups[0][1]
    return [g[1] for g in groups]
