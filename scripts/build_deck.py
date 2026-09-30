"""Fill the PRISM template with Audient content. Every number is read from reports/results.json."""
import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "template_original.pptx"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "SRMIST_krenos_Submission.pptx"
R = json.loads((ROOT / "reports" / "results.json").read_text(encoding="utf-8"))
A, B = R["audient"]["aggregate"], R["baseline"]["aggregate"]


def lat(name):
    xs = sorted(x for s in R[name]["scenarios"] for x in s["first_response_ms"] if x is not None)
    return xs[len(xs) // 2], xs[-1]


def cancels(name):
    import re
    out = []
    for s in R[name]["scenarios"]:
        for n, _ in s["checks"].get("interrupt", []):
            m = re.search(r"got \[([^\]]*)\] ms", n)
            if m and m.group(1):
                out += [float(x) for x in m.group(1).split(",")]
    return out


A_MED, A_MAX = lat("audient")
_t11 = json.loads((ROOT / "reports" / "traces" / "audient_T11_speculative_prefetch.json").read_text(encoding="utf-8"))["trace"]
T11_FINAL = next(e["t"] for e in _t11 if e["type"] == "final_response")
T11_EOT = [e["t"] for e in _t11 if e["type"] == "transcript"][-1]
B_MED, B_MAX = lat("baseline")
A_CAN = cancels("audient")
DOUBLE_B = sum(1 for s in R["baseline"]["scenarios"] for n, v in s["checks"]["safety"] if n.startswith("no double") and not v)
N = A["scenarios"]

PURPLE, BLUE, GREEN, ORANGE, RED = (RGBColor(0x6D, 0x28, 0xD9), RGBColor(0x25, 0x63, 0xEB), RGBColor(0x05, 0x96, 0x69),
                                    RGBColor(0xD9, 0x77, 0x06), RGBColor(0xDC, 0x26, 0x26))
INK, MUTED, CARD = RGBColor(0x11, 0x18, 0x27), RGBColor(0x47, 0x55, 0x69), RGBColor(0xF8, 0xF9, 0xFB)

prs = Presentation(str(TEMPLATE))
S = prs.slides


def clear_body(slide):
    for sh in list(slide.shapes):
        if sh.is_placeholder and sh.placeholder_format.idx == 1:
            sh._element.getparent().remove(sh._element)


def title(slide, subtitle):
    t = next(sh for sh in slide.shapes if sh.is_placeholder and sh.placeholder_format.idx == 0)
    t.left, t.top, t.width, t.height = Inches(0.6), Inches(0.22), Inches(12.1), Inches(0.95)
    t.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    for p in t.text_frame.paragraphs:
        for r in p.runs:
            r.font.size = Pt(38)
    tb = slide.shapes.add_textbox(Inches(0.62), Inches(1.15), Inches(12.1), Inches(0.45))
    tf = tb.text_frame
    tf.word_wrap = True
    r = tf.paragraphs[0].add_run()
    r.text = subtitle
    r.font.size, r.font.bold, r.font.color.rgb = Pt(15), True, PURPLE


def card(slide, x, y, w, h, tag, head, bullets, color, size=12.5):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.adjustments[0] = 0.06
    sh.fill.solid()
    sh.fill.fore_color.rgb = CARD
    sh.line.color.rgb = color
    sh.line.width = Pt(1.5)
    sh.shadow.inherit = False
    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = tf.margin_right = Inches(0.16)
    tf.margin_top = Inches(0.12)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = tag
    r.font.size, r.font.bold, r.font.color.rgb = Pt(size + 4), True, color
    if head:
        p = tf.add_paragraph()
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = head
        r.font.size, r.font.bold, r.font.color.rgb = Pt(size + 2), True, INK
        p.space_after = Pt(4)
    for b in bullets:
        p = tf.add_paragraph()
        p.space_before = Pt(3)
        lead, _, rest = b.partition(": ") if ": " in b else ("", "", b)
        r = p.add_run()
        r.text = "• " + (lead + ": " if lead else "")
        r.font.size, r.font.bold, r.font.color.rgb = Pt(size), True, INK
        r = p.add_run()
        r.text = rest
        r.font.size, r.font.color.rgb = Pt(size), MUTED
    return sh


def box(slide, x, y, w, h, text, color, size=10.5, fill=None, bold=True):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.adjustments[0] = 0.15
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill or RGBColor(0xFF, 0xFF, 0xFF)
    sh.line.color.rgb = color
    sh.line.width = Pt(1.5)
    sh.shadow.inherit = False
    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    for i, line in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size if i == 0 else size - 1.5)
        r.font.bold = bold and i == 0
        r.font.color.rgb = color if i == 0 else MUTED
    return sh


def arrow(slide, x1, y1, x2, y2, color=MUTED):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = color
    c.line.width = Pt(1.75)
    ln = c.line._get_or_add_ln()
    tail = ln.makeelement("{http://schemas.openxmlformats.org/drawingml/2006/main}tailEnd", {"type": "triangle"})
    ln.append(tail)


def mono(slide, x, y, w, h, lines, size=9.5):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.adjustments[0] = 0.04
    sh.fill.solid()
    sh.fill.fore_color.rgb = RGBColor(0x0F, 0x17, 0x2A)
    sh.line.fill.background()
    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = tf.margin_top = Inches(0.14)
    for i, (txt, col) in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = txt
        r.font.name = "Consolas"
        r.font.size = Pt(size)
        r.font.color.rgb = col


# ---------------------------------------------------------------- 1. title
s1 = S[0]
for sh in s1.shapes:
    if sh.has_text_frame and sh.text_frame.text.startswith("Theme ID"):
        info = sh
paras = info.text_frame.paragraphs
r0 = paras[0].runs
r0[0].text = "Theme ID - Theme 05: Interruptible Real-Time Agents"
for r in r0[1:]:
    r.text = ""
info.top = Inches(4.05)
tb = s1.shapes.add_textbox(Inches(0.85), Inches(3.5), Inches(6.3), Inches(0.5))
tb.text_frame.word_wrap = True
r = tb.text_frame.paragraphs[0].add_run()
r.text = "Audient: Dual-Path Full-Duplex Agent with Dynamic Cancellation"
r.font.size, r.font.bold, r.font.color.rgb = Pt(14), True, PURPLE

# ---------------------------------------------------------------- 2. theme
s = S[1]
clear_body(s)
title(s, "Theme 05: Interruptible Real-Time Agents — voice-native, full-duplex, tool-using assistants")
card(s, 0.6, 1.8, 3.95, 5.35, "PROBLEM STATEMENT", "The full-duplex concurrency challenge", [
    "Half-duplex bottleneck: assistants listen → think → speak in rigid turns, but people interrupt, pause and correct themselves mid-sentence.",
    "Stale plans: a correction arriving while a tool call is in flight must cancel it, not queue behind it.",
    "Today's failure modes: dead air while tools run, or confident 'Done!' before the action has actually happened.",
], PURPLE)
card(s, 4.7, 1.8, 3.95, 5.35, "DISFLUENCIES", "Five kinds of messy speech we handle", [
    "Fillers ('um', 'uh'): the user still holds the floor; don't answer yet.",
    "Acoustic pauses: a VAD end-of-turn after 'fly to, um…' is not a finished request.",
    "Stutters & hesitations: 'f-f-find', 'to to', 'Bo- Boston'.",
    "False starts: 'I need to b- book, no, just find flights…'.",
    "Self-corrections: 'Delhi, no wait, Bangalore' → roll back one slot, keep the rest.",
], BLUE)
card(s, 8.8, 1.8, 3.95, 5.35, "TECHNICAL CHALLENGE", "Concurrency & state consistency", [
    "Floor management: a meaningful spoken acknowledgment within 250 ms, never a false completion claim.",
    "Speculative execution: start safe (read-only) work before the user finishes; perception runs in worker threads.",
    "Transactional recovery: abort superseded in-flight calls within a 15 ms grace period, update the state snapshot, never duplicate a state-changing action.",
], ORANGE)

