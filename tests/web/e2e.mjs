// End-to-end test of the web app in a real (headless) browser.
//   python api/run.py            # serves public/ + /api on :8000
//   npm i --no-save puppeteer    # once
//   node tests/web/e2e.mjs       # BROWSER=/path/to/chrome to choose the browser, SHOTS=dir for screenshots
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const require = createRequire(process.env.PUPPETEER_FROM || import.meta.url);
const puppeteer = require("puppeteer");
const URL_ = process.env.APP_URL || "http://127.0.0.1:8000/";
const SHOTS = process.env.SHOTS || "reports/web";
mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

const browser = await puppeteer.launch({
  executablePath: process.env.BROWSER || undefined, headless: true,
  args: ["--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
});
const errors = [];
try {
  const page = await browser.newPage();
  // a fixed location (Pune) for the live nearby-search and directions tools
  await browser.defaultBrowserContext().overridePermissions(new URL(URL_).origin, ["geolocation"]);
  await page.setGeolocation({ latitude: 18.5204, longitude: 73.8567 });
  await page.setViewport({ width: 1440, height: 900 });
  page.on("pageerror", (e) => errors.push(e.message));
  // network failures of the live services (weather, maps) are reported by the agent, not script errors
  page.on("console", (m) => { if (m.type() === "error" && !/favicon|404|Failed to load resource/.test(m.text())) errors.push(m.text()); });
  const t0 = Date.now();
  await page.goto(URL_);
  await page.waitForFunction(() => window.audient, { timeout: 120000 });
  check("agent boots in the browser", true, `${((Date.now() - t0) / 1000).toFixed(1)} s`);
  await page.evaluate(() => { window.audient.voice.on = false; }); // headless: no speech output

  // 0. left alone, it does nothing: no agent action, no status text appearing by itself
  await new Promise((r) => setTimeout(r, 5000));
  const idle = await page.evaluate(() => ({ outs: window.audient.S().trace.filter((r) => r.dir === "out").length,
    ins: window.audient.S().trace.filter((r) => r.dir === "in" && r.type !== "tool_manifest").length,
    hint: document.getElementById("hint").textContent.trim() }));
  check("idle for 5 s: no agent action, no status text", idle.outs === 0 && idle.ins === 0 && !idle.hint, JSON.stringify(idle));
  // an example only fills the input box: the user sends it, nothing is said on their behalf
  await page.click("#demos .demo:nth-child(1)");
  await new Promise((r) => setTimeout(r, 600));
  const ex = await page.evaluate(() => ({ box: document.getElementById("sayInput").value,
    ins: window.audient.S().trace.filter((r) => r.dir === "in" && r.type !== "tool_manifest").length }));
  check("example click fills the box, sends nothing", ex.ins === 0 && /alarm/i.test(ex.box), JSON.stringify(ex));
  // hands-free: the big button switches listening on (no separate "interrupt" click needed)
  const hf = await page.evaluate(() => {
    const sp = window.audient.speech; let started = 0;
    const real = sp.start.bind(sp); sp.start = () => { started++; sp.active = true; sp.dispatchEvent(new CustomEvent("started")); };
    document.getElementById("interruptBtn").click();
    const label = document.getElementById("interruptLabel").textContent;
    sp.active = false; sp.start = real;
    return { started, label };
  });
  await new Promise((r) => setTimeout(r, 200));
  check("big button turns hands-free listening on", hf.started === 1, JSON.stringify(hf));
  // barge-in detector: learns the echo level while the agent talks; a louder voice for ~0.2 s pauses it
  const bi = await page.evaluate(() => {
    const B = window.audient.barge.constructor, d = new B(); let t = 0, firstPause = null;
    for (; t < 1500; t += 16) if (d.feed(0.2 + 0.05 * Math.sin(t / 50), true, t) && firstPause === null) firstPause = t;  // echo only
    const echoOnly = firstPause; firstPause = null;
    const start = t;
    for (; t < start + 600; t += 16) if (d.feed(0.75, true, t) && firstPause === null) firstPause = t - start;  // the user speaks up
    return { echoOnly, pauseAfterMs: firstPause };
  });
  check("barge-in: echo alone never pauses the agent, the user's voice does within ~0.2 s",
        bi.echoOnly === null && bi.pauseAfterMs !== null && bi.pauseAfterMs <= 250, JSON.stringify(bi));
  // speech sounds natural: times, money, units and dates read the way people say them
  const sp = await page.evaluate(() => window.audient.speakable("Done. Your flight is confirmed: BK123 (6E-302, at 18:45, for ₹3,683). Pune, 2026-10-03, high 31°C, rain chance 40%."));
  check("speech reads times, rupees, degrees and dates naturally", /6:45 PM/.test(sp) && /3,683 rupees/.test(sp) && /31 degrees/.test(sp)
        && /3 October/.test(sp) && /40 percent/.test(sp) && !/[()]/.test(sp), sp);
  await page.evaluate(() => { document.getElementById("sayInput").value = ""; });
  // its own voice, heard by the mic just after it stopped speaking, is not taken as a new request
  const echo = await page.evaluate(() => {
    const v = window.audient.voice;
    v._remember("I found 3 flights from Mumbai to Delhi tomorrow");
    v._remember("Where will you be departing from?");
    return { own: v.isEcho("I found three flights from Mumbai to Delhi tomorrow"), user: v.isEcho("book the second one for two people"),
             reply: v.isEcho("departing from Chennai"), word: v.isEcho("Delhi") };
  });
  check("echo of its last reply ignored, real replies are not", echo.own && !echo.user && !echo.reply && !echo.word, JSON.stringify(echo));
  const spoken = await page.evaluate(() => window.audient.forSpeech(
    "I found 3 flights from Mumbai to Delhi tomorrow: AI-751 (Air India, at 18:45, for ₹3,683); 6E-302 (IndiGo, at 18:15, for ₹8,014)."));
  check("a long list is summarised when spoken (full list stays on screen)", !/AI-751/.test(spoken), spoken);

  await page.screenshot({ path: `${SHOTS}/first-load.png` });
  const runDemo = async (i, done, timeout = 30000, mid = null) => {
    await page.evaluate((i) => { window.audient.runDemo(i); }, i);
    if (mid) {
      await page.waitForFunction(mid.when, { timeout, polling: 50 });
      await new Promise((r) => setTimeout(r, 350)); // let the new row's entrance animation finish
      await page.screenshot({ path: `${SHOTS}/${mid.shot}` });
    }
    await page.waitForFunction(done, { timeout, polling: 100 });
    await new Promise((r) => setTimeout(r, 300));
    return page.evaluate(() => {
      const S = window.audient.S();
      const outs = S.trace.filter((r) => r.dir === "out");
      return {
        outs: outs.map((a) => ({ t: a.t, type: a.type, tool: a.tool, args: a.args, kind: a.kind, text: a.text, env: a.env, status: a.state_snapshot?.status })),
        ins: S.trace.filter((r) => r.dir === "in").map((e) => ({ t: e.t, type: e.type, text: e.text })),
        commits: S.live.commits, calls: S.live.calls, cancels: S.cancels, responses: S.responses,
      };
    });
  };
  const finalIn = (n = 1) => new Function(`const S = window.audient.S(); return S.trace.filter(r => r.type === "final_response").length >= ${n};`);

  // 1. booking corrected mid-flight
  const bookingRunning = () => (window.audient.S().live?.calls || []).some((c) => c.tool === "book_flight" && c.status === "inflight");
  let r = await runDemo(0, finalIn(), 30000, { when: bookingRunning, shot: "live-inprogress.png" });
  const cancel = r.outs.find((a) => a.type === "cancel");
  check("booking demo: in-flight booking cancelled", !!cancel, cancel ? `cancel ${(r.cancels[0] * 1000).toFixed(1)} ms after the correction, env=${cancel.env}` : "");
  check("booking demo: exactly one write, for 3 passengers", r.commits.length === 1 && r.commits[0].args.passengers === 3, JSON.stringify(r.commits.map((c) => c.args)));
  check("booking demo: answer grounded in the booking", /BK\d{6}/.test(r.outs.at(-1).text), r.outs.at(-1).text);
  await page.screenshot({ path: `${SHOTS}/live-booking.png` });

  // 2. camera + spoken correction (OCR + LED in the browser)
  r = await runDemo(1, finalIn(), 90000);
  const lookups = r.outs.filter((a) => a.type === "tool_call" && a.tool === "lookup_manual");
  check("camera demo: model and code read from the frame", lookups.some((c) => c.args.device_model === "WM-3000" && c.args.error_code === "E20"), JSON.stringify(lookups.map((c) => c.args)));
  check("camera demo: stale E20 lookup cancelled", r.outs.some((a) => a.type === "cancel"));
  check("camera demo: answers the new symptom from the manual", /valve/i.test(r.outs.at(-1).text), r.outs.at(-1).text.slice(0, 90));
  await page.screenshot({ path: `${SHOTS}/live-camera.png` });

  // 3. pause mid-sentence: no premature action
  r = await runDemo(2, finalIn());
  const second = r.ins.filter((e) => e.type === "transcript")[1].t;
  const early = r.outs.filter((a) => a.t < second && ["tool_call", "clarify"].includes(a.type));
  check("pause demo: holds the floor during 'um…'", early.length === 0);
  check("pause demo: searches Mumbai → Chennai", r.outs.some((a) => a.type === "tool_call" && a.args.destination === "Chennai" && a.args.origin === "Mumbai"));

  // 4. streaming words: speculative search starts before the turn ends
  r = await runDemo(4, finalIn());
  const finalT = r.ins.filter((e) => e.type === "transcript").at(-1).t;
  const spec = r.outs.find((a) => a.type === "tool_call");
  check("streaming demo: search starts before the user finishes", spec && spec.t < finalT, spec ? `call at ${spec.t.toFixed(2)} s, turn ended ${finalT.toFixed(2)} s` : "");
  check("streaming demo: speculative result reused (single search)", r.outs.filter((a) => a.type === "tool_call").length === 1);

  // 5. in-car destination change, 6. unseen tool
  r = await runDemo(3, finalIn());
  check("route demo: office route cancelled, airport route answered", r.outs.some((a) => a.type === "cancel") && /airport/i.test(r.outs.at(-1).text));
  r = await runDemo(5, finalIn());
  check("unseen-tool demo: reservation committed once", r.commits.length === 1 && r.commits[0].tool === "reserve_table", JSON.stringify(r.commits.map((c) => c.args)));

  // 7. typed conversation through the real input box
  await page.evaluate(() => window.audient.newSession());
  await page.type("#sayInput", "Create a support ticket, my router, uh, no, my washing machine is leaking, it's urgent");
  await page.keyboard.press("Enter");
  await page.waitForFunction(finalIn(), { timeout: 15000 });
  const typed = await page.evaluate(() => window.audient.S().live.commits.map((c) => c.args));
  check("typed input: ticket for the washing machine, high priority", typed.length === 1 && typed[0].device === "washing machine" && typed[0].priority === "high", JSON.stringify(typed));
  await page.screenshot({ path: `${SHOTS}/live-typed.png` });

  // 7b. free-form follow-up: book an item from the results already shown, then an out-of-scope request
  await page.evaluate(() => window.audient.newSession());
  const sendText = async (text) => { await page.type("#sayInput", text); await page.keyboard.press("Enter"); };
  await sendText("Find flights from Mumbai to Delhi tomorrow");
  await page.waitForFunction(finalIn(1), { timeout: 15000 });
  await sendText("Book the second one for 2 people");
  await page.waitForFunction(finalIn(2), { timeout: 15000 });
  const follow = await page.evaluate(() => {
    const L = window.audient.S().live;
    const search = L.calls.find((c) => c.tool === "search_flights" && c.status === "done");
    return { second: search?.result?.flights?.[1]?.flight_id, commits: L.commits.map((c) => c.args), searches: L.calls.filter((c) => c.tool === "search_flights").length };
  });
  check("follow-up: books the 2nd result shown, no new search", follow.commits.length === 1 && follow.commits[0].flight_id === follow.second
        && follow.commits[0].passengers === 2 && follow.searches === 1, JSON.stringify(follow));
  const nCalls = await page.evaluate(() => window.audient.S().live.calls.length);
  await sendText("Book a hotel in Goa");
  await page.waitForFunction(() => window.audient.S().trace.some((r) => r.type === "clarify" && /can't/.test(r.text)), { timeout: 10000 });
  const scope = await page.evaluate(() => ({ calls: window.audient.S().live.calls.length, text: window.audient.S().trace.filter((r) => r.type === "clarify").pop().text }));
  check("out-of-scope request: says so, calls no tool", scope.calls === nCalls, scope.text.slice(0, 90));
  // the tasks live in a sidebar panel: closed until clicked, with a count on the button; Escape closes it
  const panel = () => page.evaluate(() => {
    const d = document.getElementById("tasksPanel"), b = document.getElementById("tasksBadge");
    return { open: getComputedStyle(d).visibility === "visible", badge: b.hidden ? "" : b.textContent,
             rows: [...d.querySelectorAll(".task b")].map((x) => x.textContent), expanded: document.getElementById("tasksToggle").getAttribute("aria-expanded") };
  });
  const closed = await panel();
  await page.click("#tasksToggle");
  await new Promise((r) => setTimeout(r, 400));
  const opened = await panel();
  await page.screenshot({ path: `${SHOTS}/tasks-panel.png` });
  await page.keyboard.press("Escape");
  await new Promise((r) => setTimeout(r, 400));
  const shut = await panel();
  check("tasks open from the sidebar, with a count, and close on Escape", !closed.open && closed.badge === String(opened.rows.length) && opened.rows.length >= 2
        && opened.open && opened.expanded === "true" && !shut.open && shut.expanded === "false", `badge ${closed.badge}; ${opened.rows.join(" | ")}`);
  const lines = await page.evaluate(() => document.querySelectorAll(".circuits, .corner, .tick").length);
  check("no HUD lines beside the listener or at the screen corners", lines === 0, `${lines} found`);

  // 7c. the wider domain: an alarm corrected mid-way is set once, and the weather comes from a live service
  await page.evaluate(() => window.audient.newSession());
  await sendText("Set an alarm for 7 am");
  await sendText("actually 7:30");
  await page.waitForFunction(finalIn(1), { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 600));
  const alarm = await page.evaluate(() => window.audient.S().live.commits.map((c) => c.args));
  check("alarm corrected mid-way: set once, for 07:30", alarm.length === 1 && alarm[0].time === "07:30", JSON.stringify(alarm));
  await page.evaluate(() => window.audient.newSession());
  const askWeather = async () => {
    await page.evaluate(() => window.audient.newSession());
    await sendText("What's the weather in Pune today?");
    await page.waitForFunction(finalIn(1), { timeout: 45000 });  // a live service: allow for a slow network
    return page.evaluate(() => { const c = window.audient.rt.liveState().calls.filter((x) => x.tool === "get_weather").pop(); return { result: c?.result, said: window.audient.S().trace.filter((r) => r.type === "final_response").pop()?.text }; });
  };
  let wx = await askWeather();
  if (wx.result?.source !== "open-meteo" && /failed/.test(wx.said || "")) wx = await askWeather();  // the service was briefly down: once more
  check("live weather from Open-Meteo", wx.result?.source === "open-meteo" && Number.isFinite(wx.result?.high_c), (wx.said || "").slice(0, 110));
  // nearby places and directions, live from OpenStreetMap around the (test) location
  await page.evaluate(() => window.audient.newSession());
  await sendText("Find the nearest movie theatre");
  await page.waitForFunction(finalIn(1), { timeout: 45000 });
  const pl = await page.evaluate(() => { const c = window.audient.rt.liveState().calls.find((x) => x.tool === "find_places"); return { r: c?.result, said: window.audient.S().trace.filter((r) => r.type === "final_response").pop()?.text }; });
  check("nearest cinema, live from OpenStreetMap", pl.r?.source === "openstreetmap" && pl.r.places?.length >= 1 && pl.r.places[0].distance_km < 13, (pl.said || "").slice(0, 120));
  await page.evaluate(() => window.audient.newSession());
  await sendText("Take me to the nearest cinema");
  await page.waitForFunction(finalIn(1), { timeout: 45000 });
  // the answer comes from the last completed route call (an earlier speculative one may have been cancelled)
  const rt2 = await page.evaluate(() => { const cs = window.audient.rt.liveState().calls.filter((x) => x.tool === "get_route");
    return { r: cs.filter((x) => x.result).pop()?.result, calls: cs.map((x) => `${x.status}${x.speculative ? "/spec" : ""}:${x.result?.source || "-"}`),
             said: window.audient.S().trace.filter((r) => r.type === "final_response").pop()?.text }; });
  check("real directions to it (OSRM)", rt2.r?.source === "osrm" && rt2.r.eta_min > 0 && rt2.r.distance_km > 0, `${(rt2.said || "").slice(0, 100)} [${rt2.calls.join(", ")}]`);
  // the intro describes the wider domain
  await sendText("hi");
  await page.waitForFunction(() => window.audient.S().trace.some((r) => r.type === "clarify" && /smart home/.test(r.text)), { timeout: 10000 });
  check("intro describes the wider domain", true, (await page.evaluate(() => window.audient.S().trace.filter((r) => r.type === "clarify").pop().text)).slice(0, 120));

  // 8. the glass listener (3D): reaches for a nearby cursor, perks its ears while you talk, keeps moving,
  //    and follows the agent's state
  await page.waitForFunction(() => window.audient.listener().renderer, { timeout: 30000 });
  const box = await page.evaluate(() => { const r = document.getElementById("creature").getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; });
  const cx = box[0] + box[2] / 2, cy = box[1] + box[3] / 2;
  const lis = () => page.evaluate(() => { const l = window.audient.listener(); return { reach: l.reach, ear: l.ear, t: l.t, frames: l.frames, state: l.state, rest: l.mood.ears }; });
  await page.mouse.move(5, 5);
  await new Promise((r) => setTimeout(r, 1200));
  const far = await lis();
  for (let i = 1; i <= 12; i++) await page.mouse.move(5 + ((cx + box[2] * 0.3 - 5) * i) / 12, 5 + ((cy - 5) * i) / 12);
  await new Promise((r) => setTimeout(r, 900));
  const nearC = await lis();
  check("listener reaches toward a nearby cursor and perks up", nearC.reach > 0.5 && far.reach < 0.15 && nearC.ear > far.ear + 0.15,
        `reach ${far.reach.toFixed(2)} -> ${nearC.reach.toFixed(2)}, ears ${far.ear.toFixed(2)} -> ${nearC.ear.toFixed(2)}`);
  await page.mouse.move(5, 5);
  // a voice: the app's own mic meter (fed by the fake device) is muted so only this test sets the level
  await page.evaluate(() => { const l = window.audient.listener(); l.setLevel = () => {}; window._talk = setInterval(() => { l.level = 0.6 + 0.3 * Math.random(); }, 50); });
  await new Promise((r) => setTimeout(r, 800));
  const talking = await lis();
  await page.evaluate(() => { clearInterval(window._talk); const l = window.audient.listener(); l.level = 0; });
  await new Promise((r) => setTimeout(r, 1500));
  const after = await lis();
  await page.evaluate(() => { delete window.audient.listener().setLevel; });  // the app's meter drives it again
  check("ears perk while you talk and relax after", talking.ear > 0.75 && after.ear < talking.ear - 0.15 && Math.abs(after.ear - after.rest) < 0.1,
        `ears ${talking.ear.toFixed(2)} while talking, ${after.ear.toFixed(2)} after (rest ${after.rest.toFixed(2)})`);
  const m1 = await lis(); await new Promise((r) => setTimeout(r, 600)); const m2 = await lis();
  check("listener keeps moving with nothing touching it", m2.t > m1.t && m2.frames > m1.frames, `time ${m1.t.toFixed(2)} -> ${m2.t.toFixed(2)}, ${m2.frames - m1.frames} frames`);
  // its colour follows what the agent is doing: from calm blue to violet (thinking) or pink (responding)
  const warmth = () => page.evaluate(() => { const c = window.audient.listener().colA; return { w: c.r - c.g, hex: c.getHexString(), state: window.audient.listener().state }; });
  const before = await warmth();
  await page.evaluate(() => window.audient.say("Take me to the airport"));
  await page.waitForFunction(() => window.audient.listener().state === "thinking", { timeout: 3000 });
  const seen = [];
  for (let i = 0; i < 15; i++) { seen.push(await warmth()); await new Promise((r) => setTimeout(r, 200)); }
  const warmest = seen.reduce((a, b) => (b.w > a.w ? b : a));
  check("listener changes colour as it thinks and responds", before.w < 0 && warmest.w > 0.05,
        `${before.hex} (${before.state}) -> ${warmest.hex} (${warmest.state}); states ${[...new Set(seen.map((s) => s.state))].join(", ")}`);
  await page.screenshot({ path: `${SHOTS}/hud.png` });

  // 9. phone width
  await page.setViewport({ width: 400, height: 860 });
  await new Promise((res) => setTimeout(res, 500));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check("phone width: no horizontal page scroll", overflow <= 0, `overflow ${overflow}px`);
  await page.screenshot({ path: `${SHOTS}/phone.png`, fullPage: false });
} catch (err) {
  check("test run completed", false, err.message);
} finally {
  await browser.close();
}
check("no JavaScript errors", errors.length === 0, errors.slice(0, 3).join(" | "));
const failed = results.filter((x) => !x.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
