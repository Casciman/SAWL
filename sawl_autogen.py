#!/usr/bin/env python3
"""
sawl_autogen.py — auto-generate analysis/episode.json for Scott Adams episodes
Local-only: uses Ollama HTTP API (default http://127.0.0.1:11434) + a local model (e.g., mixtral).

Design constraints:
- Writes EXACT schema (no extra keys).
- Transcript is the only “content” input; episode_id/date/title come from directory/manifest.
- Debug mode can print RAW model output before parsing so you can diagnose.

Folder layout expected:
  data/episodes/E0027-20180117/
    whisper/fw-*/transcript.txt
    whisper/fw-*/manifest.json   (optional, for title/date if you want)
    analysis/episode.json        (output)

Usage:
  python3 sawl_autogen.py --root data/episodes --model mixtral:latest --episodes 27-31
  python3 sawl_autogen.py --root data/episodes --model mixtral:latest --episodes 27-31 --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# ------------------------- schema (lock) -------------------------

SCHEMA_KEYS = [
    "episode_id",
    "date",
    "title",
    "analysis_version",
    "summary_compact",
    "summary_narrative",
    "topics",
    "traits",
    "notable_quotes",
    "persuasion_lessons",
    "predictions",
    "thought_experiments",
    "closing_observations",
    "evaluation",
]

TRAITS_KEYS = ["dale", "guest", "thought_experiment", "whiteboard"]
EVAL_KEYS = ["originality", "impact", "clarity", "focus", "timeliness", "humor"]


# ------------------------- utilities -------------------------

def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)

def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

def safe_load_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(read_text(p))
    except Exception:
        return None

def is_episode_dir_name(name: str) -> bool:
    return bool(re.match(r"^E\d{4,5}-\d{8}(-\d+)?$", name))

def parse_episode_dir(name: str) -> Tuple[str, str]:
    """
    "E0027-20180117" -> ("E0027", "2018-01-17")
    """
    m = re.match(r"^(E\d{4,5})-(\d{8})", name)
    if not m:
        return (name, "")
    ep = m.group(1)
    ymd = m.group(2)
    date = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return (ep, date)

def pick_fw_dir(ep_path: Path) -> Optional[Path]:
    whisper = ep_path / "whisper"
    if not whisper.is_dir():
        return None
    best = None
    best_m = -1.0
    for d in whisper.glob("fw-*"):
        if not d.is_dir():
            continue
        try:
            mt = d.stat().st_mtime
        except Exception:
            mt = 0
        if mt > best_m:
            best = d
            best_m = mt
    return best

def find_transcript(ep_path: Path) -> Optional[Path]:
    fw = pick_fw_dir(ep_path)
    if not fw:
        return None
    t = fw / "transcript.txt"
    return t if t.exists() else None

def find_manifest(ep_path: Path) -> Optional[Path]:
    fw = pick_fw_dir(ep_path)
    if not fw:
        return None
    m = fw / "manifest.json"
    return m if m.exists() else None

def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ------------------------- normalization -------------------------

def ensure_types_and_strip(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforce exact schema keys, types, and quote formatting rules.
    - notable_quotes must be list of strings, each wrapped in double quotes in the string content
    - thought_experiments: [] when none
    """
    out: Dict[str, Any] = {}

    # exact keys only
    for k in SCHEMA_KEYS:
        out[k] = obj.get(k, None)

    # defaults by type
    out["episode_id"] = str(out["episode_id"] or "")
    out["date"] = str(out["date"] or "")
    out["title"] = str(out["title"] or "")
    out["analysis_version"] = int(out["analysis_version"] or 1)

    def as_list_str(x) -> List[str]:
        if x is None:
            return []
        if isinstance(x, list):
            return [str(i).strip() for i in x if str(i).strip()]
        if isinstance(x, str) and x.strip():
            return [x.strip()]
        return []

    out["summary_compact"] = as_list_str(out["summary_compact"])
    out["summary_narrative"] = str(out["summary_narrative"] or "").strip()
    out["topics"] = as_list_str(out["topics"])

    # traits
    traits = out["traits"] if isinstance(out["traits"], dict) else {}
    fixed_traits = {
        "dale": bool(traits.get("dale", False)),
        "guest": traits.get("guest", None),
        "thought_experiment": bool(traits.get("thought_experiment", False)),
        "whiteboard": bool(traits.get("whiteboard", False)),
    }
    if fixed_traits["guest"] in ("", False):
        fixed_traits["guest"] = None
    out["traits"] = fixed_traits

    # quotes: clean only, no forced wrapping
    quotes = as_list_str(out["notable_quotes"])
    cleaned = []
    for q in quotes:
        q = q.strip().strip('"')
        if q:
            cleaned.append(q)
    out["notable_quotes"] = cleaned

    out["persuasion_lessons"] = as_list_str(out["persuasion_lessons"])
    out["predictions"] = as_list_str(out["predictions"])
    out["thought_experiments"] = as_list_str(out["thought_experiments"])
    out["closing_observations"] = str(out["closing_observations"] or "").strip()

    # evaluation: ints 0..10 (0 means missing; your checker can flag)
    ev = out["evaluation"] if isinstance(out["evaluation"], dict) else {}
    fixed_ev = {}
    for k in EVAL_KEYS:
        v = ev.get(k, None)
        try:
            v = int(v)
        except Exception:
            v = 0
        if v < 0:
            v = 0
        if v > 10:
            v = 10
        fixed_ev[k] = v
    out["evaluation"] = fixed_ev

    return out


