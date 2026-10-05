"""Fill the PRISM template with Audient content: at most 8 slides, as the updated Theme 05 guide asks (problem,
architecture, benchmark results, extension use case, what's next, plus the template's title, checklist and closing
slides). The queue-agent numbers are read from reports/results.json; the Full-Duplex-Bench v3 numbers are the FDB
dict below, copied from the scorers' output of the full runs (reports/fdb/<run>/evaluate_*.txt)."""
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

# FDB-v3, all 100 recordings, exact argument match (no GPT-4o judge). "audient" = the run with the hold timed from
# the end of speech (TAG=fix1); the run with the voice detector and the reply guard is still to come.
FDB = {"audient": {"pass": 47.0, "self": 70.6, "medium": 52.9, "hard": 36.7, "replied": 82, "latency": 12.1},
       "gemini2_5": {"pass": 47.0, "self": 41.2, "medium": 44.1, "hard": 40.0, "replied": 99, "latency": 13.2}}

PURPLE, BLUE, GREEN, ORANGE, RED = (RGBColor(0x6D, 0x28, 0xD9), RGBColor(0x25, 0x63, 0xEB), RGBColor(0x05, 0x96, 0x69),
                                    RGBColor(0xD9, 0x77, 0x06), RGBColor(0xDC, 0x26, 0x26))
INK, MUTED, CARD = RGBColor(0x11, 0x18, 0x27), RGBColor(0x47, 0x55, 0x69), RGBColor(0xF8, 0xF9, 0xFB)

prs = Presentation(str(TEMPLATE))
S = prs.slides


def clear_body(slide):
    for sh in list(slide.shapes):
        if sh.is_placeholder and sh.placeholder_format.idx == 1:
            sh._element.getparent().remove(sh._element)


def title(slide, subtitle, heading=None):
    t = next(sh for sh in slide.shapes if sh.is_placeholder and sh.placeholder_format.idx == 0)
    if heading:
        runs = t.text_frame.paragraphs[0].runs
        runs[0].text = heading
        for r in runs[1:]:
            r.text = ""
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
TEAM = {"Team Name": "Krenos", "College Name": "SRMIST",
        "Member Name & Email 1": "Haina Kumari, hainakumari1@gmail.com",
        "Member Name & Email 2": "Bipin Kumar, bipinkumar620013@gmail.com",
        "Member Name & Email 3": "Titas Ghosh, titas.ghosh7@gmail.com",
        "Submission Github link": "github.com/Darsh-14/Audient"}
for p in list(paras):
    if not p.runs:
        continue
    label = p.runs[0].text.split("-")[0].strip()
    if label == "Member Name & Email 4":  # a team of three
        p._p.getparent().remove(p._p)
        continue
    if label in TEAM:
        p.runs[0].text = f"{label} - {TEAM[label]}"
        for r in p.runs[1:]:
            r.text = ""
info.top = Inches(4.05)
info.width = Inches(7.4)  # one line per member
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

# ---------------------------------------------------------------- architecture (template slide 4)
s = S[3]
clear_body(s)
title(s, "Audient on Full-Duplex-Bench v3: Gemini Live through LiveKit, with a coordination layer between model and tools")
box(s, 0.6, 1.85, 5.9, 0.6, "User audio → LiveKit room\nFDB-v3 recording (48 kHz, fillers, pauses, self-corrections), streamed live", INK, 11)
box(s, 0.6, 2.8, 2.85, 1.35, "LOCAL VOICE DETECTOR\nSilero VAD on the user's audio:\nwhen the user starts and\nstops speaking", GREEN, 11)
box(s, 3.65, 2.8, 2.85, 1.35, "GEMINI LIVE\nGemini 2.5 Flash native audio:\nlistens, decides, speaks,\nissues tool calls", BLUE, 11)
box(s, 0.6, 4.5, 5.9, 1.2, "AUDIENT COORDINATION LAYER\nhold, then commit (2.0 s of quiet) · defer, don't drop\nnever repeat an identical call · ask for the reply if Gemini drops it", RED, 11)
box(s, 0.6, 6.05, 5.9, 0.6, "FDB-v3 mock tools and call log\n12 tools in 4 domains (travel, finance, housing, e-commerce), scored by FDB-v3", INK, 11)
arrow(s, 2.0, 2.45, 2.0, 2.8)
arrow(s, 5.1, 2.45, 5.1, 2.8)
arrow(s, 2.0, 4.15, 2.0, 4.5)
arrow(s, 5.1, 4.15, 5.1, 4.5)
arrow(s, 3.55, 5.7, 3.55, 6.05)
card(s, 6.75, 1.8, 6.0, 1.7, "HOLD, THEN COMMIT", None, [
    "Pauses: a call made in a pause mid-sentence waits until the user has been quiet for 2.0 s (pauses in FDB-v3 last 1.6-3.2 s).",
    "Quiet: measured on the user's own audio; LiveKit's Gemini plugin reports Gemini's replies as user speech.",
], GREEN, 10.5)
card(s, 6.75, 3.6, 6.0, 1.7, "DEFER, DON'T DROP", None, [
    "Talked over: if the user speaks again, the call waits; it is dropped only after a correction ('wait', 'actually') and a newer call to the same tool.",
    f"Result: self-corrections pass {FDB['audient']['self']:.1f}% vs {FDB['gemini2_5']['self']:.1f}% for FDB-v3's own Gemini agent.",
], RED, 10.5)
card(s, 6.75, 5.4, 6.0, 1.75, "NEVER REPEAT, NEVER SILENT", None, [
    "Idempotent: a call with the same tool and arguments runs once; its result is reused.",
    "Reply guard: when Gemini cancels its own call and drops the reply, the agent asks it once to report the result.",
], PURPLE, 10.5)

