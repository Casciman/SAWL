#!/usr/bin/env python3
"""
sawl_check.py — validate analysis/episode.json completeness + basic strength signals.

Usage:
  python3 sawl_check.py --root data/episodes --episodes 27-31
"""

from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCHEMA_KEYS = [
    "episode_id","date","title","analysis_version","summary_compact","summary_narrative",
    "topics","traits","notable_quotes","persuasion_lessons","predictions","thought_experiments",
    "closing_observations","evaluation"
]
TRAITS_KEYS = ["dale","guest","thought_experiment","whiteboard"]
EVAL_KEYS = ["originality","impact","clarity","focus","timeliness","humor"]

def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def parse_range(s: str) -> Tuple[int,int]:
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", s)
    if not m: raise ValueError("range must be like 27-31")
    return int(m.group(1)), int(m.group(2))

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/episodes")
    ap.add_argument("--episodes", default="27-31")
    args = ap.parse_args()

    lo, hi = parse_range(args.episodes)
    want = {f"E{n:04d}" for n in range(lo, hi+1)}

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Root not found: {root}", file=sys.stderr); sys.exit(1)

    failures = 0

    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir(): continue
        m = re.match(r"^(E\d{4,5})-\d{8}", d.name)
        if not m: continue
        ep = m.group(1)
        if ep not in want: continue

        p = d / "analysis" / "episode.json"
        if not p.exists():
            print(f"[MISS] {ep} missing analysis/episode.json")
            failures += 1
            continue

        obj = load_json(p)
        if not isinstance(obj, dict):
            print(f"[BAD ] {ep} invalid JSON")
            failures += 1
            continue

        # exact keys
        keys = set(obj.keys())
        if keys != set(SCHEMA_KEYS):
            extra = sorted(keys - set(SCHEMA_KEYS))
            missing = sorted(set(SCHEMA_KEYS) - keys)
            print(f"[KEYS] {ep} key mismatch extra={extra} missing={missing}")
            failures += 1

        # basic strength signals
        def bad(msg: str):
            nonlocal failures
            print(f"[WEAK] {ep} {msg}")
            failures += 1

        # required text fields non-empty
        if not str(obj.get("episode_id","")).strip(): bad("episode_id empty")
        if not str(obj.get("date","")).strip(): bad("date empty")
        if "summary_narrative" not in obj or not str(obj.get("summary_narrative","")).strip(): bad("summary_narrative empty")
        if "closing_observations" not in obj or not str(obj.get("closing_observations","")).strip(): bad("closing_observations empty")

        # bullets
        bullets = obj.get("summary_compact", [])
        if not isinstance(bullets, list) or len([b for b in bullets if str(b).strip()]) < 6:
            bad("summary_compact too thin (<6 bullets)")

        # topics
        topics = obj.get("topics", [])
        if not isinstance(topics, list) or len([t for t in topics if str(t).strip()]) < 6:
            bad("topics too thin (<6)")

        # traits exact keys
        traits = obj.get("traits", {})
        if not isinstance(traits, dict) or set(traits.keys()) != set(TRAITS_KEYS):
            bad("traits missing keys or wrong shape")

        # quotes must be double-quoted string content
        quotes = obj.get("notable_quotes", [])
        if not isinstance(quotes, list) or len(quotes) == 0:
            bad("notable_quotes empty")
        else:
            for q in quotes:
                qs = str(q).strip()
                if not (qs.startswith('"') and qs.endswith('"')):
                    bad("notable_quotes contains unquoted item"); break

        # persuasion lessons
        pl = obj.get("persuasion_lessons", [])
        if not isinstance(pl, list) or len([x for x in pl if str(x).strip()]) < 3:
            bad("persuasion_lessons too thin (<3)")

        # evaluation
        ev = obj.get("evaluation", {})
        if not isinstance(ev, dict) or set(ev.keys()) != set(EVAL_KEYS):
            bad("evaluation missing keys")
        else:
            for k in EVAL_KEYS:
                v = ev.get(k, 0)
                if not isinstance(v, int) or v < 1 or v > 10:
                    bad(f"evaluation.{k} out of range 1–10"); break

        print(f"[OK  ] {ep} {p}")

    if failures:
        print(f"\nFAILURES: {failures}")
        sys.exit(1)
    print("\nAll checks passed.")

if __name__ == "__main__":
    main()
    