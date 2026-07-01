#!/bin/bash
# Daily ticket-digest pipeline + Cloudflare Pages deploy.
# Invoked by launchd on login + every 6 hours.
# Skips if a successful run happened in the last 5 hours.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/event-digest"
LAST_RUN_FILE="$LOG_DIR/last-success"
mkdir -p "$LOG_DIR"

# Load homebrew + uv + npm into PATH (launchd has a minimal PATH)
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

# --- de-dup guard: skip if we ran successfully recently ---
MIN_INTERVAL_SEC=$((5 * 3600))   # 5 hours
if [[ -f "$LAST_RUN_FILE" ]]; then
  last=$(stat -f %m "$LAST_RUN_FILE" 2>/dev/null || echo 0)
  now=$(date +%s)
  age=$((now - last))
  if (( age < MIN_INTERVAL_SEC )); then
    echo "$(date '+%Y-%m-%d %H:%M:%S') — skipping; last successful run was ${age}s ago (< ${MIN_INTERVAL_SEC}s)" >> "$LOG_DIR/run.log"
    exit 0
  fi
fi

cd "$PROJECT_DIR"

{
  echo "==================================================="
  echo "$(date '+%Y-%m-%d %H:%M:%S') — pipeline start"
  echo "==================================================="

  uv run python -m src.main 2>&1 | tail -30

  echo ""
  echo "--- deploying to Cloudflare Pages ---"
  wrangler pages deploy output/ \
    --project-name=event-digest \
    --branch=production \
    --commit-dirty=true 2>&1 | tail -10

  echo ""
  echo "$(date '+%Y-%m-%d %H:%M:%S') — done"
  echo ""
} >> "$LOG_DIR/run.log" 2>&1

# Mark this run as successful
touch "$LAST_RUN_FILE"
