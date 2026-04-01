#!/usr/bin/env python3

import argparse
import json
import re
import time
import sys
from pathlib import Path

import requests

API_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"

PROMPT_HEADER = """
You are labeling transcript blocks.

Rules:
- Each block begins with [BLOCK N]
- Produce exactly one label for every block
- Label must be 3 to 6 words
- Use concrete topic words from the block
- No punctuation
- No explanation
- No extra text

Output format exactly:

[BLOCK N]
L: label text
""".strip()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="data/episodes")
    p.add_argument("--start", type=int)
    p.add_argument("--end", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--batch-size", type=int, default=4)
    return p.parse_args()


def find_episode_dirs(root, start_ep, end_ep):
    episodes = []

    for ep in sorted(Path(root).iterdir()):
        if not ep.is_dir():
            continue

        if not ep.name.startswith("E"):
            continue

        ep_num = int(ep.name[1:5])

        if start_ep and ep_num < start_ep:
            continue
        if end_ep and ep_num > end_ep:
            continue

        workdir = ep / "whisper" / "fw-base"

        if (workdir / "chunks.txt").exists():
            episodes.append(workdir)

    return episodes


def parse_blocks(text):
    pattern = r"\[BLOCK (\d+)\]\s*(.*?)(?=\n\[BLOCK \d+\]|\Z)"
    matches = re.findall(pattern, text, flags=re.S)

    blocks = []
    for n, body in matches:
        blocks.append({
            "block_index": int(n),
            "text": body.strip()
        })

    return blocks


def build_prompt(blocks):
    parts = [PROMPT_HEADER]

    for b in blocks:
        parts.append(f"\n[BLOCK {b['block_index']}]\n{b['text']}")

    return "\n".join(parts)


def call_model(prompt):

    payload = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    r = requests.post(API_URL, json=payload, timeout=120)
    r.raise_for_status()

    data = r.json()

    return data["choices"][0]["message"]["content"]


def parse_labels(text):

    pattern = r"\[BLOCK (\d+)\]\s*L:\s*(.+)"
    matches = re.findall(pattern, text)

    labels = []

    for n, label in matches:
        clean = re.sub(r"[^\w\s]", "", label.lower())
        clean = re.sub(r"\s+", " ", clean).strip()

        labels.append({
            "block_index": int(n),
            "label": clean
        })

    return labels


def label_batch(blocks):

    prompt = build_prompt(blocks)

    response = call_model(prompt)

    labels = parse_labels(response)

    block_ids = {b["block_index"] for b in blocks}
    label_ids = {l["block_index"] for l in labels}

    if block_ids != label_ids:
        raise ValueError("label mismatch")

    return labels


def label_blocks(blocks, batch_size):

    results = []
    i = 0

    while i < len(blocks):

        batch = blocks[i:i+batch_size]

        try:

            labels = label_batch(batch)
            results.extend(labels)

        except Exception:

            for block in batch:

                try:
                    labels = label_batch([block])
                    results.extend(labels)

                except Exception:

                    print(f"FALLBACK label for block {block['block_index']}", file=sys.stderr)

                    results.append({
                        "block_index": block["block_index"],
                        "label": "topic discussion context"
                    })

        i += batch_size

    results.sort(key=lambda x: x["block_index"])

    return results


def process_episode(ep_dir, force, batch_size):

    chunks = ep_dir / "chunks.txt"
    labels_path = ep_dir / "labels.json"

    if labels_path.exists() and not force:
        print(f"SKIP      {ep_dir}")
        return False

    text = chunks.read_text()

    blocks = parse_blocks(text)

    print(f"START     {ep_dir}  {len(blocks)} blocks")

    t0 = time.time()

    labels = label_blocks(blocks, batch_size)

    labels_path.write_text(json.dumps(labels, indent=2))

    elapsed = time.time() - t0

    print(f"WROTE     {len(labels)} labels in {elapsed:.2f}s")

    return True


def main():

    args = parse_args()

    root = Path(args.root)

    episodes = find_episode_dirs(root, args.start, args.end)

    print(f"FOUND     {len(episodes)} episode directories under {root}")

    if args.start or args.end:
        print(f"RANGE     start={args.start} end={args.end}")

    print(f"BATCH     {args.batch_size}")
    print(f"MODEL     {MODEL}\n")

    written = 0

    start = time.time()

    for ep in episodes:

        if process_episode(ep, args.force, args.batch_size):
            written += 1

    elapsed = time.time() - start

    print("\nDONE")
    print(f"wrote   {written} episodes")
    print(f"elapsed {elapsed:.2f}s")


if __name__ == "__main__":
    main()