#!/usr/bin/env python3
# sawl_viewer.py — Scott Adams episode viewer (episode-mode)
#
# Design goals (borrowed from vaw_reader.py lessons):
# - One reader ScrolledText (avoid many widgets; macOS Tk painting issues)
# - Right pane list with search; double-click loads episode
# - Transcript is optional: auto-show only if window is wide enough,
#   otherwise require explicit "Load Transcript"
#
# Expected folder layout:
#   data/episodes/<EPDIR>/episode.json            (preferred)
#   data/episodes/<EPDIR>/whisper/fw-*/transcript.txt  (fallback transcript source)
#
# Usage:
#   python3 sawl_viewer.py --root data/episodes
#   python3 sawl_viewer.py --root data/episodes --prefer_model base
#
# Notes:
# - This viewer does NOT modify any files.
# - It’s for spot-checking analysis + transcript quality quickly.

import os
import re
import json
import argparse
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path
from datetime import datetime

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

def infer_episode_id_from_dir(epdir: str) -> str:
    # E2320-20231212-1 -> E2320-20231212
    m = re.match(r"^(E\d{4,5}-\d{8})(?:-\d+)?$", epdir)
    return m.group(1) if m else epdir

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

    # otherwise choose newest transcript.txt under whisper/fw-*
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

