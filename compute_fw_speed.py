import json
from pathlib import Path

ROOT = Path("data/episodes")

total_audio = 0.0
total_elapsed = 0.0
count = 0

for episode_dir in ROOT.iterdir():
    fw_dir = episode_dir / "whisper" / "fw-base"
    manifest_path = fw_dir / "manifest.json"

    if not manifest_path.exists():
        continue

    try:
        with open(manifest_path) as f:
            m = json.load(f)

        audio_sec = m["audio"]["duration_seconds"]
        elapsed_sec = m["run"]["elapsed_seconds"]

        total_audio += audio_sec
        total_elapsed += elapsed_sec
        count += 1

    except Exception as e:
        print(f"[WARN] {episode_dir.name} skipped: {e}")

print("Episodes:", count)
print("Total audio hours:", round(total_audio / 3600, 2))
print("Total processing hours:", round(total_elapsed / 3600, 2))

if total_audio > 0:
    rtf = total_elapsed / total_audio
    speedup = 1 / rtf

    print("Real-Time Factor:", round(rtf, 4))
    print("Speed multiplier:", round(speedup, 2), "x")