# ------------------------- Ollama call (read full response) -------------------------

def ollama_generate_obj(
    base_url: str,
    model: str,
    prompt: str,
    temperature: float = 0.2,
    num_ctx: int = 32768,
    num_predict: int = 4096,
    timeout_sec: int = 900,
) -> dict:
    """
    Call Ollama /api/generate (non-streaming) and return the FULL JSON envelope.
    Critical: resp.read() blocks until the server finishes sending the full body.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url=f"{base_url.rstrip('/')}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print("POSTING TO:", req.full_url, file=sys.stderr)
    print("PAYLOAD BYTES:", len(data), file=sys.stderr)

    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            raw_bytes = resp.read()
            raw = raw_bytes.decode("utf-8", errors="replace")
        return json.loads(raw)
    except (HTTPError, URLError, TimeoutError) as e:
        raise RuntimeError(f"Ollama call failed: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama returned non-JSON envelope: {e}")


def extract_json_from_text(txt: str) -> dict:
    """
    Extract the first JSON object from a model response string and return it as a dict.
    Fixes ONE common defect: trailing commas before ] or }.
    """
    import json
    import re

    if not txt:
        raise ValueError("Empty response text")

    # Find first '{'
    start = txt.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model response.")

    # Walk forward to find the matching '}' for the first object.
    depth = 0
    in_str = False
    esc = False
    end = None

    for i in range(start, len(txt)):
        ch = txt[i]

        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue

        # not in string
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
            continue

    if end is None:
        raise ValueError("No complete JSON object found in model response.")

    candidate = txt[start : end + 1].strip()

    # Fix the ONE recurring defect: trailing commas before closing brackets/braces
    candidate = re.sub(r",\s*]", "]", candidate)
    candidate = re.sub(r",\s*}", "}", candidate)

    return json.loads(candidate)


# ------------------------- prompt (strict, but not insane) -------------------------

SYSTEM_RULES = r"""
You are generating an analysis JSON file for a Scott Adams podcast episode transcript.

OUTPUT RULES (MUST FOLLOW):
1) Output MUST be valid JSON, and MUST be ONLY the JSON object. No prose. No markdown. No code fences.
2) Output MUST contain EXACTLY these top-level keys (no more, no less):
   episode_id, date, title, analysis_version, summary_compact, summary_narrative, topics, traits,
   notable_quotes, persuasion_lessons, predictions, thought_experiments, closing_observations, evaluation
3) Types MUST match:
   - episode_id: string
   - date: string "YYYY-MM-DD" (already given; do not change)
   - title: string (already given; do not change)
   - analysis_version: integer 1
   - summary_compact: array of bullet strings (3–10 is fine)
   - summary_narrative: string paragraph
   - topics: array of strings (5–20)
   - notable_quotes: array of strings (5–10). Each quote should be a verbatim line from the transcript when possible.
   - traits: object with EXACT keys: dale (bool), guest (string or null), thought_experiment (bool), whiteboard (bool)
   - persuasion_lessons: array of strings (0–12)
   - predictions: array of strings (0–6)
   - thought_experiments: array of strings (0–6). If none, use [].
   - closing_observations: string
   - evaluation: object with EXACT keys originality, impact, clarity, focus, timeliness, humor each integer 1–10

HARD OUTPUT LIMITS (MUST OBEY):
    - Output MUST be a single JSON object and MUST end with a closing brace }.
    - summary_compact: 3 to 6 bullets MAX. Each bullet <= 110 characters.
    - summary_narrative: 2 to 4 sentences MAX (<= 600 characters total).
    - topics: 5 to 12 items MAX (single words/short phrases).
    - notable_quotes: 5 to 10 items MAX. Each quote <= 160 characters. No extra wrapping; plain text only.
    - predictions: 0 to 5 items MAX. Each <= 120 characters.
    - thought_experiments: 0 to 3 items MAX. Each <= 140 characters.
    - closing_observations: <= 240 characters.
    - If you have more material than fits these limits, DROP the extras (do not expand).

