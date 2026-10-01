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
  await page.setViewport({ width: 1440, height: 900 });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/favicon|404/.test(m.text())) errors.push(m.text()); });
  const t0 = Date.now();
  await page.goto(URL_);
  await page.waitForFunction(() => window.audient, { timeout: 120000 });
  check("agent boots in the browser", true, `${((Date.now() - t0) / 1000).toFixed(1)} s`);
  await page.evaluate(() => { window.audient.voice.on = false; }); // headless: no speech output

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

  // 8. the 3D octopus: renders, moves on its own, looks where the cursor goes, reacts to the agent
  await page.waitForFunction(() => window.audient.octo().renderer, { timeout: 30000 });
  const look = () => page.evaluate(() => { const o = window.audient.octo(); return { yaw: o.look.yaw, state: o.state, t: o.t }; });
  const box = await page.evaluate(() => { const r = document.getElementById("creature").getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; });
  const cx = box[0] + box[2] / 2, cy = box[1] + box[3] / 2;
  const goTo = async (x, y) => { for (let i = 1; i <= 12; i++) await page.mouse.move(cx + ((x - cx) * i) / 12, cy + ((y - cy) * i) / 12); await new Promise((r) => setTimeout(r, 900)); return look(); };
  const L = await goTo(box[0] - 120, cy), R = await goTo(box[0] + box[2] + 120, cy);
  check("octopus turns to look at the cursor", L.yaw < -0.3 && R.yaw > 0.3, `yaw ${L.yaw.toFixed(2)} (cursor left) / ${R.yaw.toFixed(2)} (cursor right)`);
  const armsAt = () => page.evaluate(() => Array.from(window.audient.octo().arms[0].mesh.geometry.attributes.position.array.slice(-3)));
  const a1 = await armsAt(); await new Promise((r) => setTimeout(r, 500)); const a2 = await armsAt();
  check("arms keep moving with the cursor still", Math.hypot(a1[0] - a2[0], a1[1] - a2[1], a1[2] - a2[2]) > 0.01, `tip moved ${Math.hypot(a1[0] - a2[0], a1[1] - a2[1], a1[2] - a2[2]).toFixed(3)} units in 0.5 s`);
  await page.evaluate(() => window.audient.say("Take me to the airport"));
  await page.waitForFunction(() => window.audient.octo().state === "thinking", { timeout: 3000 });
  check("octopus reacts to the agent's state", true, "state=thinking while the route is computed");

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