# ---------------------------------------------------------------- extension use case (template slide 7)
s = S[6]
clear_body(s)
title(s, "Beyond the benchmark's domains: hands-free appliance troubleshooting from a camera frame", "Extension use case")
card(s, 0.6, 1.8, 3.95, 5.35, "THE USE CASE", "Camera-grounded troubleshooting", [
    "'My washer shows error E-20…' → reads model WM-3000 from the frame, looks up E20.",
    "'…wait, no, the red light stopped, now it's blinking blue twice' → drops the stale E20 request, grounds the new symptom, answers from the manual (valve fault).",
    "Too-dark frame → asks the user to move closer instead of guessing.",
], GREEN)
card(s, 4.7, 1.8, 3.95, 5.35, "RUNS END TO END", "How to try it", [
    "Web app: python api/run.py, open localhost:8000, pick or upload a camera frame, then speak or type.",
    "Scripted run: python run_eval.py --agent audient --show V03 prints every timestamped action.",
    "Perception: RapidOCR + OpenCV read the model, error code and LED colour on the CPU.",
    "Scope: runs on Audient's original event-driven agent (same correction rules), not yet inside the LiveKit agent.",
], BLUE)
card(s, 8.8, 1.8, 3.95, 5.35, "WHO BENEFITS", "Where interruption-safety matters", [
    "Field technicians: hands-free manual lookup grounded in what the camera sees.",
    "Home users: describe a fault, change their mind, and get the answer for the fault they meant.",
    "People with disfluent speech: pauses, fillers and stutters don't cut them off.",
], PURPLE)

# ---------------------------------------------------------------- benchmark results (template slide 8)
s = S[7]
clear_body(s)
title(s, "Full-Duplex-Bench v3: all 100 recordings on GitHub Codespaces, arguments compared exactly (no GPT-4o judge)",
      "Benchmark results")
card(s, 0.6, 1.8, 3.6, 5.35, "WHAT WE COMPARED", None, [
    "Same model, voice and tools: FDB-v3's own gemini2_5 agent is the baseline; Audient adds its layer and a few instructions.",
    "Strict pass: every expected call, with the right arguments.",
    "Reproduce: bash fdb_agent/setup.sh, then bash fdb_agent/run_benchmark.sh audient.",
], PURPLE, 11.5)
fa, fb = FDB["audient"], FDB["gemini2_5"]
rows = [("Metric", "Audient", "FDB Gemini"),
        ("Strict pass rate", f"{fa['pass']:.1f}%", f"{fb['pass']:.1f}%"),
        ("Self-correction", f"{fa['self']:.1f}%", f"{fb['self']:.1f}%"),
        ("Medium (2 tools)", f"{fa['medium']:.1f}%", f"{fb['medium']:.1f}%"),
        ("Hard (3-tool chains)", f"{fa['hard']:.1f}%", f"{fb['hard']:.1f}%"),
        ("Replied at all", f"{fa['replied']}/100", f"{fb['replied']}/100"),
        ("Avg latency to reply", f"{fa['latency']:.1f} s", f"{fb['latency']:.1f} s")]
