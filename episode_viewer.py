#!/usr/bin/env python3
# episode_viewer.py — Episode Viewer (EV)
#
# Goal: fast navigation + “splat” view (copyable text), with deep-dive in one floating window.
# - Left: search + episode list
# - Right: one big read-only text “canvas” (ScrolledText) showing key fields at a glance
# - Buttons: Transcript | JSON | Copy Splat | Reload
# - Deep-dive: one floating window (ScrolledText) for Transcript / Pretty JSON / Full sections
#
# Design constraints:
# - Avoid scanning the whole tree on every keypress.
# - Prefer a lightweight index file if present (episode_index.tsv).
# - Only read one episode’s episode.json when selected.
# - Transcript loads on demand.

import os
import re
import json
import argparse
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path
from datetime import datetime

# ------------------------------ helpers ------------------------------

EPDIR_RE = re.compile(r"^E\d{4,5}-\d{8}(-\d+)?$")

def is_episode_dir_name(name: str) -> bool:
    return bool(EPDIR_RE.match(name))

def infer_episode_id_from_dir(epdir: str) -> str:
    m = re.match(r"^(E\d{4,5}-\d{8})(?:-\d+)?$", epdir)
    return m.group(1) if m else epdir

def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

def safe_load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

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

# ------------------------------ EV App ------------------------------

