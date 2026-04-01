#!/usr/bin/env python3
"""
squash_transcript.py

Deterministically "squash" a transcript:
- Collapse all whitespace (spaces/tabs/newlines) to single spaces
- Trim leading/trailing whitespace

Writes output as 'squashed.txt' adjacent to the input file.
"""

import argparse
import re
from pathlib import Path


def squash_text(s: str) -> str:
    # Collapse any whitespace run to a single space, then trim.
    return re.sub(r"\s+", " ", s).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript_path", help="Path to transcript.txt")
    ap.add_argument("--out-name", default="squashed.txt", help="Output filename (default: squashed.txt)")
    args = ap.parse_args()

    in_path = Path(args.transcript_path).expanduser()
    if not in_path.exists() or not in_path.is_file():
        raise SystemExit(f"Input file not found: {in_path}")

    raw = in_path.read_text(encoding="utf-8", errors="replace")
    squashed = squash_text(raw)

    out_path = in_path.parent / args.out_name
    out_path.write_text(squashed, encoding="utf-8")

    print(f"Wrote: {out_path}")
    print(f"Input chars:  {len(raw)}")
    print(f"Output chars: {len(squashed)}")


if __name__ == "__main__":
    main()