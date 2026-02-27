#!/usr/bin/env python3
"""
limit_probe.py
Standalone transcript-size limit probe for Ollama /api/generate.

Goal:
- Determine the largest transcript prefix length (chars) that reliably returns
  an "acceptable" output (JSON if requested) without stubbing/truncation.

This does NOT touch your production app.
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib import request, error


DEFAULT_URL = "http://127.0.0.1:11434/api/generate"


@dataclass
class RunResult:
    n_chars: int
    elapsed_s: float
    http_status: int
    done_reason: str
    eval_count: Optional[int]
    prompt_eval_count: Optional[int]
    total_duration: Optional[int]
    raw_response_len: int
    outcome: str
    response_preview: str


def read_text(file_path: Optional[str]) -> str:
    if file_path:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    # stdin
    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("No input text found. Provide --file or pipe text into stdin.")
    return data


def post_ollama(url: str, payload: dict, timeout_s: int) -> Tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", errors="replace")
            return status, text
    except error.HTTPError as e:
        status = getattr(e, "code", 0) or 0
        txt = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        return status, txt
    except Exception as e:
        return 0, f"__EXCEPTION__ {type(e).__name__}: {e}"


def extract_generate_fields(http_text: str) -> dict:
    """
    /api/generate returns JSON with keys:
      model, created_at, response, done, done_reason, context, eval_count, prompt_eval_count, total_duration, ...
    """
    # Sometimes callers accidentally print multiple things; assume first JSON object.
    http_text = http_text.lstrip()
    if not http_text.startswith("{"):
        return {"_parse_error": "HTTP body did not start with JSON object", "_raw": http_text}

    try:
        return json.loads(http_text)
    except Exception as e:
        return {"_parse_error": f"Could not parse HTTP JSON: {e}", "_raw": http_text}


def looks_like_stub(resp_text: str) -> bool:
    """
    Heuristic: the bad response you showed was literally:
      "1. The speaker is discussing"
    or similar outline start with almost no content.
    """
    t = resp_text.strip()
    if not t:
        return True
    # Very short + starts like outline
    if len(t) < 80 and re.match(r"^\s*\d+\.\s+The speaker\b", t, re.IGNORECASE):
        return True
    # Another common stub: starts bulleting but never continues
    if len(t) < 120 and re.match(r"^\s*\d+\.\s+", t):
        return True
    return False


def try_parse_json_output(resp_text: str) -> bool:
    """
    If you request format=json, Ollama should put JSON into the "response" field.
    This checks if response text contains a JSON object anywhere.
    """
    t = resp_text.strip()
    if not t:
        return False

    # Fast path
    if t.startswith("{") and t.endswith("}"):
        try:
            json.loads(t)
            return True
        except Exception:
            return False

    # Fallback: find first { ... } balanced-ish (cheap)
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return False
    candidate = t[start:end + 1]
    try:
        json.loads(candidate)
        return True
    except Exception:
        return False


def classify_outcome(resp_text: str, want_json: bool, done_reason: str) -> str:
    """
    Outcome categories:
      OK_JSON / OK_TEXT / STUB / BAD_JSON / EMPTY / TRUNCATED / ERROR
    """
    t = resp_text.strip()

    if t.startswith("__EXCEPTION__"):
        return "ERROR"

    if not t:
        return "EMPTY"

    if looks_like_stub(t):
        return "STUB"

    if want_json:
        if try_parse_json_output(t):
            return "OK_JSON"
        # If generation ended by length, likely truncation or malformed JSON
        if done_reason == "length":
            return "TRUNCATED"
        return "BAD_JSON"

    # Non-JSON requested: treat as OK unless it’s clearly truncated or stubby
    if done_reason == "length":
        return "TRUNCATED"
    return "OK_TEXT"


def build_prompt(instructions: str, transcript: str) -> str:
    # Explicit delimiter so model can separate instruction from transcript.
    # This is *not* a redesign; it’s just making the boundary unambiguous for the test harness.
    return (
        instructions.strip()
        + "\n\n"
        + "===TRANSCRIPT_BEGIN===\n"
        + transcript
        + "\n===TRANSCRIPT_END===\n"
    )


def run_once(
    *,
    url: str,
    model: str,
    instructions: str,
    transcript_prefix: str,
    want_json: bool,
    stream: bool,
    temperature: float,
    num_ctx: int,
    num_predict: int,
    timeout_s: int,
) -> RunResult:
    prompt = build_prompt(instructions, transcript_prefix)

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": stream,  # keep False; streaming complicates measurement
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if want_json:
        payload["format"] = "json"

    t0 = time.time()
    status, http_text = post_ollama(url, payload, timeout_s=timeout_s)
    elapsed = time.time() - t0

    data = extract_generate_fields(http_text)
    resp_text = data.get("response", data.get("_raw", ""))

    done_reason = data.get("done_reason", "unknown")
    outcome = classify_outcome(resp_text, want_json=want_json, done_reason=done_reason)

    preview = resp_text.strip().replace("\n", "\\n")
    if len(preview) > 220:
        preview = preview[:220] + "…"

    return RunResult(
        n_chars=len(transcript_prefix),
        elapsed_s=elapsed,
        http_status=status,
        done_reason=done_reason,
        eval_count=data.get("eval_count"),
        prompt_eval_count=data.get("prompt_eval_count"),
        total_duration=data.get("total_duration"),
        raw_response_len=len(resp_text or ""),
        outcome=outcome,
        response_preview=preview,
    )


def write_log_line(log_path: str, rr: RunResult, lo: int, hi: int) -> None:
    line = {
        "n_chars": rr.n_chars,
        "elapsed_s": round(rr.elapsed_s, 3),
        "http_status": rr.http_status,
        "done_reason": rr.done_reason,
        "eval_count": rr.eval_count,
        "prompt_eval_count": rr.prompt_eval_count,
        "total_duration": rr.total_duration,
        "raw_response_len": rr.raw_response_len,
        "outcome": rr.outcome,
        "preview": rr.response_preview,
        "search_lo": lo,
        "search_hi": hi,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Binary-search transcript size limit for Ollama /api/generate")
    ap.add_argument("--file", help="Transcript file (utf-8). If omitted, reads from stdin.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Ollama generate URL (default: {DEFAULT_URL})")
    ap.add_argument("--model", default="mixtral:latest", help="Ollama model name (default: mixtral:latest)")

    ap.add_argument("--want_json", action="store_true", help="Request format=json and require JSON output")
    ap.add_argument("--num_ctx", type=int, default=32768, help="options.num_ctx (default: 32768)")
    ap.add_argument("--num_predict", type=int, default=4096, help="options.num_predict (default: 4096)")
    ap.add_argument("--temperature", type=float, default=0.2, help="options.temperature (default: 0.2)")
    ap.add_argument("--timeout", type=int, default=300, help="HTTP timeout seconds (default: 300)")

    ap.add_argument("--min_chars", type=int, default=2000, help="Start of search range (default: 2000)")
    ap.add_argument("--max_chars", type=int, default=0, help="End of search range. 0 = full transcript length")
    ap.add_argument("--tolerance", type=int, default=500, help="Stop when hi-lo <= tolerance (default: 500)")

    ap.add_argument("--log", default="limit_probe.log.jsonl", help="Append JSONL log here (default: limit_probe.log.jsonl)")
    ap.add_argument("--instructions_file", help="Optional file containing the instruction text to use.")
    args = ap.parse_args()

    transcript = read_text(args.file)

    # Quick warning: stop tokens in model config are [INST] [/INST]
    # If transcript contains them, it can cause early stop.
    for tok in ("[INST]", "[/INST]"):
        if tok in transcript:
            print(f"[warn] transcript contains token {tok!r} (may cause early stop)", file=sys.stderr)

    if args.instructions_file:
        with open(args.instructions_file, "r", encoding="utf-8") as f:
            instructions = f.read().strip()
    else:
        # Minimal consistent instruction set for sizing.
        # You can replace this later with your full production prompt text if you want.
        instructions = (
            "You are given a transcript. Produce a structured analysis.\n"
            "If JSON is requested, output ONLY a single JSON object.\n"
            "Otherwise, output a concise structured summary.\n"
            "Do not preface with numbering unless asked.\n"
        )

    full_len = len(transcript)
    lo = max(1, min(args.min_chars, full_len))
    hi = full_len if args.max_chars == 0 else min(args.max_chars, full_len)

    if lo >= hi:
        raise SystemExit(f"Bad range: min_chars={lo} max_chars={hi} full_len={full_len}")

    print(f"[info] transcript_len_chars={full_len}")
    print(f"[info] search_range=[{lo}, {hi}] tolerance={args.tolerance}")
    print(f"[info] want_json={args.want_json} num_ctx={args.num_ctx} num_predict={args.num_predict} temp={args.temperature}")
    print(f"[info] logging -> {args.log}")
    print("")

    # First bracket check (optional but useful):
    # We will treat “OK_JSON” (if want_json) or “OK_TEXT” (if not) as success.
    def is_success(outcome: str) -> bool:
        return outcome in ("OK_JSON", "OK_TEXT")

    # Binary search: find max N where success is True.
    # Invariant: we assume smaller sizes are more likely to succeed.
    best_ok = None

    while (hi - lo) > args.tolerance:
        mid = (lo + hi) // 2
        prefix = transcript[:mid]

        print(f"[try] n_chars={mid} ... ", end="", flush=True)
        rr = run_once(
            url=args.url,
            model=args.model,
            instructions=instructions,
            transcript_prefix=prefix,
            want_json=args.want_json,
            stream=False,
            temperature=args.temperature,
            num_ctx=args.num_ctx,
            num_predict=args.num_predict,
            timeout_s=args.timeout,
        )

        # Print concise one-line status (your “progress indicator”)
        print(f"{rr.outcome}  ({rr.elapsed_s:.1f}s) done_reason={rr.done_reason} resp_len={rr.raw_response_len}")

        # Always log
        write_log_line(args.log, rr, lo=lo, hi=hi)

        if is_success(rr.outcome):
            best_ok = rr
            lo = mid + 1  # try bigger
        else:
            hi = mid - 1  # try smaller

    # Final: test the upper end of the narrowed range for a definitive answer
    final_n = max(1, min(hi, full_len))
    print("")
    print(f"[final] testing n_chars={final_n} ... ", end="", flush=True)
    rr = run_once(
        url=args.url,
        model=args.model,
        instructions=instructions,
        transcript_prefix=transcript[:final_n],
        want_json=args.want_json,
        stream=False,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        num_predict=args.num_predict,
        timeout_s=args.timeout,
    )
    print(f"{rr.outcome}  ({rr.elapsed_s:.1f}s) done_reason={rr.done_reason} resp_len={rr.raw_response_len}")
    write_log_line(args.log, rr, lo=lo, hi=hi)

    print("")
    print("=== RESULT ===")
    if is_success(rr.outcome):
        print(f"Max-good (approx): {final_n} chars")
        print(f"Preview: {rr.response_preview}")
    elif best_ok:
        print(f"Max-good (approx): {best_ok.n_chars} chars")
        print(f"Last-good outcome: {best_ok.outcome} ({best_ok.elapsed_s:.1f}s)")
        print(f"Preview: {best_ok.response_preview}")
        print(f"Last tested failed at: {final_n} chars outcome={rr.outcome}")
    else:
        print("No successful size found in the tested range.")
        print(f"Last outcome: {rr.outcome}")
        print(f"Preview: {rr.response_preview}")

    print("")
    print(f"[done] log appended to {args.log}")


if __name__ == "__main__":
    main()

    