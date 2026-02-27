#!/usr/bin/env bash
set -euo pipefail

# --- EDIT IF NEEDED ---
SAWL_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs/code/scraper/SAWL"
MODEL="base"
COMPUTE="int8"

# Studio does the largest chunk
START="E0501"
END="E2000"

cd "$SAWL_ROOT"

# Keep machine awake during long run
caffeinate -dimsu &

echo "== SAWL Studio run: $START -> $END =="
python3 sawl_fw_run.py \
  --range "$START" "$END" \
  --model "$MODEL" \
  --compute_type "$COMPUTE" \
  --force
  