# Audient on Full-Duplex-Bench v3

The updated Theme 05 guide scores agents by re-running [Full-Duplex-Bench v3](https://github.com/DanielLin94144/Full-Duplex-Bench/tree/main/v3)
(FDB-v3): 100 real human recordings with fillers, pauses, hesitations, false starts and self-corrections, answered by a
LiveKit voice agent that calls 12 mock tools. This folder is Audient packaged as that LiveKit agent, plus the scripts to
run FDB-v3 against it.

## Model provider and keys (declaration)

| What | Provider | Key (environment variable) |
|---|---|---|
| Speech in, reasoning, tool calls, speech out | Google **Gemini 2.5 Flash native audio** (`gemini-2.5-flash-native-audio-preview-12-2025`) over the Gemini Live API, through LiveKit's Google plugin | `GOOGLE_API_KEY` (Google AI Studio) |
| Real-time audio transport | **LiveKit Cloud** (free tier) | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| FDB-v3's own scoring | FDB-v3's ASR (`nvidia/parakeet-tdt-0.6b-v2`, public checkpoint) and, optionally, its GPT-4o judge | `OPENAI_API_KEY` (only for `--use-llm`) |

No keys are in the repository. `setup.sh` writes them into FDB-v3's `.env.local` (in `external/`, which git ignores).
At evaluation time the agent calls only Gemini and LiveKit; it calls no server of ours.

## How it works

```
recording (48 kHz) ──► LiveKit room ──► Gemini Live (listens, decides, speaks) ──► agent speech ──► recorded
                                              │ tool call
                                              ▼
                                   Audient coordination layer (coordination.py)
                                     • hold, then commit: a call waits until the user has paused (2.0 s)
                                     • defer, don't drop: if the user starts speaking again first, the call
                                       waits; it is dropped only if they said a correction ("wait",
                                       "actually", "instead"...) and the model issued a newer call to the
                                       same tool; otherwise it runs as requested
                                     • never repeat: an identical call (same tool, same arguments) is not
                                       executed twice; the earlier result is returned
                                              │ only calls that run
                                              ▼
                                   FDB-v3 mock tools (mock_apis.py) + FDB-v3 call log
```

The speech model, voice and tool definitions are the same as FDB-v3's `gemini2_5` template, so the difference from that
baseline is Audient's layer (and three sentences of instructions on corrections). Each conversation runs in its own
process, as LiveKit does on Linux.

| File | Role |
|---|---|
| `coordination.py` | The hold-then-commit and never-repeat rules (no LiveKit dependency; unit-tested in `tests/test_fdb_coordination.py`) |
| `audient_agent.py` | The LiveKit agent: Gemini Live, FDB-v3's 12 tools routed through the layer, FDB-v3's log format |
| `fdb_runner.py` | Runs FDB-v3's own pipeline in two phases (stream all recordings, then ASR/latency/tool-call extraction) |
| `run_benchmark.sh` | One command: start the agent, stream, score with FDB-v3's scorers, save results to `reports/fdb/<agent>/` |
| `setup.sh` | One-time setup on Linux: FDB-v3 at the tested commit, its data, Python 3.10 environment, `.env.local` |
| `run_baseline_agent.py` | Starts FDB-v3's unchanged template agent, for the baseline |
| `crt_quiet.py`, `quiet_run.py` | Windows only: a known LiveKit native crash exits instead of opening a dialog |

## Run it (GitHub Codespaces or any Linux machine)

1. Add the keys above as Codespaces secrets (GitHub > Settings > Codespaces > Secrets, allow this repository), then open a
   Codespace on this repository (4 cores / 16 GB recommended).
2. `bash fdb_agent/setup.sh` (one time; downloads 736 MB of recordings and the Python packages)
3. `bash fdb_agent/run_benchmark.sh audient travel_01` (one recording, a few minutes), then
   `bash fdb_agent/run_benchmark.sh audient` (all 100, about 2 hours) and `bash fdb_agent/run_benchmark.sh gemini2_5` (baseline).

Scores, per-recording results, the tool-call log and the run configuration land in `reports/fdb/<agent>/`.

## Status (honest)

* Built and unit-tested; on a few recordings it made exactly one correct call where FDB-v3's baseline agent called
  the same tool twice. **Not yet run on all 100 recordings**: on the Windows laptop it was built on, connections to
  LiveKit and Gemini timed out intermittently, so the full runs are meant for a Linux machine (above).
* Known open issues: Gemini's server-side turn detection sometimes never registers the end of the user's speech (no
  reply; also seen with the baseline). Moving turn detection to a local voice-activity detector is not supported by
  LiveKit's Gemini plugin yet (tried; reverted). The model sometimes picks a wrong date or year.
* Retries in `fdb_runner.py` are only for infrastructure failures and are recorded per recording.
