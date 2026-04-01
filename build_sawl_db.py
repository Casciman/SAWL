#!/usr/bin/env python3
"""
build_sawl_db.py — deterministic builder for SAWL SQLite + FTS

NEW RULES (deterministic, no “helpful” behavior):
- DISK IS TRUTH: only episode directories on disk define what exists.
- Catalog is informational only:
    - Insert catalog rows ONLY if the episode exists on disk.
    - Derive catalog.episode_id STRICTLY from catalog.local_filename ("E3063-YYYYMMDD.mp3" -> "E3063").
    - If local_filename is missing or malformed: skip the row. No fallback.
- No discovery / no guessing / no alternate paths.
- Rebuild is always fresh:
    - Delete the DB file target (follow symlink to actual file), then apply schema.sql.
- Insert order:
    1) episodes (from disk)
    2) catalog (filtered to existing episodes)
    3) whisper_runs + segments (from disk)

This script assumes your schema.sql defines at least:
- episodes(episode_id, ep_dir, date, title, ... optional analysis fields ...)
- catalog(episode_id NOT NULL, description_html, audio_url, local_filename, original_filename, etc) with FK to episodes
- whisper_runs(...)
- segments(...)
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Optional, Any


# Episode directory: E####-YYYYMMDD or E#####-YYYYMMDD or with -N suffix
EPDIR_RE = re.compile(r"^(E\d{4,5})-(\d{8})(?:-\d+)?$")

# Catalog local_filename strict: E####-YYYYMMDD.mp3
CAT_LOCAL_RE = re.compile(r"^(E\d{4,5})-(\d{8})\.mp3$", re.IGNORECASE)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(read_text(path))
    except Exception:
        return None


def is_episode_dir(name: str) -> bool:
    return bool(EPDIR_RE.match(name))


def episode_id_from_epdir(ep_dir: str) -> str:
    m = EPDIR_RE.match(ep_dir)
    if not m:
        raise ValueError(f"Invalid episode dir name: {ep_dir}")
    return m.group(1)


def date_from_epdir(ep_dir: str) -> str:
    """
    E0049-20180208 -> 2018-02-08
    If pattern fails, raise (no fallback).
    """
    m = EPDIR_RE.match(ep_dir)
    if not m:
        raise ValueError(f"Invalid episode dir name: {ep_dir}")
    ymd = m.group(2)
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"


def episode_id_from_catalog_local_filename(local_filename: str) -> Optional[str]:
    if not local_filename:
        return None
    m = CAT_LOCAL_RE.match(local_filename.strip())
    if not m:
        return None
    return m.group(1).upper()


def exec_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(read_text(schema_path))


def insert_episodes_from_disk(conn: sqlite3.Connection, episodes_root: Path, verbose: bool) -> int:
    """
    Insert one row per episode directory on disk.
    Uses analysis/episode.json if present; otherwise inserts minimal (episode_id, ep_dir, date, title="").
    Deterministic: no alternate sources; no title inference from catalog here.
    """
    if not episodes_root.is_dir():
        raise RuntimeError(f"--episodes_root is not a directory: {episodes_root}")

    n = 0
    # stable order
    for ep_path in sorted([p for p in episodes_root.iterdir() if p.is_dir()], key=lambda p: p.name):
        ep_dir = ep_path.name
        if not is_episode_dir(ep_dir):
            continue

        ep_id = episode_id_from_epdir(ep_dir)
        ep_date = date_from_epdir(ep_dir)
        title = ""

        analysis_path = ep_path / "analysis" / "episode.json"
        analysis = read_json(analysis_path) if analysis_path.exists() else None

        # If analysis exists, prefer its declared fields (still deterministic; disk file only)
        if isinstance(analysis, dict):
            # episode.json uses "episode_id" like "E0049" (no date) — match schema/FK.
            ep_id = analysis.get("episode_id") or analysis.get("episode") or ep_id
            # keep ep_dir-derived date if analysis date missing
            ep_date = analysis.get("date") or analysis.get("episode_date") or ep_date
            title = analysis.get("title") or analysis.get("episode_title") or ""

            # Optional richer fields (must match your schema.sql episodes table!)
            def j(x):
                return json.dumps(x, ensure_ascii=False) if x is not None else None

            conn.execute(
                """
                INSERT OR REPLACE INTO episodes (
                episode_id, date, title, analysis_version,
                summary_narrative, summary_compact_json,
                topics_json, traits_json,
                notable_quotes_json, persuasion_lessons_json,
                predictions_json, thought_experiments_json,
                closing_observations,
                evaluation_json,
                ep_dir, ep_root, episode_json_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ep_id,
                    ep_date,
                    title,
                    analysis.get("analysis_version"),
                    analysis.get("summary_narrative") or "",
                    j(analysis.get("summary_compact") or analysis.get("summary_bullets") or []),
                    j(analysis.get("topics") or []),
                    j(analysis.get("traits") or {}),
                    j(analysis.get("notable_quotes") or []),
                    j(analysis.get("persuasion_lessons") or analysis.get("persuasion") or []),
                    j(analysis.get("predictions") or []),
                    j(analysis.get("thought_experiments") or analysis.get("thought_experiment") or []),
                    analysis.get("closing_observations") or analysis.get("closing") or "",
                    j(analysis.get("evaluation") or {}),
                    ep_dir,
                    str(episodes_root),
                    str(analysis_path),
                ),
        )
        else:
            # Minimal deterministic insert (disk truth only)
            conn.execute(
                """
                INSERT OR REPLACE INTO episodes (
                episode_id, date, title,
                ep_dir, ep_root, episode_json_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ep_id,
                    ep_date or "",
                    title or "",
                    ep_dir,
                    str(episodes_root),
                    str(analysis_path) if analysis_path.exists() else None,
                ),
)
        n += 1
        if verbose and (n % 250 == 0):
            print(f"episodes: {n}")

    return n


def insert_catalog_filtered(conn: sqlite3.Connection, catalog_path: Path, verbose: bool) -> int:
    """
    Catalog format is fixed:
      { "meta": {...}, "episodes": [ {...}, ... ] }

    Insert catalog rows ONLY when:
      - local_filename parses to episode_id AND
      - that episode_id exists in episodes table (disk truth)
    """
    data = read_json(catalog_path)
    if not isinstance(data, dict) or "episodes" not in data or not isinstance(data["episodes"], list):
        raise RuntimeError(f"catalog.json must be an object with an 'episodes' list: {catalog_path}")

    existing_eps = {r[0] for r in conn.execute("SELECT episode_id FROM episodes")}
    rows = data["episodes"]

    n = 0
    cur = conn.cursor()

    for row in rows:
        if not isinstance(row, dict):
            continue

        ep_id = episode_id_from_catalog_local_filename(row.get("local_filename", ""))
        if not ep_id:
            continue

        # DISK TRUTH FILTER
        if ep_id not in existing_eps:
            continue

        # schema.sql uses description_html (not "description")
        cur.execute(
            """
            INSERT OR REPLACE INTO catalog (
              episode_id,
              guid, pub_date, pub_date_compact,
              title, description_html, episode_number,
              audio_url, local_filename, original_filename
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ep_id,
                row.get("guid"),
                row.get("pub_date"),
                row.get("pub_date_compact"),
                row.get("title"),
                row.get("description"),  # HTML from feed
                row.get("episode_number"),
                row.get("audio_url"),
                row.get("local_filename"),
                row.get("original_filename"),
            ),
        )
        n += 1
        if verbose and (n % 500 == 0):
            print(f"catalog: {n}")

    return n


