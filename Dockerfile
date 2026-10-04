FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fonts-dejavu-core libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-asr.txt ./
# CPU-only PyTorch (the default Linux wheels pull in CUDA); then fetch the speech model into the image,
# so audio works even if the evaluation machine is offline
RUN pip install --no-cache-dir -r requirements.txt -r requirements-asr.txt pytest \
        --extra-index-url https://download.pytorch.org/whl/cpu \
    && python -c "from transformers import WhisperProcessor as P, WhisperForConditionalGeneration as M; P.from_pretrained('openai/whisper-base.en'); M.from_pretrained('openai/whisper-base.en')"
COPY . .
CMD ["python", "run_eval.py"]