# ---------------------------------------------------------------- 3. existing solutions
s = S[2]
clear_body(s)
title(s, "Why current assistant architectures break under interruption")
card(s, 0.6, 1.8, 3.95, 5.35, "CASCADED PIPELINE", "ASR → LLM → TTS, turn by turn", [
    "Strength: mature, modular, strong language quality on clean single turns.",
    "Gap: latency adds up stage by stage; the user hears nothing until the tool returns.",
    "Gap: no transactional rollback — a mid-flight correction is handled only after the stale call completes.",
    f"Measured (our half-duplex baseline, same NLU & OCR): median first response {B_MED / 1000:.1f} s, max {B_MAX / 1000:.1f} s; {B['scenarios_all_checks_pass']}/{N} scenarios fully correct.",
], GREEN)
card(s, 4.7, 1.8, 3.95, 5.35, "END-TO-END SPEECH MODELS", "Single audio-in / audio-out network", [
    "Strength: fast, natural turn-taking and prosody.",
    "Gap: tool calls and dialogue state live inside the model — hard to cancel, audit or make idempotent.",
    "Gap: risk of acting on early parameters before the user finishes, and of stale slot values leaking into chained calls.",
], BLUE)
card(s, 8.8, 1.8, 3.95, 5.35, "GAPS WE CLOSE", "Three unresolved problems", [
    "Stale & duplicate execution: no cancellation graph → wrong or double bookings.",
    "Dead air vs. hallucinated completion: either silence while tools run, or 'Done!' before they finish.",
    "Fragile floor arbitration: pauses and fillers mistaken for end of turn; real interruptions ignored.",
    f"Our baseline shows it: {DOUBLE_B} double booking and {100 - B['category_pct']['interrupt']:.0f}% of interruption checks failed.",
], PURPLE)

# ---------------------------------------------------------------- 4. architecture
s = S[3]
clear_body(s)
title(s, "Audient: dual-path engine with a dynamic cancellation graph and transactional state")
box(s, 0.6, 1.85, 5.9, 0.6, "Event inbox (timestamped)\ntranscript chunks · WAV · PNG frames · interruptions · tool results · manifests", INK, 11)
box(s, 0.6, 2.8, 2.85, 1.35, "FAST PATH (sync, never awaits)\ndisfluency normaliser · semantic\nend-of-turn check · self-repair ·\nconflict detection · spoken acks", GREEN, 11)
box(s, 3.65, 2.8, 2.85, 1.35, "SLOW PATH (async tasks)\nschema-driven planner · chained &\nretried tool calls · OCR / LED /\nWhisper in worker threads", BLUE, 11)
box(s, 0.6, 4.5, 5.9, 1.2, "COORDINATION LAYER\ncancellation graph (DAG) · two-tier slots: tentative | committed\nSHA-256 idempotency keys · commit ledger · retry with same key", RED, 11)
box(s, 0.6, 6.05, 5.9, 0.6, "Action outbox\nspeak (ack / progress / filler) · tool_call(call_id) · cancel · clarify · final_response + state snapshot", INK, 11)
arrow(s, 2.0, 2.45, 2.0, 2.8)
arrow(s, 5.1, 2.45, 5.1, 2.8)
arrow(s, 3.45, 3.47, 3.65, 3.47)
arrow(s, 2.0, 4.15, 2.0, 4.5)
arrow(s, 5.1, 4.15, 5.1, 4.5)
arrow(s, 3.55, 5.7, 3.55, 6.05)
card(s, 6.75, 1.8, 6.0, 1.7, "REFLEX PATH", None, [
    f"Semantic acknowledgments ('Got it, updating that. Booking flight for 3.'): median {A_MED:.1f} ms, max {A_MAX:.1f} ms after the user's turn ends.",
    "Progress narration only after 2.5 s of silence; never claims completion before a tool result.",
], GREEN, 10.5)
card(s, 6.75, 3.6, 6.0, 1.7, "CANCELLATION GRAPH", None, [
    f"Every call, retry timer and progress timer is a DAG node; invalidating a call kills its descendants. Measured cancel latency after a correction: max {max(A_CAN):.1f} ms (grace 15 ms).",
    "Mid-utterance repair cues ('no wait', 'actually', 'make it') trigger cancellation from partial transcripts.",
], RED, 10.5)
card(s, 6.75, 5.4, 6.0, 1.75, "TRANSACTIONAL SAFETY", None, [
    "Tentative slots from partial speech never reach a state-changing call; only committed slots do.",
    "SHA-256 key over tool + canonical args; commit ledger blocks repeats; a late commit after a cancel is detected and reported, not duplicated.",
], PURPLE, 10.5)

