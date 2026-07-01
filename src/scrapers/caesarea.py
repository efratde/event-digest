"""
Scraper for Caesarea Amphitheater — major outdoor summer venue.

The venue's owner (Caesarea Development Corporation, caesarea.com) does NOT
publish an upcoming-shows feed of its own; its "Caesarea Amphi – Concerts and
Music" page just links out to the Zappa/Eventim ticketing site, which acts as
the de-facto official listing:

  https://www.zappa-club.co.il/city/<caesarea-1573>/venue/<caesarea-amphitheater-21941>/

This is the same Zappa/Eventim platform that powers `zappa_tlv`, so we reuse
the same scraping pattern (article.listing-item cards with embedded
time[datetime] and a /event/<slug>-<id>/ link). We also reuse the Akamai-
friendly browser headers + http/1.1 transport, since the default httpx
fingerprint gets RST'd on this host.

Notes:
- The venue is summer-only — most of the year the listing is sparse (single
  digits of cards) and that's expected, not a bug.
- Many concerts run as multi-night residencies (e.g. HaKeves HaShisha Asar for two
  consecutive nights). We collapse these by canonical title so they appear as
  one Show with multiple performance datetimes — the Zappa pattern.
- Detail pages on Eventim are 403-blocked by Akamai even for a normal browser
  fingerprint, so we extract everything from the listing page itself; posters
  are best-effort from the card HTML and may be empty.
"""

from __future__ import annotations

# NOTE: source-site text-matching literals were translated from the original Hebrew for this English demo.

import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


BASE = "https://www.zappa-club.co.il"
# city: caesarea-1573 / venue: caesarea-amphitheater-21941 — URL-encoded so curl /
# shell tools downstream don't choke on Hebrew bytes.
LISTING_URL = (
    "https://www.zappa-club.co.il/"
    "city/%D7%A7%D7%99%D7%A1%D7%A8%D7%99%D7%94-1573/"
    "venue/%D7%90%D7%9E%D7%A4%D7%99%D7%AA%D7%99%D7%90%D7%98%D7%A8%D7%95%D7%9F-"
    "%D7%A7%D7%99%D7%A1%D7%A8%D7%99%D7%94-21941/"
)
MAX_PAGES = 10  # safety cap — venue rarely needs more than 1-2 pages

# Same browser-mimicking header set used by zappa_tlv. Akamai resets
# HTTP/2 streams for non-browser TLS fingerprints on this host, so we also
# force HTTP/1.1 in __init__.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


