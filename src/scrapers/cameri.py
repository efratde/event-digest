"""
Scraper for the Cameri Theatre (Cameri).

Listing page: https://www.cameri.co.il/cameri_shows/
Each show card (li.show-item) contains:
  - <a class="inner block show-badge" href="...">  → detail-page link
  - <h2>                                            → show title
  - <p class="summary">                             → short blurb (also has credits)
  - <img class="the-show-image">                    → poster (sometimes <video poster=...>)

Detail pages embed:
  - h1                                               → show title
  - .show-content                                    → credits block (bimui, etc.)
  - article.about-show                               → multi-paragraph synopsis
  - ul.events-of-this-show > li                      → upcoming performances:
        <span>DD.MM</span><span>day</span><span>HH:MM</span> ... + ticket button/link
        Ticket link goes to https://tickets.cameri.co.il/order/{id}

Dates are DD.MM (no year). Same as Habima — assume next occurrence; if more
than 30 days in the past, roll to next year.
"""

# NOTE: source-site text-matching literals were translated from the original Hebrew for this English demo.
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


LISTING_URL = "https://www.cameri.co.il/cameri_shows/"
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


class CameriScraper(Scraper):
    source_id = "cameri"
    source_name = "Cameri Theatre"
    venue = "Cameri Theatre"
    city = "Tel Aviv"

    def fetch_shows(self) -> Iterable[Show]:
        listing = self.get(LISTING_URL)
        soup = BeautifulSoup(listing.text, "lxml")
        cards = soup.select("li.show-item")
        self.log.info("Cameri listing: %d cards", len(cards))

        seen_urls: set[str] = set()
        for card in cards:
            link = card.select_one("a.show-badge, a[href*='cameri_shows/']")
            if not link:
                continue
            url = link.get("href", "").strip()
            if not url or url in seen_urls:
                continue
            # Skip the listing-page URL itself
            if url.rstrip("/").endswith("cameri_shows"):
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

        # Title — prefer h1 on detail page, fall back to listing card h2
        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            t2 = listing_card.select_one("h2")
            title = t2.get_text(strip=True) if t2 else ""
        if not title:
            return None

        # Description — prefer the synopsis in article.about-show, fall back to
        # listing card summary.
        description = self._extract_description(soup)
        if not description:
            sm = listing_card.select_one("p.summary")
            if sm:
                description = sm.get_text(" ", strip=True)[:600]

        # Credits live in .show-content (multi-line "label: value" pairs)
        credits_text = ""
        sc = soup.select_one(".show-content")
        if sc:
            credits_text = sc.get_text("\n", strip=True)

        director = self._extract_credit(credits_text, ["Directing", "Director"])
        performers = self._extract_performers(soup, credits_text)
        duration_minutes = self._extract_duration(soup)

        # Poster — detail page rarely has it; pull from listing card.
        poster_url = ""
        img = listing_card.select_one("img.the-show-image, img")
        if img:
            poster_url = img.get("src") or img.get("data-src") or ""
        if not poster_url:
            video = listing_card.select_one("video[poster]")
            if video:
                poster_url = video.get("poster") or ""

        # Performances
        performances = self._extract_performances(soup)

        # Source ID = slug from URL
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
        )

    @staticmethod
    def _extract_description(soup) -> str:
        """Pull synopsis paragraphs from article.about-show (skip headings/links)."""
        art = soup.select_one("article.about-show")
        if not art:
            return ""
        paras = []
        for p in art.find_all("p"):
            txt = p.get_text(" ", strip=True)
            # Skip short / link-only / map-only paragraphs
            if len(txt) < 30:
                continue
            if "View map" in txt or "View program" in txt:
                continue
            paras.append(txt)
        return " ".join(paras[:2])[:600]

    @staticmethod
    def _extract_credit(credits_text: str, labels: list[str]) -> str:
        """Find a 'label: value' segment in the credits block.

        Some shows separate credits with newlines, others inline them with '|'.
        Match either, capturing up to the next separator.
        """
        if not credits_text:
            return ""
        for label in labels:
            m = re.search(
                rf"(?:^|\n|\|)\s*{re.escape(label)}\s*[:：]?\s*([^\n|]+)",
                credits_text,
            )
            if m:
                value = m.group(1).strip(" .|")
                if value:
                    return value[:200]
        return ""

    @staticmethod
    def _extract_performers(soup, credits_text: str) -> list[str]:
        # Cameri pages usually carry a "The cast of this show ..." sentence in body.
        body_text = soup.get_text(" ", strip=True)
        for label in ["The cast of this show", "Cast", "Starring", "Actors"]:
            m = re.search(rf"{re.escape(label)}[:：\s]+([^.]{{10,400}})", body_text)
            if m:
                raw = m.group(1).strip()
                # Split on commas, slashes (lead actor variants), middots, and "and"
                parts = re.split(r"[,/•·]|\s+and\s+", raw)
                parts = [p.strip(" .") for p in parts if p.strip(" .")]
                if parts:
                    return parts[:8]
        # Fall back: actor-image alt tags on the page (each performer has one)
        actor_imgs = soup.select(".actor-image img[alt]")
        names = [im.get("alt", "").strip() for im in actor_imgs]
        names = [n for n in names if n]
        return names[:8]

    @staticmethod
    def _extract_duration(soup) -> int | None:
        text = soup.get_text(" ", strip=True)
        # "Show duration: 90 minutes" / "approx. 90 minutes"
        m = re.search(
            r"(?:duration|length)\s+of\s+the\s+show\s*[:：]?\s*(?:approx\.?-?\s*)?(\d{2,3})\s*min", text
        )
        if m:
            return int(m.group(1))
        # Word forms: "an hour and a half", "two hours", "two hours and 45 minutes", "N hours"
        m = re.search(
            r"(?:duration|length)\s+of\s+the\s+show[^.]*?(two hours|(\d)\s*hours|hour)"
            r"(?:\s*and(?:-)?\s*(half|(\d{1,2})\s*min))?",
            text,
        )
        if m:
            word = m.group(1)
            if word == "hour":
                hours = 1
            elif word == "two hours":
                hours = 2
            else:
                hours = int(m.group(2))
            extra = m.group(3)
            if extra == "half":
                mins = 30
            elif m.group(4):
                mins = int(m.group(4))
            else:
                mins = 0
            return hours * 60 + mins
        return None

    @staticmethod
    def _extract_performances(soup) -> list[datetime]:
        """Each li in ul.events-of-this-show has spans for date, weekday, time."""
        performances: list[datetime] = []
        now = datetime.now()
        for li in soup.select("ul.events-of-this-show li"):
            text = li.get_text(" ", strip=True)
            d_m = DATE_RE.search(text)
            t_m = TIME_RE.search(text)
            if not d_m or not t_m:
                continue
            d, mo = int(d_m.group(1)), int(d_m.group(2))
            h, mi = int(t_m.group(1)), int(t_m.group(2))
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
