#!/usr/bin/env bash
set -euo pipefail

# --- EDIT IF NEEDED ---
SAWL_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs/code/scraper/SAWL"
MODEL="base"
COMPUTE="int8"

# M4 mini does the next chunk
START="E2001"
END="E2800"

cd "$SAWL_ROOT"

caffeinate -dimsu &

echo "== SAWL M4 mini run: $START -> $END =="
python3 sawl_fw_run.py \
  --range "$START" "$END" \
  --model "$MODEL" \
  --compute_type "$COMPUTE" 
  