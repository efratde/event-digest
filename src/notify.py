"""
Daily digest emailer with a rich Jinja2 template.

Composes a personalized email with:
  - Time-of-day greeting
  - Tel Aviv weather
  - A "feature box" picked by priority:
      1. Fresh TV/streaming recommendation (via local `claude` CLI)
      2. "On this day" fact, if today's date matches data/this_day.yaml
      3. Random fact from data/fun_facts.yaml
  - List of new shows (with poster thumbnails) — first_seen == today
  - CTA to the live URL

Skips silently if there are no new shows.

Configuration in `secrets.env`:
  SMTP_USER, SMTP_PASSWORD, DIGEST_RECIPIENTS (comma-separated)
"""

from __future__ import annotations

import json
import logging
import os
import random
import smtplib
import sqlite3
import subprocess
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .weather import fetch_tlv_today

log = logging.getLogger("notify")

DB_PATH = "data/shows.db"
PUBLIC_URL = "https://dad-tickets.pages.dev/"
PUBLIC_IMAGE_BASE = "https://dad-tickets.pages.dev/images/"

HEBREW_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
HEBREW_MONTHS = [
    "ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
    "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר",
]


def he_date(d: date) -> str:
    return f"יום {HEBREW_DAYS[d.weekday()]}, {d.day} ב{HEBREW_MONTHS[d.month - 1]} {d.year}"


def he_short_date(d: date) -> str:
    return f"{d.day} ב{HEBREW_MONTHS[d.month - 1]}"


def time_of_day_greeting(now: datetime | None = None) -> str:
    now = now or datetime.now()
    h = now.hour
    weekday = HEBREW_DAYS[now.weekday()]
    if 5 <= h < 11:
        return f"בוקר טוב, יום {weekday}"
    if 11 <= h < 16:
        return f"צהריים טובים, יום {weekday}"
    if 16 <= h < 20:
        return f"אחר צהריים נעים, יום {weekday}"
    return f"ערב טוב, יום {weekday}"


def load_secrets(path: str = "secrets.env") -> dict[str, str]:
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# -- Data fetchers ---------------------------------------------------------

