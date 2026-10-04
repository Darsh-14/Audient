"""Generate the WAV clips used by the audio scenarios (A01-A06).

Speech is synthesised with the Windows built-in voices (System.Speech: Microsoft David / Zira), 16 kHz mono
16-bit, so the clips are reproducible. The silent clip is written directly. The generated WAVs are committed
in assets/audio/, so other platforms do not need to run this.

    python scripts/make_audio.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

OUT = Path(__file__).resolve().parents[1] / "assets" / "audio"

CLIPS = [  # file, voice, words
    ("a01_find_flights.wav", "Zira", "Find me flights from Mumbai to Delhi tomorrow for two people."),
    ("a02_search_delhi.wav", "David", "Search flights from Mumbai to Delhi on Friday."),
    ("a02_make_it_bangalore.wav", "David", "Actually, make it Bangalore."),
    # (Pune / Chennai were misheard by both Whisper tiny.en and base.en with this voice, so this clip uses Delhi / Goa)
    ("a03_book_delhi_goa.wav", "Zira", "Book the cheapest flight from Delhi to Goa tomorrow for two passengers."),
    ("a03_three_passengers.wav", "Zira", "Wait, make it three passengers."),
    ("a05_navigate_office.wav", "David", "Navigate to the office."),
    ("a05_airport_instead.wav", "David", "No wait, take me to the airport instead."),
    ("a06_ticket.wav", "Zira", "Create a support ticket. My washing machine is leaking, it's urgent."),
]

PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft {voice} Desktop')
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{path}', $fmt)
$s.Speak('{text}')
$s.Dispose()
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        print("speech clips need the Windows voices; the committed WAVs in assets/audio are used instead")
    else:
        for name, voice, text in CLIPS:
            script = PS.format(voice=voice, path=str(OUT / name), text=text.replace("'", "''"))
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    sf.write(OUT / "a04_silence.wav", np.zeros(32000, dtype=np.float32), 16000, subtype="PCM_16")  # 2 s
    for p in sorted(OUT.glob("*.wav")):
        info = sf.info(p)
        print(f"{p.name:30} {info.samplerate} Hz  {info.channels} ch  {info.duration:.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
