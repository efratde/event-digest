"""
Scraper for Reading 3 — open-air music venue at Tel Aviv port.

The venue's marketing site at https://www.reading3.co.il/ splits into two
sub-sites; the one we want is the shows listing:

  https://www.reading3.co.il/he/shows/a/main/

This is a classic ASP.NET-era page that renders all upcoming shows as plain
HTML server-side. Each card is a `.show-box-wraper` div carrying:

  - `.show-title-vert`              -> show / artist title
  - `.show-date`                    -> DD/MM/YYYY (Israel local)
  - `.show-clock span`              -> HH:MM (24h)
  - `a.buy-ticket-link[href]`       -> '../view/?ContentID=<id>' (stable id)
  - `style="background-image: url('/Warehouse/content/pics/pic_<id>_C.png')"`
    -> per-show poster image

There is no pagination, no JSON API, and no JSON-LD. The detail page
(`/he/shows/a/view/?ContentID=<id>`) renders no extra structured data, so
we stay listing-only — same approach as `zappa_tlv` and `barby`.

Reading 3 is operated by Eventim, but the venue's own page is easier to
parse than the Eventim catalog (which would require filtering across all
Israeli venues). We keep Eventim as a mental fallback only.

Genre is hard-coded "Music" (the venue books concerts exclusively).
Each booking is a single night — no residencies have been observed — but
we still group by canonical title in case two performances of the same
artist appear in the listing window.
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


BASE = "https://www.reading3.co.il"
LISTING_URL = "https://www.reading3.co.il/he/shows/a/main/"
DETAIL_URL_FMT = "https://www.reading3.co.il/he/shows/a/view/?ContentID={cid}"

DATE_FMT = "%d/%m/%Y"

# The site is plain ASP.NET — default httpx headers are accepted, but we
# keep a browser UA + Hebrew Accept-Language to match the rest of the
# scrapers and avoid surprises if WAF rules change.
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
}

CONTENT_ID_RE = re.compile(r"ContentID=(\d+)")
BG_IMG_RE = re.compile(
    r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)", re.I
)


class Reading3Scraper(Scraper):
    source_id = "reading3"
    source_name = "Reading 3"
    venue = "Reading 3"
    city = "Tel Aviv"

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        # Replace the base client with our browser-headered one. Pattern
        # mirrors zappa_tlv / barby for consistency.
        self.client.close()
        self.client = httpx.Client(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=timeout,
        )

    def fetch_shows(self) -> Iterable[Show]:
        try:
            resp = self.get(LISTING_URL)
        except Exception as e:
            self.log.error("Reading3 listing fetch failed: %s", e)
            return

        soup = BeautifulSoup(resp.text, "lxml")
        boxes = soup.select(".show-box-wraper")
        self.log.info("Reading3 listing: %d cards", len(boxes))

        # Group by canonical title so any unexpected multi-night entries
        # collapse to a single Show with multiple performance datetimes.
        groups: dict[str, dict] = {}
        for box in boxes:
            self._collect_card(box, groups)

        for data in groups.values():
            yield self._make_show(data)

    # -- internals -------------------------------------------------------
    def _collect_card(self, box, groups: dict[str, dict]) -> None:
        title_el = box.select_one(".show-title-vert")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            return

        date_el = box.select_one(".show-date")
        date_str = date_el.get_text(strip=True) if date_el else ""
        clock_el = box.select_one(".show-clock span")
        time_str = clock_el.get_text(strip=True) if clock_el else ""
        dt = self._parse_dt(date_str, time_str)
        if dt is None:
            return

        # ContentID — drives both the canonical URL and the source_id.
        link_el = box.select_one("a.buy-ticket-link")
        href = link_el.get("href", "").strip() if link_el else ""
        cid = ""
        m = CONTENT_ID_RE.search(href)
        if m:
            cid = m.group(1)
        url = DETAIL_URL_FMT.format(cid=cid) if cid else urljoin(BASE, href) if href else LISTING_URL

        # Poster — extracted from the inline background-image style on the
        # wrapper. Path is /Warehouse/content/pics/pic_<id>_C.<ext>.
        poster = ""
        style = box.get("style", "")
        bm = BG_IMG_RE.search(style)
        if bm:
            poster = urljoin(BASE, bm.group(1))

        key = self._title_key(title)
        bucket = groups.setdefault(
            key,
            {
                "title": title,
                "performances": [],
                "content_ids": [],
                "urls": [],
                "posters": [],
            },
        )
        bucket["performances"].append(dt)
        if cid:
            bucket["content_ids"].append(cid)
        if url:
            bucket["urls"].append(url)
        if poster:
            bucket["posters"].append(poster)

    def _make_show(self, data: dict) -> Show:
        performances = sorted(set(data["performances"]))
        content_ids = data["content_ids"]
        urls = data["urls"]
        posters = data["posters"]

        if content_ids:
            source_id = "-".join(
                sorted(set(content_ids), key=lambda s: int(s) if s.isdigit() else 0)
            )
        else:
            source_id = self._title_key(data["title"])

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=urls[0] if urls else LISTING_URL,
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
        # Reading 3 always lists a time, but fall back to a sane doors hour
        # if the cell is ever empty rather than dropping the show.
        time_str = (time_str or "").strip() or "21:00"
        if not date_str:
            return None
        try:
            d = datetime.strptime(date_str, DATE_FMT).date()
        except ValueError:
            return None
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
        """Normalise title for grouping multi-night entries."""
        t = re.sub(r"\s+", " ", title).strip()
        t = re.sub(r"[\"'`]", "", t)
        return t.lower()
