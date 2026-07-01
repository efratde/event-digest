#!/bin/bash
# Prints the complete Claude Routine prompt with credentials baked in.
# Usage: bash scripts/print_routine_prompt.sh
# Output: a single complete prompt to paste into claude.ai/code/routines.
#
# Fill in two placeholders: <<CF_TOKEN>> (Cloudflare API token) and your Cloudflare account ID.
set -euo pipefail

ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-<YOUR_CLOUDFLARE_ACCOUNT_ID>}"

PROMPT=$(cat <<EOF
You are operating the daily ticket-digest pipeline for a family member.

Run these steps in order. Stop and report if any step fails.

### 1. Working environment
cd /tmp
rm -rf dad-tickets
git clone https://github.com/efratde/dad-tickets.git
cd dad-tickets
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="\$HOME/.local/bin:\$PATH"
uv sync --frozen

### 2. Run the pipeline
uv run python -m src.main 2>&1 | tail -25
ls -la output/index.html output/images/ | head -3

Expect ~1-2 minutes. Output should include:
  "Total shows after upsert: 5XX"
  "Wrote digest → .../output/index.html"

### 3. Deploy to Cloudflare Pages
npm install -g wrangler
export CLOUDFLARE_API_TOKEN="<<CF_TOKEN>>"
export CLOUDFLARE_ACCOUNT_ID="${ACCOUNT_ID}"
wrangler pages deploy output/ \\
  --project-name=dad-tickets \\
  --branch=production \\
  --commit-dirty=true

### 4. Send Gmail to dad-tickets@example.com
Use the Gmail send_message tool (NOT a draft):
  Subject: 🎭 Shows for Dad — <today's date, e.g. "Monday, May 4, 2026">
  Body (HTML):
    <div dir="ltr" style="font-family: Heebo, Arial, sans-serif; font-size: 15px; line-height: 1.5">
      <p>The daily digest is ready 🎭</p>
      <p><a href="https://dad-tickets.pages.dev/" style="color:#c4392f;font-weight:600">Click here to open the page</a></p>
      <p style="color:#888;font-size:13px;margin-top:24px">
        Runs automatically twice a day (07:00 and 16:00 Israel time).<br>
        Preferences (pins, hidden items, favorites) are saved in the browser.
      </p>
    </div>

If anything fails, report the failure cause and the last 30 lines of output. Do not retry.
EOF
)

# Copy to clipboard so the token doesn't end up in your terminal scrollback
echo "$PROMPT" | pbcopy

cat <<MSG
✅ The full prompt has been copied to the clipboard (Cmd+V to paste).
   You need to fill in your Cloudflare Account ID and API token (placeholders).

Now you just need to do two things:

1. Create a Cloudflare API token (30 seconds):
   • https://dash.cloudflare.com/profile/api-tokens
   • Create Token → "Edit Cloudflare Workers" template → Use template
   • Continue to summary → Create Token → copy it

2. At https://claude.ai/code/routines:
   • Open the routine → paste the content from the clipboard (Cmd+V) over the existing text
   • Replace <<CF_TOKEN>> with the token you created
   • Save → Run now

That's it. Everything else is automatic.
MSG
