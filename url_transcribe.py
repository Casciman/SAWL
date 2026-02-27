#!/usr/bin/env python3
"""
url_transcribe.py — transcribe public media URLs (YouTube, etc.) with faster-whisper.

Outputs (in --outdir):
  - transcript.txt
  - segments.json
  - timestamps.vtt
  - timings.json
  - manifest.json
  - audio.<ext> (downloaded / extracted)

Requires:
  - yt-dlp
  - ffmpeg installed on system (for audio extraction)
  - faster-whisper
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from faster_whisper import WhisperModel


def _run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"Command failed ({p.returncode}): {' '.join(cmd)}\n\nSTDOUT:\n{p.stdout}\n\nSTDERR:\n{p.stderr}"
        )


def _safe_slug(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-zA-Z0-9._ -]+", "", s)
    s = s.strip().replace(" ", "_")
    return s[:max_len] if len(s) > max_len else s


def _seconds_to_vtt(ts: float) -> str:
    if ts < 0:
        ts = 0.0
    ms = int(round(ts * 1000))
    h = ms // 3600000
    ms -= h * 3600000
    m = ms // 60000
    ms -= m * 60000
    s = ms // 1000
    ms -= s * 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _download_audio(url: str, outdir: Path) -> Path:
    """
    Use yt-dlp to extract best audio to a single file.
    Produces something like: outdir/audio.<ext>
    """
    outdir.mkdir(parents=True, exist_ok=True)
    # Let yt-dlp pick format; use ffmpeg to extract audio.
    # The %(ext)s will become m4a/mp3/opus/etc depending on source.
    template = str(outdir / "audio.%(ext)s")

    cmd = [
        "yt-dlp",
        "-x",  # extract audio
        "--audio-format",
        "best",
        "--audio-quality",
        "0",
        "-o",
        template,
        url,
    ]
    _run(cmd)

    # Find the produced audio.* file
    candidates = sorted(outdir.glob("audio.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("yt-dlp did not produce an audio file (audio.*).")
    return candidates[0]


def _transcribe(
    audio_path: Path,
    outdir: Path,
    model_name: str,
    compute_type: str,
    language: Optional[str],
    beam_size: int,
) -> Dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    model = WhisperModel(model_name, compute_type=compute_type)

    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=beam_size,
        vad_filter=True,
    )

    seg_list: List[Dict[str, Any]] = []
    full_text_parts: List[str] = []

    for seg in segments:
        text = (seg.text or "").strip()
        full_text_parts.append(text)
        seg_list.append(
            {
                "id": seg.id,
                "start": float(seg.start),
                "end": float(seg.end),
                "text": text,
                "avg_logprob": getattr(seg, "avg_logprob", None),
                "no_speech_prob": getattr(seg, "no_speech_prob", None),
                "compression_ratio": getattr(seg, "compression_ratio", None),
            }
        )

    transcript_text = "\n".join([t for t in full_text_parts if t]).strip() + "\n"
    (outdir / "transcript.txt").write_text(transcript_text, encoding="utf-8")

    (outdir / "segments.json").write_text(json.dumps(seg_list, ensure_ascii=False, indent=2), encoding="utf-8")

    # VTT
    vtt_lines = ["WEBVTT", ""]
    for i, s in enumerate(seg_list, start=1):
        vtt_lines.append(str(i))
        vtt_lines.append(f"{_seconds_to_vtt(s['start'])} --> {_seconds_to_vtt(s['end'])}")
        vtt_lines.append(s["text"])
        vtt_lines.append("")
    (outdir / "timestamps.vtt").write_text("\n".join(vtt_lines), encoding="utf-8")

    t1 = time.time()

    # Timing + summary stats
    audio_seconds = float(getattr(info, "duration", 0.0) or 0.0)
    proc_seconds = float(t1 - t0)
    rtf = (proc_seconds / audio_seconds) if audio_seconds > 0 else None
    speed = (audio_seconds / proc_seconds) if proc_seconds > 0 else None

    timings = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "processing_seconds": proc_seconds,
        "audio_seconds": audio_seconds,
        "real_time_factor": rtf,
        "speed_multiplier": speed,
        "model": model_name,
        "compute_type": compute_type,
        "language": language or getattr(info, "language", None),
        "beam_size": beam_size,
        "num_segments": len(seg_list),
    }
    (outdir / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")

    manifest = {
        "manifest_version": 1,
        "source_url": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "audio_file": str(audio_path.name),
        "model": model_name,
        "compute_type": compute_type,
        "language": timings["language"],
        "beam_size": beam_size,
        "audio_seconds": audio_seconds,
        "processing_seconds": proc_seconds,
        "real_time_factor": rtf,
        "speed_multiplier": speed,
        "files": {
            "transcript_txt": "transcript.txt",
            "segments_json": "segments.json",
            "timestamps_vtt": "timestamps.vtt",
            "timings_json": "timings.json",
        },
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return timings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Public media URL (YouTube, podcast page, direct mp3, etc.)")
    ap.add_argument("--outdir", default="out_transcribe", help="Output directory")
    ap.add_argument("--model", default="base", help="faster-whisper model (tiny/base/small/medium/large-v3, etc.)")
    ap.add_argument("--compute_type", default="int8", help="int8/float16/float32 (depends on your machine)")
    ap.add_argument("--language", default=None, help="Force language (e.g. en). Default: auto")
    ap.add_argument("--beam_size", type=int, default=5, help="Decoding beam size")
    ap.add_argument("--tag", default=None, help="Optional label appended to outdir name")
    args = ap.parse_args()

    outdir = Path(args.outdir).expanduser().resolve()
    if args.tag:
        outdir = outdir / _safe_slug(args.tag)

    outdir.mkdir(parents=True, exist_ok=True)

    # Download audio
    audio_path = _download_audio(args.url, outdir)
    print(f"[✓] Audio: {audio_path}")

    # Transcribe
    timings = _transcribe(
        audio_path=audio_path,
        outdir=outdir,
        model_name=args.model,
        compute_type=args.compute_type,
        language=args.language,
        beam_size=args.beam_size,
    )

    print(f"[✓] Outdir:  {outdir}")
    print(f"[✓] Audio:   {timings.get('audio_seconds', 0.0):.1f}s")
    print(f"[✓] Time:    {timings.get('processing_seconds', 0.0):.2f}s")
    if timings.get("speed_multiplier"):
        print(f"[✓] Speed:  {timings['speed_multiplier']:.2f}x (RTF {timings['real_time_factor']:.4f})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    
# python3 -m venv .venv
# source .venv/bin/activate
# pip install -U faster-whisper yt-dlp
# # macOS: if ffmpeg missing:
# brew install ffmpeg 

# python3 url_transcribe.py --url "https://www.youtube.com/watch?v=VIDEOID" --outdir data/public --tag "my_clip" --model base --compute_type int8