"""
Scraper for Barby Club, south Tel Aviv — Israel's iconic
rock / Israeli-music venue.

The site at https://barby.co.il/ is a React SPA that renders nothing useful
server-side. It does, however, talk to a clean JSON API on the same origin:

  GET https://barby.co.il/api/shows/find
       -> {"returnShow": {"show": [<row>, ...]}, "tierPriceData": ...}

  GET https://barby.co.il/api/shows/show/<showId>
       -> {showId, showName, showDate, showTime, description (HTML), ...}

The listing endpoint already carries everything we need for the digest:
title (showName), date (DD/MM/YYYY), time (HH:MM), poster filename, and a
stable showId. Description text is only on the detail endpoint, but it's
rich HTML; we skip detail fetches to keep latency low and follow the
listing-only pattern used by zappa_tlv.

Posters are served from a CDN:
  Listing-card image -> https://images.barby.co.il/Logos/<showImage>
  Banner             -> https://images.barby.co.il/Banners/<showShortLogo>

Front-end ticket page: https://barby.co.il/show/<showId>

Multi-night residencies (e.g. Evyatar Banai runs five nights in a row) appear
as separate rows with identical showName. We collapse them by canonical
title key — same pattern as Zappa.

Genre is hard-coded "Music" (the venue books exclusively concerts).
Venue/city are constant: "Barby Club" / "Tel Aviv".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

import httpx

from ..models import Show
from .base import Scraper

# NOTE: source-site text-matching literals were translated from the original Hebrew for this English demo.

API_LISTING = "https://barby.co.il/api/shows/find"
SHOW_PAGE_BASE = "https://barby.co.il/show/"
IMAGE_LOGO_BASE = "https://images.barby.co.il/Logos/"
IMAGE_BANNER_BASE = "https://images.barby.co.il/Banners/"

# Barby's API rejects requests that look like generic bots — it returns 403
# unless the browser-like Origin / Referer / Accept-JSON triplet is present.
# These headers were verified live against /api/shows/find.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://barby.co.il",
    "Referer": "https://barby.co.il/",
}

# Date is DD/MM/YYYY, time is HH:MM (24h, Israel local).
DATE_FMT = "%d/%m/%Y"
TIME_FMT = "%H:%M"

# A single placeholder/test row that the API has been serving with the bogus
# date 31/12/2027 and the title "customer service email". Anything dated
# >= a couple of years out with a meta-ish title is safe to drop; we filter
# only that exact row to avoid false positives.
TEST_ROW_TITLE_RE = re.compile(r"customer\s*service\s*email")


class BarbyScraper(Scraper):
    source_id = "barby"
    source_name = "Barby Club"
    venue = "Barby Club"
    city = "Tel Aviv"

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        # Replace the base client with one that carries the Origin / Referer
        # / Accept-JSON headers the Barby API requires (it 403s otherwise).
        self.client.close()
        self.client = httpx.Client(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=timeout,
        )

    def fetch_shows(self) -> Iterable[Show]:
        try:
            resp = self.get(API_LISTING)
            payload = resp.json()
        except Exception as e:
            self.log.error("Barby listing fetch failed: %s", e)
            return

        rows = (payload.get("returnShow") or {}).get("show") or []
        self.log.info("Barby listing: %d rows", len(rows))

        # Group by canonical title so a multi-night residency collapses into
        # one Show with several performance datetimes.
        groups: dict[str, dict] = {}
        for row in rows:
            self._collect_row(row, groups)

        for data in groups.values():
            yield self._make_show(data)

    # -- internals -------------------------------------------------------
    def _collect_row(self, row: dict, groups: dict[str, dict]) -> None:
        title = (row.get("showName") or "").strip()
        if not title:
            return
        if TEST_ROW_TITLE_RE.search(title):
            return

        dt = self._parse_dt(row.get("showDate", ""), row.get("showTime", ""))
        if dt is None:
            return

        show_id = str(row.get("showId") or "").strip()
        url = f"{SHOW_PAGE_BASE}{show_id}" if show_id else "https://barby.co.il/"

        poster = ""
        img = (row.get("showImage") or "").strip()
        if img:
            poster = IMAGE_LOGO_BASE + img
        else:
            banner = (row.get("showShortLogo") or "").strip()
            if banner:
                poster = IMAGE_BANNER_BASE + banner

        key = self._title_key(title)
        bucket = groups.setdefault(
            key,
            {
                "title": title,
                "performances": [],
                "show_ids": [],
                "urls": [],
                "posters": [],
            },
        )
        bucket["performances"].append(dt)
        if show_id:
            bucket["show_ids"].append(show_id)
        if url:
            bucket["urls"].append(url)
        if poster:
            bucket["posters"].append(poster)

    def _make_show(self, data: dict) -> Show:
        performances = sorted(set(data["performances"]))
        show_ids = data["show_ids"]
        urls = data["urls"]
        posters = data["posters"]

        # Use earliest performance's id/url as canonical. For residencies
        # we combine ids deterministically so the stable_id is unique
        # per group rather than per night.
        if show_ids:
            source_id = "-".join(sorted(set(show_ids), key=lambda s: int(s) if s.isdigit() else 0))
        else:
            source_id = self._title_key(data["title"])

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=urls[0] if urls else "https://barby.co.il/",
            title=data["title"],
            venue=self.venue,
            city=self.city,
            performances=performances,
            description="",
            performers=[],
            director="",
            duration_minutes=None,
            genre="Music",
            poster_url=posters[0] if posters else "",
        )

    @staticmethod
    def _parse_dt(date_str: str, time_str: str) -> datetime | None:
        date_str = (date_str or "").strip()
        time_str = (time_str or "").strip() or "20:30"  # Barby's default doors time
        if not date_str:
            return None
        try:
            d = datetime.strptime(date_str, DATE_FMT).date()
        except ValueError:
            return None
        # Time may come as "HH:MM" or sometimes "HH:MM:SS"; normalise.
        m = re.match(r"^(\d{1,2}):(\d{2})", time_str)
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
        try:
            return datetime(d.year, d.month, d.day, hour, minute)
        except ValueError:
            return None

    @staticmethod
    def _title_key(title: str) -> str:
        """Normalise title for grouping multi-night residencies."""
        t = re.sub(r"\s+", " ", title).strip()
        t = re.sub(r"[\"'`]", "", t)
        return t.lower()
