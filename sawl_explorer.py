#!/usr/bin/env python3
# sawl_explorer.py — corpus-wide explorer for SAWL episodes table
#
# First-step explorer:
# - left: compact result rail (episode_id + date)
# - center: deterministic query controls over episodes table
# - right: full splat/detail for selected episode
#
# Rules:
# - blank control => ANY
# - across categories => AND
# - within a control: adjacency means AND, '+' or AND means AND, '|' or OR means OR
# - AND precedence before OR
# - no parentheses yet
# - query is compiled deterministically to SQL over episodes table

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path
from typing import Optional

from pygments import style

from click import style
import shlex


# ------------------------------ helpers ------------------------------


def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


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


TOKEN_SPLIT_RE = re.compile(r"(\|)|(?:\bOR\b)|(\+)|(?:\bAND\b)", re.IGNORECASE)
WORD_RE = re.compile(r"\S+")




def normalize_expr(text: str) -> list[list[str]]:
    """
    Parse one panel expression into OR-groups of AND-terms.

    Rules:
    - quoted phrases stay together
    - adjacency means AND
    - '+' or AND means AND
    - '|' or OR means OR
    - AND binds tighter than OR
    - no parentheses
    """
    s = (text or "").strip()
    if not s:
        return []

    # normalize words to symbolic operators
    s = re.sub(r"\bAND\b", " + ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bOR\b", " | ", s, flags=re.IGNORECASE)

    # make sure symbolic operators are separated so shlex sees them cleanly
    s = s.replace("|", " | ").replace("+", " + ")

    try:
        raw = shlex.split(s)
    except ValueError:
        # fallback if user has an unmatched quote
        raw = s.split()

    groups: list[list[str]] = [[]]

    for tok in raw:
        if tok == "|":
            if groups[-1]:
                groups.append([])
            continue
        if tok == "+":
            continue
        groups[-1].append(tok)

    groups = [g for g in groups if g]
    return groups


def compile_contains_expr(column: str, text: str) -> tuple[str, list[str]]:
    """
    Compile one panel expression to SQL.
    Across OR-groups => OR
    Within group => AND
    Each token => column LIKE ? with %token%
    """
    groups = normalize_expr(text)
    if not groups:
        return "", []

    params: list[str] = []
    or_parts: list[str] = []
    for group in groups:
        and_parts: list[str] = []
        for term in group:
            and_parts.append(f"{column} LIKE ? COLLATE NOCASE")
            params.append(f"%{term}%")
        or_parts.append("(" + " AND ".join(and_parts) + ")")
    return "(" + " OR ".join(or_parts) + ")", params


# ------------------------------ Explorer App ------------------------------