def fetch_total_active_count(today: date) -> int:
    """Total shows currently in the digest — same definition as render.py uses."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT performances_json FROM shows WHERE last_seen >= date(?, '-2 days')",
        (today.isoformat(),),
    ).fetchall()
    conn.close()
    n = 0
    for r in rows:
        perfs = json.loads(r["performances_json"] or "[]")
        if any(datetime.fromisoformat(p).date() >= today for p in perfs):
            n += 1
    return n


def fetch_new_shows(today: date) -> list[dict]:
    """Shows where first_seen == today AND have at least one upcoming performance."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT title, venue, source, performances_json, description, poster_url, genre, url
        FROM shows
        WHERE first_seen = ?
        ORDER BY title
        """,
        (today.isoformat(),),
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        perfs = json.loads(r["performances_json"] or "[]")
        upcoming = sorted(
            datetime.fromisoformat(p) for p in perfs
            if datetime.fromisoformat(p).date() >= today
        )
        if not upcoming:
            continue
        next_p = upcoming[0]
        when_he = f"{he_short_date(next_p.date())} בשעה {next_p.strftime('%H:%M')}"
        # Convert local image path to public URL
        poster = r["poster_url"] or ""
        if poster.startswith("images/"):
            poster = PUBLIC_IMAGE_BASE + poster[len("images/"):]
        out.append({
            "title": r["title"],
            "venue": r["venue"],
            "source": r["source"],
            "when_he": when_he,
            "perf_count": len(upcoming),
            "description": (r["description"] or "")[:160],
            "poster_url": poster,
            "genre": r["genre"] or "",
            "url": r["url"],
        })
    return out


def fetch_tv_recommendation() -> dict | None:
    """Ask the local `claude` CLI for a fresh TV/streaming recommendation."""
    prompt = (
        "ענה רק עם JSON אחד נקי, ללא טקסט נוסף לפני או אחרי. "
        "המלץ על סדרת טלוויזיה אחת שיצאה ב-2024 או 2025 ומתאימה לקהל בוגר אינטליגנטי "
        "ישראלי בן 70 שאוהב דרמה איכותית, מתח, ביוגרפיות, או היסטוריה. "
        "סדרה צריכה להיות זמינה בישראל (Netflix, Apple TV+, Disney+, Max, yes, HOT). "
        "פורמט: {\"title\":\"...\",\"platform\":\"...\",\"year\":\"...\",\"genre\":\"...\","
        "\"blurb\":\"שני משפטים בעברית למה זה מומלץ\"}"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.info("claude CLI returned %d", result.returncode)
            return None
        # Strip code-fence wrapping if present
        text = result.stdout.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        data = json.loads(text)
        if not data.get("title"):
            return None
        return data
    except Exception as e:
        log.info("claude CLI / parse failed: %s", e)
        return None


def pick_today_in_history(today: date) -> dict | None:
    p = Path("data/this_day.yaml")
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    today_key = f"{today.month:02d}-{today.day:02d}"
    matches = [e for e in data.get("events", []) if e.get("date") == today_key]
    if not matches:
        return None
    return random.choice(matches)


def pick_random_fact() -> dict | None:
    p = Path("data/fun_facts.yaml")
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    facts = data.get("facts") or []
    if not facts:
        return None
    return random.choice(facts)


def build_feature(today: date) -> dict | None:
    """Pick the best feature for today. Priority: TV rec → today-in-history → random fact."""
    tv = fetch_tv_recommendation()
    if tv:
        return {
            "icon": "📺",
            "kind_label": "המלצה לסדרה",
            "title": tv["title"],
            "subtitle": f"{tv.get('platform', '')} · {tv.get('year', '')} · {tv.get('genre', '')}".strip(" ·"),
            "body": tv.get("blurb", ""),
            "link": None,
            "link_label": None,
        }
    today_event = pick_today_in_history(today)
    if today_event:
        years_ago = today.year - today_event.get("year", today.year)
        return {
            "icon": "📅",
            "kind_label": f"היום לפני {years_ago} שנים",
            "title": None,
            "subtitle": None,
            "body": today_event["text"],
            "link": today_event.get("link"),
            "link_label": today_event.get("link_label"),
        }
    fact = pick_random_fact()
    if fact:
        # Support both old (string) and new (dict) format
        if isinstance(fact, str):
            return {"icon": "💡", "kind_label": "הידעת?", "title": None, "subtitle": None,
                    "body": fact, "link": None, "link_label": None}
        return {
            "icon": "💡",
            "kind_label": "הידעת?",
            "title": None,
            "subtitle": None,
            "body": fact.get("text", ""),
            "link": fact.get("link"),
            "link_label": fact.get("link_label"),
        }
    return None


# -- Email send ------------------------------------------------------------

# Categories rotated daily for the "fun item" — each yields a different vibe.
# Claude generates a specific item in the chosen category. Better than a
# generic "say something fun" prompt that produces bland quips.
FUN_CATEGORIES = [
    ("🌍", "סיפור חדשותי מוזר אבל אמיתי",
     "סיפר חדשותי אמיתי שקרה לאחרונה (או בכל זמן) — מוזר, אבסורדי, או מצחיק. למשל: 'שגריר נורבגי בעלמא נתפס מנסה להחזיר לדואר חבילה של 17 קילו של פירות מיובשים שאף אחד לא הזמין'. תן סיפור אחד בלבד, 2-3 משפטים."),
    ("🦒", "עובדה מוזרה אבל אמיתית על חיה",
     "עובדה מוזרה ולא ידועה על חיה כלשהי. למשל: 'הצב הענק של גלפגוס יכול לחיות יותר מ-150 שנה — הוא לא מזדקן בקצב נורמלי, ובאמצעות מחקר מצאו שהדרך שלו לחלוקת תאים שונה מבני אדם'. תן עובדה אחת, 2-3 משפטים, מפתיעה."),
    ("📚", "אטימולוגיה מפתיעה של מילה בעברית",
     "סיפור על מקור מפתיע של מילה בעברית. למשל: 'המילה ׳אבן׳ נשארה כמעט ללא שינוי מאז שפת העברית של ימי הבית הראשון לפני 3000 שנה — אחת מהמילים העתיקות ביותר ששרדו בשפה חיה'. תן עובדה אחת, מפתיעה, 2-3 משפטים."),
    ("🧠", "עובדה מצחיקה על המוח האנושי",
     "עובדה מוזרה על איך המוח שלנו עובד. למשל: 'המוח שלך לא יכול להבדיל בין כאב פיזי לכאב חברתי — אותם אזורים בדיוק נדלקים. אז כשמישהו שובר לך את הלב, זה באמת כואב לך באותה דרך כמו שכואב לישבן'. תן עובדה אחת, 2-3 משפטים."),
    ("🌐", "עובדה מוזרה על גיאוגרפיה",
     "עובדה גיאוגרפית מוזרה ומפתיעה. למשל: 'יש כפר בנורבגיה בו השמש לא זורחת כלל ב-5 חודשים בשנה — אז בנו תחנה רובוטית של ראיות שמשליכה אור קרני שמש מלאכותיות לשטח הכפר'. עובדה אחת, מפתיעה, 2-3 משפטים."),
    ("🔬", "המצאה מוזרה או היסטוריה של המצאה",
     "סיפור על המצאה מוזרה או היסטוריה לא ידועה של המצאה רגילה. למשל: 'התליפון נמצא במקור על ידי המוצא שלא בטוח. אנטוניו מוצ'י המציא טלפון 16 שנה לפני בל, אבל לא היה לו כסף לחדש את הפטנט. בל קיבל את הקרדיט'. עובדה אחת, מפתיעה, 2-3 משפטים."),
    ("🇮🇱", "סיפור מוזר מההיסטוריה הישראלית",
     "אנקדוטה מוזרה אבל אמיתית מההיסטוריה של ישראל. למשל: 'בכפר ירוק (1948) צה\"ל גילה שבמאפייה גרמנית באזור היה תנור גדול מספיק לאפיית 800 כיכרות לחם. במקום להרוס אותו — הם השאירו אותו ועד היום הוא מאפה ללחם בית ספר'. סיפור אחד, מוזר, 2-3 משפטים."),
    ("🎨", "סיפור מצחיק על אמן/יצירה מפורסמת",
     "אנקדוטה מצחיקה או מוזרה על יצירת אמנות מפורסמת או על אמן. למשל: 'ואן גוך מכר רק תמונה אחת בחייו — והיה אחיו של הקונה. הוא ידע על זה רק 6 חודשים אחרי שעמדה לפי שנמכרה'. אנקדוטה אחת, 2-3 משפטים."),
]


def fetch_fun_quip() -> tuple[str, str] | None:
    """
    Ask Claude CLI for a fun item in a randomly-chosen category.
    Returns (emoji, text) or None on failure.
    """
    emoji, label, prompt_template = random.choice(FUN_CATEGORIES)
    prompt = (
        f"ענה רק עם הטקסט עצמו, ללא הקדמות, ללא הסברים, ללא ציטוטים. "
        f"{prompt_template}"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            return None
        text = result.stdout.strip().strip("\"'״׳ \n")
        if not text:
            return None
        if len(text) > 400:
            text = text[:400] + "…"
        return emoji, text
    except Exception:
        return None


def render_email_html(today: date, shows: list[dict], total_count: int) -> tuple[str, str]:
    """Return (subject, html_body). `shows` is the trimmed display list, `total_count` the full count."""
    weather = fetch_tlv_today()
    tv_rec = fetch_tv_recommendation()
    today_event_raw = pick_today_in_history(today)
    fact_raw = pick_random_fact() if not today_event_raw else None
    fun_pair = fetch_fun_quip()
    fun_item = None
    if fun_pair:
        emoji, text = fun_pair
        fun_item = {"emoji": emoji, "text": text}

    today_event = None
    if today_event_raw:
        years_ago = today.year - today_event_raw.get("year", today.year)
        today_event = {**today_event_raw, "years_ago": years_ago}

    fact = None
    if fact_raw:
        if isinstance(fact_raw, str):
            fact = {"text": fact_raw, "link": None, "link_label": None}
        else:
            fact = fact_raw

    template_dir = Path(__file__).parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("email_digest.html")

    n = total_count
    subject = f"🎭 {n} {'הופעה חדשה' if n == 1 else 'הופעות חדשות'} היום"

    grand_total = fetch_total_active_count(today)

    html = template.render(
        subject=subject,
        greeting=time_of_day_greeting(),
        today_he=he_date(today),
        weather=weather,
        new_count=total_count,
        total_count=grand_total,
        tv_rec=tv_rec,
        today_event=today_event,
        fact=fact,
        fun_item=fun_item,
        shows=shows,
        public_url=PUBLIC_URL,
    )
    return subject, html


def send_email(html: str, subject: str, recipients: list[str], smtp_user: str, smtp_password: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


MAX_SHOWS_IN_EMAIL = 10


def main() -> int:
    today = date.today()
    all_new = fetch_new_shows(today)
    if not all_new:
        print(f"[notify] no new shows on {today.isoformat()}; skipping email")
        return 0

    # Trim to max 10 for the email; full list is on the website
    shows_for_email = all_new[:MAX_SHOWS_IN_EMAIL]
    total_count = len(all_new)

    secrets = {**os.environ, **load_secrets()}
    smtp_user = secrets.get("SMTP_USER")
    smtp_password = secrets.get("SMTP_PASSWORD")
    recipients_raw = secrets.get("DIGEST_RECIPIENTS", smtp_user or "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    if not (smtp_user and smtp_password and recipients):
        print("[notify] missing SMTP_USER / SMTP_PASSWORD / DIGEST_RECIPIENTS — skipping")
        return 1

    subject, html = render_email_html(today, shows_for_email, total_count)
    try:
        send_email(html, subject, recipients, smtp_user, smtp_password)
    except Exception as e:
        print(f"[notify] send failed: {e}")
        return 1
    print(f"[notify] sent {len(shows_for_email)}/{total_count} shows to {len(recipients)} recipient(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
