"""
Scraper for Habima — Israel's national theater.

Listing page: https://www.habima.co.il/repertoire/
Each show card links to a detail page like
  https://www.habima.co.il/shows/<slug>/
which in turn embeds:
  - h3.show-title           → show title
  - .show-desc              → multi-paragraph description
  - .presentations li       → upcoming performances ("DD.MM HH:MM day-X Buy")
                              each containing <a href="https://tickets.habima.co.il/order/{id}">
  - .show-card-image img    → poster

Dates appear in DD.MM format (no year). We assume the next occurrence — if
the date is more than 60 days in the past, we roll it to next year.
"""

# NOTE: source-site text-matching literals were translated from the original Hebrew for this English demo.

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


LISTING_URL = "https://www.habima.co.il/repertoire/"
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})")


class HabimaScraper(Scraper):
    source_id = "habima"
    source_name = "Habima Theatre"
    venue = "Habima Theatre"
    city = "Tel Aviv"

    def fetch_shows(self) -> Iterable[Show]:
        listing = self.get(LISTING_URL)
        soup = BeautifulSoup(listing.text, "lxml")
        cards = soup.select(".show-card")
        self.log.info("Habima listing: %d cards", len(cards))

        seen_urls: set[str] = set()
        for card in cards:
            link = card.select_one("a.purchase, a[href*='/shows/']")
            if not link:
                continue
            url = link.get("href", "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                show = self._fetch_detail(url, card)
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue

            if show is None:
                continue
            yield show

    # -- internals -------------------------------------------------------
    def _fetch_detail(self, url: str, listing_card) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        title_el = soup.select_one("h1, h3.show-title, .show-title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            # Fall back to listing card title
            t2 = listing_card.select_one(".show-title")
            title = t2.get_text(strip=True) if t2 else ""
        if not title:
            return None

        # Description
        desc_el = soup.select_one(".show-desc")
        description = ""
        if desc_el:
            # Take first 2 paragraphs of meaningful text
            paras = [p.get_text(" ", strip=True) for p in desc_el.find_all("p")]
            paras = [p for p in paras if len(p) > 20]
            description = " ".join(paras[:2])[:600]

        # Performers — try to find a "Starring" / "Cast" section
        performers = self._extract_performers(soup)

        # Director
        director = self._extract_field(soup, ["Directed by", "Director"])

        # Duration
        duration_minutes = self._extract_duration(soup)

        # Poster
        poster_url = ""
        img_el = soup.select_one(".show-image img, .show-card-image img, picture img")
        if img_el:
            poster_url = img_el.get("src") or ""
        if not poster_url:
            img2 = listing_card.select_one("img")
            if img2:
                poster_url = img2.get("src") or ""

        # Performances
        performances = self._extract_performances(soup)

        # When did the page first get published? Best proxy for "when did tickets open".
        tickets_opened_on = self._extract_tickets_opened(soup)

        # Use the show URL as source_id (slug). Strip trailing slash.
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
            genre="Theatre",
            poster_url=poster_url,
            tickets_opened_on=tickets_opened_on,
        )

    @staticmethod
    def _extract_performances(soup) -> list[datetime]:
        """Parse `.presentations li` items — each '<DD>.<MM> <HH>:<MM> ...'."""
        performances: list[datetime] = []
        now = datetime.now()
        items = soup.select(".presentations li")
        for li in items:
            text = li.get_text(" ", strip=True)
            m = DATE_RE.search(text)
            if not m:
                continue
            d, mo, h, mi = map(int, m.groups())
            year = now.year
            try:
                dt = datetime(year, mo, d, h, mi)
            except ValueError:
                continue
            # If parsed date is more than 30 days in the past, bump to next year
            if dt < now - timedelta(days=30):
                dt = dt.replace(year=year + 1)
            performances.append(dt)
        return sorted(set(performances))

    @staticmethod
    def _extract_field(soup, labels: list[str]) -> str:
        """Find paragraphs starting with any of the labels and return the text after."""
        for label in labels:
            for el in soup.find_all(string=re.compile(rf"^\s*{re.escape(label)}\s*[:：]")):
                parent = el.parent
                txt = parent.get_text(" ", strip=True)
                # Strip the label prefix
                m = re.match(rf"^\s*{re.escape(label)}\s*[:：]\s*(.+)", txt)
                if m:
                    return m.group(1).strip()[:200]
        return ""

    @staticmethod
    def _extract_performers(soup) -> list[str]:
        # Look for "Starring" / "Cast" / "Actors"
        for label in ["Starring", "Cast", "Actors", "Performers"]:
            for el in soup.find_all(string=re.compile(rf"{re.escape(label)}")):
                parent = el.parent
                txt = parent.get_text(" ", strip=True)
                m = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", txt)
                if m:
                    raw = m.group(1).strip()
                    # Split by commas, "and", or middots
                    parts = re.split(r"[,•·]|\s+and\s+", raw)
                    parts = [p.strip(" .") for p in parts if p.strip()]
                    return parts[:8]
        return []

    @staticmethod
    def _extract_tickets_opened(soup) -> date | None:
        """Pull `datePublished` out of the page's JSON-LD graph."""
        for s in soup.find_all("script", type="application/ld+json"):
            raw = s.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else data
            for entry in graph:
                if not isinstance(entry, dict):
                    continue
                dp = entry.get("datePublished")
                if dp:
                    try:
                        return datetime.fromisoformat(dp.replace("Z", "+00:00")).date()
                    except (ValueError, AttributeError):
                        continue
        return None

    @staticmethod
    def _extract_duration(soup) -> int | None:
        # Look for "Show duration" / "Show length"
        text = soup.get_text(" ", strip=True)
        m = re.search(r"(?:duration|length)\s+of the show\s*[:：]?\s*(?:approx\.?\s*-?\s*)?(\d{2,3})\s*min", text)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d)\s*(?:hour|hours)\s*(?:and\s*-?\s*(\d{1,2})\s*min)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        return None