class SAWLExplorer(tk.Tk):
    def __init__(self, db_path: Path):
        super().__init__()
        self.title("SAWL Explorer")
        self.geometry("2250x1150")

        self.db_path = db_path

        self.results: list[dict] = []
        self.current: dict | None = None
        self.current_json: dict | None = None

        self.detail_win: tk.Toplevel | None = None
        self.detail_text: scrolledtext.ScrolledText | None = None
        self.sql_win: tk.Toplevel | None = None
        self.sql_text: scrolledtext.ScrolledText | None = None

        self.vars = {
            "episode_id": tk.StringVar(),
            "date": tk.StringVar(),
            "title": tk.StringVar(),
            "summary_narrative": tk.StringVar(),
            "summary_compact": tk.StringVar(),
            "topics": tk.StringVar(),
            "notable_quotes": tk.StringVar(),
            "predictions": tk.StringVar(),
            "persuasion_lessons": tk.StringVar(),
            "thought_experiments": tk.StringVar(),
            "closing_observations": tk.StringVar(),
            "guest": tk.StringVar(),
            "limit": tk.StringVar(value="200"),
            "dale": tk.BooleanVar(value=False),
            "whiteboard": tk.BooleanVar(value=False),
            "thought_experiment_flag": tk.BooleanVar(value=False),
        }

        style = ttk.Style(self)
        style.configure("Explorer.TButton", font=("Menlo", 14))
        style.configure("Explorer.TLabel", font=("Menlo", 14))
        style.configure("Explorer.TCheckbutton", font=("Menlo", 14))
        style.configure("Explorer.TLabelframe.Label", font=("Menlo", 14))

        self._build_ui()
        self.run_query(select_first=True)

    # ---------------- db helpers ----------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_episode_row(self, episode_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=0, minsize=235)
        self.columnconfigure(1, weight=0, minsize=620)
        self.columnconfigure(2, weight=1)
        self.rowconfigure(0, weight=1)

        # left result rail
        left = ttk.Frame(self, padding=8)
        left.grid(row=0, column=0, sticky="nsew")
        left.configure(width=235)
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        ttk.Label(left, text="Results:").grid(row=0, column=0, sticky="w")
        self.lbl_count = ttk.Label(left, text="0")
        self.lbl_count.grid(row=0, column=0, sticky="e")

        self.result_list = tk.Listbox(
            left,
            activestyle="dotbox",
            font=("Menlo", 14),
            width=20,
        )
        self.result_list.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.result_list.bind("<<ListboxSelect>>", lambda e: self._on_select())

        sb = ttk.Scrollbar(left, orient="vertical", command=self.result_list.yview)
        sb.grid(row=2, column=1, sticky="ns", pady=(8, 0))
        self.result_list.configure(yscrollcommand=sb.set)

        # center explorer pane
        center = ttk.Frame(self, padding=(8, 8, 8, 8))
        center.grid(row=0, column=1, sticky="nsew")
        center.columnconfigure(1, weight=1)
        center.rowconfigure(1, weight=1)

        top = ttk.Frame(center)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(0, weight=1)

        ttk.Label(
            top,
            text="Explorer View",
            font=("Menlo", 14, "bold"),
            foreground="#3A7BD5",
        ).grid(row=0, column=0, sticky="w")

        btns = ttk.Frame(top)
        btns.grid(row=0, column=1, sticky="e")
        ttk.Button(btns, text="Run Query", command=self.run_query, style="Explorer.TButton").grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Clear", command=self.clear_controls, style="Explorer.TButton").grid(row=0, column=1, padx=(0, 6))
        ttk.Button(btns, text="Show SQL", command=self.show_sql, style="Explorer.TButton").grid(row=0, column=2, padx=(0, 6))
        ttk.Button(btns, text="Record View", command=self._open_json, style="Explorer.TButton").grid(row=0, column=3, padx=(0, 6))
        ttk.Button(btns, text="Copy Splat", command=self._copy_splat, style="Explorer.TButton").grid(row=0, column=4)
        form = ttk.Frame(center)
        form.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        r = 0
        self._add_entry(form, r, "Episode # contains", "episode_id", 0)
        self._add_entry(form, r, "Date contains", "date", 2)
        r += 1
        self._add_entry(form, r, "Title contains", "title", 0)
        self._add_entry(form, r, "Guest contains", "guest", 2)
        r += 1
        self._add_entry(form, r, "Summary contains", "summary_narrative", 0, span=3)
        r += 1
        self._add_entry(form, r, "Summary bullets contain", "summary_compact", 0, span=3)
        r += 1
        self._add_entry(form, r, "Topics contain", "topics", 0, span=3)
        r += 1
        self._add_entry(form, r, "Notable quotes contain", "notable_quotes", 0, span=3)
        r += 1
        self._add_entry(form, r, "Predictions contain", "predictions", 0, span=3)
        r += 1
        self._add_entry(form, r, "Persuasion lessons contain", "persuasion_lessons", 0, span=3)
        r += 1
        self._add_entry(form, r, "Thought experiments contain", "thought_experiments", 0, span=3)
        r += 1
        self._add_entry(form, r, "Closing observations contain", "closing_observations", 0, span=3)
        r += 1

        flags = ttk.LabelFrame(form, text="Traits / Flags", padding=8, style="Explorer.TLabelframe")
        flags.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(12, 8))
        ttk.Checkbutton(flags, text="Dale", variable=self.vars["dale"], style="Explorer.TCheckbutton").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Checkbutton(flags, text="Whiteboard", variable=self.vars["whiteboard"], style="Explorer.TCheckbutton").grid(row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Checkbutton(flags, text="Thought Experiment", variable=self.vars["thought_experiment_flag"], style="Explorer.TCheckbutton").grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Label(flags, text="Limit", style="Explorer.TLabel").grid(row=0, column=3, sticky="e", padx=(24, 6))
        ttk.Entry(flags, textvariable=self.vars["limit"], width=8, font=("Menlo", 14)).grid(row=0, column=4, sticky="w")

        grammar = (
            'Within a field: adjacency = AND, "+" or AND = AND, "|" or OR = OR.\n'
            'Use double quotes for phrases: "hello there" | "fake news".\n'
            'AND binds tighter than OR. Between fields: default AND. Blank = ANY.'
        )

        ttk.Label(
            form,
            text=grammar,
            foreground="#666666",
            justify="left",
            font=("Menlo", 14),
        ).grid(row=r + 1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # right splat
        right = ttk.Frame(self, padding=(8, 8, 8, 8))
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        rtop = ttk.Frame(right)
        rtop.grid(row=0, column=0, sticky="ew")
        rtop.columnconfigure(0, weight=1)

        self.lbl_header = ttk.Label(
            rtop,
            text="(select a result)",
            font=("Menlo", 12, "bold"),
            foreground="#3A7BD5",
        )
        self.lbl_header.grid(row=0, column=0, sticky="w")

        rbtns = ttk.Frame(rtop)
        rbtns.grid(row=0, column=1, sticky="e")
        self.splat = scrolledtext.ScrolledText(right, wrap=tk.WORD)
        self.splat.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.splat.configure(font=("Menlo", 14))
        self.splat.configure(state=tk.DISABLED)

    def _add_entry(self, parent, row: int, label: str, key: str, col: int, span: int = 1):
        ttk.Label(parent, text=label, font=("Menlo", 14)).grid(
            row=row, column=col, sticky="w", pady=(4, 4), padx=(0, 6)
        )
        entry = ttk.Entry(parent, textvariable=self.vars[key], font=("Menlo", 14))
        entry.grid(
            row=row, column=col + 1, columnspan=span, sticky="ew", pady=(4, 4), padx=(0, 16)
        )
        entry.bind("<Return>", lambda e: self.run_query())

    # ---------------- query builder ----------------

    def build_query(self) -> tuple[str, list[str]]:
        where_parts: list[str] = []
        params: list[str] = []

        text_map = {
            "episode_id": "episode_id",
            "date": "date",
            "title": "title",
            "summary_narrative": "summary_narrative",
            "summary_compact": "summary_compact_json",
            "topics": "topics_json",
            "notable_quotes": "notable_quotes_json",
            "predictions": "predictions_json",
            "persuasion_lessons": "persuasion_lessons_json",
            "thought_experiments": "thought_experiments_json",
            "closing_observations": "closing_observations",
            "guest": "traits_json",
        }

        for key, column in text_map.items():
            expr = (self.vars[key].get() or "").strip()
            if not expr:
                continue
            clause, p = compile_contains_expr(column, expr)
            if clause:
                where_parts.append(clause)
                params.extend(p)

        if self.vars["dale"].get():
            where_parts.append("traits_json LIKE ? COLLATE NOCASE")
            params.append('%"dale": true%')
        if self.vars["whiteboard"].get():
            where_parts.append("traits_json LIKE ? COLLATE NOCASE")
            params.append('%"whiteboard": true%')
        if self.vars["thought_experiment_flag"].get():
            where_parts.append("traits_json LIKE ? COLLATE NOCASE")
            params.append('%"thought_experiment": true%')

        sql = (
            "SELECT episode_id, date, title, traits_json "
            "FROM episodes"
        )
        if where_parts:
            sql += "\nWHERE " + "\n  AND ".join(where_parts)
        sql += "\nORDER BY episode_id"

        limit_raw = (self.vars["limit"].get() or "").strip()
        if limit_raw:
            try:
                limit_n = int(limit_raw)
                if limit_n > 0:
                    sql += f"\nLIMIT {limit_n}"
            except ValueError:
                pass

        return sql, params

    # ---------------- result handling ----------------

    def run_query(self, select_first: bool = False):
        sql, params = self.build_query()
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            messagebox.showerror("Query Error", f"Could not run query:\n\n{e}")
            return

        self.results = []
        for row in rows:
            traits = load_json_field(row["traits_json"], {})
            guest = traits.get("guest") if isinstance(traits, dict) else ""
            self.results.append(
                {
                    "episode_id": row["episode_id"] or "",
                    "date": row["date"] or "",
                    "title": row["title"] or "",
                    "guest": guest or "",
                }
            )
        self._refresh_results(select_first=select_first)

    def _refresh_results(self, select_first: bool = False):
        self.result_list.delete(0, tk.END)
        for e in self.results:
            line = f"{e.get('episode_id','')}  {e.get('date','')}"
            self.result_list.insert(tk.END, line)
        self.lbl_count.config(text=str(len(self.results)))

        if self.results and (select_first or self.result_list.curselection() == ()):  # default to first
            self.result_list.selection_set(0)
            self.result_list.activate(0)
            self._on_select()
        elif not self.results:
            self.current = None
            self.current_json = None
            self.lbl_header.config(text="(no results)")
            self._set_splat_text("")

    def _on_select(self):
        sel = self.result_list.curselection()
        if not sel:
            return
        e = self.results[sel[0]]
        self.current = e
        row = self._fetch_episode_row(e["episode_id"])
        self.current_json = row_to_episode_dict(row) if row is not None else None

        ep_id = e.get("episode_id", "")
        dt = e.get("date", "")
        title = e.get("title", "")
        guest = e.get("guest", "")
        parts = [ep_id, dt, compact_one_line(title, 180)]
        if guest:
            parts.append(compact_one_line(guest, 60))
        self.lbl_header.config(text="  ".join(parts))
        self._set_splat_text(self._render_splat(self.current_json, e))

    # ---------------- splat / views ----------------

    def _render_splat(self, data: dict | None, entry: dict) -> str:
        d = data or {}
        ep_id = safe_get(d, "episode_id", default=entry.get("episode_id", "")) or entry.get("episode_id", "")
        title = safe_get(d, "title", default=entry.get("title", "")) or entry.get("title", "")
        date = safe_get(d, "date", default=entry.get("date", "")) or entry.get("date", "")
        traits = safe_get(d, "traits", default={})
        guest = ""
        if isinstance(traits, dict):
            guest = traits.get("guest") or ""
        guest = guest or entry.get("guest", "")

        summary_narr = safe_get(d, "summary_narrative", default="") or ""
        summary_compact = safe_get(d, "summary_compact", default=[]) or []
        topics = safe_get(d, "topics", default=[]) or []
        quotes = safe_get(d, "notable_quotes", default=[]) or []
        preds = safe_get(d, "predictions", default=[]) or []
        persu = safe_get(d, "persuasion_lessons", default=[]) or []
        thoughts = safe_get(d, "thought_experiments", default=[]) or []
        closing = safe_get(d, "closing_observations", default="") or ""

        summary_narr = limit_lines(summary_narr.strip(), 12)
        bullets_txt = bullets(summary_compact, max_items=12, max_line_chars=180)
        topics_txt = bullets(topics, max_items=12, max_line_chars=180)
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
        parts.append("")
        parts.append("SUMMARY (NARRATIVE)")
        parts.append(summary_narr or "(none)")
        parts.append("")
        parts.append("SUMMARY (BULLETS)")
        parts.append(bullets_txt or "(none)")
        parts.append("")
        parts.append("TOPICS")
        parts.append(topics_txt or "(none)")
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
        return "\n".join(parts).rstrip() + "\n"

    def _set_splat_text(self, text: str):
        self.splat.configure(state=tk.NORMAL)
        self.splat.delete("1.0", tk.END)
        self.splat.insert("1.0", text or "")
        self._apply_splat_highlights()
        self.splat.configure(state=tk.DISABLED)

    def _copy_splat(self):
        try:
            text = self.splat.get("1.0", tk.END).rstrip("\n")
            self.clipboard_clear()
            self.clipboard_append(text)
        except Exception:
            pass

    def _ensure_detail_window(self, title: str = "Detail"):
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

    def _open_json(self):
        if not self.current_json:
            return
        ep_id = self.current.get("episode_id", "") if self.current else ""
        self._set_detail_text(pretty_json(self.current_json), title=f"{ep_id} — Record View")

    def show_sql(self):
        sql, params = self.build_query()
        text = sql + "\n\nPARAMS:\n" + pretty_json(params)
        if self.sql_win and self.sql_win.winfo_exists():
            self.sql_win.title("Generated SQL")
            self.sql_win.lift()
        else:
            self.sql_win = tk.Toplevel(self)
            self.sql_win.title("Generated SQL")
            self.sql_win.geometry("1000x700")
            self.sql_text = scrolledtext.ScrolledText(self.sql_win, wrap=tk.WORD)
            self.sql_text.pack(fill="both", expand=True)
            self.sql_text.configure(font=("Menlo", 12))
        assert self.sql_text is not None
        self.sql_text.configure(state=tk.NORMAL)
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert("1.0", text)
        self.sql_text.configure(state=tk.DISABLED)

    def clear_controls(self):
        for key, var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(False)
            else:
                var.set("")
        self.vars["limit"].set("200")
        self.run_query(select_first=True)

    def _collect_highlight_terms(self) -> list[str]:
        """
        Collect real user terms from all active Explorer controls.
        Ignore operators like AND / OR / + / |.
        Preserve quoted phrases as single terms.
        """
        field_keys = [
            "episode_id",
            "date",
            "title",
            "summary_narrative",
            "summary_compact",
            "topics",
            "notable_quotes",
            "predictions",
            "persuasion_lessons",
            "thought_experiments",
            "closing_observations",
            "guest",
        ]

        terms: list[str] = []
        seen: set[str] = set()

        for key in field_keys:
            raw = (self.vars[key].get() or "").strip()
            if not raw:
                continue

            raw = re.sub(r"\bAND\b", " + ", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\bOR\b", " | ", raw, flags=re.IGNORECASE)
            raw = raw.replace("|", " | ").replace("+", " + ")

            try:
                pieces = shlex.split(raw)
            except ValueError:
                pieces = raw.split()

            for tok in pieces:
                upper_tok = tok.upper()
                if upper_tok in {"AND", "OR"} or tok in {"+", "|"}:
                    continue

                tok = tok.strip()
                if not tok:
                    continue

                norm = tok.lower()
                if norm in seen:
                    continue
                seen.add(norm)
                terms.append(tok)

        if self.vars["dale"].get() and "dale" not in seen:
            terms.append("dale")
            seen.add("dale")

        if self.vars["whiteboard"].get() and "whiteboard" not in seen:
            terms.append("whiteboard")
            seen.add("whiteboard")

        if self.vars["thought_experiment_flag"].get():
            for tok in ("thought experiment", "thought_experiment"):
                if tok not in seen:
                    terms.append(tok)
                    seen.add(tok)

        terms.sort(key=len, reverse=True)
        return terms


    def _apply_splat_highlights(self):
        """
        Highlight all active search terms anywhere in the splat, case-insensitively.
        """
        self.splat.tag_remove("match", "1.0", tk.END)
        self.splat.tag_config("match", background="#c6f6c6")

        terms = self._collect_highlight_terms()
        if not terms:
            return

        content = self.splat.get("1.0", tk.END)
        if not content.strip():
            return

        for term in terms:
            start = "1.0"
            while True:
                pos = self.splat.search(term, start, stopindex=tk.END, nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(term)}c"
                self.splat.tag_add("match", pos, end)
                start = end    


# ------------------------------ CLI ------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/sawl.sqlite", help="SQLite database path")
    args = ap.parse_args()

    app = SAWLExplorer(Path(args.db))
    app.mainloop()


if __name__ == "__main__":
    main()