CONTENT RULES:
- Base everything ONLY on the transcript provided.
- If a field is not supported by transcript, keep it empty ([] or null or false) rather than inventing.
- Set traits.whiteboard=true only if the transcript clearly indicates whiteboard/chalk talk.
- Set traits.dale=true only if a clear Dale bit appears.
""".strip()


def build_prompt(episode_id: str, date: str, title: str, transcript: str) -> str:
    return (
        SYSTEM_RULES
        + "\n\nEPISODE METADATA (DO NOT CHANGE):\n"
        + f"episode_id: {episode_id}\n"
        + f"date: {date}\n"
        + f"title: {title}\n\n"
        + "TRANSCRIPT:\n"
        + transcript.strip()
        + "\n"
    )


# ------------------------- main -------------------------

@dataclass
class EpisodeJob:
    ep_dir: Path
    episode_id: str
    date: str
    title: str
    transcript_path: Path


def collect_jobs(root: Path, ep_range: Tuple[int, int]) -> List[EpisodeJob]:
    jobs: List[EpisodeJob] = []
    lo, hi = ep_range
    want = {f"E{n:04d}" for n in range(lo, hi + 1)}

    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not is_episode_dir_name(child.name):
            continue

        episode_id, date = parse_episode_dir(child.name)
        if episode_id not in want:
            continue

        tpath = find_transcript(child)
        if not tpath:
            print(f"[skip] {child.name}: transcript.txt not found", file=sys.stderr)
            continue

        title = ""
        mpath = find_manifest(child)
        if mpath:
            man = safe_load_json(mpath)
            if isinstance(man, dict):
                ep_idx = man.get("episode_index") or {}
                title = str(ep_idx.get("title") or title)
                date_from_manifest = str(ep_idx.get("date") or "")
                if date_from_manifest:
                    date = date_from_manifest

        jobs.append(EpisodeJob(child, episode_id, date, title, tpath))

    return jobs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/episodes", help="Episodes root dir")
    ap.add_argument("--model", default="mixtral:latest", help="Ollama model name (must already be pulled)")
    ap.add_argument("--base_url", default="http://127.0.0.1:11434", help="Ollama base URL")
    ap.add_argument("--episodes", default="27-31", help="Episode range like 27-31")
    ap.add_argument("--force", action="store_true", help="Overwrite existing analysis/episode.json")
    ap.add_argument("--num_ctx", type=int, default=32768, help="Context size to request (model must support)")
    ap.add_argument("--num_predict", type=int, default=4096, help="Max tokens to generate")
    ap.add_argument("--debug_raw", action="store_true", help="Print raw model output before parsing")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        die(f"Root not found: {root}")

    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", args.episodes)
    if not m:
        die("Bad --episodes. Use like 27-31")
    lo, hi = int(m.group(1)), int(m.group(2))

    jobs = collect_jobs(root, (lo, hi))
    if not jobs:
        die("No episodes found for that range (or missing transcripts).")

    print(f"Jobs: {len(jobs)}")
    for j in jobs:
        out_path = j.ep_dir / "analysis" / "episode.json"
        if out_path.exists() and not args.force:
            print(f"[skip] {j.episode_id}: analysis/episode.json exists")
            continue

        transcript = read_text(j.transcript_path)
        if not transcript.strip():
            print(f"[skip] {j.episode_id}: empty transcript", file=sys.stderr)
            continue

        prompt = build_prompt(j.episode_id, j.date, j.title, transcript)

        print(f"[gen ] {j.episode_id}  {j.date}  {j.title}")

        # ---- PROGRESS INDICATOR: START ----
        t0 = time.time()
        print(f"[http] POST /api/generate ...", end="", flush=True)

        obj = ollama_generate_obj(
            base_url=args.base_url,
            model=args.model,
            prompt=prompt,
            temperature=0.2,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            timeout_sec=900,
        )


        dt = time.time() - t0
        print(f" done ({dt:.1f}s)", flush=True)
        # ---- PROGRESS INDICATOR: END ----

        resp = str(obj.get("response", "") or "")

        if args.debug_raw:
            print("\n" + "=" * 80)
            print(f"RAW MODEL OUTPUT for {j.episode_id}")
            print("=" * 80)
            print(resp)
            print("=" * 80)
            print("DONE_REASON:", obj.get("done_reason"))
            print("EVAL_COUNT:", obj.get("eval_count"), "PROMPT_EVAL_COUNT:", obj.get("prompt_eval_count"))
            print("TOTAL_DURATION:", obj.get("total_duration"))
            print("=" * 80 + "\n")

        try:
            parsed = extract_json_from_text(resp)
        except Exception as e:
            die(f"{j.episode_id}: model did not return valid JSON: {e}\n---\n{resp[:2000]}")

        fixed = ensure_types_and_strip(parsed)
        fixed = {k: fixed.get(k) for k in SCHEMA_KEYS}  # stable key order, no extras

        atomic_write_json(out_path, fixed)
        print(f"[write] {out_path}")

        time.sleep(0.25)

    print("Done.")


if __name__ == "__main__":
    main()