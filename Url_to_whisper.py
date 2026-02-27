#!/usr/bin/env python3
"""
url_to_whisper.py

Pipeline:
  URL -> download best audio (yt-dlp) -> transcribe (whisper)

Requires:
  - Python 3.9+
  - yt-dlp installed (pip or brew)
  - ffmpeg installed
  - whisper installed (pip install -U openai-whisper)  [local transcription]
      OR faster-whisper if you prefer (not used in this script)

Usage:
  python url_to_whisper.py "https://www.youtube.com/watch?v=VIDEOID" --model medium --outdir ./out
  python url_to_whisper.py "URL" --model large --language en --task transcribe
  python url_to_whisper.py "URL" --model small --task translate

Notes:
  - This downloads media from the URL. Make sure you have rights/permission.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"Missing dependency: '{name}'. Install it and ensure it's on PATH.\n"
            f"  - macOS (brew): brew install {name}\n"
            f"  - or follow official install steps"
        )
    return path


def run(cmd: list[str], cwd: Optional[Path] = None) -> None:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}):\n{' '.join(cmd)}\n\n{proc.stdout}")
    # For visibility:
    if proc.stdout.strip():
        print(proc.stdout, end="")


def download_audio(url: str, outdir: Path) -> Path:
    """
    Downloads best available audio and converts to .m4a (or best audio container)
    Returns path to downloaded audio file.
    """
    outdir.mkdir(parents=True, exist_ok=True)

    # Put a stable filename in outdir
    # %(ext)s will be m4a (or other) after extraction.
    output_template = str(outdir / "audio.%(ext)s")

    # yt-dlp will download + extract audio; ffmpeg needed.
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "m4a",
        "--audio-quality",
        "0",
        "-o",
        output_template,
        url,
    ]
    run(cmd)

    # Find resulting file (audio.m4a)
    audio_path = outdir / "audio.m4a"
    if not audio_path.exists():
        # Fallback: search for any audio.* created
        matches = list(outdir.glob("audio.*"))
        if not matches:
            raise FileNotFoundError("yt-dlp finished but no audio file was found in outdir.")
        audio_path = matches[0]
    return audio_path


def transcribe_with_whisper(
    audio_path: Path,
    outdir: Path,
    model: str,
    language: Optional[str],
    task: str,
) -> None:
    """
    Uses the 'whisper' CLI to generate txt, srt, vtt, json outputs into outdir.
    """
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "whisper",
        str(audio_path),
        "--model",
        model,
        "--output_dir",
        str(outdir),
        "--output_format",
        "all",  # txt, vtt, srt, tsv, json
        "--task",
        task,  # transcribe or translate
    ]
    if language:
        cmd += ["--language", language]

    run(cmd)

    # Whisper CLI names outputs based on input filename. Help the user by printing paths.
    stem = audio_path.stem
    produced = {
        "txt": outdir / f"{stem}.txt",
        "srt": outdir / f"{stem}.srt",
        "vtt": outdir / f"{stem}.vtt",
        "json": outdir / f"{stem}.json",
    }
    print("\nOutputs:")
    for k, p in produced.items():
        if p.exists():
            print(f"  {k}: {p}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Download audio from a URL and transcribe with Whisper.")
    ap.add_argument("url", help="Video/audio URL (e.g., YouTube link)")
    ap.add_argument("--outdir", default="./transcripts", help="Output directory (default: ./transcripts)")
    ap.add_argument("--model", default="medium", help="Whisper model (tiny|base|small|medium|large)")
    ap.add_argument("--language", default=None, help="Language code (e.g., en). If omitted, auto-detect.")
    ap.add_argument("--task", default="transcribe", choices=["transcribe", "translate"], help="Task (default: transcribe)")
    ap.add_argument("--keep-audio", action="store_true", help="Keep downloaded audio file in outdir")
    args = ap.parse_args()

    # Hard deps
    require_tool("yt-dlp")
    require_tool("ffmpeg")
    require_tool("whisper")

    outdir = Path(args.outdir).expanduser().resolve()
    workdir = outdir / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading audio from: {args.url}")
    audio_path = download_audio(args.url, workdir)
    print(f"\nAudio saved to: {audio_path}")

    print("\nTranscribing with Whisper...")
    transcribe_with_whisper(
        audio_path=audio_path,
        outdir=outdir,
        model=args.model,
        language=args.language,
        task=args.task,
    )

    if args.keep_audio:
        # Move audio to outdir root
        final_audio = outdir / audio_path.name
        if final_audio.exists():
            final_audio.unlink()
        audio_path.replace(final_audio)
        print(f"\nKept audio at: {final_audio}")

    # Clean workdir if not keeping audio
    if not args.keep_audio:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
        
# brew install yt-dlp ffmpeg
# pip install -U openai-whisper        

# python url_to_whisper.py "https://www.youtube.com/watch?v=XXXXXXXXXXX" --model medium --outdir ./out --language en




