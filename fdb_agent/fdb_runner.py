"""Run FDB-v3's own inference and scoring pipeline in two phases (nothing in FDB-v3's files is edited).

  infer  stream each recording into a LiveKit room with the agent running, exactly as FDB-v3's
         run_livekit_inference() does (same client script, room naming and STREAM_START_TIME parsing), and keep
         the room name and stream start time next to the output wav. No ASR model is loaded meanwhile, so the
         agent has the machine to itself (on a small machine the ASR model starved the agent).
  score  load FDB-v3's ASR model (nvidia/parakeet-tdt-0.6b-v2; on the GPU if there is one, else the CPU, where
         FDB-v3's own loader would fail by calling .cuda()) and run their process_single() for each recording;
         its call to run_livekit_inference() is answered from what "infer" saved, so their ASR, latency and
         tool-call extraction run as written.

Retries: a recording is streamed a second time only for infrastructure failures, recorded in its
inference_<provider>.json: the streaming client crashed, or (for the Audient agent, which reports when it
joined) the agent was not in the room when the recording started and so could not have heard the user. A wrong
or missing answer from an agent that was listening is never retried.

Usage (cwd: FDB-v3's v3/ directory):
  python <repo>/fdb_agent/fdb_runner.py infer --provider audient [--example travel_01]
  python <repo>/fdb_agent/fdb_runner.py score --provider audient [--example travel_01]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
V3 = Path(os.getenv("FDB_V3_DIR", HERE.parent / "external" / "Full-Duplex-Bench" / "v3")).resolve()
sys.path.insert(0, str(V3))

import run_tool_benchmark as rtb  # noqa: E402  (FDB-v3's module)

SIDECAR = "inference_{provider}.json"
HEARTBEAT_LOG = "/tmp/agent_heartbeat.log"


def load_asr_model():
    import nemo.collections.asr as nemo_asr
    import torch
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=rtb.ASR_MODEL_NAME)
    if torch.cuda.is_available():
        model = model.cuda()
    print(f"✅ ASR model loaded ({'GPU' if torch.cuda.is_available() else 'CPU'})")
    return model


def selected(args):
    inputs = rtb.discover_inputs()
    if args.example:
        inputs = [x for x in inputs if x[1] == args.example]
    return inputs


def stream(input_path, output_path):
    room = f"eval-{uuid.uuid4().hex[:8]}"
    print(f"  🔗 Streaming via livekit_inference.py into room: {room}")
    r = subprocess.run([sys.executable, str(HERE / "quiet_run.py"), str(V3 / "livekit_inference.py"),
                        "-i", str(input_path), "-o", str(output_path), "--room", room],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ❌ livekit_inference.py exited with code {r.returncode}: {(r.stderr or '').strip().splitlines()[-1:]}")
        return None, None, r.returncode
    start = next((float(l[19:]) for l in r.stdout.splitlines() if l.startswith("STREAM_START_TIME: ")), None)
    return room, start, 0


def ready_at(room):
    """When the Audient agent reported it had joined the room (AUDIENT_READY line), or None."""
    try:
        for line in open(HEARTBEAT_LOG, encoding="utf-8", errors="ignore"):
            if line.startswith(f"AUDIENT_READY {room} "):
                return float(line.split()[2])
    except OSError:
        pass
    return None


def infer(args):
    inputs = selected(args)
    print(f"🚀 inference for {len(inputs)} recording(s), provider={args.provider}")
    for i, (pid, example_id, input_path) in enumerate(inputs, 1):
        out = input_path.parent / f"output_{args.provider}.wav"
        side = input_path.parent / SIDECAR.format(provider=args.provider)
        if side.exists() and out.exists() and not args.force:
            print(f"[{i}/{len(inputs)}] {example_id} ({pid[:8]}): already streamed, skipping")
            continue
        t0 = time.time()
        attempts = []
        for _ in range(2):
            room, start, code = stream(input_path, out)
            late = None
            if room and args.provider == "audient":
                ready = ready_at(room)
                late = None if (ready is None or start is None) else round(ready - start, 2)
            agent_ok = args.provider != "audient" or (late is not None and late <= 1.0)
            attempts.append({"room": room, "exit_code": code, "agent_ready_after_start_s": late})
            if room is not None and agent_ok:
                break
            print(f"  ↻ infrastructure retry: client exit={code}, agent ready {late}s after the recording started")
            time.sleep(args.gap)
        side.write_text(json.dumps({"room_name": room, "stream_start_time": start, "attempts": attempts,
                                    "inference_time_s": round(time.time() - t0, 2)}), encoding="utf-8")
        print(f"[{i}/{len(inputs)}] {example_id} ({pid[:8]}): room={room} ok={room is not None} attempts={len(attempts)}")
        time.sleep(args.gap)  # let the agent finish closing the previous room (it uploads a session report)


def score(args):
    def replay_inference(input_path, output_path, provider):
        side = Path(input_path).parent / SIDECAR.format(provider=provider)
        if not side.exists():
            return None, None
        d = json.loads(side.read_text(encoding="utf-8"))
        return d.get("room_name"), d.get("stream_start_time")

    rtb.run_livekit_inference = replay_inference
    data = rtb.load_data()
    inputs = selected(args)
    asr = load_asr_model()
    done = failed = 0
    for pid, example_id, input_path in inputs:
        print(f"\n📂 {example_id} ({pid[:8]})")
        r = rtb.process_single(pid, example_id, input_path, args.provider, data, asr, force=True)
        done += bool(r and r.get("status") == "completed")
        failed += not (r and r.get("status") == "completed")
    print(f"\n📊 scored: completed={done} failed={failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["infer", "score"])
    ap.add_argument("--provider", default="audient")
    ap.add_argument("--example")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--gap", type=float, default=25.0)  # 5 s apart lost 1 of 3 LiveKit connections here; 25 s, 0 of 3
    a = ap.parse_args()
    infer(a) if a.phase == "infer" else score(a)