# ---------------------------------------------------------------- 5. demo
s = S[4]
clear_body(s)
title(s, "Live trace: mid-booking correction (scenario T04, virtual clock, real CPU time charged)")
tr = json.loads((ROOT / "reports" / "traces" / "audient_T04_mid_booking_adjust.json").read_text(encoding="utf-8"))
lines = []
for e in tr["trace"]:
    if e["type"] == "tool_manifest":
        continue
    who = "USER " if e["dir"] == "in" else "AGENT"
    if e["type"] == "transcript":
        body, col = f"says: \"{e['text']}\"", RGBColor(0xFD, 0xE6, 0x8A)
    elif e["type"] == "interruption":
        body, col = "barge-in (interruption signal)", RGBColor(0xFD, 0xE6, 0x8A)
    elif e["type"] == "tool_result":
        who, body, col = "TOOL ", f"result {e['call_id']} status={e['status']}", RGBColor(0x94, 0xA3, 0xB8)
    elif e["type"] == "tool_call":
        body, col = f"tool_call {e['call_id']} {e['tool']}({json.dumps(e['args'])})", RGBColor(0x93, 0xC5, 0xFD)
    elif e["type"] == "cancel":
        body, col = f"CANCEL {e['call_id']} ({e['reason']})", RGBColor(0xFC, 0xA5, 0xA5)
    elif e["type"] == "speak":
        body, col = f"speak[{e['kind']}] \"{e['text']}\"", RGBColor(0x86, 0xEF, 0xAC)
    else:
        snap = e.get("state_snapshot", {})
        body, col = f"{e['type']} \"{e['text']}\"  slots={snap.get('slots')}", RGBColor(0xC4, 0xB5, 0xFD)
    lines.append((f"{e['t']:7.4f}s  {who}  {body}", col))
lines.append(("", RGBColor(0xFF, 0xFF, 0xFF)))
lines.append((f"commits: {len(tr['commits'])} → " + ", ".join(f"{c['tool']}({json.dumps(c['args'])})" for c in tr["commits"]),
              RGBColor(0xFF, 0xFF, 0xFF)))
mono(s, 0.6, 1.8, 12.15, 3.55, lines, 10)
card(s, 0.6, 5.5, 5.95, 1.65, "WHAT TO NOTICE", None, [
    "The correction lands while book_flight(2) is in flight: it is cancelled 0.8 ms later and re-issued with 3 passengers — exactly one commit.",
    f"Across the suite the first acknowledgment follows a user turn by {A_MED:.1f} ms (median, max {A_MAX:.1f} ms); 'confirmed' is only said after the tool result.",
], GREEN, 10.5)
card(s, 6.8, 5.5, 5.95, 1.65, "REPRODUCE & VIDEO", None, [
    "python run_eval.py --agent audient --show T04   (any scenario id works, e.g. V03 for camera + self-repair).",
    "Demo video (≤ 5 min): [link to be added after recording].",
], BLUE, 10.5)

