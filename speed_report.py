#!/usr/bin/env python3
import argparse, json, re, statistics
from pathlib import Path

EP_RE = re.compile(r"^E(\d{4})-\d{8}$")

def ep_code(ep_dirname: str) -> int | None:
    m = EP_RE.match(ep_dirname)
    return int(m.group(1)) if m else None

def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="E#### (e.g., E1501)")
    ap.add_argument("--end", required=True, help="E#### (e.g., E2400)")
    ap.add_argument("--root", default="data/episodes", help="episodes root")
    ap.add_argument("--model", default="base", help="fw-<model> folder (default: base)")
    args = ap.parse_args()

    start = int(args.start[1:])
    end = int(args.end[1:])
    episodes_root = Path(args.root)
    fw_dirname = f"fw-{args.model}"

    rows = []
    for d in episodes_root.iterdir():
        if not d.is_dir():
            continue
        code = ep_code(d.name)
        if code is None or code < start or code > end:
            continue

        out = d / "whisper" / fw_dirname
        manifest_p = out / "manifest.json"
        timings_p = out / "timings.json"
        if not manifest_p.exists() or not timings_p.exists():
            continue

        try:
            manifest = load_json(manifest_p)
            timings = load_json(timings_p)
            dur = manifest.get("audio", {}).get("duration_seconds")
            elapsed = timings.get("elapsed_seconds")
            exit_code = timings.get("exit_code", 1)
            if exit_code != 0 or dur is None or elapsed is None or dur <= 0 or elapsed <= 0:
                continue

            rtf = elapsed / dur                 # lower is better
            xrt = dur / elapsed                 # higher is better ("x realtime")
            rows.append((d.name, dur, elapsed, rtf, xrt))
        except Exception:
            continue

    if not rows:
        print("No completed episodes found in that range.")
        return

    total_dur = sum(r[1] for r in rows)
    total_elapsed = sum(r[2] for r in rows)
    rtf_list = [r[3] for r in rows]
    xrt_list = [r[4] for r in rows]

    def pct(vals, p):
        vals2 = sorted(vals)
        k = int(round((p/100) * (len(vals2)-1)))
        return vals2[k]

    print(f"Episodes counted: {len(rows)}")
    print(f"Total audio:      {total_dur/3600:.2f} hours")
    print(f"Total wall:       {total_elapsed/3600:.2f} hours")
    print(f"Overall speed:    {total_dur/total_elapsed:.2f}× realtime")
    print(f"Avg RTF:          {statistics.mean(rtf_list):.4f}")
    print(f"Median ×RT:       {statistics.median(xrt_list):.2f}×")
    print(f"P10/P90 ×RT:      {pct(xrt_list,10):.2f}× / {pct(xrt_list,90):.2f}×")

if __name__ == "__main__":
    main()
    