// Audient runtime: runs the Python agent (the same code the benchmark scores) inside the
// browser with Pyodide, on the browser's real event loop. Actions stream out as DOM events.
const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";

export class AudientRuntime extends EventTarget {
  constructor() {
    super();
    this.py = null;
    this.session = null;
    this.manifest = [];
  }

  async load(onProgress = () => {}) {
    onProgress("Downloading Python runtime…");
    const { loadPyodide } = await import(PYODIDE_URL + "pyodide.mjs");
    this.py = await loadPyodide({ indexURL: PYODIDE_URL });
    onProgress("Loading the Audient agent…");
    const bundle = await (await fetch("py/bundle.json", { cache: "no-cache" })).json();
    for (const [path, src] of Object.entries(bundle.files)) {
      const full = "/home/pyodide/" + path;
      this.py.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
      this.py.FS.writeFile(full, src);
    }
    this.manifest = bundle.manifest;
    await this.py.runPythonAsync(`
import sys
if "/home/pyodide" not in sys.path:
    sys.path.insert(0, "/home/pyodide")
import audient.live
`);
    onProgress("Ready");
  }

  // perception: { analyzeFrame(ref) -> Promise<JSON string>, transcribe(ref) -> Promise<JSON string> }
  async start(perception = {}) {
    if (this.session) this.stop();
    const g = this.py.globals;
    g.set("_on_action", (s) => this.dispatchEvent(new CustomEvent("action", { detail: JSON.parse(s) })));
    g.set("_on_event", (s) => this.dispatchEvent(new CustomEvent("input", { detail: JSON.parse(s) })));
    g.set("_frame", perception.analyzeFrame ? (ref) => perception.analyzeFrame(ref) : null);
    g.set("_asr", perception.transcribe ? (ref) => perception.transcribe(ref) : null);
    g.set("_manifest", JSON.stringify(this.manifest));
    await this.py.runPythonAsync(`
import datetime, json
from audient.live import BridgePerception, LiveSession
_session = LiveSession(json.loads(_manifest), _on_action, _on_event,
                       perception=BridgePerception(_frame, _asr), session_date=datetime.date.today())
await _session.start()
`);
    this.session = g.get("_session");
  }

  // Feed one event (transcript / interruption / frame / audio). Returns the time-stamped event.
  push(ev) {
    const out = this.session.push(JSON.stringify(ev));
    const js = out.toJs({ dict_converter: Object.fromEntries });
    out.destroy();
    return js;
  }

  now() {
    return this.session ? this.session.now() : 0;
  }

  // { snapshot, calls, commits, holding_floor } straight from the Python session
  liveState() {
    return this.session ? JSON.parse(this.session.snapshot()) : null;
  }

  commits() {
    return JSON.parse(this.py.runPython("json.dumps(_session.commits())"));
  }

  stop() {
    if (!this.session) return;
    this.session.close();
    this.session.destroy();
    this.session = null;
  }
}