def insert_whisper_runs_and_segments(conn: sqlite3.Connection, episodes_root: Path, verbose: bool) -> tuple[int, int]:
    """
    Ingest whisper/fw-*/manifest.json, timings.json, segments.json
    Deterministic: only what exists on disk.
    """
    runs = 0
    segs_total = 0

    for ep_path in sorted([p for p in episodes_root.iterdir() if p.is_dir()], key=lambda p: p.name):
        ep_dir = ep_path.name
        if not is_episode_dir(ep_dir):
            continue

        ep_id = episode_id_from_epdir(ep_dir)

        whisper = ep_path / "whisper"
        if not whisper.is_dir():
            continue

        fw_dirs = sorted([p for p in whisper.glob("fw-*") if p.is_dir()], key=lambda p: p.name)
        for fw_dir in fw_dirs:
            manifest_path = fw_dir / "manifest.json"
            timings_path = fw_dir / "timings.json"
            segments_path = fw_dir / "segments.json"
            transcript_path = fw_dir / "transcript.txt"
            vtt_path = fw_dir / "timestamps.vtt"

            manifest = read_json(manifest_path) if manifest_path.exists() else None
            timings = read_json(timings_path) if timings_path.exists() else None

            # Prefer manifest.run; otherwise timings (still deterministic)
            run_tool = run_model = run_compute = run_lang = run_task = run_device = None
            elapsed = started_at = finished_at = exit_code = None
            m_ep = m_date = m_title = None

            if isinstance(manifest, dict) and isinstance(manifest.get("run"), dict):
                r = manifest["run"]
                run_tool = r.get("tool")
                run_model = r.get("model")
                run_compute = r.get("compute_type")
                run_lang = r.get("language")
                run_task = r.get("task")
                run_device = r.get("device")
                elapsed = r.get("elapsed_seconds")
                started_at = r.get("started_at")
                finished_at = r.get("finished_at")
                exit_code = r.get("exit_code")

                ei = manifest.get("episode_index") if isinstance(manifest.get("episode_index"), dict) else {}
                m_ep = ei.get("episode")
                m_date = ei.get("date")
                m_title = ei.get("title")

            elif isinstance(timings, dict):
                run_tool = timings.get("tool")
                run_model = timings.get("model")
                run_compute = timings.get("compute_type")
                run_lang = timings.get("language")
                run_task = timings.get("task")
                run_device = timings.get("device")
                elapsed = timings.get("elapsed_seconds")
                started_at = timings.get("started_at")
                finished_at = timings.get("finished_at")
                exit_code = timings.get("exit_code")

                ei = timings.get("episode_index") if isinstance(timings.get("episode_index"), dict) else {}
                m_ep = ei.get("episode")
                m_date = ei.get("date")
                m_title = ei.get("title")

            conn.execute(
                """
                INSERT INTO whisper_runs (
                episode_id,
                tool, model, compute_type, language, task, device,
                elapsed_seconds, started_at, finished_at, exit_code,
                audio_path,
                transcript_path, segments_path, vtt_path, timings_path, manifest_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ep_id,
                    run_tool, run_model, run_compute, run_lang, run_task, run_device,
                    elapsed, started_at, finished_at, exit_code,
                    None,                               # audio_path — you don't seem to set this; use None or derive it
                    str(transcript_path) if transcript_path.exists() else None,
                    str(segments_path) if segments_path.exists() else None,
                    str(vtt_path) if vtt_path.exists() else None,
                    str(timings_path) if timings_path.exists() else None,
                    str(manifest_path) if manifest_path.exists() else None,
                ),
    )
            runs += 1

            segs = read_json(segments_path) if segments_path.exists() else None
            if isinstance(segs, list):
                for i, seg in enumerate(segs):
                    if not isinstance(seg, dict):
                        continue
                    conn.execute(
                    """
                        INSERT INTO segments (
                            episode_id,
                            run_pk,           -- can be NULL for now, or link it later
                            seg_index,
                            start_s,
                            end_s,
                            text
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ep_id,
                            None,             # ← replace with real run_pk later (recommended)
                            i,
                            seg.get("start") or 0.0,
                            seg.get("end")   or 0.0,
                            seg.get("text")  or "",
                        )
                    )

                    segs_total += 1

            if verbose and (runs % 250 == 0):
                print(f"whisper_runs: {runs}   segments: {segs_total}")

    return runs, segs_total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes_root", required=True, help="Folder containing episode directories on disk")
    ap.add_argument("--catalog", required=True, help="SAWL data/catalog.json (fixed format)")
    ap.add_argument("--db", required=True, help="Output SQLite DB path (symlink OK)")
    ap.add_argument("--schema", required=True, help="schema.sql path")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    episodes_root = Path(args.episodes_root).expanduser()
    catalog_path = Path(args.catalog).expanduser()
    schema_path = Path(args.schema).expanduser()

    # IMPORTANT: follow symlink to target DB file (you said DB lives on NVME)
    db_link = Path(args.db).expanduser()
    db_path = db_link.resolve()

    if args.verbose:
        print(f"db: {db_link} -> {db_path}")
        print(f"episodes_root: {episodes_root.resolve()}")
        print(f"catalog: {catalog_path.resolve()}")
        print(f"schema: {schema_path.resolve()}")

    if not schema_path.exists():
        raise RuntimeError(f"schema not found: {schema_path}")
    if not catalog_path.exists():
        raise RuntimeError(f"catalog not found: {catalog_path}")
    if not episodes_root.exists():
        raise RuntimeError(f"episodes_root not found: {episodes_root}")

    # Fresh rebuild: delete the actual target DB file
    if db_path.exists():
        db_path.unlink()

    # Create parent if needed
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")

        exec_schema(conn, schema_path)

        conn.execute("BEGIN;")
        n_eps = insert_episodes_from_disk(conn, episodes_root, args.verbose)
        conn.execute("COMMIT;")

        conn.execute("BEGIN;")
        n_cat = insert_catalog_filtered(conn, catalog_path, args.verbose)
        conn.execute("COMMIT;")

        conn.execute("BEGIN;")
        n_runs, n_segs = insert_whisper_runs_and_segments(conn, episodes_root, args.verbose)
        conn.execute("COMMIT;")

        if args.verbose:
            print("DONE")
            print(f"episodes: {n_eps}")
            print(f"catalog: {n_cat}")
            print(f"whisper_runs: {n_runs}")
            print(f"segments: {n_segs}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()