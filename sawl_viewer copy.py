#!/usr/bin/env python3
# sawl_viewer.py — Scott Adams episode viewer (STRICT episode.json schema)
#
# STRICT RULES (per Jim):
# - NO FALLBACKS. EVER.
# - Only read analysis/episode.json for metadata/analysis fields.
# - Only use schema keys exactly as defined.
# - Transcript is separate: loaded only from whisper/fw-*/transcript.txt.
# - Viewer does not modify any files.
#
# Expected layout:
#   data/episodes/<EPDIR>/analysis/episode.json
#   data/episodes/<EPDIR>/whisper/fw-*/transcript.txt
#
# Usage:
#   python3 sawl_viewer.py --root data/episodes
#   python3 sawl_viewer.py --root data/episodes --prefer_model base

import re
import json
import argparse
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

# ------------------------------ helpers ------------------------------

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

def is_episode_dir_name(name: str) -> bool:
    # Accept: E2320-20231212 or E2320-20231212-1
    return bool(re.match(r"^E\d{4,5}-\d{8}(-\d+)?$", name))

def compact(s: str, n: int = 120) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else (s[: n - 1] + "…")

def ensure_str(x) -> str:
    return "" if x is None else str(x)

def bullets_to_text(items) -> str:
    if items is None:
        return ""
    if isinstance(items, str):
        return items.strip()
    if isinstance(items, list):
        out = []
        for x in items:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            if s.startswith(("-", "•", "*")):
                out.append(s)
            else:
                out.append(f"- {s}")
        return "\n".join(out)
    return str(items).strip()

