#!/usr/bin/env python3

import os
import shutil

SRC_AUDIO = "../adams/audio"
DST_ROOT = "SAWL/episodes"

os.makedirs(DST_ROOT, exist_ok=True)

for fname in sorted(os.listdir(SRC_AUDIO)):
    if not fname.lower().endswith(".mp3"):
        continue

    episode = os.path.splitext(fname)[0]
    src = os.path.join(SRC_AUDIO, fname)

    ep_dir = os.path.join(DST_ROOT, episode)
    audio_dir = os.path.join(ep_dir, "audio")
    dst = os.path.join(audio_dir, fname)

    if os.path.exists(dst):
        print(f"[skip] {episode}")
        continue

    os.makedirs(audio_dir, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[copy] {episode}")