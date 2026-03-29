#!/usr/bin/env python3
# episode_db_viewer.py — Episode Viewer backed by SQLite episodes table
#
# Goal: keep the current UI/behavior, but source episode metadata and analysis
# from SQLite instead of per-episode JSON files.
#
# Notes:
# - Transcript button remains file-based for ground truth.
# - Search remains lightweight and in-memory.
# - Episode row is converted back into the original episode.json-like shape so
#   the existing rendering code can stay mostly unchanged.

from __future__ import annotations

import os
import re
import json
import argparse
import sqlite3
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path
from typing import Optional

# ------------------------------ helpers ------------------------------

EPDIR_RE = re.compile(r"^E\d{4,5}-\d{8}(-\d+)?$")


def is_episode_dir_name(name: str) -> bool:
    return bool(EPDIR_RE.match(name))


def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def pick_transcript_file(ep_path: Path, prefer_model: str | None = None) -> Path | None:
    """
    Pick a transcript file.
    Priority:
      1) whisper/fw-<prefer_model>/transcript.txt if exists
      2) whisper/fw-*/transcript.txt (newest mtime)
    """
    whisper = ep_path / "whisper"
    if not whisper.is_dir():
        return None

    if prefer_model:
        candidate = whisper / f"fw-{prefer_model}" / "transcript.txt"
        if candidate.exists():
            return candidate

    best = None
    best_mtime = -1.0
    for d in whisper.glob("fw-*"):
        t = d / "transcript.txt"
        if t.exists():
            try:
                mt = t.stat().st_mtime
            except Exception:
                mt = 0
            if mt > best_mtime:
                best = t
                best_mtime = mt
    return best


def compact_one_line(s: str, n: int = 180) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else (s[: n - 1] + "…")


