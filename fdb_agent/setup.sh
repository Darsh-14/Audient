#!/usr/bin/env bash
# One-time setup for running Full-Duplex-Bench v3 (FDB-v3) with the Audient agent on Linux (e.g. GitHub Codespaces).
#
# Needs these environment variables (in Codespaces: repository or user secrets):
#   LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   your LiveKit Cloud project (free tier is enough)
#   GOOGLE_API_KEY                                      Gemini API key (Google AI Studio); used for the Live model
#   OPENAI_API_KEY (optional)                           only for FDB-v3's GPT-4o judge (--use-llm scoring)
#
# Creates (all under external/, which git ignores): the FDB-v3 checkout at the tested commit, its 736 MB
# benchmark audio, a Python 3.10 environment, and FDB-v3's .env.local. Safe to run again.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
EXT="$REPO/external"
FDB="$EXT/Full-Duplex-Bench"
V3="$FDB/v3"
VENV="$EXT/fdb-venv"
FDB_COMMIT=3e799c45a045256f47d5f1c9cda90157e2d2ec9e   # the FDB-v3 version this agent was run with
DATA_ID=1SO_4MTazWQ_jvCx0dtmpQ-t40bdd07yz              # FDB-v3 released data (Google Drive id from its README)
mkdir -p "$EXT"

echo "== 1/6 keys"
missing=0
for k in LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET GOOGLE_API_KEY; do
  if [ -z "${!k:-}" ]; then echo "   missing: $k"; missing=1; else echo "   found:   $k"; fi
done
[ "$missing" = 0 ] || { echo "Set the missing variables (Codespaces: Settings > Secrets) and run again."; exit 1; }

echo "== 2/6 ffmpeg"
command -v ffmpeg >/dev/null || { sudo apt-get update -y -qq && sudo apt-get install -y -qq ffmpeg; }
ffmpeg -version | head -1

echo "== 3/6 FDB-v3 at $FDB_COMMIT"
[ -d "$FDB/.git" ] || git clone -q https://github.com/DanielLin94144/Full-Duplex-Bench.git "$FDB"
git -C "$FDB" checkout -q "$FDB_COMMIT"
git -C "$FDB" log -1 --format="   %h %ad %s" --date=short

echo "== 4/6 Python 3.10 environment"
python3 -m pip install -q --user uv
python3 -m uv venv -q --python 3.10 "$VENV" 2>/dev/null || true
PY="$VENV/bin/python"
if python3 -c "import subprocess,sys; sys.exit(subprocess.call(['nvidia-smi'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))"; then
  TORCH_INDEX=()   # a GPU: the default (CUDA) PyTorch wheels
else
  # no GPU: the much smaller CPU build of PyTorch (best match across both indexes for everything else)
  TORCH_INDEX=(--extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match)
fi
python3 -m uv pip install -q --python "$PY" "${TORCH_INDEX[@]}" -r "$REPO/fdb_agent/requirements-fdb.txt"
(cd "$V3" && "$PY" -m livekit.agents download-files >/dev/null)
"$PY" -c "import livekit.agents, nemo; print('   livekit-agents', livekit.agents.__version__, '| nemo', nemo.__version__)"

echo "== 5/6 benchmark audio"
if [ ! -d "$V3/fdb_v3_data_released" ]; then
  mkdir -p "$EXT/data"
  (cd "$EXT/data" && "$PY" -m gdown --continue "https://drive.google.com/uc?id=$DATA_ID")
  "$PY" -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None; z.extractall(sys.argv[2])" \
    "$EXT/data/fdb_v3_data_released.zip" "$V3"
  rm -rf "$V3/__MACOSX"
fi
echo "   recordings: $(ls "$V3"/fdb_v3_data_released/*/input.wav | wc -l)"

echo "== 6/6 FDB-v3 .env.local (from your environment; not committed)"
{
  echo "LIVEKIT_URL=$LIVEKIT_URL"
  echo "LIVEKIT_API_KEY=$LIVEKIT_API_KEY"
  echo "LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET"
  echo "GOOGLE_API_KEY=$GOOGLE_API_KEY"
  [ -n "${OPENAI_API_KEY:-}" ] && echo "OPENAI_API_KEY=$OPENAI_API_KEY"
} > "$V3/.env.local"
chmod 600 "$V3/.env.local"
echo "Done. Next: bash fdb_agent/run_benchmark.sh audient travel_01   (one recording)  or  ... audient   (all 100)"
