#!/usr/bin/env python3
"""
make_chunks.py

Input:  squashed.txt (single spaces, trimmed)
Output: chunks.txt (blank line between blocks)
        chunks_manifest.json (offsets into chunks.txt + source offsets)

Chunking rules:
- Aim for target_chars per block.
- End blocks on sentence boundaries ('.', '?', '!') followed by a space, when possible.
- Search forward within +max_forward chars for a boundary; if none, search backward within -max_backward chars.
- Enforce minimum block size (min_chars) when possible to avoid tiny chunks.
- Offsets are half-open [start, end).

Optional:
- --block-markers: write "[BLOCK N]" header lines in chunks.txt.
  Manifest will include:
    - out_block_start/out_block_end: offsets for the entire block including header + text
    - out_text_start/out_text_end: offsets for the chunk text only (recommended for later slicing)
"""

import argparse
import json
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional


SENT_END_RE = re.compile(r"[.!?]\s")  # sentence boundary is punctuation followed by space


def find_sentence_boundary_forward(s: str, start: int, end_limit: int) -> Optional[int]:
    """
    Return index AFTER the boundary (exclusive end), within [start, end_limit].
    We return the position of the space after punctuation -> end = that space+1.
    """
    m = SENT_END_RE.search(s, start, end_limit)
    if not m:
        return None
    return m.end()


def find_sentence_boundary_backward(s: str, start_limit: int, end: int) -> Optional[int]:
    """
    Find the last sentence boundary whose end is within [start_limit, end].
    Returns boundary end index (exclusive end).
    """
    last = None
    for m in SENT_END_RE.finditer(s, start_limit, end):
        last = m.end()
    return last


def chunk_text(
    s: str,
    target_chars: int = 2000,
    min_chars: int = 900,
    max_forward: int = 500,
    max_backward: int = 500,
) -> List[Tuple[int, int]]:
    n = len(s)
    pos = 0
    spans: List[Tuple[int, int]] = []

    while pos < n:
        if n - pos <= target_chars:
            spans.append((pos, n))
            break

        ideal = pos + target_chars
        fwd_limit = min(n, ideal + max_forward)
        back_limit = max(pos, ideal - max_backward)

        # Prefer a forward boundary close to ideal
        end = find_sentence_boundary_forward(s, ideal, fwd_limit)

        # If forward boundary yields a too-small chunk, try a later boundary within fwd_limit
        if end is not None and (end - pos) < min_chars:
            end2 = find_sentence_boundary_forward(s, end, fwd_limit)
            if end2 is not None:
                end = end2

        # If no forward boundary (or still too small), try backward boundary
        if end is None or (end - pos) < min_chars:
            back_end = find_sentence_boundary_backward(s, back_limit, ideal)
            if back_end is not None and (back_end - pos) >= min_chars:
                end = back_end

        # Fallback: if we still have no usable boundary, just cut at ideal (or near it)
        if end is None or end <= pos:
            end = min(n, ideal)

        # Final safety: ensure progress
        if end <= pos:
            end = min(n, pos + target_chars)

        spans.append((pos, end))
        pos = end

    return spans


def write_chunks_and_manifest(
    squashed_path: Path,
    chunks_path: Path,
    manifest_path: Path,
    target_chars: int,
    min_chars: int,
    max_forward: int,
    max_backward: int,
    block_markers: bool,
) -> Dict:
    text = squashed_path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\s+", " ", text).strip()

    spans = chunk_text(
        text,
        target_chars=target_chars,
        min_chars=min_chars,
        max_forward=max_forward,
        max_backward=max_backward,
    )

    out_pos = 0
    manifest_paras: List[Dict] = []
    out_parts: List[str] = []

    for i, (src_start, src_end) in enumerate(spans, start=1):
        chunk = text[src_start:src_end]

        out_block_start = out_pos

        header = ""
        if block_markers:
            header = f"[BLOCK {i}]\n"
            out_parts.append(header)
            out_pos += len(header)

        out_text_start = out_pos
        out_parts.append(chunk)
        out_pos += len(chunk)
        out_text_end = out_pos

        out_block_end = out_pos

        manifest_paras.append(
            {
                "para_index": i,
                "src_start": src_start,
                "src_end": src_end,
                # output offsets into chunks.txt:
                "out_block_start": out_block_start,
                "out_block_end": out_block_end,
                "out_text_start": out_text_start,
                "out_text_end": out_text_end,
            }
        )

        # separator between blocks (blank line), not part of any block
        if i != len(spans):
            out_parts.append("\n\n")
            out_pos += 2

    chunks_text = "".join(out_parts)
    chunks_path.write_text(chunks_text, encoding="utf-8")

    manifest = {
        "source_file": str(squashed_path),
        "chunks_file": str(chunks_path),
        "total_src_chars": len(text),
        "total_out_chars": len(chunks_text),
        "target_chars": target_chars,
        "min_chars": min_chars,
        "max_forward": max_forward,
        "max_backward": max_backward,
        "block_markers": bool(block_markers),
        "paragraphs": manifest_paras,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("squashed_path", help="Path to squashed.txt")
    ap.add_argument("--target", type=int, default=2000, help="Target chars per block (default: 2000)")
    ap.add_argument("--min", dest="min_chars", type=int, default=900, help="Minimum chars per block (default: 900)")
    ap.add_argument("--fwd", type=int, default=500, help="Max forward search for sentence end (default: 500)")
    ap.add_argument("--back", type=int, default=500, help="Max backward search for sentence end (default: 500)")
    ap.add_argument("--chunks-name", default="chunks.txt", help="Output chunks filename (default: chunks.txt)")
    ap.add_argument("--manifest-name", default="chunks_manifest.json", help="Output manifest filename (default: chunks_manifest.json)")
    ap.add_argument("--block-markers", action="store_true", help='Write "[BLOCK N]" header lines in chunks.txt')
    args = ap.parse_args()

    squashed = Path(args.squashed_path).expanduser()
    if not squashed.exists() or not squashed.is_file():
        raise SystemExit(f"Not found: {squashed}")

    out_dir = squashed.parent
    chunks_path = out_dir / args.chunks_name
    manifest_path = out_dir / args.manifest_name

    manifest = write_chunks_and_manifest(
        squashed_path=squashed,
        chunks_path=chunks_path,
        manifest_path=manifest_path,
        target_chars=args.target,
        min_chars=args.min_chars,
        max_forward=args.fwd,
        max_backward=args.back,
        block_markers=args.block_markers,
    )

    print(f"Wrote: {chunks_path}")
    print(f"Wrote: {manifest_path}")
    print(f"Blocks: {len(manifest['paragraphs'])}")
    print(f"src chars: {manifest['total_src_chars']}")
    print(f"out chars: {manifest['total_out_chars']}")
    if manifest.get("block_markers"):
        print("block markers: ON")
    else:
        print("block markers: OFF")


if __name__ == "__main__":
    main()