#!/usr/bin/env python3
"""
check_hydration.py

Verifies that each episode directory is "fully hydrated" for one or more run output dirs,
e.g. data/episodes/E0022-20180112/whisper/fw-base/

Default behavior:
- scans data/episodes/*/
- finds all run dirs matching: <episode>/whisper/*   (e.g. fw-base, fw-small, etc)
- checks required files in each run dir
- validates JSON parses and basic VTT sanity

Outputs TSV reports suitable for grep/sort.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_REQUIRED = [
    "transcript.txt",
    "segments.json",
    "timestamps.vtt",
    "timings.json",
    "log/run.log",          # allow nested
    "manifest.json",
]

# Some of your runs write run.log at top level, some in log/run.log.
# We'll treat either as acceptable if one of them is present.
ALTERNATE_GROUPS = [
    ("run.log", "log/run.log"),
]


@dataclass
class CheckResult:
    episode_dir: str
    episode_id: str
    run_dir: str
    status: str  # OK / FAIL / SKIP
    missing: str
    empty: str
    bad_json: str
    bad_vtt: str
    notes: str


def is_nonempty_file(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def try_parse_json(p: Path) -> bool:
    try:
        with p.open("r", encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False


def vtt_sane(p: Path) -> bool:
    # Minimal sanity: starts with WEBVTT, file non-empty
    try:
        if not is_nonempty_file(p):
            return False
        with p.open("r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        return first.startswith("WEBVTT")
    except Exception:
        return False


def pick_run_dirs(episode_path: Path, rel_run: Optional[str]) -> List[Path]:
    """
    If rel_run provided (e.g. 'whisper/fw-base'), check only that one.
    Otherwise, discover all immediate subdirs under episode/whisper/*
    """
    if rel_run:
        return [episode_path / rel_run]

    whisper_dir = episode_path / "whisper"
    if not whisper_dir.is_dir():
        return []

    # only subdirs one-level deep: whisper/<runname>/
    return sorted([p for p in whisper_dir.iterdir() if p.is_dir()])


def required_with_alternates(required: List[str]) -> Tuple[List[str], List[Tuple[str, ...]]]:
    """
    Split requirements into:
    - strict: required paths that must exist
    - groups: alternative groups where any one satisfies the group
    """
    strict = []
    groups = []

    alt_flat = set()
    for g in ALTERNATE_GROUPS:
        alt_flat.update(g)

    for r in required:
        if r in alt_flat:
            # handled by groups
            continue
        strict.append(r)

    # include only groups that are relevant to this required list (or always keep them)
    groups.extend(ALTERNATE_GROUPS)
    return strict, groups


def check_run_dir(episode_path: Path, run_path: Path, required: List[str]) -> CheckResult:
    episode_id = episode_path.name
    strict, groups = required_with_alternates(required)

    missing: List[str] = []
    empty: List[str] = []
    bad_json: List[str] = []
    bad_vtt: List[str] = []
    notes: List[str] = []

    if not run_path.exists():
        return CheckResult(
            episode_dir=str(episode_path),
            episode_id=episode_id,
            run_dir=str(run_path),
            status="FAIL",
            missing="(run_dir_missing)",
            empty="",
            bad_json="",
            bad_vtt="",
            notes="run dir does not exist",
        )
    if not run_path.is_dir():
        return CheckResult(
            episode_dir=str(episode_path),
            episode_id=episode_id,
            run_dir=str(run_path),
            status="FAIL",
            missing="(run_dir_not_dir)",
            empty="",
            bad_json="",
            bad_vtt="",
            notes="run path is not a directory",
        )

    # strict required files
    for rel in strict:
        p = run_path / rel
        if not p.exists():
            missing.append(rel)
        elif not is_nonempty_file(p):
            empty.append(rel)

    # alternate groups
    for group in groups:
        # if any exists and non-empty, group is satisfied
        ok = False
        any_exists = False
        for rel in group:
            p = run_path / rel
            if p.exists():
                any_exists = True
                if is_nonempty_file(p):
                    ok = True
                    break
        if not ok:
            # If none exist -> missing group; if exist but all empty -> empty group.
            label = " | ".join(group)
            if not any_exists:
                missing.append(f"({label})")
            else:
                empty.append(f"({label})")

    # JSON validation
    for rel in ("segments.json", "manifest.json", "timings.json"):
        p = run_path / rel
        if p.exists() and is_nonempty_file(p):
            if not try_parse_json(p):
                bad_json.append(rel)

    # VTT validation
    vttp = run_path / "timestamps.vtt"
    if vttp.exists() and is_nonempty_file(vttp):
        if not vtt_sane(vttp):
            bad_vtt.append("timestamps.vtt")

    status = "OK"
    if missing or empty or bad_json or bad_vtt:
        status = "FAIL"

    # Handy: detect "legacy log placement"
    if (run_path / "run.log").exists() and (run_path / "log/run.log").exists():
        notes.append("both run.log and log/run.log present")

    return CheckResult(
        episode_dir=str(episode_path),
        episode_id=episode_id,
        run_dir=str(run_path),
        status=status,
        missing=";".join(missing),
        empty=";".join(empty),
        bad_json=";".join(bad_json),
        bad_vtt=";".join(bad_vtt),
        notes=";".join(notes),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/episodes", help="Episodes root directory")
    ap.add_argument(
        "--rel_run",
        default=None,
        help="Relative run dir to check (e.g. whisper/fw-base). If omitted, checks all whisper/* subdirs found.",
    )
    ap.add_argument(
        "--required",
        nargs="*",
        default=DEFAULT_REQUIRED,
        help="Required files relative to run dir (default includes transcript/segments/vtt/timings/manifest/log).",
    )
    ap.add_argument("--out_dir", default="output", help="Where to write TSV reports")
    ap.add_argument("--limit", type=int, default=0, help="Optional limit of episodes (0 = no limit)")
    args = ap.parse_args()

    root = Path(args.root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "hydration_report.tsv"
    missing_path = out_dir / "hydration_missing.tsv"

    if not root.is_dir():
        raise SystemExit(f"Root not found or not a dir: {root}")

    episode_dirs = sorted([p for p in root.iterdir() if p.is_dir()])
    if args.limit and args.limit > 0:
        episode_dirs = episode_dirs[: args.limit]

    rows: List[CheckResult] = []
    fail_rows: List[CheckResult] = []

    total_run_dirs = 0
    for ep in episode_dirs:
        run_dirs = pick_run_dirs(ep, args.rel_run)
        if not run_dirs:
            # No whisper runs found: record one SKIP row
            rows.append(
                CheckResult(
                    episode_dir=str(ep),
                    episode_id=ep.name,
                    run_dir=str(ep / (args.rel_run or "whisper/*")),
                    status="FAIL",
                    missing="(no_run_dirs_found)",
                    empty="",
                    bad_json="",
                    bad_vtt="",
                    notes="no run dirs discovered",
                )
            )
            fail_rows.append(rows[-1])
            continue

        for rd in run_dirs:
            total_run_dirs += 1
            r = check_run_dir(ep, rd, args.required)
            rows.append(r)
            if r.status != "OK":
                fail_rows.append(r)

    def write_tsv(path: Path, data: List[CheckResult]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(
                [
                    "episode_id",
                    "episode_dir",
                    "run_dir",
                    "status",
                    "missing",
                    "empty",
                    "bad_json",
                    "bad_vtt",
                    "notes",
                ]
            )
            for r in data:
                w.writerow(
                    [
                        r.episode_id,
                        r.episode_dir,
                        r.run_dir,
                        r.status,
                        r.missing,
                        r.empty,
                        r.bad_json,
                        r.bad_vtt,
                        r.notes,
                    ]
                )

    write_tsv(report_path, rows)
    write_tsv(missing_path, fail_rows)

    ok = sum(1 for r in rows if r.status == "OK")
    fail = sum(1 for r in rows if r.status != "OK")

    print(f"[✓] Episodes scanned: {len(episode_dirs)}")
    print(f"[✓] Run dirs checked: {total_run_dirs}")
    print(f"[✓] OK rows: {ok}")
    print(f"[!] FAIL rows: {fail}")
    print(f"[✓] Wrote: {report_path}")
    print(f"[✓] Wrote: {missing_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())