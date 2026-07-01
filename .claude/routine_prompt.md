# Claude Routine — Daily ticket digest

## What this routine does

Twice a day (07:00 and 16:00 Israel time), runs the scraping pipeline,
deploys the freshly-built HTML to Cloudflare Pages, and emails the user a
notification with the link.

---

## One-time setup before creating the routine

### 1. Create a Cloudflare API token

1. Go to https://dash.cloudflare.com/profile/api-tokens
2. Click **Create Token**
3. Pick the template **"Edit Cloudflare Workers"** (the simplest match —
   includes Pages permission). Or use the template **"Custom token"** with:
   - Account permissions: `Cloudflare Pages: Edit`
   - Zone resources: All zones
4. Click **Continue to summary** → **Create Token**
5. **Copy the token immediately** — Cloudflare won't show it again.

### 2. Find your Cloudflare account ID

Visible at https://dash.cloudflare.com/ (right sidebar of any page in your
account, labeled "Account ID"). Copy it.

---

## Create the routine

1. Go to https://claude.ai/code/routines
2. Click **New Routine**
3. **Schedule:**
   - Cron: `0 4,13 * * *` (07:00 and 16:00 Israel — UTC offset +3)
   - Timezone: select **Asia/Jerusalem** if available
4. **Connectors / integrations:** enable
   - **GitHub** — pick the `efratde/dad-tickets` repo
   - **Gmail** — your Google account
5. **Environment variables / secrets:**
   - `CLOUDFLARE_API_TOKEN` — paste the token from step 1
   - `CLOUDFLARE_ACCOUNT_ID` — paste your account ID from step 2
6. **Prompt:** paste exactly the block below.

---

## Routine prompt (copy-paste this into the routine)

```
You are operating the daily ticket-digest pipeline for a family member.

Repo: efratde/dad-tickets (on GitHub, available via the GitHub connector).
Required env vars: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.

Run these steps in order. Stop and report if any step fails.

### 1. Set up the working environment
  cd /tmp
  rm -rf dad-tickets
  git clone https://github.com/efratde/dad-tickets.git
  cd dad-tickets
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv sync --frozen

### 2. Run the pipeline
  uv run python -m src.main 2>&1 | tail -25
  ls -la output/index.html output/images/ | head -3

  Expect ~1-2 minutes. Output should include:
    "Total shows after upsert: 5XX"
    "Wrote digest → .../output/index.html"

### 3. Deploy to Cloudflare Pages
  npm install -g wrangler
  wrangler pages deploy output/ \
    --project-name=dad-tickets \
    --branch=production \
    --commit-dirty=true
  
  Capture the deployment URL from the output (looks like
  https://XXXXXXXX.dad-tickets.pages.dev). The stable URL for the latest
  is always https://dad-tickets.pages.dev/.

### 4. Send a Gmail notification
  Use the Gmail send_message tool (NOT a draft):
    To: dad-tickets@example.com
    Subject: 🎭 Shows for Dad — <today's date, e.g. "Monday, 4 May 2026">
    Body (HTML, RTL):
      <div dir="rtl" style="font-family: Heebo, Arial, sans-serif; font-size: 15px; line-height: 1.5">
        <p>Your daily digest is ready 🎭</p>
        <p><a href="https://dad-tickets.pages.dev/" style="color:#c4392f;font-weight:600">Click here to open the page</a></p>
        <p style="color:#888;font-size:13px;margin-top:24px">
          Runs automatically twice a day (07:00 and 16:00 Israel time).<br>
          Your preferences (pins, hides, favorites) are saved in the browser only —
          you can edit them via the ⚙️ icon at the top of the page.
        </p>
      </div>

If anything fails, report the failure cause and the last 30 lines of
output. Do not retry.
```

---

## After the routine runs successfully once

- Replace the email recipient with dad's actual email
- Consider adding `--no-web-enrich` to step 2 for the morning run if you want
  it faster (web enrichment adds ~1 minute and isn't critical daily — it's
  only useful when new shows appear that aren't yet in the cache)
