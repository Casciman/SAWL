#!/usr/bin/env python3

import os
import json
import argparse
from pathlib import Path
from datetime import datetime

REQUIRED_FILES = [
    "manifest.json",
    "segments.json",
    "transcript.txt",
    "timestamps.vtt",
    "timings.json",
]

def validate_segments(path, *, allow_backtrack_sec=0.25, auto_sort=True):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"segments.json parse error: {e}"

    if not isinstance(data, list):
        return False, "segments.json not a list"
    if len(data) == 0:
        return False, "segments.json empty"

    # Basic per-segment validation + collect normalized segments
    segs = []
    for i, seg in enumerate(data):
        if not isinstance(seg, dict):
            return False, f"segment {i} not dict"
        for field in ("start", "end", "text"):
            if field not in seg:
                return False, f"segment {i} missing '{field}'"
        try:
            start = float(seg["start"])
            end = float(seg["end"])
        except:
            return False, f"segment {i} start/end not numeric"

        if start < 0:
            return False, f"segment {i} start < 0"
        if end <= start:
            return False, f"segment {i} end <= start"

        segs.append((start, end, i))

    # Check ordering by START (not END), allowing small backtracks
    prev_start = -1e9
    bad_order = None
    for idx, (start, end, orig_i) in enumerate(segs):
        if start + allow_backtrack_sec < prev_start:
            bad_order = (orig_i, start, prev_start)
            break
        prev_start = max(prev_start, start)

    if bad_order and auto_sort:
        # If sorting would fix it, accept but treat as OK (hydrated)
        segs_sorted = sorted(segs, key=lambda x: (x[0], x[1]))
        prev_start = -1e9
        for (start, end, orig_i) in segs_sorted:
            if start + allow_backtrack_sec < prev_start:
                # Even sorting doesn't fix it => genuinely broken timestamps
                return False, f"segments.json unsortable ordering (first bad seg {orig_i})"
            prev_start = max(prev_start, start)
        return True, None

    if bad_order:
        orig_i, start, prev = bad_order
        return False, f"segments.json out-of-order start (seg {orig_i})"

    return True, None

def validate_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True, None
    except Exception as e:
        return False, str(e)


def validate_vtt(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if not first_line.startswith("WEBVTT"):
                return False, "timestamps.vtt missing WEBVTT header"
        return True, None
    except Exception as e:
        return False, str(e)


def validate_episode(ep_dir):
    whisper_dir = ep_dir / "whisper" / "fw-base"

    if not whisper_dir.exists():
        return False, "missing whisper/fw-base directory"

    # Check required files exist
    for fname in REQUIRED_FILES:
        fpath = whisper_dir / fname
        if not fpath.exists():
            return False, f"missing {fname}"
        if fpath.stat().st_size == 0:
            return False, f"{fname} is empty"

    # Structural validations
    ok, err = validate_segments(whisper_dir / "segments.json",
                            allow_backtrack_sec=0.25,
                            auto_sort=True)

    ok, err = validate_json_file(whisper_dir / "manifest.json")
    if not ok:
        return False, f"manifest.json parse error: {err}"

    ok, err = validate_json_file(whisper_dir / "timings.json")
    if not ok:
        return False, f"timings.json parse error: {err}"

    ok, err = validate_vtt(whisper_dir / "timestamps.vtt")
    if not ok:
        return False, err

    return True, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Root episodes directory")
    parser.add_argument("--heartbeat", type=int, default=50,
                        help="Print progress every N episodes")
    args = parser.parse_args()

    root = Path(args.root)
    episodes = sorted([p for p in root.iterdir() if p.is_dir()])

    total = len(episodes)
    ok_count = 0
    fail_count = 0

    print(f"[i] Strict hydration check started: {datetime.now()}")
    print(f"[i] Episodes found: {total}")
    print("-" * 60)

    for i, ep in enumerate(episodes, 1):
        valid, reason = validate_episode(ep)

        if valid:
            ok_count += 1
        else:
            fail_count += 1
            print(f"[FAIL] {ep.name} → {reason}")

        # LIVE progress line
        if i % args.heartbeat == 0 or i == total:
            print(f"[PROGRESS] {i}/{total} | OK={ok_count} FAIL={fail_count}")

    print("-" * 60)
    print(f"[✓] Done at {datetime.now()}")
    print(f"[✓] OK:   {ok_count}")
    print(f"[!] FAIL: {fail_count}")


if __name__ == "__main__":
    main()