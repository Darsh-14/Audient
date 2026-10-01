"""Slow-path perception: camera frames (OCR + LED indicator + quality) and audio (Whisper ASR).

Everything here is blocking and is run through ``vclock.compute`` in a worker thread, so
the fast path keeps reacting to interruptions while a frame or clip is processed.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np
from PIL import Image

CODE_RE = re.compile(r"\b(?:ERR(?:OR)?\s*)?([EF])\s*-?\s*(\d{1,3})\b")
MODEL_RE = re.compile(r"\b([A-Z]{2,}-?\d{3,}[A-Z]?)\b")
HUES = [("red", 0, 15), ("amber", 15, 45), ("yellow", 45, 70), ("green", 70, 170), ("blue", 170, 265),
        ("red", 330, 361)]


class Perception:
    def __init__(self, asr_model: str = "openai/whisper-base.en") -> None:
        self.asr_model = asr_model
        self._ocr = None
        self._asr = None

    # ------------------------------------------------------------------ setup hook
    def warmup(self) -> None:
        if self._ocr is not None:
            return  # models are stateless and shared; no session data is cached
        from rapidocr_onnxruntime import RapidOCR
        self._ocr = RapidOCR()
        try:  # ASR is optional: only used if the model is already on disk (no silent downloads)
            from transformers import WhisperForConditionalGeneration, WhisperProcessor
            self._asr = (WhisperProcessor.from_pretrained(self.asr_model, local_files_only=True),
                         WhisperForConditionalGeneration.from_pretrained(self.asr_model, local_files_only=True).eval())
        except Exception:
            self._asr = None

    # ------------------------------------------------------------------ vision
    def analyze_frame(self, path: str) -> dict[str, Any]:
        img = Image.open(path).convert("RGB")
        arr = np.asarray(img)
        gray = arr.mean(axis=2)
        out: dict[str, Any] = {"brightness": float(gray.mean())}
        lap = np.abs(np.diff(gray, 2, axis=0)).mean() + np.abs(np.diff(gray, 2, axis=1)).mean()
        out["sharpness"] = float(lap)
        if out["brightness"] < 35:
            out["ambiguous"] = "image_too_dark"
            return out
        if self._ocr is None:
            self.warmup()
        res, _ = self._ocr(arr[:, :, ::-1].copy())
        texts = [(t, float(c)) for _, t, c in (res or [])]
        out["ocr"] = texts
        joined = " ".join(t.upper() for t, c in texts if c >= 0.5)
        codes = sorted({f"{m.group(1)}{int(m.group(2))}" for m in CODE_RE.finditer(joined)})
        models = [m.group(1) for m in MODEL_RE.finditer(joined) if not CODE_RE.fullmatch(m.group(1))]
        if len(codes) == 1:
            out["code"] = codes[0]
        elif len(codes) > 1:
            out["ambiguous"] = "multiple_error_codes_visible"
        if models:
            out["model"] = models[0]
        led = self._led(arr)
        if led:
            out["indicator"] = f"{led}_solid"
        if "code" not in out and "indicator" not in out and "ambiguous" not in out:
            out["ambiguous"] = "no_code_or_indicator_visible" if out["sharpness"] > 4 else "image_blurry"
        return out

    @staticmethod
    def _led(arr: np.ndarray) -> str | None:
        """Largest bright, saturated, roughly round blob -> colour name."""
        import cv2
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV_FULL).astype(np.float32)
        h, s, v = hsv[..., 0] * 360 / 255, hsv[..., 1] / 255, hsv[..., 2] / 255
        mask = ((s > 0.55) & (v > 0.75)).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        best, best_area = None, 0
        for i in range(1, n):
            x, y, w, hh, area = stats[i]
            if area < 40 or area > 0.05 * mask.size or not (0.6 < w / max(hh, 1) < 1.6) or area / (w * hh) < 0.55:
                continue
            if area > best_area:
                hue = float(np.median(h[labels == i]))
                best = next(name for name, lo, hi in HUES if lo <= hue < hi)
                best_area = area
        return best

    # ------------------------------------------------------------------ audio
    def transcribe(self, path: str) -> dict[str, Any]:
        import soundfile as sf
        audio, sr = sf.read(path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
        if rms < 1e-3:
            return {"text": "", "ambiguous": "silence"}
        if self._asr is None:
            return {"text": "", "ambiguous": "asr_model_not_installed"}
        import torch
        import torchaudio
        if sr != 16000:
            audio = torchaudio.functional.resample(torch.from_numpy(audio), sr, 16000).numpy()
        proc, model = self._asr
        feats = proc(audio, sampling_rate=16000, return_tensors="pt").input_features
        with torch.inference_mode():
            gen = model.generate(feats, output_scores=True, return_dict_in_generate=True, max_new_tokens=120)
        text = proc.batch_decode(gen.sequences, skip_special_tokens=True)[0].strip()
        lp = float(model.compute_transition_scores(gen.sequences, gen.scores, normalize_logits=True).mean())
        return {"text": text, "avg_logprob": lp, "ambiguous": "low_asr_confidence" if lp < -1.0 else None}


# ---------------------------------------------------------------------- process isolation
_WORKER: Perception | None = None


def _w_init() -> None:
    global _WORKER
    _WORKER = Perception()
    _WORKER.warmup()


def _w_ping() -> bool:
    return _WORKER is not None and _WORKER._asr is not None


def _w_analyze(path: str) -> dict[str, Any]:
    return _WORKER.analyze_frame(path)


def _w_transcribe(path: str) -> dict[str, Any]:
    return _WORKER.transcribe(path)


class ProcessPerception:
    """Runs :class:`Perception` in a separate worker process.

    OCR / ASR pre- and post-processing is Python code that holds the GIL; run in a thread it
    delayed the agent's fast path by 10-23 ms (measured). In its own process it cannot: the
    calling thread only waits on a future, with the GIL released.
    """

    def __init__(self) -> None:
        self._pool = None
        self.asr_available = False

    def warmup(self) -> None:
        if self._pool is None:
            from concurrent.futures import ProcessPoolExecutor
            self._pool = ProcessPoolExecutor(max_workers=1, initializer=_w_init)
            self.asr_available = self._pool.submit(_w_ping).result()

    def analyze_frame(self, path: str) -> dict[str, Any]:
        return self._pool.submit(_w_analyze, path).result()

    def transcribe(self, path: str) -> dict[str, Any]:
        return self._pool.submit(_w_transcribe, path).result()