gt = s.shapes.add_table(len(rows), 3, Inches(4.35), Inches(1.85), Inches(4.6), Inches(3.9)).table
gt.columns[0].width, gt.columns[1].width, gt.columns[2].width = Inches(2.2), Inches(1.2), Inches(1.2)
for i, row in enumerate(rows):
    for j, val in enumerate(row):
        cell = gt.cell(i, j)
        cell.text = val
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.CENTER
        for r in p.runs:
            r.font.size = Pt(11)
            r.font.bold = i == 0 or j == 1
            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF) if i == 0 else INK
        cell.fill.solid()
        cell.fill.fore_color.rgb = PURPLE if i == 0 else (RGBColor(0xF3, 0xF0, 0xFF) if i % 2 else RGBColor(0xFF, 0xFF, 0xFF))
tb = s.shapes.add_textbox(Inches(4.35), Inches(5.9), Inches(4.6), Inches(1.2))
tb.text_frame.word_wrap = True
r = tb.text_frame.paragraphs[0].add_run()
r.text = ("Audient column: the full run before the voice-detector and reply-guard fixes; the full run with them is pending. "
          "Source: reports/fdb/<run>/evaluate_*.txt, from FDB-v3's own scorers.")
r.font.size, r.font.italic, r.font.color.rgb = Pt(9), True, MUTED
card(s, 9.1, 1.8, 3.65, 5.35, "WHAT HELD IT BACK", None, [
    "No reply: LiveKit's Gemini plugin reported Gemini's replies as user speech, so calls were held up to 20 s.",
    "Fixed: a local voice detector; of the 24 recordings that had gone silent, 18 now reply (was 6).",
    "Still silent: Gemini server errors (1011, 1007), outside our control.",
    "Exact match: '3000' vs 3000 fails without the judge.",
], ORANGE, 11.5)

# ---------------------------------------------------------------- what's next (template slide 9)
s = S[8]
clear_body(s)
title(s, "Turn the self-correction gain into a higher overall pass rate")
card(s, 0.6, 1.8, 5.95, 2.6, "BENCHMARK", "Close the gap", [
    "Full run with the voice detector and reply guard; report it whatever it shows.",
    "Normalise argument formats (dates, numbers, IDs) before a call runs, so exact match and the judge agree.",
], BLUE, 12.5)
card(s, 6.8, 1.8, 5.95, 2.6, "TURN-TAKING", "Local end-of-turn", [
    "Move end-of-turn to the local voice detector once LiveKit's Gemini plugin supports manual turns.",
    "Handle Gemini's own call cancellations directly instead of asking again for the reply.",
], GREEN, 12.5)
card(s, 0.6, 4.55, 5.95, 2.6, "EXTENSION", "Inside the LiveKit agent", [
    "Give the LiveKit agent the camera-troubleshooting tools, so the extension runs on the benchmark agent.",
    "Blink-pattern recognition across frames, not just LED colour.",
], PURPLE, 12.5)
card(s, 6.8, 4.55, 5.95, 2.6, "ON-DEVICE", "Galaxy-class deployment", [
    "Plain Python: the coordination layer has no model on the critical path, a natural fit for on-device execution.",
    "Swap the hosted speech model for an on-device one behind the same layer.",
], ORANGE, 12.5)

# ---------------------------------------------------------------- 11. checklist
s = S[10]
body = next(sh for sh in s.shapes if sh.is_placeholder and sh.placeholder_format.idx == 1)
title_ph = next(sh for sh in s.shapes if sh.is_placeholder and sh.placeholder_format.idx == 0)
title_ph.left, title_ph.top, title_ph.width, title_ph.height = Inches(0.6), Inches(0.22), Inches(12.1), Inches(0.95)
items = ["Working prototype code — public GitHub repo (Y/N): Y — github.com/Darsh-14/Audient",
         "README with reproducible setup instructions (Y/N): Y — README.md and fdb_agent/README.md (setup.sh, run_benchmark.sh)",
         "Demo video, 3 to 5 minutes (YouTube or Drive link): Google Drive folder, linked in README.md",
         "Presentation file (PPT or PDF) (Y/N): Y — this deck (8 slides)"]
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
r.text = "Repository: github.com/Darsh-14/Audient   ·   Team: Krenos, SRMIST"
r.font.size, r.font.color.rgb = Pt(13), MUTED

# keep 8 slides, in the guide's order: title, problem, architecture, results, extension, next, checklist, thanks
KEEP = [0, 1, 3, 7, 6, 8, 10, 11]
ids = prs.slides._sldIdLst
slides = list(ids)
for i, el in enumerate(slides):
    if i not in KEEP:
        prs.part.drop_rel(el.rId)
    ids.remove(el)
for i in KEEP:
    ids.append(slides[i])

prs.save(str(OUT))
print("saved", OUT)
