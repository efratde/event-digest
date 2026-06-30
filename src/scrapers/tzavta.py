"""
Scraper for Tzavta — Tel Aviv arts venue (theatre, music, stand-up, kids).

Listing page: https://www.tzavta.co.il/shows/
The listing is a flat schedule of performance rows — one `<li class="shedule_item">`
per performance, each with date (DD.MM.YYYY), time (HH:MM), hall, link to the
event page (`/event/{id}`), and a short subtitle. Multiple rows can share the
same event URL when a show has several upcoming performances; we group on URL.

Each event detail page (e.g. https://www.tzavta.co.il/event/3878) contains:
  - h1                          → show title
  - .show_title_txt             → subtitle / tagline
  - .show_pict_block img        → poster
  - .show_content_insert        → multi-paragraph description with credits
                                  ("מאת:", "בימוי:", "משחק:" inside <strong>)

Genre is mapped from the venue's category pages (/category/{id}); we build the
URL→genre map once per scrape and look each show up.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


BASE_URL = "https://www.tzavta.co.il"
LISTING_URL = "https://www.tzavta.co.il/shows/"

# Date in listing rows: DD.MM.YYYY
LISTING_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
# Date without year (fallback): DD.MM
SHORT_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\b")

CATEGORIES: list[tuple[str, str]] = [
    ("1", "תיאטרון"),
    ("2", "מוזיקה"),
    ("4", "סטנדאפ"),
    ("5", "הורים וילדים"),
    ("9", "מיוחדים"),
    ("7", "צוותא 2"),
    ("10", "אופרה"),
    ("17", "פסטיבל תיאטרון קצר"),
]


class TzavtaScraper(Scraper):
    source_id = "tzavta"
    source_name = "צוותא"
    venue = "צוותא"
    city = "תל אביב"

    def fetch_shows(self) -> Iterable[Show]:
        # Build URL → genre map from category pages (best-effort)
        genre_map = self._build_genre_map()

        listing = self.get(LISTING_URL)
        soup = BeautifulSoup(listing.text, "lxml")
        items = soup.select("li.shedule_item")
        self.log.info("Tzavta listing: %d schedule rows", len(items))

        # Group performance rows by event URL
        grouped: dict[str, dict] = {}
        for li in items:
            link = li.select_one("a[href*='/event/']")
            if not link:
                continue
            href = link.get("href", "").strip()
            if not href:
                continue
            url = urljoin(BASE_URL, href)

            dt = self._parse_listing_datetime(li)
            title = link.get_text(strip=True)

            entry = grouped.setdefault(url, {"title": title, "performances": []})
            if dt is not None:
                entry["performances"].append(dt)
            if not entry["title"] and title:
                entry["title"] = title

        self.log.info("Tzavta unique events: %d", len(grouped))

        for url, info in grouped.items():
            try:
                show = self._fetch_detail(
                    url,
                    fallback_title=info["title"],
                    listing_performances=info["performances"],
                    genre_map=genre_map,
                )
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue
            if show is None:
                continue
            yield show

    # -- internals -------------------------------------------------------
    def _build_genre_map(self) -> dict[str, str]:
        """Return {event_url: genre_name} from each category listing page."""
        mapping: dict[str, str] = {}
        for cat_id, name in CATEGORIES:
            try:
                r = self.get(f"{BASE_URL}/category/{cat_id}")
            except Exception as e:
                self.log.debug("category %s failed: %s", cat_id, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href*='/event/']"):
                href = a.get("href", "").strip()
                if not href:
                    continue
                url = urljoin(BASE_URL, href)
                # Don't overwrite — first category wins
                mapping.setdefault(url, name)
        return mapping

    @staticmethod
    def _parse_listing_datetime(li) -> datetime | None:
        date_el = li.select_one(".shedule_date_txt")
        time_el = li.select_one(".shedule_it_block.time")
        if not date_el or not time_el:
            return None
        dm = LISTING_DATE_RE.search(date_el.get_text(" ", strip=True))
        tm = TIME_RE.search(time_el.get_text(" ", strip=True))
        if not dm or not tm:
            return None
        d, mo, y = map(int, dm.groups())
        h, mi = map(int, tm.groups())
        try:
            return datetime(y, mo, d, h, mi)
        except ValueError:
            return None

    def _fetch_detail(
        self,
        url: str,
        fallback_title: str,
        listing_performances: list[datetime],
        genre_map: dict[str, str],
    ) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Title
        title_el = soup.select_one(".show_title_sec h1, h1")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            title = fallback_title
        if not title:
            return None

        # Subtitle / tagline (used to enrich description)
        subtitle_el = soup.select_one(".show_title_txt")
        subtitle = subtitle_el.get_text(" ", strip=True) if subtitle_el else ""

        # Description — first 2 substantial paragraphs from the content insert
        desc_el = soup.select_one(".show_content_insert")
        description = ""
        if desc_el:
            paras: list[str] = []
            for p in desc_el.find_all("p"):
                txt = p.get_text(" ", strip=True)
                # Skip credit lines (start with a strong label like "מאת:")
                if not txt:
                    continue
                if len(txt) < 30:
                    continue
                # Heuristic: credit lines are short or start with a label
                if re.match(r"^(מאת|בימוי|משחק|כוריאוגרפיה|מוזיקה|תרגום|תפאורה|תלבושות|תאורה|הפקה|מבצעים|משתתפים|בכיכוב)\s*[:：]", txt):
                    continue
                paras.append(txt)
                if len(paras) >= 2:
                    break
            description = " ".join(paras)[:600]
        if not description and subtitle:
            description = subtitle[:600]

        # Performers — try "משחק", "מבצעים", "משתתפים", "בכיכובם"
        performers = self._extract_performers(soup)

        # Director — "בימוי" / "במאי"
        director = self._extract_field(soup, ["בימוי", "במאי"])

        # Duration
        duration_minutes = self._extract_duration(soup)

        # Poster
        poster_url = ""
        img_el = soup.select_one(".show_pict_block img")
        if img_el:
            src = img_el.get("src") or ""
            if src:
                poster_url = urljoin(BASE_URL, src)

        # Performances — prefer detailed schedule on the listing
        performances = sorted(set(listing_performances))

        # Genre
        genre = genre_map.get(url, "")

        # source_id from /event/{id}
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
        )

    @staticmethod
    def _extract_field(soup, labels: list[str]) -> str:
        """Find paragraphs starting with any of the labels and return the text after."""
        for label in labels:
            for el in soup.find_all(string=re.compile(rf"^\s*{re.escape(label)}\s*[:：]?\s*$|^\s*{re.escape(label)}\s*[:：]")):
                parent = el.parent
                # Walk up to the enclosing <p> or block element to capture sibling value
                container = parent
                for _ in range(3):
                    if container is None:
                        break
                    if container.name in ("p", "div", "li"):
                        break
                    container = container.parent
                if container is None:
                    container = parent
                txt = container.get_text(" ", strip=True)
                m = re.match(rf"^\s*{re.escape(label)}\s*[:：]\s*(.+)", txt)
                if m:
                    val = m.group(1).strip()
                    # Stop at the next label if present
                    val = re.split(r"\s+(?:מאת|בימוי|משחק|כוריאוגרפיה|תרגום|תפאורה|תלבושות|תאורה|הפקה)\s*[:：]", val)[0]
                    return val.strip(" .")[:200]
        return ""

    @classmethod
    def _extract_performers(cls, soup) -> list[str]:
        for label in ["משחק", "מבצעים", "משתתפים", "בכיכובם", "שחקנים", "בכיכוב"]:
            raw = cls._extract_field(soup, [label])
            if raw:
                parts = re.split(r"[,•·]|\s+ו(?=\S)", raw)
                parts = [p.strip(" .") for p in parts if p.strip()]
                if parts:
                    return parts[:8]
        return []

    @staticmethod
    def _extract_duration(soup) -> int | None:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*(?:כ-?\s*)?(\d{2,3})\s*דק", text)
        if m:
            return int(m.group(1))
        m = re.search(r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*(?:כ-?\s*)?(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        m = re.search(r"(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            total = hours * 60 + mins
            if 30 <= total <= 300:
                return total
        return None
