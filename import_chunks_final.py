#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(
        description="Import chunks_final.json files into SQLite."
    )
    p.add_argument("--root", default="data/episodes")
    p.add_argument("--db", default="data/sawl.sqlite")
    p.add_argument("--force", action="store_true", help="Delete existing DB first")
    return p.parse_args()


def find_chunks_final_files(root: Path):
    return sorted(root.rglob("chunks_final.json"))


def create_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA temp_store=MEMORY;
        PRAGMA mmap_size=30000000000;

        CREATE TABLE IF NOT EXISTS chunks (
            block_id TEXT PRIMARY KEY,
            episode TEXT NOT NULL,
            block_index INTEGER NOT NULL,
            label TEXT NOT NULL,
            src_start INTEGER NOT NULL,
            src_end INTEGER NOT NULL,
            start_anchor TEXT,
            end_anchor TEXT,
            text TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            word_count INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_episode_block
        ON chunks(episode, block_index);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            label,
            text,
            content='chunks',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, label, text)
            VALUES (new.rowid, new.label, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, label, text)
            VALUES('delete', old.rowid, old.label, old.text);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, label, text)
            VALUES('delete', old.rowid, old.label, old.text);
            INSERT INTO chunks_fts(rowid, label, text)
            VALUES (new.rowid, new.label, new.text);
        END;
        """
    )


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def import_one_file(conn: sqlite3.Connection, path: Path) -> int:
    data = load_json(path)
    episode = str(data["episode"]).split("-")[0]
    blocks = data["blocks"]

    rows = []
    for b in blocks:
        rows.append(
            (
                b["block_id"],
                episode,
                b["block_index"],
                b["label"],
                b["src_start"],
                b["src_end"],
                b.get("start_anchor", ""),
                b.get("end_anchor", ""),
                b["text"],
                b["char_count"],
                b["word_count"],
            )
        )

    conn.executemany(
        """
        INSERT OR REPLACE INTO chunks (
            block_id,
            episode,
            block_index,
            label,
            src_start,
            src_end,
            start_anchor,
            end_anchor,
            text,
            char_count,
            word_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    return len(rows)


def main():
    args = parse_args()

    root = Path(args.root)
    db_path = Path(args.db)

    if args.force and db_path.exists():
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    files = find_chunks_final_files(root)

    print(f"FOUND     {len(files)} chunks_final.json files under {root}")

    t0 = time.time()

    conn = sqlite3.connect(db_path)
    try:
        create_schema(conn)

        total_blocks = 0
        total_files = 0

        for path in files:
            count = import_one_file(conn, path)
            total_blocks += count
            total_files += 1

        conn.commit()

    finally:
        conn.close()

    elapsed = time.time() - t0

    print()
    print("DONE")
    print(f"imported {total_files} episode files")
    print(f"imported {total_blocks} blocks")
    print(f"db       {db_path}")
    print(f"elapsed  {elapsed:.2f}s")


if __name__ == "__main__":
    main()