def limit_lines(text: str, max_lines: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + "\n…"


def bullets(items, max_items: int = 12, max_line_chars: int = 180) -> str:
    if not items:
        return ""
    if isinstance(items, str):
        return limit_lines(items.strip(), max_items)
    if isinstance(items, list):
        out = []
        for x in items[:max_items]:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            s = compact_one_line(s, max_line_chars)
            if s.startswith(("-", "•", "*")):
                out.append(s)
            else:
                out.append(f"- {s}")
        if len(items) > max_items:
            out.append("…")
        return "\n".join(out)
    return compact_one_line(str(items), max_line_chars)


def pretty_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d.get(k)
    return default


def load_json_field(value: Optional[str], default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def row_to_episode_dict(row: sqlite3.Row) -> dict:
    return {
        "episode_id": row["episode_id"],
        "date": row["date"] or "",
        "title": row["title"] or "",
        "analysis_version": row["analysis_version"] or 1,
        "summary_narrative": row["summary_narrative"] or "",
        "summary_compact": load_json_field(row["summary_compact_json"], []),
        "topics": load_json_field(row["topics_json"], []),
        "traits": load_json_field(row["traits_json"], {}),
        "notable_quotes": load_json_field(row["notable_quotes_json"], []),
        "persuasion_lessons": load_json_field(row["persuasion_lessons_json"], []),
        "predictions": load_json_field(row["predictions_json"], []),
        "thought_experiments": load_json_field(row["thought_experiments_json"], []),
        "closing_observations": row["closing_observations"] or "",
        "evaluation": load_json_field(row["evaluation_json"], {}),
    }


# ------------------------------ EV App ------------------------------

class EpisodeDBViewer(tk.Tk):
    def __init__(self, db_path: Path, prefer_model: str | None = None):
        super().__init__()
        self.title("Episode DB Viewer (EDV)")
        self.geometry("1600x1100")

        self.db_path = db_path
        self.prefer_model = prefer_model

        # Index entries: dict with keys:
        #   episode_id, date, title, guest, ep_dir, ep_root, episode_json_path, search_blob
        self.episodes: list[dict] = []
        self.filtered: list[dict] = []
        self.current: dict | None = None
        self.current_json: dict | None = None
        self.current_transcript_path: Path | None = None

        # UI vars
        self.var_search = tk.StringVar()

        # deep-dive window (single floating)
        self.detail_win: tk.Toplevel | None = None
        self.detail_text: scrolledtext.ScrolledText | None = None
        self.detail_mode = None

        self._build_ui()
        self._load_index()
        self._apply_filter()

    # ---------------- db helpers ----------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_episode_list(self) -> list[dict]:
        sql = """
        SELECT episode_id, date, title, traits_json, ep_dir, ep_root, episode_json_path
        FROM episodes
        ORDER BY episode_id
        """
        out: list[dict] = []
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        for row in rows:
            traits = load_json_field(row["traits_json"], {})
            guest = traits.get("guest") if isinstance(traits, dict) else ""
            guest = guest or ""
            blob = " ".join([
                (row["episode_id"] or "").strip(),
                (row["title"] or "").strip(),
                str(guest).strip(),
                (row["date"] or "").strip(),
            ]).lower()
            out.append(
                {
                    "episode_id": row["episode_id"] or "",
                    "date": row["date"] or "",
                    "title": row["title"] or "",
                    "guest": guest,
                    "ep_dir": row["ep_dir"] or "",
                    "ep_root": row["ep_root"] or "",
                    "episode_json_path": row["episode_json_path"] or "",
                    "search_blob": blob,
                }
            )
        return out

    def _fetch_episode_row(self, episode_id: str) -> Optional[sqlite3.Row]:
        sql = "SELECT * FROM episodes WHERE episode_id = ?"
        with self._connect() as conn:
            return conn.execute(sql, (episode_id,)).fetchone()

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=0, minsize=520)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        nav = ttk.Frame(self, padding=8)
        nav.grid(row=0, column=0, sticky="nsew")
        nav.configure(width=520)
        nav.grid_propagate(False)
        nav.columnconfigure(0, weight=1)
        nav.rowconfigure(2, weight=1)

        ttk.Label(nav, text="Search:").grid(row=0, column=0, sticky="w")
        ent = ttk.Entry(nav, textvariable=self.var_search)
        ent.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        ent.bind("<KeyRelease>", lambda e: self._apply_filter())
        ent.bind("<Escape>", lambda e: self._clear_search())
        ent.bind("<Return>", lambda e: self._open_first_match())

        self.lbl_count = ttk.Label(nav, text="0/0")
        self.lbl_count.grid(row=1, column=0, sticky="e")

        self.listbox = tk.Listbox(
            nav,
            activestyle="dotbox",
            font=("Menlo", 14),
            width=48,
        )
        self.listbox.grid(row=2, column=0, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self.listbox.bind("<Double-Button-1>", lambda e: self._open_detail_default())

        sb = ttk.Scrollbar(nav, orient="vertical", command=self.listbox.yview)
        sb.grid(row=2, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)

        main = ttk.Frame(self, padding=(8, 8, 8, 8))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        btns = ttk.Frame(top)
        btns.grid(row=0, column=0, sticky="e", pady=(0, 4))

        self.lbl_header = ttk.Label(
            top,
            text="(select an episode)",
            font=("Menlo", 12, "bold"),
            foreground="#3A7BD5",
        )
        self.lbl_header.grid(row=1, column=0, sticky="w", pady=(2, 0))

        ttk.Button(btns, text="Transcript", command=self._open_transcript).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="JSON", command=self._open_json).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(btns, text="Copy Splat", command=self._copy_splat).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(btns, text="Reload", command=self._reload).grid(row=0, column=3)

        self.splat = scrolledtext.ScrolledText(main, wrap=tk.WORD)
        self.splat.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.splat.configure(font=("Menlo", 14))
        self.splat.configure(state=tk.DISABLED)

        self.bind("<Up>", self._kb_up)
        self.bind("<Down>", self._kb_down)
        self.bind("<Prior>", self._kb_page_up)
        self.bind("<Next>", self._kb_page_down)
        self.bind("<Return>", lambda e: self._open_detail_default())
        self.bind("t", lambda e: self._open_transcript())
        self.bind("j", lambda e: self._open_json())
        self.bind("c", lambda e: self._copy_splat())

    # ---------------- Indexing ----------------

    def _load_index(self):
        try:
            self.episodes = self._fetch_episode_list()
        except Exception as e:
            messagebox.showerror("DB Error", f"Could not load episodes from SQLite:\n\n{e}")
            self.episodes = []

    # ---------------- Filtering / list ----------------

    def _clear_search(self):
        self.var_search.set("")
        self._apply_filter()

    def _apply_filter(self):
        q = (self.var_search.get() or "").strip().lower()
        if not q:
            self.filtered = list(self.episodes)
        else:
            self.filtered = [e for e in self.episodes if q in (e.get("search_blob") or "")]
        self._refresh_listbox()

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for e in self.filtered:
            ep = e.get("episode_id", "")
            dt = e.get("date", "")
            title = compact_one_line(e.get("title", ""), 90)
            guest = compact_one_line(e.get("guest", ""), 40)

            parts = [ep, dt, title]
            if guest:
                parts.append(guest)
            line = " | ".join(parts)

            self.listbox.insert(tk.END, line)

        self.lbl_count.config(text=f"{len(self.filtered)}/{len(self.episodes)}")

        if self.filtered and self.listbox.size() > 0 and self.listbox.curselection() == ():
            self.listbox.selection_set(0)
            self.listbox.activate(0)
            self._on_select()

    def _open_first_match(self):
        if not self.filtered:
            return
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(0)
        self.listbox.activate(0)
        self._on_select()

    # ---------------- Selection / loading ----------------

    def _on_select(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        e = self.filtered[sel[0]]
        self._load_episode(e)

    def _load_episode(self, e: dict):
        self.current = e
        self.current_json = None
        self.current_transcript_path = None

        row = self._fetch_episode_row(e["episode_id"])
        if row is not None:
            self.current_json = row_to_episode_dict(row)

        ep_path = None
        ep_root = (e.get("ep_root") or "").strip()
        ep_dir = (e.get("ep_dir") or "").strip()
        if ep_root and ep_dir:
            candidate = Path(ep_root) / ep_dir
            if candidate.exists():
                ep_path = candidate
        if ep_path is not None:
            self.current_transcript_path = pick_transcript_file(ep_path, prefer_model=self.prefer_model)

        ep_id = e.get("episode_id", "")
        dt = e.get("date", "")
        title = e.get("title", "")
        guest = e.get("guest", "")
        parts = [ep_id, dt, compact_one_line(title, 180)]
        if guest:
            parts.append(compact_one_line(guest, 60))
        self.lbl_header.config(text="  ".join(parts))

        splat_text = self._render_splat(self.current_json, e)
        self._set_splat_text(splat_text)

    # ---------------- Splat rendering ----------------

    def _render_splat(self, data: dict | None, entry: dict) -> str:
        d = data or {}
        ep_id = safe_get(d, "episode_id", "episode", default=entry.get("episode_id", "")) or entry.get("episode_id", "")
        title = safe_get(d, "title", "episode_title", default=entry.get("title", "")) or entry.get("title", "")
        date = safe_get(d, "date", "episode_date", "published", default=entry.get("date", "")) or entry.get("date", "")
        traits = safe_get(d, "traits", default={})
        guest = ""
        if isinstance(traits, dict):
            guest = traits.get("guest") or ""
        guest = guest or entry.get("guest", "")

        duration = safe_get(d, "duration_seconds", "audio_seconds", default="")
        model = safe_get(d, "model", default="")
        source = safe_get(d, "source", default="")

        summary_narr = safe_get(d, "summary_narrative", default="") or ""
        summary_compact = safe_get(d, "summary_compact", "summary_bullets", default=[]) or []

        quotes = safe_get(d, "notable_quotes", default=[]) or []
        preds = safe_get(d, "predictions", default=[]) or []
        persu = safe_get(d, "persuasion_lessons", "persuasion", default=[]) or []
        thoughts = safe_get(d, "thought_experiments", "thought_experiment", default=[]) or []
        closing = safe_get(d, "closing_observations", "closing", default="") or ""

        has_db = "yes" if data else "no"
        has_tx = "yes" if (self.current_transcript_path and self.current_transcript_path.exists()) else "no"

        summary_narr = limit_lines(summary_narr.strip(), 12)
        bullets_txt = bullets(summary_compact, max_items=12, max_line_chars=180)

        quotes_txt = bullets([f'"{q}"' for q in quotes], max_items=5, max_line_chars=180)
        preds_txt = bullets(preds, max_items=8, max_line_chars=160)
        persu_txt = bullets(persu, max_items=8, max_line_chars=160)
        thoughts_txt = bullets(thoughts, max_items=6, max_line_chars=160)

        traits_txt = ""
        if isinstance(traits, dict) and traits:
            keys = [k for k in traits.keys() if k != "guest"]
            lines = []
            if guest:
                lines.append(f"guest: {guest}")
            for k in sorted(keys):
                v = traits.get(k)
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v if x)
                lines.append(f"{k}: {v}")
            traits_txt = "\n".join(lines).strip()

        parts = []
        parts.append(f"EPISODE: {ep_id}    DATE: {date}")
        parts.append(f"TITLE: {title}")
        if guest:
            parts.append(f"GUEST: {guest}")
        meta_bits = []
        if duration:
            meta_bits.append(f"duration_seconds: {duration}")
        if model:
            meta_bits.append(f"model: {model}")
        if source:
            meta_bits.append(f"source: {source}")
        if meta_bits:
            parts.append("META: " + " | ".join(meta_bits))

        parts.append("")
        parts.append("SUMMARY (NARRATIVE)")
        parts.append(summary_narr or "(none)")

        parts.append("")
        parts.append("SUMMARY (BULLETS)")
        parts.append(bullets_txt or "(none)")

        parts.append("")
        parts.append("QUOTES (TOP)")
        parts.append(quotes_txt or "(none)")

        parts.append("")
        parts.append("PREDICTIONS")
        parts.append(preds_txt or "(none)")

        parts.append("")
        parts.append("PERSUASION LESSONS")
        parts.append(persu_txt or "(none)")

        parts.append("")
        parts.append("THOUGHT EXPERIMENTS")
        parts.append(thoughts_txt or "(none)")

        if closing.strip():
            parts.append("")
            parts.append("CLOSING OBSERVATIONS (TRUNC)")
            parts.append(limit_lines(closing.strip(), 10))

        if traits_txt:
            parts.append("")
            parts.append("TRAITS")
            parts.append(limit_lines(traits_txt, 14))

        parts.append("")
        parts.append(f"FILES: episode_db={has_db} | transcript={has_tx}")
        parts.append("HOTKEYS: t=Transcript  j=JSON  c=Copy Splat  Enter=Open Detail")

        return "\n".join(parts).rstrip() + "\n"

    def _set_splat_text(self, text: str):
        self.splat.configure(state=tk.NORMAL)
        self.splat.delete("1.0", tk.END)
        self.splat.insert("1.0", text or "")
        self.splat.configure(state=tk.DISABLED)

    # ---------------- Deep-dive window ----------------

    def _ensure_detail_window(self, title: str = "Episode Detail"):
        if self.detail_win and self.detail_win.winfo_exists():
            self.detail_win.title(title)
            self.detail_win.lift()
            return

        self.detail_win = tk.Toplevel(self)
        self.detail_win.title(title)
        self.detail_win.geometry("900x900")

        self.detail_text = scrolledtext.ScrolledText(self.detail_win, wrap=tk.WORD)
        self.detail_text.pack(fill="both", expand=True)
        self.detail_text.configure(font=("Menlo", 12))
        self.detail_text.configure(state=tk.DISABLED)

    def _set_detail_text(self, text: str, title: str):
        self._ensure_detail_window(title=title)
        assert self.detail_text is not None
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text or "")
        self.detail_text.configure(state=tk.DISABLED)

    def _open_detail_default(self):
        if not self.current:
            return
        if not self.current_json:
            self._open_json()
            return

        d = self.current_json
        ep_id = safe_get(d, "episode_id", "episode", default=self.current.get("episode_id", ""))
        title = safe_get(d, "title", "episode_title", default=self.current.get("title", ""))
        summary_narr = safe_get(d, "summary_narrative", default="") or ""
        summary_compact = safe_get(d, "summary_compact", "summary_bullets", default=[]) or []
        quotes = safe_get(d, "notable_quotes", default=[]) or []

        body = []
        body.append(f"{ep_id} — {title}\n")
        body.append("SUMMARY (NARRATIVE)\n" + (summary_narr.strip() or "(none)") + "\n")
        body.append("SUMMARY (BULLETS)\n" + (bullets(summary_compact, max_items=999) or "(none)") + "\n")
        body.append("NOTABLE QUOTES\n" + (bullets([f'"{q}"' for q in quotes], max_items=999) or "(none)") + "\n")

        self._set_detail_text("\n".join(body), title=f"{ep_id} — Detail")

    def _open_json(self):
        if not self.current:
            return
        if not self.current_json:
            messagebox.showwarning("JSON", "No episode row loaded from database for this episode.")
            return
        ep_id = self.current.get("episode_id", "")
        self._set_detail_text(pretty_json(self.current_json), title=f"{ep_id} — Structured JSON")

    def _open_transcript(self):
        if not self.current:
            return
        if not self.current_transcript_path or not self.current_transcript_path.exists():
            messagebox.showwarning("Transcript", "Transcript file not found for this episode.")
            return

        text = safe_read_text(self.current_transcript_path)
        ep_id = self.current.get("episode_id", "")
        self._set_detail_text(text, title=f"{ep_id} — Transcript")

    # ---------------- Clipboard / reload ----------------

    def _copy_splat(self):
        try:
            text = self.splat.get("1.0", tk.END).rstrip("\n")
            self.clipboard_clear()
            self.clipboard_append(text)
        except Exception:
            pass

    def _reload(self):
        self._load_index()
        self._apply_filter()

    # ---------------- Keyboard nav ----------------

    def _move_selection(self, delta: int):
        n = self.listbox.size()
        if n <= 0:
            return
        cur = self.listbox.curselection()
        idx = cur[0] if cur else 0
        new_idx = max(0, min(n - 1, idx + delta))
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(new_idx)
        self.listbox.activate(new_idx)
        self.listbox.see(new_idx)
        self._on_select()

    def _kb_up(self, event): self._move_selection(-1)
    def _kb_down(self, event): self._move_selection(+1)
    def _kb_page_up(self, event): self._move_selection(-20)
    def _kb_page_down(self, event): self._move_selection(+20)


# ------------------------------ CLI ------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/sawl.sqlite", help="SQLite database path")
    ap.add_argument("--prefer_model", default=None, help="Prefer fw-<model> transcript (e.g., base)")
    args = ap.parse_args()

    app = EpisodeDBViewer(Path(args.db), prefer_model=args.prefer_model)
    app.mainloop()


if __name__ == "__main__":
    main()