class CaesareaScraper(Scraper):
    source_id = "caesarea"
    source_name = "Caesarea Amphitheater"
    venue = "Caesarea Amphitheater"
    city = "Caesarea"

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        # Replace the base client with one that survives Akamai inspection.
        self.client.close()
        self.client = httpx.Client(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            http2=False,
        )

    def fetch_shows(self) -> Iterable[Show]:
        # title -> aggregated data (collapses multi-night residencies)
        groups: dict[str, dict] = {}

        for page_num in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page_num == 1 else f"{LISTING_URL}?pnum={page_num}"
            try:
                r = self.get(url)
            except Exception as e:
                self.log.warning("Caesarea page %d failed: %s", page_num, e)
                break

            soup = BeautifulSoup(r.text, "lxml")
            arts = soup.select("article.listing-item")
            self.log.info("Caesarea page %d: %d cards", page_num, len(arts))
            if not arts:
                break

            for art in arts:
                self._collect_card(art, groups)

            # Stop if no further pages — same logic as zappa_tlv
            next_link = soup.select_one(".pagination a[rel=next]")
            if not next_link:
                pages = [
                    self._extract_pnum(a.get("href", ""))
                    for a in soup.select(".pagination a")
                ]
                pages = [p for p in pages if p is not None]
                if not pages or max(pages) <= page_num:
                    break

        if not groups:
            self.log.info(
                "Caesarea: no upcoming shows found — venue is summer-only "
                "and may have nothing scheduled off-season."
            )

        for data in groups.values():
            yield self._make_show(data)

    # -- internals -------------------------------------------------------
    def _collect_card(self, art, groups: dict[str, dict]) -> None:
        title_el = art.select_one("h2.event-listing-city")
        if not title_el:
            return
        title = title_el.get_text(" ", strip=True)
        if not title:
            return

        # URL — first /event/ link
        link_el = art.select_one('a[href*="/event/"]')
        href = link_el.get("href", "").strip() if link_el else ""
        if not href:
            click_el = art.select_one("[onclick*='/event/']")
            if click_el:
                m = re.search(r"location\.href='([^']+)'", click_el.get("onclick", ""))
                if m:
                    href = m.group(1)
        url = urljoin(BASE, href) if href else ""

        # Caesarea-only filter — the URL slug always carries the venue name
        # in some form. Belt-and-suspenders against cross-promo cards leaking
        # in from another Zappa branch.
        if href and not self._is_caesarea(href):
            return

        # Datetime
        time_el = art.select_one("time[datetime]")
        dt = self._parse_dt(time_el.get("datetime", "")) if time_el else None
        if dt is None:
            return

        # Eventim event id — we use it for stable_id
        event_id_el = art.select_one("[data-event-id]")
        event_id = event_id_el.get("data-event-id", "").strip() if event_id_el else ""

        # Poster — best effort. Eventim sometimes lazy-loads, sometimes uses
        # background-image. We try several attributes and fall back to a regex
        # over the raw card HTML.
        poster = self._extract_poster(art)

        key = self._title_key(title)
        bucket = groups.setdefault(
            key,
            {
                "title": title,
                "performances": [],
                "urls": [],
                "event_ids": [],
                "posters": [],
            },
        )
        bucket["performances"].append(dt)
        if url:
            bucket["urls"].append(url)
        if event_id:
            bucket["event_ids"].append(event_id)
        if poster:
            bucket["posters"].append(poster)

    @staticmethod
    def _extract_poster(art) -> str:
        """Pull a per-event image URL out of a listing card. Best-effort."""
        # Direct <img> tags
        for img in art.find_all("img"):
            for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                src = img.get(attr) or ""
                if src and "/teaser/" in src and "venue" not in src:
                    return urljoin(BASE, src)
        # Inline style background-image
        for el in art.find_all(style=True):
            m = re.search(
                r'background-image:\s*url\(["\']?([^"\')]+)["\']?\)',
                el.get("style", ""),
            )
            if m and "/teaser/" in m.group(1) and "venue" not in m.group(1):
                return urljoin(BASE, m.group(1))
        # Last resort: any teaser URL in the card's raw HTML
        m = re.search(
            r'(/obj/media/IL-eventim/teaser/[^"\'\s>]+\.(?:jpg|jpeg|png|webp))',
            str(art),
        )
        if m and "venue" not in m.group(1):
            return urljoin(BASE, m.group(1))
        return ""

    def _make_show(self, data: dict) -> Show:
        performances = sorted(set(data["performances"]))
        urls = data["urls"]
        event_ids = data["event_ids"]

        url = urls[0] if urls else ""
        if event_ids:
            # Combine multiple ids deterministically when there's a residency
            source_id = "-".join(sorted(set(event_ids)))
        else:
            source_id = self._title_key(data["title"])

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=url,
            title=data["title"],
            venue=self.venue,
            city=self.city,
            performances=performances,
            description="",
            performers=[],
            director="",
            duration_minutes=None,
            genre="Music",
            poster_url=(data.get("posters") or [""])[0],
        )

    @staticmethod
    def _parse_dt(raw: str) -> datetime | None:
        """Parse '2026-05-28T21:00:00.000+03:00' (with or without ms / colon)."""
        if not raw:
            return None
        s = raw.strip()
        s = re.sub(r"\.\d+", "", s)
        m = re.search(r"([+\-]\d{2})(\d{2})$", s)
        if m and ":" not in s[m.start():]:
            s = s[: m.start()] + f"{m.group(1)}:{m.group(2)}"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt

    @staticmethod
    def _title_key(title: str) -> str:
        """Normalise title for grouping multi-night residencies."""
        t = re.sub(r"\s+", " ", title).strip()
        t = re.sub(r"[\"'`]", "", t)
        return t.lower()

    @staticmethod
    def _is_caesarea(href: str) -> bool:
        """Crude venue filter — slug should mention the amphi/Caesarea."""
        from urllib.parse import unquote
        decoded = unquote(href).lower()
        markers = ["caesarea", "caesarea-amphitheater", "amphi"]
        return any(m in decoded for m in markers)

    @staticmethod
    def _extract_pnum(href: str) -> int | None:
        m = re.search(r"[?&]pnum=(\d+)", href)
        return int(m.group(1)) if m else None