class EpisodeViewer(tk.Tk):
    def __init__(self, root_dir: Path, prefer_model: str | None = None, index_file: str = "episode_index.tsv"):
        super().__init__()
        self.title("Episode Viewer (EV)")
        self.geometry("1400x850")

        self.root_dir = root_dir
        self.prefer_model = prefer_model
        self.index_path = Path(index_file) if Path(index_file).is_absolute() else (Path.cwd() / index_file)

        # Index entries: dict with keys:
        #   dir, path, episode_id, date, title, guest, has_json, has_transcript, search_blob
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
        self.detail_mode = None  # "transcript" | "json" | "section"

        self._build_ui()
        self._load_index()
        self._apply_filter()

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)  # left nav
        self.columnconfigure(1, weight=3)  # right splat
        self.rowconfigure(0, weight=1)

        # Left nav frame
        nav = ttk.Frame(self, padding=8)
        nav.grid(row=0, column=0, sticky="nsew")
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

        self.listbox = tk.Listbox(nav, activestyle="dotbox")
        self.listbox.grid(row=2, column=0, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self.listbox.bind("<Double-Button-1>", lambda e: self._open_detail_default())

        sb = ttk.Scrollbar(nav, orient="vertical", command=self.listbox.yview)
        sb.grid(row=2, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)

        # Right content frame
        main = ttk.Frame(self, padding=(8, 8, 8, 8))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # Button strip
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        self.lbl_header = ttk.Label(top, text="(select an episode)", font=("Menlo", 12, "bold"))
        self.lbl_header.grid(row=0, column=0, sticky="w")

        btns = ttk.Frame(top)
        btns.grid(row=0, column=1, sticky="e")

        ttk.Button(btns, text="Transcript", command=self._open_transcript).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="JSON", command=self._open_json).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(btns, text="Copy Splat", command=self._copy_splat).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(btns, text="Reload", command=self._reload).grid(row=0, column=3)

        # Splat text (copyable “canvas”)
        self.splat = scrolledtext.ScrolledText(main, wrap=tk.WORD)
        self.splat.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.splat.configure(font=("Menlo", 12))
        self.splat.configure(state=tk.DISABLED)

        # Keyboard navigation
        self.bind("<Up>", self._kb_up)
        self.bind("<Down>", self._kb_down)
        self.bind("<Prior>", self._kb_page_up)   # PageUp
        self.bind("<Next>", self._kb_page_down)  # PageDown
        self.bind("<Return>", lambda e: self._open_detail_default())
        self.bind("t", lambda e: self._open_transcript())
        self.bind("j", lambda e: self._open_json())
        self.bind("c", lambda e: self._copy_splat())

    # ---------------- Indexing ----------------

    def _load_index(self):
        # Prefer: episode_index.tsv if present in repo root (or provided absolute path).
        if self.index_path.exists():
            try:
                self.episodes = self._load_index_tsv(self.index_path)
                return
            except Exception:
                # Fall back to scan
                pass

        self.episodes = self._scan_root_for_index(self.root_dir)

    def _load_index_tsv(self, path: Path) -> list[dict]:
        # Expected columns (flexible): dir, episode_id, date, title, guest, has_json, has_transcript
        # If your existing episode_index.tsv differs, adjust mapping here.
        text = safe_read_text(path)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return []

        # naive TSV parse with header detection
        header = lines[0].split("\t")
        start_idx = 1
        has_header = any(h.lower() in ("episode_id", "title", "date", "dir") for h in header)
        if not has_header:
            # no header; assume fixed order
            header = ["dir", "episode_id", "date", "title", "guest"]
            start_idx = 0

        cols = [h.strip() for h in header]
        out: list[dict] = []
        for ln in lines[start_idx:]:
            parts = ln.split("\t")
            row = {cols[i]: parts[i] if i < len(parts) else "" for i in range(len(cols))}
            epdir = row.get("dir", "").strip()
            if not epdir:
                continue
            ep_path = self.root_dir / epdir
            episode_id = row.get("episode_id", "").strip() or infer_episode_id_from_dir(epdir)
            title = row.get("title", "").strip()
            date = row.get("date", "").strip()
            guest = row.get("guest", "").strip()

            # Existence flags (cheap checks)
            ep_json_path = ep_path / "analysis" / "episode.json"
            has_json = ep_json_path.exists()
            tpath = pick_transcript_file(ep_path, prefer_model=self.prefer_model)
            has_transcript = bool(tpath and tpath.exists())

            out.append(self._make_entry(
                dir=epdir,
                path=ep_path,
                episode_id=episode_id,
                date=date,
                title=title,
                guest=guest,
                has_json=has_json,
                has_transcript=has_transcript,
            ))

        return out

    def _scan_root_for_index(self, root_dir: Path) -> list[dict]:
        # “Light” scan: list episode dirs and read ONLY analysis/episode.json if present
        # (Still can be heavy on very large trees; prefer index file in practice.)
        if not root_dir.is_dir():
            messagebox.showerror("Error", f"Root not found: {root_dir}")
            return []

        out: list[dict] = []
        for child in sorted(root_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if not is_episode_dir_name(child.name):
                continue

            epdir = child.name
            ep_json_path = child / "analysis" / "episode.json"
            episode_id = infer_episode_id_from_dir(epdir)
            title = ""
            date = ""
            guest = ""

            if ep_json_path.exists():
                data = safe_load_json(ep_json_path)
                if isinstance(data, dict):
                    episode_id = safe_get(data, "episode_id", "episode", default=episode_id) or episode_id
                    title = safe_get(data, "title", "episode_title", default="") or ""
                    date = safe_get(data, "date", "episode_date", "published", default="") or ""
                    traits = safe_get(data, "traits", default={})
                    if isinstance(traits, dict):
                        guest = traits.get("guest") or safe_get(data, "guest", default="") or ""

            tpath = pick_transcript_file(child, prefer_model=self.prefer_model)
            has_transcript = bool(tpath and tpath.exists())

            out.append(self._make_entry(
                dir=epdir,
                path=child,
                episode_id=episode_id,
                date=date,
                title=title,
                guest=guest,
                has_json=ep_json_path.exists(),
                has_transcript=has_transcript,
            ))

        return out

    def _make_entry(self, **kwargs) -> dict:
        # Build a search blob that leverages AI outputs (titles, summaries, predictions, quotes).
        # In v0 skeleton, we only include title/guest/date/id until we load JSON on demand.
        # Later: you can enrich this blob by reading additional fields into an index file.
        ep_id = (kwargs.get("episode_id") or "").strip()
        title = (kwargs.get("title") or "").strip()
        guest = (kwargs.get("guest") or "").strip()
        date = (kwargs.get("date") or "").strip()
        blob = " ".join([ep_id, title, guest, date]).lower()

        kwargs["search_blob"] = blob
        return kwargs

    # ---------------- Filtering / list ----------------

    def _clear_search(self):
        self.var_search.set("")
        self._apply_filter()

    def _apply_filter(self):
        q = (self.var_search.get() or "").strip().lower()
        if not q:
            self.filtered = list(self.episodes)
        else:
            # v0: search_blob is lightweight
            self.filtered = [e for e in self.episodes if q in (e.get("search_blob") or "")]
        self._refresh_listbox()

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for e in self.filtered:
            ep = e.get("episode_id", "")
            dt = e.get("date", "")
            title = compact_one_line(e.get("title", ""), 90)
            guest = compact_one_line(e.get("guest", ""), 40)
            tag = "json" if e.get("has_json") else "nojson"
            line = f"{ep} | {dt} | {title} | {guest} [{tag}]"
            self.listbox.insert(tk.END, line)

        self.lbl_count.config(text=f"{len(self.filtered)}/{len(self.episodes)}")

        # keep selection stable if possible
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

        ep_path: Path = e["path"]
        ep_json_path = ep_path / "analysis" / "episode.json"
        if ep_json_path.exists():
            data = safe_load_json(ep_json_path)
            if isinstance(data, dict):
                self.current_json = data

        self.current_transcript_path = pick_transcript_file(ep_path, prefer_model=self.prefer_model)

        # Header label
        ep_id = e.get("episode_id", "")
        dt = e.get("date", "")
        title = e.get("title", "")
        guest = e.get("guest", "")
        self.lbl_header.config(text=f"{ep_id}  |  {dt}  |  {compact_one_line(title, 140)}  |  {compact_one_line(guest, 60)}")

        # Render splat
        splat_text = self._render_splat(self.current_json, e)
        self._set_splat_text(splat_text)

        # If detail window is open and “following”, you can choose to update it here later.

    # ---------------- Splat rendering ----------------

    def _render_splat(self, data: dict | None, entry: dict) -> str:
        """
        v0 splat: dense but readable; prefer AI fields if present.
        Truncates to keep most episodes on one screen.
        """
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

        # Files status
        has_json = "yes" if (entry.get("has_json") or bool(data)) else "no"
        has_tx = "yes" if (self.current_transcript_path and self.current_transcript_path.exists()) else "no"

        # Truncation rules (tune by feel)
        summary_narr = limit_lines(summary_narr.strip(), 12)
        bullets_txt = bullets(summary_compact, max_items=12, max_line_chars=180)

        quotes_txt = bullets([f'"{q}"' for q in quotes], max_items=5, max_line_chars=180)
        preds_txt = bullets(preds, max_items=8, max_line_chars=160)
        persu_txt = bullets(persu, max_items=8, max_line_chars=160)
        thoughts_txt = bullets(thoughts, max_items=6, max_line_chars=160)

        # Traits compact (bottom zone)
        traits_txt = ""
        if isinstance(traits, dict) and traits:
            # keep guest first
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

        # Bottom zone (may scroll)
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
        parts.append(f"FILES: analysis_json={has_json} | transcript={has_tx}")
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
        # Default deep-dive: show full narrative + bullets + quotes (or JSON if missing)
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
        ep_path: Path = self.current["path"]
        ep_json_path = ep_path / "analysis" / "episode.json"
        if not ep_json_path.exists():
            messagebox.showwarning("JSON", "analysis/episode.json not found for this episode.")
            return

        raw = safe_read_text(ep_json_path)
        data = safe_load_json(ep_json_path)
        text = pretty_json(data) if isinstance(data, dict) else raw
        ep_id = self.current.get("episode_id", "")
        self._set_detail_text(text, title=f"{ep_id} — Structured JSON")

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
    ap.add_argument("--root", default="data/episodes", help="Episodes root directory (symlink OK)")
    ap.add_argument("--prefer_model", default=None, help="Prefer fw-<model> transcript (e.g., base)")
    ap.add_argument("--index", default="episode_index.tsv", help="Optional TSV index file (default: episode_index.tsv in CWD)")
    args = ap.parse_args()

    app = EpisodeViewer(Path(args.root), prefer_model=args.prefer_model, index_file=args.index)
    app.mainloop()

if __name__ == "__main__":
    main()
    