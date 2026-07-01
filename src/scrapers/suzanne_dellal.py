"""
Scraper for Suzanne Dellal Center — Israel's premier dance venue.

Listing page: https://suzannedellal.org.il/shows/
Each card links to a detail page like
  https://suzannedellal.org.il/shows/<percent-encoded-hebrew-slug>/
which contains:
  - h2 inside .show-details        → show title
  - .show-cats                     → category label (e.g. "Israeli dance", "children", "lectures")
  - .single-event blocks           → one per performance date, each holds
                                     "<DD>/<MM>/<YYYY>", "<HH>:<MM>", hall name, price
  - free-text body w/ description, performers ("Dancers/Collaborators:"), credits
  - JSON-LD WebPage entry with `datePublished` (used as tickets_opened_on proxy)

The center hosts mostly contemporary dance, but the user has explicitly
EXCLUDED ballet. Modern/contemporary dance is acceptable.
We filter out shows whose title or category mentions "ballet".

Per-show poster images on the detail page are loaded via JS into a slick slider
(`.slide-image .image-entity`); the rendered HTML doesn't include the image src.
We fall back to the og:image (the venue logo) and leave the upstream renderer to
hide if absent.
"""

# NOTE: source-site text-matching literals were translated from the original Hebrew for this English demo.

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterable

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


LISTING_URL = "https://suzannedellal.org.il/shows/"
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*\|?\s*(\d{1,2}):(\d{2})")
DURATION_RE = re.compile(r"(\d{2,3})\s*min")

# Map raw category labels to a coarse genre.
GENRE_MAP = {
    "dance": "dance",
    "Israeli dance": "dance",
    "music": "music",
    "theater": "theater",
    "children": "children",
    "lectures": "lectures",
    "festival": "festivals",
}


