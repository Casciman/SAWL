#!/usr/bin/env python3
# make_episode_index.py
# Run from SAWL root.
# Builds: data/episode_index.tsv
#
# Inputs:
#   - canonical mp3 list (from ls) OR mp3files.txt (lines from `ls -l` or `ls`)
#   - all_files.txt (historical names w/ embedded titles)
#
# Output columns:
#   episode   date        title

import argparse, re
from pathlib import Path

EP_CANON_RE = re.compile(r"\bE(\d{4})-(\d{8})\b", re.IGNORECASE)

def yyyymmdd_to_iso(s: str) -> str:
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"

def parse_allfiles_date(s: str) -> str:
    # Accept YYYY-MM-DD, MM-DD-YY, MM-DD-YYYY in the all_files naming soup
    m = re.search(r"\b(20\d{2})[-_/](\d{2})[-_/](\d{2})\b", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{2})[-_/](\d{2})[-_/](\d{2})\b", s)  # MM-DD-YY
    if m:
        mm, dd, yy = m.group(1), m.group(2), m.group(3)
        return f"20{yy}-{mm}-{dd}"
    m = re.search(r"\b(\d{2})[-_/](\d{2})[-_/](20\d{2})\b", s)  # MM-DD-YYYY
    if m:
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{yyyy}-{mm}-{dd}"
    return ""

def clean_title(stem: str) -> str:
    # Remove bracketed ids, leading numbers, "Episode ####", and "Scott Adams"
    s = re.sub(r"\s*\[[^\]]+\]\s*", " ", stem).strip()
    s = re.sub(r"^\d+\s*-\s*", "", s).strip()
    s = re.sub(r"^Episode\s+\d+[A-Za-z]*\s*-\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^Episode\s+\d+[A-Za-z]*\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\bScott\s+Adams\b", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—")
    return s

def build_title_map(all_files_path: Path) -> dict:
    """
    Returns:
      titles_by_epnum: { int_epnum: best_title_str }
    We pick the first seen title for an episode number (you can change tie-break rules later).
    """
    titles_by_epnum = {}
    for raw in all_files_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        fn = Path(line).name
        stem = re.sub(r"\.(mp3|m4a|wav|flac)(\.part)?$", "", fn, flags=re.IGNORECASE)

        ep_num = None
        m = re.match(r"^(\d{1,5})\s*-\s*", stem)
        if m:
            ep_num = int(m.group(1))
        else:
            m = re.search(r"\bEpisode\s+(\d{1,5})", stem, flags=re.IGNORECASE)
            if m:
                ep_num = int(m.group(1))

        if ep_num is None:
            continue

        title = clean_title(stem)
        if ep_num not in titles_by_epnum and title:
            titles_by_epnum[ep_num] = title

    return titles_by_epnum

def parse_canonical_from_list(list_path: Path) -> list:
    """
    Accepts mp3files.txt content that might be `ls` or `ls -l` output.
    Extracts canonical episode ids + dates from filenames like E0022-20180112.mp3
    Returns: list of tuples (ep_num:int, episode_id:str, date_iso:str)
    """
    eps = []
    for raw in list_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        fn = Path(line.split()[-1]).name  # works for ls -l and plain ls
        m = EP_CANON_RE.search(fn)
        if not m:
            continue
        ep_num = int(m.group(1))
        episode_id = f"E{ep_num:04d}"
        date_iso = yyyymmdd_to_iso(m.group(2))
        eps.append((ep_num, episode_id, date_iso))
    # dedupe + sort
    eps = sorted(set(eps), key=lambda x: x[0])
    return eps

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical_list", required=True,
                    help="A file containing filenames (or ls -l lines) for canonical E####-YYYYMMDD.mp3")
    ap.add_argument("--all_files", required=True, help="Path to all_files.txt")
    ap.add_argument("--out", default="data/episode_index.tsv", help="Output TSV path (relative to SAWL root)")
    args = ap.parse_args()

    root = Path.cwd()
    canon_list = (root / args.canonical_list).resolve() if not Path(args.canonical_list).is_absolute() else Path(args.canonical_list)
    all_files = (root / args.all_files).resolve() if not Path(args.all_files).is_absolute() else Path(args.all_files)
    out_path = root / args.out

    titles = build_title_map(all_files)
    eps = parse_canonical_from_list(canon_list)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["episode\tdate\ttitle"]
    missing_titles = 0
    for ep_num, ep_id, date_iso in eps:
        title = titles.get(ep_num, "")
        if not title:
            missing_titles += 1
        lines.append(f"{ep_id}\t{date_iso}\t{title}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[✓] Wrote: {out_path}")
    print(f"[i] Episodes: {len(eps)}")
    print(f"[i] Missing titles: {missing_titles}")

if __name__ == "__main__":
    main()