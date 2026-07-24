"""ASR providers for press-meet videos (Telugu/Hindi/English, code-switched).

Sarvam STT (saarika) REST caps at 30s, so audio is chunked to ~25s windows via
ffmpeg and offsets are tracked → each transcript segment carries a
(start_s, end_s) span used for video-timestamp citations.
Fallback: local faster-whisper (native timestamps).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .sarvam import _unwrap, client

CHUNK_S = 25


def _extract_audio(video: Path, out_wav: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-ac", "1", "-ar", "16000",
         "-vn", str(out_wav)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _duration(media: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


class SarvamASR:
    def __init__(self, model: str = "saarika:v2"):
        self.model = model

    def transcribe(self, video: Path) -> list[tuple[float, float, str]]:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            wav = tmp / "audio.wav"
            _extract_audio(video, wav)
            total = _duration(wav)
            out: list[tuple[float, float, str]] = []
            start = 0.0
            idx = 0
            while start < max(total, CHUNK_S):
                seg = tmp / f"seg_{idx:04d}.wav"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(wav), "-ss", str(start),
                     "-t", str(CHUNK_S), "-ac", "1", "-ar", "16000", str(seg)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if not seg.exists() or seg.stat().st_size < 1024:
                    break
                with seg.open("rb") as fh:
                    resp = client().speech_to_text.transcribe(
                        file=fh, model=self.model, language_code="unknown",
                    )
                text = _unwrap(resp, "transcript", "text").strip()
                if text:
                    out.append((start, min(start + CHUNK_S, total or start + CHUNK_S), text))
                start += CHUNK_S
                idx += 1
                if total and start >= total:
                    break
            return out


class WhisperASR:
    def __init__(self, model: str = "large-v3"):
        from faster_whisper import WhisperModel
        self._model = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, video: Path) -> list[tuple[float, float, str]]:
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "audio.wav"
            _extract_audio(video, wav)
            segments, _ = self._model.transcribe(str(wav), vad_filter=True)
            return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]


class NoASR:
    def transcribe(self, video: Path) -> list[tuple[float, float, str]]:
        return []
