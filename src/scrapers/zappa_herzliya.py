"""
Scraper for Zappa Club Herzliya (זאפה הרצליה).

Sister venue to Zappa Tel Aviv on the same Eventim-backed zappa-club.co.il
listing site. The HTML structure is identical to ``zappa_tlv.py`` — one
``article.listing-item`` per night, ``time[datetime]`` for the ISO performance
datetime, ``data-event-id`` for the stable id, ``/event/<slug>`` for the URL.

Listing page (paginated via ?pnum=N):
  https://www.zappa-club.co.il/city/הרצליה-314/venue/זאפה-הרצליה-25735/

Akamai blocks /event/<slug>/ detail pages with 403 for non-browser
fingerprints, so — like the Zappa TLV scraper — we extract everything from
the listing cards and leave description/performers empty rather than fake
them. Multi-night runs of the same artist are collapsed into a single
``Show`` with multiple performance datetimes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


BASE = "https://www.zappa-club.co.il"
LISTING_URL = (
    "https://www.zappa-club.co.il/"
    "city/הרצליה-314/venue/זאפה-הרצליה-25735/"
)
MAX_PAGES = 10  # safety cap

# Same browser-impersonating headers as the Zappa TLV scraper — Akamai on
# this host resets HTTP/2 streams for the default httpx fingerprint.
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


class ZappaHerzliyaScraper(Scraper):
    source_id = "zappa_herzliya"
    source_name = "זאפה הרצליה"
    venue = "זאפה הרצליה"
    city = "הרצליה"

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.client.close()
        self.client = httpx.Client(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            http2=False,
        )

    def fetch_shows(self) -> Iterable[Show]:
        # title -> aggregated data
        groups: dict[str, dict] = {}

        for page_num in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page_num == 1 else f"{LISTING_URL}?pnum={page_num}"
            try:
                r = self.get(url)
            except Exception as e:
                self.log.warning("Zappa Herzliya page %d failed: %s", page_num, e)
                break

            soup = BeautifulSoup(r.text, "lxml")
            arts = soup.select("article.listing-item")
            self.log.info("Zappa Herzliya page %d: %d cards", page_num, len(arts))
            if not arts:
                break

            for art in arts:
                self._collect_card(art, groups)

            # Stop if no "next" pagination link
            next_link = soup.select_one(".pagination a[rel=next]")
            if not next_link:
                pages = [
                    self._extract_pnum(a.get("href", ""))
                    for a in soup.select(".pagination a")
                ]
                pages = [p for p in pages if p is not None]
                if not pages or max(pages) <= page_num:
                    break

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

        # Herzliya-only filter — the URL slug always carries the venue name.
        # Belt-and-suspenders against the listing accidentally cross-promoting
        # another Zappa branch.
        if href and not self._is_herzliya(href):
            return

        # Datetime
        time_el = art.select_one("time[datetime]")
        dt = self._parse_dt(time_el.get("datetime", "")) if time_el else None
        if dt is None:
            return

        # Event id (used for stable_id when we have a single performance)
        event_id_el = art.select_one("[data-event-id]")
        event_id = event_id_el.get("data-event-id", "").strip() if event_id_el else ""

        # Poster
        poster = self._extract_poster(art)

        # Group by canonical title so multi-night runs collapse to one Show.
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
        """Pull a per-event image URL out of a Zappa listing card.

        Tries (in order): img[src], img[data-src], inline background-image,
        any /obj/media/IL-eventim/teaser/... URL anywhere in the card HTML.
        Skips logos and the static venue gallery.
        """
        for img in art.find_all("img"):
            for attr in ("src", "data-src", "data-original"):
                src = img.get(attr) or ""
                if src and "/teaser/" in src and "venue" not in src:
                    return urljoin(BASE, src)
        for el in art.find_all(style=True):
            m = re.search(r'background-image:\s*url\(["\']?([^"\')]+)["\']?\)', el.get("style", ""))
            if m and "/teaser/" in m.group(1) and "venue" not in m.group(1):
                return urljoin(BASE, m.group(1))
        m = re.search(r'(/obj/media/IL-eventim/teaser/[^"\'\s>]+\.(?:jpg|jpeg|png|webp))', str(art))
        if m and "venue" not in m.group(1):
            return urljoin(BASE, m.group(1))
        return ""

    def _make_show(self, data: dict) -> Show:
        performances = sorted(set(data["performances"]))
        urls = data["urls"]
        event_ids = data["event_ids"]

        url = urls[0] if urls else ""
        if event_ids:
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
            genre="מוזיקה",
            poster_url=(data.get("posters") or [""])[0],
        )

    @staticmethod
    def _parse_dt(raw: str) -> datetime | None:
        """Parse '2026-05-05T21:30:00.000+03:00' (with or without ms / colon)."""
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
        t = re.sub(r"[\"'״׳`]", "", t)
        return t.lower()

    @staticmethod
    def _is_herzliya(href: str) -> bool:
        from urllib.parse import unquote
        decoded = unquote(href).lower()
        markers = ["זאפה-הרצליה", "הרצליה", "herzliya", "hertzliya"]
        return any(m in decoded for m in markers)

    @staticmethod
    def _extract_pnum(href: str) -> int | None:
        m = re.search(r"[?&]pnum=(\d+)", href)
        return int(m.group(1)) if m else None