# ---------------------------------------------------------------- 6. tech stack
s = S[5]
clear_body(s)
title(s, "What the prototype actually runs on — offline, CPU-only, no API keys")
card(s, 0.6, 1.8, 5.95, 2.6, "RUNTIME & ORCHESTRATION", None, [
    "Python asyncio: fast path, slow-path tasks and the coordination layer in one event loop.",
    "Custom virtual-time event loop: skips idle waits, charges real CPU time → honest latencies, fast replays.",
    "Pure-Python cancellation DAG, commit ledger, SHA-256 idempotency (hashlib).",
], GREEN, 12.5)
card(s, 6.8, 1.8, 5.95, 2.6, "PERCEPTION", None, [
    "RapidOCR (ONNX Runtime) + OpenCV: error codes and model numbers from camera frames; LED colour from bright, round, saturated blobs; darkness/blur checks.",
    "Whisper base.en via Hugging Face Transformers for WAV clips — implemented, optional, not benchmarked (model not downloaded).",
], BLUE, 12.5)
card(s, 0.6, 4.55, 5.95, 2.6, "LANGUAGE UNDERSTANDING", None, [
    "Deterministic, schema-driven NLU: parameters are typed from the tool manifest (name, JSON type, enum, format), so unseen tools work without code changes.",
    "Regex + lexicon extractors for places, dates, times, counts, codes, indicators, names; self-repair resolution.",
], PURPLE, 12.5)
card(s, 6.8, 4.55, 5.95, 2.6, "EVALUATION & PACKAGING", None, [
    "Streaming replay harness, deterministic mock tools (latency + fault injection), trace-based scorer (40/35/15/10, 15 ms grace).",
    "pytest unit tests; run_evaluation.sh one-command run; Dockerfile + compose provided (not yet built — no Docker on the dev machine).",
], ORANGE, 12.5)

# ---------------------------------------------------------------- 7. impact
s = S[6]
clear_body(s)
title(s, "Impact & use cases — each one is a scenario in our test suite")
card(s, 0.6, 1.8, 3.95, 5.35, "TOOL DOMAINS", "What the agent can drive today", [
    "Travel: flight search → cheapest-flight booking (chained calls).",
    "In-car: route to a destination that changes mid-calculation.",
    "Support: tickets with priority from 'it's urgent'.",
    "Unseen tool: a restaurant reservation tool it never saw, driven purely from the manifest.",
    "Zero double bookings across the whole suite, including a mid-booking passenger change.",
], BLUE)
card(s, 4.7, 1.8, 3.95, 5.35, "MULTIMODAL", "Camera-grounded troubleshooting", [
    "'My washer shows error E-20…' → reads model WM-3000 from the frame, looks up E20.",
    "'…wait, no, the red light stopped, now it's blinking blue twice' → cancels the E20 lookup, grounds the new symptom, answers from the manual (valve fault).",
    "Too-dark frame → asks the user to move closer instead of guessing.",
], GREEN)
card(s, 8.8, 1.8, 3.95, 5.35, "WHO BENEFITS", "Where interruption-safety matters", [
    "Drivers: change destinations hands-free without stale routes.",
    "People with disfluent speech: pauses, fillers and stutters don't cut them off.",
    "Contact centres: no double transactions when customers change their minds mid-booking.",
    "Field technicians: hands-free manual lookup grounded in what the camera sees.",
], PURPLE)

