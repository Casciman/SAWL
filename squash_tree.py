#!/usr/bin/env python3
"""
squash_tree.py

Deterministically create squashed transcripts across an episodes tree.

Rules:
- Read transcript.txt (UTF-8, errors=replace)
- Collapse all whitespace runs to single spaces
- Trim leading/trailing whitespace
- Write sibling file 'squashed.txt' in the same directory as transcript.txt

By default: does NOT overwrite existing squashed.txt (use --overwrite).
"""

import argparse
import re
from pathlib import Path


WS_RE = re.compile(r"\s+")


def squash_text(s: str) -> str:
    return WS_RE.sub(" ", s).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes_root", required=True, help="Root folder containing episode directories")
    ap.add_argument("--pattern", default="**/transcript.txt", help="Glob pattern under episodes_root (default: **/transcript.txt)")
    ap.add_argument("--out-name", default="squashed.txt", help="Output filename adjacent to transcript.txt (default: squashed.txt)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing squashed.txt")
    ap.add_argument("--dry-run", action="store_true", help="Do not write files; just report what would happen")
    ap.add_argument("--progress-every", type=int, default=100, help="Print progress every N transcripts (default: 100)")
    args = ap.parse_args()

    root = Path(args.episodes_root).expanduser()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"--episodes_root is not a directory: {root}")

    transcripts = sorted(root.glob(args.pattern), key=lambda p: str(p))
    if not transcripts:
        print(f"No transcripts found under {root} with pattern: {args.pattern}")
        return

    n_total = len(transcripts)
    n_written = 0
    n_skipped = 0
    n_errors = 0

    for i, tpath in enumerate(transcripts, start=1):
        out_path = tpath.parent / args.out_name

        if out_path.exists() and not args.overwrite:
            n_skipped += 1
        else:
            try:
                raw = tpath.read_text(encoding="utf-8", errors="replace")
                squashed = squash_text(raw)

                if not args.dry_run:
                    out_path.write_text(squashed, encoding="utf-8")

                n_written += 1
            except Exception as e:
                n_errors += 1
                print(f"ERROR: {tpath} -> {out_path} : {e}")

        if args.progress_every > 0 and (i % args.progress_every == 0 or i == n_total):
            print(f"[{i}/{n_total}] written={n_written} skipped={n_skipped} errors={n_errors}")

    print("DONE")
    print(f"transcripts: {n_total}")
    print(f"written:     {n_written}")
    print(f"skipped:     {n_skipped}")
    print(f"errors:      {n_errors}")


if __name__ == "__main__":
    main()