def pick_transcript_file(ep_path: Path, prefer_model: str | None = None) -> Path | None:
    """
    STRICT transcript picker (not schema):
      1) whisper/fw-<prefer_model>/transcript.txt if exists
      2) newest whisper/fw-*/transcript.txt
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

# ------------------------------ main app ------------------------------

class SAWLViewer(tk.Tk):
    """
    STRICT: viewer renders only schema fields from analysis/episode.json

    Schema keys used:
      episode_id (str)
      date (str)
      title (str)
      analysis_version (int)
      summary_compact (list[str])
      summary_narrative (str)
      topics (list[str])
      traits (dict: dale, guest, thought_experiment, whiteboard)
      notable_quotes (list[str])
      persuasion_lessons (list[str])
      predictions (list[str])
      closing_observations (str)
      evaluation (dict: originality, impact, clarity, focus, timeliness, humor)
    """

    def __init__(self, root_dir: Path, prefer_model: str | None):
        super().__init__()
        self.title("Scott Adams — Episode Viewer (STRICT)")
        self.geometry("1200x800")

        self.root_dir = root_dir
        self.prefer_model = prefer_model

        # indexed items: dict {dir, path, has_episode_json, episode_id, date, title}
        self.episodes: list[dict] = []
        self.filtered: list[dict] = []

        self.current_ep: dict | None = None
        self.current_episode_json: dict | None = None

        self.current_transcript_path: Path | None = None
        self.current_transcript_loaded: bool = False
        self.current_transcript_text: str = ""

        # UI state
        self.var_search = tk.StringVar()
        self.var_section = tk.StringVar(value="Closing Observations")
        self.var_auto_transcript = tk.BooleanVar(value=True)

        self._build_ui()
        self._load_episode_index()
        self._apply_filter()

        self.bind("<Configure>", self._on_resize)

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=6)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(4, weight=1)

        ttk.Label(top, text="Search:").grid(row=0, column=0, sticky="w")
        ent = ttk.Entry(top, textvariable=self.var_search, width=40)
        ent.grid(row=0, column=1, sticky="w", padx=(6, 10))
        ent.bind("<KeyRelease>", lambda e: self._apply_filter())

        ttk.Label(top, text="Section:").grid(row=0, column=2, sticky="w")
        section = ttk.Combobox(
            top,
            textvariable=self.var_section,
            state="readonly",
            values=[
                "Header",
                "Traits",
                "Summary (Bullets)",
                "Summary (Narrative)",
                "Topics",
                "Notable Quotes",
                "Persuasion Lessons",
                "Predictions",
                "Closing Observations",
                "Evaluation",
                "Transcript",
                "Raw episode.json",
            ],
            width=24,
        )
        section.grid(row=0, column=3, sticky="w", padx=(6, 10))
        section.bind("<<ComboboxSelected>>", lambda e: self._render_current_section())

        chk = ttk.Checkbutton(
            top,
            text="Auto-load transcript when wide",
            variable=self.var_auto_transcript,
            command=self._render_current_section,
        )
        chk.grid(row=0, column=4, sticky="w")

        btn_reload = ttk.Button(top, text="Reload Index", command=self._reload_all)
        btn_reload.grid(row=0, column=5, sticky="e", padx=(10, 0))

        # Left: meta + reader + buttons
        left = ttk.Frame(self, padding=(6, 0, 6, 6))
        left.grid(row=1, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        self.lbl_meta = ttk.Label(left, text="(select an episode)", justify="left")
        self.lbl_meta.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.reader = scrolledtext.ScrolledText(left, wrap=tk.WORD)
        self.reader.grid(row=1, column=0, sticky="nsew")
        self.reader.configure(font=("Menlo", 12))
        self.reader.configure(state=tk.DISABLED)

        bottom = ttk.Frame(left)
        bottom.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(3, weight=1)

        self.btn_load_transcript = ttk.Button(bottom, text="Load Transcript", command=self._load_transcript_now)
        self.btn_load_transcript.grid(row=0, column=0, sticky="w")

        self.btn_copy = ttk.Button(bottom, text="Copy Text", command=self._copy_reader)
        self.btn_copy.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.btn_clear = ttk.Button(bottom, text="Clear", command=self._clear_reader)
        self.btn_clear.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.lbl_status = ttk.Label(bottom, text="")
        self.lbl_status.grid(row=0, column=3, sticky="e")

        # Right: list
        right = ttk.Frame(self, padding=(0, 0, 6, 6))
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(right)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.listbox.bind("<Double-Button-1>", lambda e: self._open_selected())

        sb = ttk.Scrollbar(right, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)

    # ---------------- indexing (STRICT: analysis/episode.json only) ----------------

    def _load_episode_index(self):
        self.episodes = []
        if not self.root_dir.is_dir():
            messagebox.showerror("Error", f"Root not found: {self.root_dir}")
            return

        for child in sorted(self.root_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if not is_episode_dir_name(child.name):
                continue

            ep_json_path = child / "analysis" / "episode.json"

            episode_id = ""
            date = ""
            title = ""

            if ep_json_path.exists():
                data = safe_load_json(ep_json_path)
                if isinstance(data, dict):
                    # STRICT schema keys ONLY
                    episode_id = ensure_str(data.get("episode_id"))
                    date = ensure_str(data.get("date"))
                    title = ensure_str(data.get("title"))

            self.episodes.append(
                {
                    "dir": child.name,
                    "path": child,
                    "has_episode_json": ep_json_path.exists(),
                    "episode_id": episode_id,
                    "date": date,
                    "title": title,
                }
            )

        self.filtered = list(self.episodes)

    def _reload_all(self):
        self._load_episode_index()
        self._apply_filter()

    def _apply_filter(self):
        q = (self.var_search.get() or "").strip().lower()
        self.filtered = []

        for e in self.episodes:
            # STRICT: search only indexed strings (episode_id/date/title/dir)
            hay = " ".join(
                [
                    ensure_str(e.get("dir")),
                    ensure_str(e.get("episode_id")),
                    ensure_str(e.get("date")),
                    ensure_str(e.get("title")),
                ]
            ).lower()
            if not q or q in hay:
                self.filtered.append(e)

        self._refresh_listbox()

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for e in self.filtered:
            ep = ensure_str(e.get("episode_id"))
            dt = ensure_str(e.get("date"))
            title = ensure_str(e.get("title"))
            tag = "json" if e.get("has_episode_json") else "nojson"
            line = f"{ep} | {dt} | {compact(title, 90)} [{tag}]"
            self.listbox.insert(tk.END, line)
        self.lbl_status.config(text=f"Episodes: {len(self.filtered)}/{len(self.episodes)}")

    # ---------------- episode load ----------------

    def _open_selected(self):
        idx = self.listbox.curselection()
        if not idx:
            return
        e = self.filtered[idx[0]]
        self._load_episode(e)

    def _load_episode(self, e: dict):
        self.current_ep = e
        self.current_episode_json = None

        # reset transcript state for each episode
        self.current_transcript_path = None
        self.current_transcript_loaded = False
        self.current_transcript_text = ""

        ep_path = e["path"]
        ep_json_path = ep_path / "analysis" / "episode.json"

        if ep_json_path.exists():
            data = safe_load_json(ep_json_path)
            if isinstance(data, dict):
                self.current_episode_json = data

        # transcript path is not schema; it’s IO convenience
        self.current_transcript_path = pick_transcript_file(ep_path, prefer_model=self.prefer_model)

        # STRICT: meta displays indexed fields only (no guessing)
        meta_lines = [
            f"Episode: {ensure_str(e.get('episode_id'))}   Dir: {ensure_str(e.get('dir'))}",
            f"Title: {ensure_str(e.get('title'))}",
            f"Date: {ensure_str(e.get('date'))}",
        ]
        if self.current_transcript_path:
            meta_lines.append(f"Transcript: {self.current_transcript_path}")
        else:
            meta_lines.append("Transcript: (not found)")

        self.lbl_meta.config(text="\n".join(meta_lines))
        self._render_current_section()

    # ---------------- rendering (STRICT schema) ----------------

    def _set_reader_text(self, text: str):
        self.reader.configure(state=tk.NORMAL)
        self.reader.delete("1.0", tk.END)
        self.reader.insert("1.0", text or "")
        self.reader.configure(state=tk.DISABLED)

    def _clear_reader(self):
        self._set_reader_text("")
        self.lbl_status.config(text="")

    def _copy_reader(self):
        try:
            text = self.reader.get("1.0", tk.END).rstrip("\n")
            self.clipboard_clear()
            self.clipboard_append(text)
            self.lbl_status.config(text="Copied.")
        except Exception:
            pass

    def _render_current_section(self):
        if not self.current_ep:
            self._set_reader_text("Select an episode from the list on the right.")
            return

        data = self.current_episode_json
        if not isinstance(data, dict):
            self._set_reader_text("(analysis/episode.json not found for this episode)")
            return

        sec = self.var_section.get()

        # STRICT single-key getters
        def S(key: str) -> str:
            return ensure_str(data.get(key))

        def L(key: str) -> list:
            v = data.get(key)
            return v if isinstance(v, list) else []

        def D(key: str) -> dict:
            v = data.get(key)
            return v if isinstance(v, dict) else {}

        wide_enough = self.winfo_width() >= 1300
        auto_ok = bool(self.var_auto_transcript.get()) and wide_enough

        if sec == "Header":
            out = []
            out.append(f"episode_id: {S('episode_id')}")
            out.append(f"date: {S('date')}")
            out.append(f"title: {S('title')}")
            out.append(f"analysis_version: {S('analysis_version')}")
            self._set_reader_text("\n".join(out).strip())
            return

        if sec == "Traits":
            t = D("traits")
            # STRICT: show only known schema keys inside traits
            lines = []
            lines.append(f"dale: {ensure_str(t.get('dale'))}")
            lines.append(f"guest: {ensure_str(t.get('guest'))}")
            lines.append(f"thought_experiment: {ensure_str(t.get('thought_experiment'))}")
            lines.append(f"whiteboard: {ensure_str(t.get('whiteboard'))}")
            self._set_reader_text("\n".join(lines).strip())
            return

        if sec == "Summary (Bullets)":
            self._set_reader_text(bullets_to_text(L("summary_compact")))
            return

        if sec == "Summary (Narrative)":
            self._set_reader_text(S("summary_narrative"))
            return

        if sec == "Topics":
            self._set_reader_text(bullets_to_text(L("topics")))
            return

        if sec == "Notable Quotes":
            self._set_reader_text(bullets_to_text(L("notable_quotes")))
            return

        if sec == "Persuasion Lessons":
            self._set_reader_text(bullets_to_text(L("persuasion_lessons")))
            return

        if sec == "Predictions":
            self._set_reader_text(bullets_to_text(L("predictions")))
            return

        if sec == "Closing Observations":
            self._set_reader_text(S("closing_observations"))
            return

        if sec == "Evaluation":
            ev = D("evaluation")
            # STRICT: show known schema keys, no extras
            lines = []
            lines.append(f"originality: {ensure_str(ev.get('originality'))}")
            lines.append(f"impact: {ensure_str(ev.get('impact'))}")
            lines.append(f"clarity: {ensure_str(ev.get('clarity'))}")
            lines.append(f"focus: {ensure_str(ev.get('focus'))}")
            lines.append(f"timeliness: {ensure_str(ev.get('timeliness'))}")
            lines.append(f"humor: {ensure_str(ev.get('humor'))}")
            self._set_reader_text("\n".join(lines).strip())
            return

        if sec == "Transcript":
            if not self.current_transcript_path or not self.current_transcript_path.exists():
                self._set_reader_text("(transcript not found)")
                return

            # If loaded, always render (no Clear required)
            if self.current_transcript_loaded:
                self._set_reader_text(self.current_transcript_text)
                self.lbl_status.config(text=f"Transcript loaded ({len(self.current_transcript_text):,} chars).")
                return

            if auto_ok:
                self._load_transcript_now()
            else:
                msg = []
                msg.append("Transcript is available, but not auto-loaded (window not wide enough).")
                msg.append("")
                msg.append("Click “Load Transcript” below if you want it rendered.")
                msg.append("")
                msg.append(f"File: {self.current_transcript_path}")
                self._set_reader_text("\n".join(msg))
            return

        if sec == "Raw episode.json":
            self._set_reader_text(json.dumps(data, ensure_ascii=False, indent=2))
            return

        self._set_reader_text("(unknown section)")

    def _load_transcript_now(self):
        if not self.current_transcript_path or not self.current_transcript_path.exists():
            messagebox.showwarning("Transcript", "Transcript file not found.")
            return
        text = safe_read_text(self.current_transcript_path)
        self.current_transcript_text = text
        self.current_transcript_loaded = True
        self._set_reader_text(text)
        self.lbl_status.config(text=f"Transcript loaded ({len(text):,} chars).")

    # ---------------- resize logic ----------------

    def _on_resize(self, event):
        # If currently in Transcript and auto-load is enabled, load when wide enough.
        if not self.current_ep:
            return
        if self.var_section.get() != "Transcript":
            return
        if not self.var_auto_transcript.get():
            return
        if self.current_transcript_loaded:
            return
        if self.winfo_width() >= 1300:
            self._load_transcript_now()

# ------------------------------ cli ------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/episodes", help="Episodes root directory")
    ap.add_argument("--prefer_model", default=None, help="Prefer fw-<model> transcript (e.g., base)")
    args = ap.parse_args()

    app = SAWLViewer(Path(args.root), prefer_model=args.prefer_model)
    app.mainloop()

if __name__ == "__main__":
    main()