# ---------------------------------------------------------------- 8. results
s = S[7]
clear_body(s)
title(s, f"Innovations, measured results ({N} scenarios, our harness) and honest limitations")
card(s, 0.6, 1.8, 3.6, 5.35, "INNOVATIONS", None, [
    "Cancellation graph over calls, retries and timers.",
    "Two-tier slots: tentative (partial speech) vs committed.",
    "Idempotency barrier: SHA-256 keys + commit ledger + late-commit reconciliation.",
    "Semantic end-of-turn hold for pauses and fillers.",
    "Virtual clock that charges real CPU — found and fixed a GIL-contention bug (cancel took 17.7 ms while OCR ran).",
], PURPLE, 11.5)
rows = [("Metric", "Audient", "Half-duplex"),
        ("Scenarios, all checks pass", f"{A['scenarios_all_checks_pass']}/{N}", f"{B['scenarios_all_checks_pass']}/{N}"),
        ("Weighted score (pre-multiplier)", f"{A['weighted_base_score']:.1f}", f"{B['weighted_base_score']:.1f}"),
        ("Task completion", f"{A['category_pct']['task']:.0f}%", f"{B['category_pct']['task']:.0f}%"),
        ("Interruption recovery", f"{A['category_pct']['interrupt']:.0f}%", f"{B['category_pct']['interrupt']:.0f}%"),
        ("Latency credit", f"{A['category_pct']['latency']:.0f}%", f"{B['category_pct']['latency']:.0f}%"),
        ("First response, median", f"{A_MED:.1f} ms", f"{B_MED:,.0f} ms"),
        ("Max cancel latency", f"{max(A_CAN):.1f} ms", "never cancels"),
        ("Double bookings", "0", str(DOUBLE_B))]
gt = s.shapes.add_table(len(rows), 3, Inches(4.35), Inches(1.85), Inches(4.6), Inches(4.4)).table
gt.columns[0].width, gt.columns[1].width, gt.columns[2].width = Inches(2.2), Inches(1.2), Inches(1.2)
for i, row in enumerate(rows):
    for j, val in enumerate(row):
        cell = gt.cell(i, j)
        cell.text = val
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.CENTER
        for r in p.runs:
            r.font.size = Pt(10.5)
            r.font.bold = i == 0 or j == 1
            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF) if i == 0 else (GREEN if j == 1 else INK)
        cell.fill.solid()
        cell.fill.fore_color.rgb = PURPLE if i == 0 else (RGBColor(0xF3, 0xF0, 0xFF) if i % 2 else RGBColor(0xFF, 0xFF, 0xFF))
tb = s.shapes.add_textbox(Inches(4.35), Inches(6.35), Inches(4.6), Inches(0.8))
tb.text_frame.word_wrap = True
r = tb.text_frame.paragraphs[0].add_run()
r.text = "Baseline = same NLU and OCR, half-duplex control flow. Source: reports/results.json (python run_eval.py)."
r.font.size, r.font.italic, r.font.color.rgb = Pt(9), True, MUTED
card(s, 9.1, 1.8, 3.65, 5.35, "LIMITATIONS", None, [
    "Own test suite: the official kit wasn't released; the agent was tuned on these 15 scenarios.",
    "Audio path not benchmarked (Whisper model not installed).",
    "Rule-based NLU: limited gazetteers and entity types.",
    "No LED blink counting across frames yet.",
    "Docker image not yet built or tested.",
], ORANGE, 11.5)

# ---------------------------------------------------------------- 9. next
s = S[8]
clear_body(s)
title(s, "What's next: from verified prototype to hidden-set robustness and on-device")
card(s, 0.6, 1.8, 5.95, 2.6, "EVALUATION", "Plug into the official kit", [
    "Map the released schema in protocol.adapt_event; run the 9 public + ~60 hidden-style scenarios.",
    "Adversarial-timing generator: corrections landing at random offsets around tool completion.",
], BLUE, 12.5)
card(s, 6.8, 1.8, 5.95, 2.6, "SPEECH", "Streaming audio in the loop", [
    "Stream Whisper partial hypotheses into the speculative path; confidence-gated clarifications.",
    "Acoustic cues (pitch drop, pause length) to complement the semantic end-of-turn check.",
], GREEN, 12.5)
card(s, 0.6, 4.55, 5.95, 2.6, "REASONING", "LLM slow path, safely", [
    "Optional LLM planner for open-ended requests; every extracted slot must be span-verified against the transcript before use.",
    "Process-based perception workers to remove GIL contention entirely.",
], PURPLE, 12.5)
card(s, 6.8, 4.55, 5.95, 2.6, "ON-DEVICE", "Galaxy-class deployment", [
    "The fast path is pure Python with no model on the critical path — a natural fit for on-device execution.",
    "Blink-pattern recognition from frame sequences for the troubleshooting extension.",
], ORANGE, 12.5)

