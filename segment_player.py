from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template_string, request, send_file

# Minimal local app.
# Edit these two paths for your machine.
DB_PATH = Path("data/sawl.sqlite")
AUDIO_ROOT = Path("/Volumes/NVME4TB/episodes_scottadams")

app = Flask(__name__)

HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Scott Adams Segment Player</title>
  <style>
    body { font-family: sans-serif; max-width: 900px; margin: 24px auto; padding: 0 16px; }
    input { width: 100%; padding: 8px; margin: 6px 0 12px; }
    button { padding: 10px 14px; margin-right: 8px; }
    .meta { margin: 12px 0; padding: 12px; background: #f5f5f5; border-radius: 8px; }
    .text { white-space: pre-wrap; line-height: 1.5; padding: 12px; background: #fafafa; border: 1px solid #ddd; border-radius: 8px; }
    audio { width: 100%; margin-top: 16px; }
    .row { margin-bottom: 10px; }
  </style>
</head>
<body>
  <h1>Scott Adams Segment Player</h1>

  <div class="row">
    <label>Episode audio full path</label>
    <input id="audioPath" value="/Volumes/NVME4TB/episodes_scottadams/E1539-20211023/audio/E1539-20211023.mp3">
  </div>

  <div class="row">
    <label>Segment index</label>
    <input id="segIndex" type="number" value="16">
  </div>

  <button onclick="loadSegment()">Load segment</button>
  <button onclick="playSegment()">Play segment</button>
  <button onclick="pauseAudio()">Pause</button>

  <div class="meta" id="meta">No segment loaded yet.</div>
  <div class="text" id="text"></div>

  <audio id="player" controls preload="metadata"></audio>

  <script>
    let segment = null;
    const audio = document.getElementById('player');

    async function loadSegment() {
      const audioPath = document.getElementById('audioPath').value.trim();
      const segIndex = document.getElementById('segIndex').value.trim();
      const url = `/segment?audio_path=${encodeURIComponent(audioPath)}&seg_index=${encodeURIComponent(segIndex)}`;
      const res = await fetch(url);
      if (!res.ok) {
        const txt = await res.text();
        document.getElementById('meta').textContent = txt;
        document.getElementById('text').textContent = '';
        return;
      }
      segment = await res.json();
      audio.src = `/audio?audio_path=${encodeURIComponent(segment.audio_path)}`;
      document.getElementById('meta').textContent =
        `episode=${segment.episode_id} seg=${segment.seg_index} start=${segment.start_s}s end=${segment.end_s}s`;
      document.getElementById('text').textContent = segment.text || '';
    }

    async function playSegment() {
      if (!segment) {
        await loadSegment();
        if (!segment) return;
      }
      audio.currentTime = Number(segment.start_s);
      await audio.play();
    }

    function pauseAudio() {
      audio.pause();
    }

    audio.addEventListener('timeupdate', () => {
      if (segment && audio.currentTime >= Number(segment.end_s)) {
        audio.pause();
      }
    });
  </script>
</body>
</html>
"""


def derive_episode_id(audio_path: str) -> str:
    name = Path(audio_path).name
    m = re.match(r"^(E\d{4,5})-\d{8}.*\.mp3$", name, re.IGNORECASE)
    if not m:
        raise ValueError(f"Could not derive episode id from filename: {name}")
    return m.group(1)


def get_segment_row(db_path: Path, episode_id: str, seg_index: int) -> Optional[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT episode_id, seg_index, start_s, end_s, text
            FROM segments
            WHERE episode_id = ? AND seg_index = ?
            """,
            (episode_id, seg_index),
        ).fetchone()
        return row
    finally:
        conn.close()


@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/segment")
def segment():
    audio_path = request.args.get("audio_path", "").strip()
    seg_index_raw = request.args.get("seg_index", "").strip()
    if not audio_path:
        abort(400, "Missing audio_path")
    if not seg_index_raw:
        abort(400, "Missing seg_index")

    try:
        seg_index = int(seg_index_raw)
    except ValueError:
        abort(400, "seg_index must be an integer")

    try:
        episode_id = derive_episode_id(audio_path)
    except ValueError as e:
        abort(400, str(e))

    row = get_segment_row(DB_PATH, episode_id, seg_index)
    if row is None:
        abort(404, f"No segment found for {episode_id} seg_index={seg_index}")

    return jsonify(
        {
            "audio_path": audio_path,
            "episode_id": row["episode_id"],
            "seg_index": row["seg_index"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "text": row["text"],
        }
    )


@app.get("/audio")
def audio():
    audio_path = request.args.get("audio_path", "").strip()
    if not audio_path:
        abort(400, "Missing audio_path")

    p = Path(audio_path)
    if not p.exists() or not p.is_file():
        abort(404, f"Audio file not found: {audio_path}")

    return send_file(p, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=True)