class SuzanneDellalScraper(Scraper):
    source_id = "suzanne_dellal"
    source_name = "Suzanne Dellal Center"
    venue = "Suzanne Dellal Center"
    city = "Tel Aviv"

    def fetch_shows(self) -> Iterable[Show]:
        listing = self.get(LISTING_URL)
        soup = BeautifulSoup(listing.text, "lxml")

        # Collect distinct detail-page URLs (each card links from multiple anchors).
        detail_urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/shows/" not in href or "/en/" in href:
                continue
            # Detail pages have a slug after /shows/
            tail = href.split("/shows/", 1)[1].strip("/")
            if not tail:
                continue
            if href in seen:
                continue
            seen.add(href)
            detail_urls.append(href)

        self.log.info("Suzanne Dellal listing: %d show pages", len(detail_urls))

        for url in detail_urls:
            try:
                show = self._fetch_detail(url)
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue
            if show is None:
                continue
            # Ballet filter — the user has explicitly excluded ballet.
            haystack = f"{show.title} {show.genre} {show.description}"
            if "ballet" in haystack.lower():
                self.log.info("Skipping ballet show: %s", show.title)
                continue
            yield show

    # -- internals -------------------------------------------------------
    def _fetch_detail(self, url: str) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Title: h2 inside the .show-details block (top of the show body).
        title = ""
        title_el = soup.select_one(".show-details h2")
        if title_el:
            title = title_el.get_text(strip=True)
        if not title:
            # Fallback: og:title (strip " - Suzanne Dellal" suffix)
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                title = og["content"].split(" - Suzanne Dellal")[0].strip()
        if not title:
            return None

        # Category → genre
        cat_el = soup.select_one(".show-cats")
        raw_category = cat_el.get_text(" ", strip=True) if cat_el else ""
        genre = self._map_genre(raw_category)

        # Description: og:description plus any nearby long paragraph from the body.
        description = self._extract_description(soup)

        # Performers + director (best-effort from the credits block).
        performers = self._extract_performers(soup)
        director = self._extract_field(soup, ["Choreography", "Choreographer", "Director", "Directed by"])

        # Duration ("60 minutes")
        duration_minutes = self._extract_duration(soup)

        # Performances from .single-event blocks
        performances = self._extract_performances(soup)

        # Poster — per-show images are JS-injected into a slider; fall back to og:image.
        poster_url = ""
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            poster_url = og_img["content"]

        # tickets_opened_on from JSON-LD datePublished
        tickets_opened_on = self._extract_tickets_opened(soup)

        # source_id = the URL slug (decoded would be Hebrew; keep encoded for stability)
        source_id = url.rstrip("/").split("/")[-1]

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=url,
            title=title,
            venue=self.venue,
            city=self.city,
            performances=performances,
            description=description,
            performers=performers,
            director=director,
            duration_minutes=duration_minutes,
            genre=genre,
            poster_url=poster_url,
            tickets_opened_on=tickets_opened_on,
        )

    @staticmethod
    def _map_genre(raw: str) -> str:
        if not raw:
            return "dance"  # venue's specialty
        for key, mapped in GENRE_MAP.items():
            if key in raw:
                return mapped
        return raw or "dance"

    @staticmethod
    def _extract_description(soup) -> str:
        # Prefer og:description for a clean blurb.
        og = soup.find("meta", property="og:description")
        og_text = og.get("content", "").strip() if og else ""

        # Also try to pull a paragraph from the body for richer copy.
        body_text = ""
        body = soup.select_one(".show-content, .single-show, .show-text, article, main")
        if body:
            paras = [p.get_text(" ", strip=True) for p in body.find_all("p")]
            paras = [p for p in paras if len(p) > 40]
            if paras:
                body_text = " ".join(paras[:2])

        if og_text and body_text and og_text not in body_text:
            combined = f"{og_text} {body_text}"
        else:
            combined = body_text or og_text
        return combined[:600].strip()

    @staticmethod
    def _extract_performances(soup) -> list[datetime]:
        performances: list[datetime] = []
        for ev in soup.select(".single-event"):
            text = ev.get_text(" | ", strip=True)
            m = DATE_RE.search(text)
            if not m:
                continue
            d, mo, yr, h, mi = map(int, m.groups())
            try:
                dt = datetime(yr, mo, d, h, mi)
            except ValueError:
                continue
            performances.append(dt)

        # Fallback: header line "Monday, 11 May, 2026, 20:30" — only if no .single-event found.
        if not performances:
            full = soup.get_text(" ", strip=True)
            for m in DATE_RE.finditer(full):
                d, mo, yr, h, mi = map(int, m.groups())
                try:
                    performances.append(datetime(yr, mo, d, h, mi))
                except ValueError:
                    continue

        return sorted(set(performances))

    @staticmethod
    def _extract_field(soup, labels: list[str]) -> str:
        for label in labels:
            for el in soup.find_all(string=re.compile(rf"{re.escape(label)}\s*[:：]")):
                parent = el.parent
                txt = parent.get_text(" ", strip=True)
                m = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", txt)
                if m:
                    return m.group(1).strip()[:200]
        return ""

    @staticmethod
    def _extract_performers(soup) -> list[str]:
        for label in ["Dancers/Collaborators", "Dancers", "Performers", "Participants", "Starring"]:
            for el in soup.find_all(string=re.compile(rf"{re.escape(label)}\s*[:：]")):
                parent = el.parent
                txt = parent.get_text(" ", strip=True)
                m = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", txt)
                if m:
                    raw = m.group(1).strip()
                    parts = re.split(r"[,•·]|\s+and\s+", raw)
                    parts = [p.strip(" .") for p in parts if p.strip()]
                    return parts[:10]
        return []

    @staticmethod
    def _extract_duration(soup) -> int | None:
        # Look in .show-details first to avoid matching unrelated "min" elsewhere.
        scope = soup.select_one(".show-details") or soup
        text = scope.get_text(" ", strip=True)
        m = DURATION_RE.search(text)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d)\s*hours?\s*(?:and\s*(\d{1,2})\s*min)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        return None

    @staticmethod
    def _extract_tickets_opened(soup) -> date | None:
        for s in soup.find_all("script", type="application/ld+json"):
            raw = s.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else data
            entries = graph if isinstance(graph, list) else [graph]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                dp = entry.get("datePublished")
                if dp:
                    try:
                        return datetime.fromisoformat(dp.replace("Z", "+00:00")).date()
                    except (ValueError, AttributeError):
                        continue
        return None