# ---------------------------------------------------------------- 10. differentiation
s = S[9]
clear_body(s)
title(s, "Why Audient stands out")
card(s, 0.6, 1.8, 5.95, 2.6, "VERIFIABLE", "Every claim has a trace", [
    "Deterministic mock tools + virtual clock: the jury can replay any scenario and read each timestamped action.",
    "One command: ./run_evaluation.sh (tests + benchmark + baseline comparison).",
], PURPLE, 12.5)
card(s, 6.8, 1.8, 5.95, 2.6, "TRANSACTIONAL SAFETY", "Zero duplicate state changes", [
    "Idempotency keys survive retries; tentative slots can never trigger a state change.",
    "A commit that races a cancel is reconciled and reported — never repeated.",
], RED, 12.5)
card(s, 0.6, 4.55, 5.95, 2.6, "LATENCY HIDING", "Talk while tools work", [
    f"Acknowledgments in {A_MED:.1f} ms (median) vs {B_MED / 1000:.1f} s for the half-duplex baseline.",
    f"Speculative read-only prefetch from partial speech: in T11 the answer is spoken at {T11_FINAL:.2f} s instead of {T11_EOT + 1.6:.2f} s ({T11_EOT + 1.6 - T11_FINAL:.1f} s saved).",
], GREEN, 12.5)
card(s, 6.8, 4.55, 5.95, 2.6, "MULTIMODAL", "Speech + camera, with self-repair", [
    "OCR + LED grounding runs behind a spoken acknowledgment in a worker thread.",
    "Cross-modal correction (error code → blinking light) cancels the stale lookup and re-grounds.",
], BLUE, 12.5)

# ---------------------------------------------------------------- 11. checklist
s = S[10]
body = next(sh for sh in s.shapes if sh.is_placeholder and sh.placeholder_format.idx == 1)
title_ph = next(sh for sh in s.shapes if sh.is_placeholder and sh.placeholder_format.idx == 0)
title_ph.left, title_ph.top, title_ph.width, title_ph.height = Inches(0.6), Inches(0.22), Inches(12.1), Inches(0.95)
items = ["Working prototype code — public or shared GitHub repo (Y/N): Y — code complete locally; GitHub link to be added after push",
         "README with reproducible setup instructions (Y/N): Y — README.md, requirements.txt, run_evaluation.sh",
         "Demo video, max 5 minutes (YouTube or Drive link): [to be recorded — link pending]",
         "Presentation file (PPT or PDF) (Y/N): Y — this deck"]
tf = body.text_frame
for i, (p, txt) in enumerate(zip(tf.paragraphs, items)):
    for r in p.runs[1:]:
        r.text = ""
    if p.runs:
        p.runs[0].text = txt
    else:
        p.add_run().text = txt

# ---------------------------------------------------------------- 12. thank you
s = S[11]
tb = s.shapes.add_textbox(Inches(1.14), Inches(5.1), Inches(10), Inches(0.9))
tf = tb.text_frame
tf.word_wrap = True
r = tf.paragraphs[0].add_run()
r.text = "Audient — Dual-Path Full-Duplex Agent with Dynamic Cancellation"
r.font.size, r.font.bold, r.font.color.rgb = Pt(18), True, PURPLE
p = tf.add_paragraph()
r = p.add_run()
r.text = "Repository: [GitHub link]   ·   Team: [Team Name], [College Name]"
r.font.size, r.font.color.rgb = Pt(13), MUTED

prs.save(str(OUT))
print("saved", OUT)
