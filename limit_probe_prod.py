#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import requests
from typing import Tuple

# Import prod prompt builder EXACTLY (no changes to prod).
# This assumes sawl_autogen.py has: if __name__ == "__main__": main()
# so importing it doesn't execute main().
import sawl_autogen


def is_json_object(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    # Try strict first
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def call_ollama(
    model: str,
    system: str,
    prompt: str,
    want_json: bool,
    num_ctx: int,
    num_predict: int,
    temperature: float,
    url: str,
) -> Tuple[str, dict]:
    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 1.0,
            "top_k": 1,
            "repeat_penalty": 1.0,
            "seed": 1,              # if your Ollama build honors it, this is huge
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if want_json:
        payload["format"] = "json"

    t0 = time.time()
    r = requests.post(url, json=payload, timeout=600)
    dt = time.time() - t0
    r.raise_for_status()

    raw_http = r.text  # <-- ADD THIS

    # /api/generate returns a JSON object that includes "response", "done_reason", etc.
    obj = r.json()
    txt = obj.get("response", "") or ""
    meta = {
        "http_seconds": round(dt, 3),
        "done_reason": obj.get("done_reason"),
        "eval_count": obj.get("eval_count"),
        "prompt_eval_count": obj.get("prompt_eval_count"),
        "total_duration": obj.get("total_duration"),
        "payload_bytes": len(json.dumps(payload)),
        "prompt_chars": len(prompt),
        "resp_chars": len(txt),
    }
    return txt, meta, raw_http


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="transcript text file (e.g., ep29.txt)")
    ap.add_argument("--model", default="mixtral:latest")
    ap.add_argument("--episode_id", default="E0029")
    ap.add_argument("--date", default="2018-01-19")
    ap.add_argument("--title", default="")
    ap.add_argument("--url", default="http://127.0.0.1:11434/api/generate")
    ap.add_argument("--want_json", action="store_true")
    ap.add_argument("--num_ctx", type=int, default=32768)
    ap.add_argument("--num_predict", type=int, default=4096)
    ap.add_argument("--temp", type=float, default=0.2)
    ap.add_argument("--min_chars", type=int, default=2000)
    ap.add_argument("--tolerance", type=int, default=500)
    ap.add_argument("--log", default="limit_probe_prod.log.jsonl")
    args = ap.parse_args()

    transcript = open(args.file, "r", encoding="utf-8").read()
    full_len = len(transcript)

    print(f"[info] transcript_len_chars={full_len}")
    print(f"[info] want_json={args.want_json} num_ctx={args.num_ctx} num_predict={args.num_predict} temp={args.temp}")
    print(f"[info] logging -> {args.log}")
    print(f"[info] using prod prompt builder: sawl_autogen.build_prompt(...)")

    lo = args.min_chars
    hi = full_len

    best = 0
    best_meta = None
    best_preview = ""

    def attempt(n: int):
        partial = transcript[:n]
        # prompt = sawl_autogen.build_prompt(args.episode_id, args.date, args.title, partial)

        prompt = (
            "EPISODE METADATA (DO NOT CHANGE):\n"
            f"episode_id: {args.episode_id}\n"
            f"date: {args.date}\n"
            f"title: {args.title}\n\n"
            "TRANSCRIPT:\n"
            + partial.strip()
            + "\n"
        )
        system = sawl_autogen.SYSTEM_RULES


        txt, meta, raw_http = call_ollama(
            model=args.model,
            system=system,
            prompt=prompt,
            want_json=args.want_json,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            temperature=args.temp,
            url=args.url,
        )

        ok = is_json_object(txt) if args.want_json else (len(txt.strip()) > 0)

        # dump full raw http for BAD cases (or always if you prefer)
        if not ok:
            dump = {
                "n_chars": n,
                "meta": meta,
                "raw_http": raw_http,
            }
            with open(f"limit_probe_dump_{args.episode_id}_{n}.json", "w", encoding="utf-8") as f:
                json.dump(dump, f)

        row = {
            "n_chars": n,
            "ok": ok,
            "meta": meta,
            "resp_head": txt[:120].replace("\n", "\\n"),
        }
        with open(args.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

        status = "OK_JSON" if (ok and args.want_json) else ("OK" if ok else "BAD")
        print(f"[try] n_chars={n} ... {status} ({meta['http_seconds']}s) done_reason={meta['done_reason']} resp_len={meta['resp_chars']} prompt_chars={meta['prompt_chars']} prompt_eval_count={meta['prompt_eval_count']}")
        return ok, txt, meta

    # Binary search for max “good”
    while (hi - lo) > args.tolerance:
        mid = (lo + hi) // 2
        ok, txt, meta = attempt(mid)
        if ok:
            best = mid
            best_meta = meta
            best_preview = txt[:300]
            lo = mid + 1
        else:
            hi = mid - 1

    # Final spot-check at full length
    ok, txt, meta = attempt(full_len)
    if ok:
        best = full_len
        best_meta = meta
        best_preview = txt[:300]

    print("\n=== RESULT ===")
    print(f"Max-good (approx): {best} chars")
    if best_meta:
        print(f"At best: done_reason={best_meta['done_reason']} prompt_eval_count={best_meta['prompt_eval_count']} prompt_chars={best_meta['prompt_chars']}")
    print("Preview:", best_preview.replace("\n", "\\n"))
    print(f"\n[done] log appended to {args.log}")


if __name__ == "__main__":
    main()
    