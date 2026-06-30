"""
Scraper for Heichal Hatarbut Givatayim — תיאטרון גבעתיים (also known as
"היכל התרבות גבעתיים" or "תיאטרון גבעתיים על שם יצחק ירון"). It's the main
culture hall in the city of Givatayim and programs theater, music, stand-up,
and the occasional family/lecture content.

The visitor-facing site is https://t-g.co.il/ — a WordPress install that
exposes its events as a custom post type at:

    https://t-g.co.il/wp-json/wp/v2/events?per_page=100

That single call returns all upcoming + recently-past events (typically ~60-80
items) with structured fields:

  - title.rendered            → show title (HTML-encoded)
  - link                      → canonical detail page on t-g.co.il
  - event-date                → Unix timestamp of the show date (NUMERIC meta)
  - event-time                → "HH:MM" of the start time
  - event-cat / main-event /
    events-tags / venue       → taxonomy term IDs (we resolve names via the
                                tax endpoints below)
  - image-1                   → poster image URL
  - excerpt.rendered          → short blurb (HTML)
  - content.rendered          → full description (HTML)

Taxonomy lookups:
  - https://t-g.co.il/wp-json/wp/v2/main-event?per_page=100  (genre-ish)
  - https://t-g.co.il/wp-json/wp/v2/event-cat?per_page=100   (broader category)
  - https://t-g.co.il/wp-json/wp/v2/venue?per_page=100       (sub-hall name)

We hit each taxonomy once at scrape start and cache the id→name map.

Detail-page round-trip: only used to scrape JSON-LD `datePublished` (a proxy
for "tickets opened on"). The detail page is mostly Elementor boilerplate so
we don't extract anything else from it.

Classical filter: this venue programs some classical/opera/ballet content
(e.g. סולני האופרה, תזמורת ירושלים, סימפונט רעננה). The user has explicitly
excluded those genres — we drop a show if its title OR any of its genre/tag
taxonomy names match the classical-keyword regex.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


BASE = "https://t-g.co.il"
EVENTS_API = f"{BASE}/wp-json/wp/v2/events?per_page=100"
TAX_ENDPOINTS = {
    "main-event": f"{BASE}/wp-json/wp/v2/main-event?per_page=100",
    "event-cat":  f"{BASE}/wp-json/wp/v2/event-cat?per_page=100",
    "events-tags": f"{BASE}/wp-json/wp/v2/events-tags?per_page=100",
    "venue":      f"{BASE}/wp-json/wp/v2/venue?per_page=100",
}

TIME_RE = re.compile(r"(\d{1,2})\s*[:\.]\s*(\d{2})")

# Classical / opera / ballet exclusion. Dad has explicitly excluded these
# genres. We match on the show title AND on resolved taxonomy names so a
# show tagged "סולני האופרה" gets dropped even if the title doesn't say so.
CLASSICAL_PATTERNS = [
    r"סימפונ",          # סימפונית, סימפונט
    r"פילהרמונ",
    r"אופרה",
    r"בלט",
    r"קלאסי",
    r"קונצ['׳]?רט",
    r"סונט",
    r"קוורטט",
    r"רביעיי?ה",
    r"חמישיי?ה",
    r"תזמורת",
    r"בארוק",
    r"קאמרי(?!ת)",      # avoid matching "תיאטרון הקאמרי"
    r"ברנשטיין",
    r"בטהובן|מוצרט|באך|שופן|ברהמס|מאהלר",
]
CLASSICAL_RE = re.compile("|".join(CLASSICAL_PATTERNS))


class HeichalGivatayimScraper(Scraper):
    source_id = "heichal_givatayim"
    source_name = "היכל התרבות גבעתיים"
    venue = "היכל התרבות גבעתיים"
    city = "גבעתיים"

    def fetch_shows(self) -> Iterable[Show]:
        # 1. Resolve taxonomies once.
        tax_maps = self._fetch_taxonomies()

        # 2. Pull the events list.
        try:
            data = self.client.get(EVENTS_API).json()
        except Exception as e:
            self.log.error("Failed to fetch events list: %s", e)
            return

        self.log.info("Heichal Givatayim REST: %d events returned", len(data))

        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        # We accept events whose event-date is at most ~12h in the past so the
        # current evening's show still appears for "tonight".
        cutoff = now_ts - 12 * 3600

        kept = 0
        skipped_classical = 0
        skipped_past = 0
        skipped_no_date = 0

        for ev in data:
            try:
                show = self._build_show(ev, tax_maps, cutoff)
            except _SkipReason as sr:
                if sr.kind == "classical":
                    skipped_classical += 1
                elif sr.kind == "past":
                    skipped_past += 1
                elif sr.kind == "no_date":
                    skipped_no_date += 1
                continue
            except Exception as e:
                self.log.warning("Failed to build show from event %s: %s",
                                 ev.get("id"), e)
                continue
            if show is None:
                continue
            kept += 1
            yield show

        self.log.info(
            "Heichal Givatayim: kept %d, classical %d, past %d, no-date %d",
            kept, skipped_classical, skipped_past, skipped_no_date,
        )

    # -- internals -------------------------------------------------------

    def _fetch_taxonomies(self) -> dict[str, dict[int, str]]:
        out: dict[str, dict[int, str]] = {}
        for name, url in TAX_ENDPOINTS.items():
            try:
                terms = self.client.get(url).json()
                out[name] = {t["id"]: t["name"] for t in terms if isinstance(t, dict)}
            except Exception as e:
                self.log.warning("Failed to fetch taxonomy %s: %s", name, e)
                out[name] = {}
        return out

    def _build_show(
        self,
        ev: dict,
        tax_maps: dict[str, dict[int, str]],
        cutoff_ts: int,
    ) -> Optional[Show]:
        # --- Date (Unix timestamp) ----------------------------------------
        ev_date = ev.get("event-date")
        try:
            ev_date_ts = int(ev_date)
        except (TypeError, ValueError):
            raise _SkipReason("no_date")

        if ev_date_ts < cutoff_ts:
            raise _SkipReason("past")

        # --- Title --------------------------------------------------------
        title_raw = (ev.get("title") or {}).get("rendered") or ""
        title = html.unescape(title_raw).strip()
        if not title:
            raise _SkipReason("no_date")  # treat as garbage

        # --- Resolve taxonomy term names ----------------------------------
        main_event_names = self._names(ev.get("main-event"), tax_maps["main-event"])
        event_cat_names = self._names(ev.get("event-cat"), tax_maps["event-cat"])
        tag_names = self._names(ev.get("events-tags"), tax_maps["events-tags"])
        venue_names = self._names(ev.get("venue"), tax_maps["venue"])

        # --- Classical filter (title + any tax name) ---------------------
        if self._is_classical(title) or any(
            self._is_classical(n) for n in (*main_event_names, *event_cat_names, *tag_names)
        ):
            raise _SkipReason("classical")

        # --- Performance datetime ----------------------------------------
        # event-date is a UTC midnight-ish timestamp for the day; combine with
        # the event-time string for the actual start datetime.
        day = datetime.fromtimestamp(ev_date_ts, tz=timezone.utc).date()
        start_h, start_m = self._parse_time(ev.get("event-time"))
        try:
            performance = datetime(day.year, day.month, day.day, start_h, start_m)
        except ValueError:
            performance = datetime(day.year, day.month, day.day, 20, 0)

        # --- URL & stable id ---------------------------------------------
        url = (ev.get("link") or "").strip()
        if not url:
            raise _SkipReason("no_date")
        # Slug is the last path segment (URL-encoded Hebrew). Use the post id
        # for a stable, ascii id so the SQLite store doesn't choke.
        source_id = str(ev.get("id") or url.rstrip("/").split("/")[-1])

        # --- Description -------------------------------------------------
        description = self._extract_description(ev)

        # --- Poster ------------------------------------------------------
        poster_url = (ev.get("image-1") or "").strip()
        # Some entries have only featured_media (id) — the REST endpoint
        # doesn't embed the URL by default, so we fall back to og:image on
        # the detail page if we'd otherwise have nothing. Cheap optimization:
        # only do that when needed.

        # --- Genre (best-effort) -----------------------------------------
        genre = self._best_genre(main_event_names, event_cat_names, tag_names)

        # --- Hall (sub-venue name within the building) -------------------
        sub_hall = ""
        # Filter out venues that look like geo or generic markers
        for v in venue_names:
            if v in {"ת\"א", "אירוע"}:
                continue
            sub_hall = v
            break

        venue_display = self.venue
        if sub_hall:
            venue_display = f"{self.venue} - {sub_hall}"

        # --- tickets_opened_on (datePublished from JSON-LD on detail) ----
        # Cheaper proxy: the WP REST `date` field is the post-creation time.
        # We prefer JSON-LD datePublished when available because it survives
        # post re-saves better — but to keep this scraper fast (no per-event
        # round-trip), default to the REST `date` field when it parses.
        tickets_opened_on = self._parse_iso_date(ev.get("date"))

        # If we lack a poster, pull it from the detail page's og:image (one
        # round-trip, only when needed).
        if not poster_url and url:
            poster_url = self._fetch_poster_from_detail(url) or poster_url

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=url,
            title=title,
            venue=venue_display,
            city=self.city,
            performances=[performance],
            description=description,
            performers=[],
            director="",
            duration_minutes=None,
            genre=genre,
            poster_url=poster_url,
            tickets_opened_on=tickets_opened_on,
        )

    # -- small helpers ---------------------------------------------------

    @staticmethod
    def _names(ids, name_map: dict[int, str]) -> list[str]:
        if not ids:
            return []
        out = []
        for i in ids:
            try:
                n = name_map.get(int(i))
            except (TypeError, ValueError):
                continue
            if n:
                out.append(n)
        return out

    @staticmethod
    def _is_classical(text: str) -> bool:
        return bool(CLASSICAL_RE.search(text or ""))

    @staticmethod
    def _parse_time(s) -> tuple[int, int]:
        if not s:
            return (20, 0)
        m = TIME_RE.search(str(s))
        if not m:
            return (20, 0)
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return (20, 0)
        return (h, mi)

    @staticmethod
    def _parse_iso_date(s) -> Optional[date]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _extract_description(ev: dict) -> str:
        """Prefer excerpt (already short); fall back to first paragraph(s)
        of the full content. Strip HTML, collapse whitespace, cap length."""
        for source_field in ("excerpt", "content"):
            block = (ev.get(source_field) or {}).get("rendered") or ""
            if not block:
                continue
            text = BeautifulSoup(block, "lxml").get_text(" ", strip=True)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 30:
                return text[:600]
        return ""

    @staticmethod
    def _best_genre(main_event: list[str], event_cat: list[str], tags: list[str]) -> str:
        """Pick a single Hebrew genre string. Prefers the broad event-cat
        ('מוסיקה' / 'בידור' / 'הרצאה' / 'ילדים' / 'מועדון חברות'), falls back
        to scanning the main-event/tag lists for known buckets, else "".
        """
        # Generic 'main' wrappers — drop them; they're not useful as a genre.
        skip = {"אירוע ראשי", "ראשי", "אירוע גמלאים", "גמלאים", "מועדון חברות",
                "מועדון חברות בית החייל"}
        # Try event-cat first.
        for name in event_cat:
            if name and name not in skip:
                return name
        # Fall back to scanning main-event for a known bucket.
        known_buckets = {
            "סטנדאפ", "מוסיקה", "תיאטרון", "מחזמר", "מחול", "ילדים", "הרצאה",
            "סרט", "קרקס", "שירה", "בידור",
        }
        for name in main_event:
            if name in known_buckets:
                return name
        for name in tags:
            if name in known_buckets:
                return name
        # If main-event has exactly one non-skip name, use it.
        candidates = [n for n in main_event if n and n not in skip]
        if len(candidates) == 1:
            return candidates[0]
        return ""

    def _fetch_poster_from_detail(self, url: str) -> str:
        """One-shot fallback: pull og:image from the detail page."""
        try:
            r = self.client.get(url)
            r.raise_for_status()
        except Exception:
            return ""
        soup = BeautifulSoup(r.text, "lxml")
        og = soup.select_one('meta[property="og:image"]')
        if og:
            return (og.get("content") or "").strip()
        return ""


class _SkipReason(Exception):
    """Internal control-flow exception so the main loop can tally drop reasons."""
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind
