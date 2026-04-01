#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(
        description="Build chunks_final.json from squashed.txt, chunks_manifest.json, and labels.json"
    )
    p.add_argument("--root", default="data/episodes")
    p.add_argument("--start", type=int)
    p.add_argument("--end", type=int)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def find_episode_dirs(root, start_ep, end_ep):
    episodes = []

    for ep in sorted(Path(root).iterdir()):
        if not ep.is_dir():
            continue

        if not ep.name.startswith("E"):
            continue

        ep_num = int(ep.name[1:5])

        if start_ep is not None and ep_num < start_ep:
            continue
        if end_ep is not None and ep_num > end_ep:
            continue

        workdir = ep / "whisper" / "fw-base"

        needed = [
            workdir / "squashed.txt",
            workdir / "chunks_manifest.json",
            workdir / "labels.json",
        ]

        if all(p.exists() for p in needed):
            episodes.append(workdir)

    return episodes


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_ws(text):
    return re.sub(r"\s+", " ", text).strip()


def words(text):
    return re.findall(r"\S+", text)


def first_n_words(text, n=6):
    w = words(text)
    return " ".join(w[:n])


def last_n_words(text, n=6):
    w = words(text)
    return " ".join(w[-n:])


def get_manifest_blocks(manifest):
    # Try the obvious keys first
    for key in ("blocks", "chunks", "paras", "paragraphs"):
        value = manifest.get(key)
        if isinstance(value, list):
            return value

    # Fallback: if manifest itself looks like a list
    if isinstance(manifest, list):
        return manifest

    raise ValueError("Could not find block list in chunks_manifest.json")


def get_block_index(block):
    for key in ("block_index", "para_index", "chunk_index", "index"):
        if key in block:
            return int(block[key])
    raise ValueError(f"Missing block index in manifest block: {block}")


def get_src_start(block):
    for key in ("src_start", "start", "text_start"):
        if key in block:
            return int(block[key])
    raise ValueError(f"Missing src_start in manifest block: {block}")


def get_src_end(block):
    for key in ("src_end", "end", "text_end"):
        if key in block:
            return int(block[key])
    raise ValueError(f"Missing src_end in manifest block: {block}")


def build_label_map(labels):
    label_map = {}
    for item in labels:
        idx = int(item["block_index"])
        label = normalize_ws(item["label"])
        label_map[idx] = label
    return label_map


def process_episode(workdir, force=False):
    squashed_path = workdir / "squashed.txt"
    manifest_path = workdir / "chunks_manifest.json"
    labels_path = workdir / "labels.json"
    out_path = workdir / "chunks_final.json"

    if out_path.exists() and not force:
        print(f"SKIP      {workdir}")
        return False

    squashed = squashed_path.read_text(encoding="utf-8")
    manifest = load_json(manifest_path)
    labels = load_json(labels_path)

    manifest_blocks = get_manifest_blocks(manifest)
    label_map = build_label_map(labels)

    episode_id = None
    for part in workdir.parts:
        if part.startswith("E") and "-" in part:
            episode_id = part
            break
    if episode_id is None:
        raise ValueError(f"Could not determine episode id from path: {workdir}")

    blocks_out = []
    errors = []

    for raw_block in manifest_blocks:
        block_index = get_block_index(raw_block)
        src_start = get_src_start(raw_block)
        src_end = get_src_end(raw_block)

        if src_start < 0 or src_end < 0 or src_end < src_start or src_end > len(squashed):
            errors.append(f"bad offsets for block {block_index}: {src_start}-{src_end}")
            continue

        text = squashed[src_start:src_end]
        text = normalize_ws(text)

        episode_prefix = episode_id.split("-")[0]

        label = label_map.get(block_index)
        if not label:
            label = "topic discussion context"
            errors.append(f"missing label for block {block_index}")

        block_id = f"{episode_prefix}_{block_index:03d}"

        block_out = {
            "block_id": block_id,
            "block_index": block_index,
            "label": label,
            "src_start": src_start,
            "src_end": src_end,
            "start_anchor": first_n_words(text, 6),
            "end_anchor": last_n_words(text, 6),
            "text": text,
            "char_count": len(text),
            "word_count": len(words(text)),
        }

        blocks_out.append(block_out)

    blocks_out.sort(key=lambda x: x["block_index"])

    result = {
        "episode": episode_id,
        "block_count": len(blocks_out),
        "blocks": blocks_out,
    }

    if errors:
        result["warnings"] = errors

    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"WROTE     {workdir}  {len(blocks_out)} blocks")
    if errors:
        print(f"WARNING   {workdir}  {len(errors)} issues", file=sys.stderr)

    return True


def main():
    args = parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"ERROR: root does not exist: {root}", file=sys.stderr)
        return 1

    episodes = find_episode_dirs(root, args.start, args.end)

    print(f"FOUND     {len(episodes)} episode directories under {root}")
    if args.start is not None or args.end is not None:
        print(f"RANGE     start={args.start} end={args.end}")
    print()

    wrote = 0
    errors = 0
    t0 = time.time()

    for workdir in episodes:
        try:
            if process_episode(workdir, force=args.force):
                wrote += 1
        except Exception as e:
            errors += 1
            print(f"ERROR     {workdir}: {e}", file=sys.stderr)

    elapsed = time.time() - t0

    print()
    print("DONE")
    print(f"wrote   {wrote} episodes")
    print(f"errors  {errors} episodes")
    print(f"elapsed {elapsed:.2f}s")

    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
