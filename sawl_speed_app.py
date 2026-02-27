#!/usr/bin/env python3
"""
sawl_speed_app.py

Scan data/episodes/*/whisper/fw-base/{manifest.json,timings.json} and compute
weighted throughput (x realtime) per machine assignment range.

Usage:
  python3 sawl_speed_app.py
  python3 sawl_speed_app.py --episodes-root data/episodes --model base
  python3 sawl_speed_app.py --show-missing 20
"""

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Tuple, List


EP_DIR_RE = re.compile(r"^E(\d{4})-(\d{8})$")


@dataclass
class RangeStat:
    name: str
    start: int
    end: int
    # progress
    assigned_total: int = 0
    completed: int = 0
    failed: int = 0
    missing: int = 0
    # weighted totals for completed successes
    total_audio_s: float = 0.0
    total_elapsed_s: float = 0.0


def load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def which_bucket(code: int, buckets: List[RangeStat]) -> Optional[RangeStat]:
    for b in buckets:
        if b.start <= code <= b.end:
            return b
    return None


def fmt_hours(seconds: float) -> str:
    return f"{seconds/3600:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes-root", default="data/episodes", help="Path to episodes root")
    ap.add_argument("--model", default="base", help="Model name (expects whisper/fw-<model>/...)")
    ap.add_argument("--show-missing", type=int, default=0,
                    help="Show N sample episodes that are missing outputs (0 = none)")
    args = ap.parse_args()

    # Your machine assignment split
    buckets = [
        RangeStat("Studio", 22, 2000),
        RangeStat("M4",     2001, 2800),
        RangeStat("M1",     2801, 3074),
    ]

    fw_dir = f"fw-{args.model}"
    episodes_root = Path(args.episodes_root)

    if not episodes_root.exists():
        print(f"[!] Episodes root not found: {episodes_root}")
        return 2

    # Precompute assigned totals (purely by numeric range, regardless of which dirs exist)
    for b in buckets:
        b.assigned_total = (b.end - b.start + 1)

    missing_samples: List[str] = []
    failed_samples: List[str] = []

    # Scan episode directories
    for ep_dir in episodes_root.iterdir():
        if not ep_dir.is_dir():
            continue

        m = EP_DIR_RE.match(ep_dir.name)
        if not m:
            continue

        code = int(m.group(1))
        bucket = which_bucket(code, buckets)
        if bucket is None:
            continue

        out_dir = ep_dir / "whisper" / fw_dir
        manifest_p = out_dir / "manifest.json"
        timings_p = out_dir / "timings.json"

        if not manifest_p.exists() or not timings_p.exists():
            bucket.missing += 1
            if args.show_missing and len(missing_samples) < args.show_missing:
                missing_samples.append(ep_dir.name)
            continue

        manifest = load_json(manifest_p)
        timings = load_json(timings_p)
        if manifest is None or timings is None:
            bucket.failed += 1
            if len(failed_samples) < 20:
                failed_samples.append(ep_dir.name + " (json parse)")
            continue

        exit_code = timings.get("exit_code", 1)
        dur = (manifest.get("audio", {}) or {}).get("duration_seconds", None)
        elapsed = timings.get("elapsed_seconds", None)

        if exit_code != 0:
            bucket.failed += 1
            if len(failed_samples) < 20:
                failed_samples.append(ep_dir.name + f" (exit_code={exit_code})")
            continue

        # Require duration and elapsed for speed calc
        if not isinstance(dur, (int, float)) or not isinstance(elapsed, (int, float)) or dur <= 0 or elapsed <= 0:
            bucket.failed += 1
            if len(failed_samples) < 20:
                failed_samples.append(ep_dir.name + " (missing dur/elapsed)")
            continue

        bucket.completed += 1
        bucket.total_audio_s += float(dur)
        bucket.total_elapsed_s += float(elapsed)

    # Print report
    print("\n=== SAWL Throughput Report (fw-{}) ===\n".format(args.model))
    print(f"Episodes root: {episodes_root.resolve()}")
    print(f"Output folder: whisper/{fw_dir}\n")

    # Summary table-ish output
    for b in buckets:
        done = b.completed
        assigned = b.assigned_total
        pct = (done / assigned * 100.0) if assigned else 0.0

        speed = (b.total_audio_s / b.total_elapsed_s) if b.total_elapsed_s > 0 else 0.0

        print(f"{b.name}  (E{b.start:04d}–E{b.end:04d})")
        print(f"  Assigned:      {assigned}")
        print(f"  Completed:     {done}  ({pct:.1f}%)")
        print(f"  Missing:       {b.missing}")
        print(f"  Failed:        {b.failed}")
        if b.total_elapsed_s > 0:
            print(f"  Audio hours:   {fmt_hours(b.total_audio_s)}")
            print(f"  Wall hours:    {fmt_hours(b.total_elapsed_s)}")
            print(f"  Speed:         {speed:.2f}× realtime")
        else:
            print(f"  Audio hours:   0.00")
            print(f"  Wall hours:    0.00")
            print(f"  Speed:         (no completed episodes yet)")
        print("")

    # Cluster total (completed only)
    total_audio = sum(b.total_audio_s for b in buckets)
    total_elapsed = sum(b.total_elapsed_s for b in buckets)
    total_completed = sum(b.completed for b in buckets)
    total_assigned = sum(b.assigned_total for b in buckets)

    cluster_speed = (total_audio / total_elapsed) if total_elapsed > 0 else 0.0
    print("CLUSTER (completed only)")
    print(f"  Completed:     {total_completed} / {total_assigned} ({(total_completed/total_assigned*100.0):.1f}%)")
    print(f"  Audio hours:   {fmt_hours(total_audio)}")
    print(f"  Wall hours:    {fmt_hours(total_elapsed)}")
    print(f"  Speed:         {cluster_speed:.2f}× realtime\n")

    if args.show_missing and missing_samples:
        print(f"Sample missing outputs (up to {args.show_missing}):")
        for s in missing_samples:
            print(f"  - {s}")
        print("")

    if failed_samples:
        print("Sample failures (up to 20):")
        for s in failed_samples:
            print(f"  - {s}")
        print("")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
