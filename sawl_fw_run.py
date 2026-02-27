#!/usr/bin/env python3
# sawl_fw_run.py
# Run from SAWL root. Creates needed dirs and writes into:
# data/episodes/<EP>/whisper/fw-<MODEL>/
#
# Adds:
# - --range E0022 E0025 (inclusive), maps E#### -> E####-YYYYMMDD by scanning data/episodes
# - Accepts --episode as either E#### (code) OR E####-YYYYMMDD (dir)
# - Loads data/episode_index.tsv (or --index) and stamps date/title into manifest.json (+ timings.json)
# - ffprobe duration_seconds saved into manifest.json
# - prints relative outdir
# - manifest/timings store portable relative paths
# - keeps artifacts/filenames intact

import argparse, json, platform, sys, time, subprocess, re, csv
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

def vtt_timestamp(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def write_vtt(segments, out_path: Path):
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{vtt_timestamp(seg['start'])} --> {vtt_timestamp(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")

def iso_now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def safe_rel(root: Path, p: Path) -> str:
    return str(p.resolve().relative_to(root.resolve()))

def get_audio_duration_seconds(audio_path: Path) -> Optional[float]:
    """
    Uses ffprobe to get duration in seconds (float).
    Returns None if ffprobe fails.
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(audio_path)
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        if not out:
            return None
        return float(out)
    except Exception:
        return None

EP_CODE_RE = re.compile(r"^E(\d{4})$")
EP_DIR_RE  = re.compile(r"^E(\d{4})-(\d{8})$")

def parse_ep_code(s: str) -> int:
    m = EP_CODE_RE.match(s.strip())
    if not m:
        raise ValueError(f"Episode code must look like E0022 (got {s!r})")
    return int(m.group(1))

def extract_ep_code_from_dirname(dir_name: str) -> Optional[str]:
    m = EP_DIR_RE.match(dir_name.strip())
    if not m:
        return None
    return f"E{int(m.group(1)):04d}"

def build_code_to_episode_dir_map(episodes_root: Path) -> Dict[int, str]:
    """
    Map numeric episode code -> full episode dir name like E0022-20180112
    """
    mapping: Dict[int, str] = {}
    if not episodes_root.exists():
        return mapping

    for p in episodes_root.iterdir():
        if not p.is_dir():
            continue
        name = p.name
        m = EP_DIR_RE.match(name)
        if m:
            code = int(m.group(1))
            mapping[code] = name
    return mapping

def episode_dir_for_code(code: int, mapping: Dict[int, str]) -> Optional[str]:
    return mapping.get(code)

def load_episode_index(tsv_path: Path) -> Dict[str, Dict[str, str]]:
    """
    Reads episode_index.tsv:
      episode<TAB>date<TAB>title
    Returns dict keyed by episode code (E####) -> {"date": "...", "title": "..."}.
    Missing/blank title allowed.
    """
    index: Dict[str, Dict[str, str]] = {}
    if not tsv_path.exists():
        return index

    with tsv_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        rdr = csv.DictReader(f, delimiter="\t")
        # Expected headers: episode, date, title
        for row in rdr:
            ep = (row.get("episode") or "").strip()
            if not ep:
                continue
            index[ep] = {
                "date": (row.get("date") or "").strip(),
                "title": (row.get("title") or "").strip(),
            }
    return index

def episode_index_payload(ep_dir_name: str, ep_index: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    """
    ep_dir_name: E####-YYYYMMDD
    ep_index keyed by E####
    """
    ep_code = extract_ep_code_from_dirname(ep_dir_name)
    if not ep_code:
        return None
    rec = ep_index.get(ep_code)
    if not rec:
        return {"episode": ep_code, "date": "", "title": ""}
    return {"episode": ep_code, "date": rec.get("date", ""), "title": rec.get("title", "")}

def resolve_episode_arg_to_dirname(s: str, mapping: Dict[int, str]) -> str:
    """
    Accepts:
      - E####-YYYYMMDD (returns as-is)
      - E#### (maps via data/episodes scan)
    """
    s = s.strip()
    if EP_DIR_RE.match(s):
        return s
    if EP_CODE_RE.match(s):
        code = parse_ep_code(s)
        ep = episode_dir_for_code(code, mapping)
        if not ep:
            raise ValueError(f"No episode dir found for code {s} under data/episodes/")
        return ep
    raise ValueError(f"--episode must be E#### or E####-YYYYMMDD (got {s!r})")

def run_one_episode(
    *,
    root: Path,
    episodes_root: Path,
    episode_dir_name: str,   # e.g. E0022-20180112
    model_name: str,
    compute_type: str,
    language: str,
    task: str,
    force: bool,
    ep_index: Dict[str, Dict[str, str]],
) -> int:
    ep_dir = episodes_root / episode_dir_name
    audio_path = ep_dir / "audio" / f"{episode_dir_name}.mp3"

    if not audio_path.exists():
        print(f"[!] Missing audio: {safe_rel(root, audio_path) if audio_path.exists() else str(audio_path)}", file=sys.stderr)
        return 2

    out_dir = ep_dir / "whisper" / f"fw-{model_name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    transcript_txt = out_dir / "transcript.txt"
    segments_json = out_dir / "segments.json"
    timestamps_vtt = out_dir / "timestamps.vtt"
    timings_json = out_dir / "timings.json"
    run_log = out_dir / "run.log"
    manifest_json = out_dir / "manifest.json"

    outputs = [transcript_txt, segments_json, timestamps_vtt, timings_json, run_log, manifest_json]

    if not force and any(p.exists() for p in outputs):
        print(f"[!] Outputs already exist in: {safe_rel(root, out_dir)}", file=sys.stderr)
        print("    Re-run with --force to overwrite.", file=sys.stderr)
        return 3

    duration_seconds = get_audio_duration_seconds(audio_path)
    ep_idx = episode_index_payload(episode_dir_name, ep_index)

    started_at = iso_now_utc()
    exit_code = 0
    start = time.time()

    import contextlib
    segments = []
    full_text_parts = []

    try:
        with run_log.open("w", encoding="utf-8") as log_fp:
            with contextlib.redirect_stdout(log_fp), contextlib.redirect_stderr(log_fp):
                from faster_whisper import WhisperModel

                model = WhisperModel(model_name, device="cpu", compute_type=compute_type)

                segments_iter, info = model.transcribe(
                    str(audio_path),
                    language=language,
                    task=task,
                    vad_filter=False,
                )

                for seg in segments_iter:
                    segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text})
                    full_text_parts.append(seg.text.strip())

    except Exception:
        exit_code = 1
        raise
    finally:
        elapsed = time.time() - start
        finished_at = iso_now_utc()

    transcript_txt.write_text("\n".join(full_text_parts).strip() + "\n", encoding="utf-8")
    segments_json.write_text(json.dumps(segments, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_vtt(segments, timestamps_vtt)

    timing_payload: Dict[str, Any] = {
        "tool": "faster-whisper",
        "model": model_name,
        "compute_type": compute_type,
        "language": language,
        "task": task,
        "episode": episode_dir_name,
        "audio": safe_rel(root, audio_path),
        "elapsed_seconds": round(elapsed, 3),
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "platform": {
            "python": sys.version.split()[0],
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }
    if ep_idx is not None:
        timing_payload["episode_index"] = ep_idx
    timings_json.write_text(json.dumps(timing_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    manifest: Dict[str, Any] = {
        "episode": episode_dir_name,
        "audio": {
            "path": safe_rel(root, audio_path),
            "duration_seconds": None if duration_seconds is None else round(duration_seconds, 3),
        },
        "run": {
            "tool": "faster-whisper",
            "model": model_name,
            "compute_type": compute_type,
            "language": language,
            "task": task,
            "device": "cpu",
            "elapsed_seconds": round(elapsed, 3),
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
        },
        "outputs": {
            "dir": safe_rel(root, out_dir),
            "transcript": "transcript.txt",
            "segments": "segments.json",
            "timestamps": "timestamps.vtt",
            "timings": "timings.json",
            "log": "run.log",
            "manifest": "manifest.json",
        },
        "platform": {
            "python": sys.version.split()[0],
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }
    if ep_idx is not None:
        manifest["episode_index"] = ep_idx
    manifest_json.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"[✓] Episode: {episode_dir_name}")
    print(f"[✓] Outdir:  {safe_rel(root, out_dir)}")
    if ep_idx is not None:
        title = (ep_idx.get("title") or "").strip()
        date  = (ep_idx.get("date") or "").strip()
        if title or date:
            print(f"[✓] Index:   {ep_idx.get('episode','')} {date} — {title}".rstrip())
        else:
            print(f"[✓] Index:   {ep_idx.get('episode','')} (no title)")
    if duration_seconds is not None:
        mins = duration_seconds / 60.0
        print(f"[✓] Audio:   {mins:.2f} min ({duration_seconds:.1f}s)")
    else:
        print(f"[!] Audio:   duration unavailable (ffprobe failed)")
    print(f"[✓] Time:    {elapsed:.2f}s")
    print()
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", help="Episode code E#### OR episode dir E####-YYYYMMDD (single run)")
    ap.add_argument("--range", nargs=2, metavar=("START", "END"),
                    help="Episode code range like E0022 E0025 (inclusive). Codes map to E####-YYYYMMDD by scanning data/episodes/")
    ap.add_argument("--model", default="base", help="faster-whisper model, e.g. base, small, medium, large-v3-turbo")
    ap.add_argument("--compute_type", default="int8", help="e.g. int8 (CPU-friendly), int8_float16, float16")
    ap.add_argument("--language", default="en")
    ap.add_argument("--task", default="transcribe", choices=["transcribe", "translate"])
    ap.add_argument("--force", action="store_true", help="Overwrite existing outputs for this model run")
    ap.add_argument("--index", default="data/episode_index.tsv",
                    help="TSV with episode/date/title (default: data/episode_index.tsv)")
    args = ap.parse_args()

    if not args.episode and not args.range:
        ap.error("Provide --episode E####(or E####-YYYYMMDD) OR --range E#### E####")

    root = Path.cwd()
    episodes_root = root / "data" / "episodes"
    mapping = build_code_to_episode_dir_map(episodes_root)

    ep_index_path = root / args.index
    ep_index = load_episode_index(ep_index_path)
    if not ep_index:
        # non-fatal; manifests will just omit episode_index or have blanks
        print(f"[!] episode_index not loaded (missing/empty?): {args.index}", file=sys.stderr)

    episodes_to_run: List[str] = []

    if args.episode:
        try:
            episodes_to_run = [resolve_episode_arg_to_dirname(args.episode, mapping)]
        except ValueError as e:
            ap.error(str(e))
    else:
        start_code = parse_ep_code(args.range[0])
        end_code = parse_ep_code(args.range[1])
        if end_code < start_code:
            ap.error("Range END must be >= START")

        missing_codes = []
        for code in range(start_code, end_code + 1):
            ep_name = episode_dir_for_code(code, mapping)
            if not ep_name:
                missing_codes.append(code)
            else:
                episodes_to_run.append(ep_name)

        if missing_codes:
            miss = ", ".join(f"E{c:04d}" for c in missing_codes[:30])
            more = "" if len(missing_codes) <= 30 else f" ... (+{len(missing_codes)-30} more)"
            print(f"[!] Missing episode dirs for: {miss}{more}", file=sys.stderr)

    any_fail = False
    for ep_name in episodes_to_run:
        rc = run_one_episode(
            root=root,
            episodes_root=episodes_root,
            episode_dir_name=ep_name,
            model_name=args.model,
            compute_type=args.compute_type,
            language=args.language,
            task=args.task,
            force=args.force,
            ep_index=ep_index,
        )
        if rc != 0:
            any_fail = True

    if args.range:
        print("[✓] Done.")
    sys.exit(1 if any_fail else 0)

if __name__ == "__main__":
    main()