def compact(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else (s[:n-1] + "…")

def pretty_dt(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts

def bullets_to_text(items) -> str:
    if not items:
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
            # keep already-bulleted lines as-is
            if s.startswith(("-", "•", "*")):
                out.append(s)
            else:
                out.append(f"- {s}")
        return "\n".join(out)
    return str(items).strip()

def ensure_str(x) -> str:
    return "" if x is None else str(x)

# ------------------------------ main app ------------------------------

class SAWLViewer(tk.Tk):
    def __init__(self, root_dir: Path, prefer_model: str | None):
        super().__init__()
        self.title("Scott Adams — Episode Viewer (SAWL)")
        self.geometry("1200x800")

        self.root_dir = root_dir
        self.prefer_model = prefer_model

        # loaded index: list of dict {dir, episode_id, title, date, has_episode_json}
        self.episodes = []
        self.filtered = []

        self.current_ep = None  # dict entry
        self.current_episode_json = None
        self.current_transcript_path = None
        self.current_transcript_loaded = False

        # UI vars
        self.var_search = tk.StringVar()
        self.var_section = tk.StringVar(value="Closing Observations")
        self.var_auto_transcript = tk.BooleanVar(value=True)

        self._build_ui()
        self._load_episode_index()
        self._apply_filter()

        self.after(150, self._bring_to_front)

        # react to resize (to decide whether to auto-load transcript)
        self.bind("<Configure>", self._on_resize)

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(1, weight=1)

        # Top controls
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
                "Notable Quotes",
                "Persuasion Lessons",
                "Predictions",
                "Thought Experiments",
                "Dale",
                "Closing Observations",
                "Transcript",
                "Raw episode.json",
                "Evaluation",
            ],
            width=22,
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

        # Left: detail header + reader
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

        # Transcript control buttons under reader
        bottom = ttk.Frame(left)
        bottom.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(3, weight=1)

        self.btn_load_transcript = ttk.Button(
            bottom, text="Load Transcript", command=self._load_transcript_now
        )
        self.btn_load_transcript.grid(row=0, column=0, sticky="w")

        self.btn_copy = ttk.Button(bottom, text="Copy Text", command=self._copy_reader)
        self.btn_copy.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.btn_clear = ttk.Button(bottom, text="Clear", command=self._clear_reader)
        self.btn_clear.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.lbl_status = ttk.Label(bottom, text="",font=("Menlo", 14, "bold"))
        self.lbl_status.grid(row=0, column=3, sticky="e")

        # Right: list
        right = ttk.Frame(self, padding=(0, 0, 6, 6))
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(right)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.listbox.bind("<Double-Button-1>", lambda e: self._open_selected())

        # Scrollbar for listbox
        sb = ttk.Scrollbar(right, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)


    def _bring_to_front(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(250, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    # ---------------- indexing ----------------
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

            # NEW: analysis/episode.json is the primary analysis container
            ep_json = child / "analysis" / "episode.json"

            episode_id = infer_episode_id_from_dir(child.name)
            title = ""
            date = ""

            if ep_json.exists():
                data = safe_load_json(ep_json)
                if isinstance(data, dict):
                    episode_id = data.get("episode_id") or data.get("episode") or episode_id
                    title = data.get("title") or data.get("episode_title") or ""
                    date = data.get("date") or data.get("episode_date") or data.get("published") or ""
            else:
                # fallback: whisper manifest if present
                whisper = child / "whisper"
                if whisper.is_dir():
                    for d in whisper.glob("fw-*"):
                        m = d / "manifest.json"
                        if m.exists():
                            manifest = safe_load_json(m)
                            if isinstance(manifest, dict):
                                title = manifest.get("title") or manifest.get("episode_title") or title
                                date = manifest.get("date") or manifest.get("episode_date") or date
                                episode_id = manifest.get("episode_id") or episode_id
                            break

            self.episodes.append(
                {
                    "dir": child.name,
                    "path": child,
                    "episode_id": episode_id,
                    "title": title,
                    "date": date,
                    "has_episode_json": ep_json.exists(),
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

    # ---------------- episode loading ----------------

    def _open_selected(self):
        idx = self.listbox.curselection()
        if not idx:
            return
        e = self.filtered[idx[0]]
        self._load_episode(e)

    def _load_episode(self, e: dict):
        self.current_ep = e
        self.current_episode_json = None
        self.current_transcript_path = None
        self.current_transcript_loaded = False

        ep_path = e["path"]
        ep_json_path = ep_path / "analysis" /"episode.json"
        if ep_json_path.exists():
            data = safe_load_json(ep_json_path)
            if isinstance(data, dict):
                self.current_episode_json = data

        self.current_transcript_path = pick_transcript_file(ep_path, prefer_model=self.prefer_model)

        meta_lines = [
            f"Episode: {e.get('episode_id','')}   Dir: {e.get('dir','')}",
        ]
        if e.get("title"):
            meta_lines.append(f"Title: {e.get('title')}")
        if e.get("date"):
            meta_lines.append(f"Date: {e.get('date')}")
        if self.current_transcript_path:
            meta_lines.append(f"Transcript: {self.current_transcript_path}")
        else:
            meta_lines.append("Transcript: (not found)")

        self.lbl_meta.config(text="\n".join(meta_lines))

        # section render
        self._render_current_section()

    # ---------------- rendering ----------------

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

        sec = self.var_section.get()
        data = self.current_episode_json or {}

        # helper to safely fetch structured fields
        def get(*keys, default=None):
            for k in keys:
                if k in data:
                    return data.get(k)
            return default

        # transcript behavior controls
        wide_enough = self.winfo_width() >= 1300  # tweak threshold as you like
        auto_ok = bool(self.var_auto_transcript.get()) and wide_enough

        if sec == "Header":
            out = []
            out.append(f"episode_id: {get('episode_id','episode', default=self.current_ep.get('episode_id'))}")
            out.append(f"title: {get('title','episode_title', default=self.current_ep.get('title'))}")
            out.append(f"date: {get('date','episode_date', default=self.current_ep.get('date'))}")
            out.append(f"duration_seconds: {get('duration_seconds','audio_seconds', default='')}")
            out.append(f"source: {get('source', default='')}")
            out.append(f"model: {get('model', default='')}")
            out.append(f"compute_type: {get('compute_type', default='')}")
            self._set_reader_text("\n".join(out).strip())
            return

        if sec == "Traits":
            traits = get("traits", default={})
            if not isinstance(traits, dict):
                self._set_reader_text(ensure_str(traits))
                return
            # include guest explicitly (your requirement)
            guest = traits.get("guest") or get("guest", default="")
            lines = []
            lines.append(f"guest: {ensure_str(guest)}")
            for k in sorted(traits.keys()):
                if k == "guest":
                    continue
                v = traits.get(k)
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v if x)
                lines.append(f"{k}: {ensure_str(v)}")
            self._set_reader_text("\n".join(lines).strip())
            return

        if sec == "Summary (Bullets)":
            # user wants summary_compact as bullets
            bullets = get("summary_compact", "summary_bullets", default=[])
            self._set_reader_text(bullets_to_text(bullets))
            return

        if sec == "Summary (Narrative)":
            self._set_reader_text(ensure_str(get("summary_narrative", default="")))
            return

        if sec == "Notable Quotes":
            q = data.get("notable_quotes")
            if not isinstance(q, list) or not q:
                self._set_reader_text("(none)")
                return

            lines = []
            for item in q:
                s = str(item).strip()
                if not s:
                    continue
                # ensure wrapped in double quotes
                if not (s.startswith('"') and s.endswith('"')):
                    s = f'"{s}"'
                lines.append(s)

            # blank line between each quote
            self._set_reader_text("\n\n".join(lines))
            return
        
        if sec == "Persuasion Lessons":
            tips = get("persuasion_lessons", "persuasion", default=[])
            self._set_reader_text(bullets_to_text(tips))
            return

        if sec == "Predictions":
            preds = get("predictions", default=[])
            self._set_reader_text(bullets_to_text(preds))
            return

        if sec == "Thought Experiments":
            te = get("thought_experiments", "thought_experiment", default=[])
            self._set_reader_text(bullets_to_text(te))
            return

        if sec == "Dale":
            # allow either a string (bit) or list (moments)
            dale = get("dale", "dale_bit", default=None)
            if dale in (None, "", [], {}):
                self._set_reader_text("(none)")
            else:
                self._set_reader_text(bullets_to_text(dale))
            return

        if sec == "Closing Observations":
            self._set_reader_text(ensure_str(get("closing_observations", "closing", default="")))
            return

        if sec == "Transcript":
            # transcript is big; only auto-load when wide, else require button
            if not self.current_transcript_path or not self.current_transcript_path.exists():
                self._set_reader_text("(transcript not found)")
                return

            if self.current_transcript_loaded:
                # already loaded, just keep it
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
            if not self.current_episode_json:
                self._set_reader_text("(episode.json not found in this folder)")
                return
            self._set_reader_text(json.dumps(self.current_episode_json, ensure_ascii=False, indent=2))
            return
        
        if sec == "Topics":
            t = L("topics")
            self._set_reader_text("(none)" if not t else bullets_to_text(t))
            return

        if sec == "Evaluation":
            ev = data.get("evaluation")
            if not isinstance(ev, dict) or not ev:
                self._set_reader_text("(none)")
                return

            order = ["originality", "impact", "clarity", "focus", "timeliness", "humor"]
            lines = []
            for k in order:
                v = ev.get(k, None)
                lines.append(f"{k}: {'' if v is None else v}")
            self._set_reader_text("\n".join(lines).strip())
            return   

        self._set_reader_text("(unknown section)")

    def _load_transcript_now(self):
        if not self.current_transcript_path or not self.current_transcript_path.exists():
            messagebox.showwarning("Transcript", "Transcript file not found.")
            return
        text = safe_read_text(self.current_transcript_path)
        self.current_transcript_loaded = True
        self._set_reader_text(text)
        self.lbl_status.config(text=f"Transcript loaded ({len(text):,} chars).")

    # ---------------- resize logic ----------------

    def _on_resize(self, event):
        # If user is currently on Transcript section and auto-load is enabled,
        # and we become wide enough, then load.
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
