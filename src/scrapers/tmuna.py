"""
Scraper for Tmuna Theater (תיאטרון תמונע) — independent fringe theater in Tel
Aviv (Soncino 8).

Site root:    https://www.tmu-na.org.il/
Schedule:     https://www.tmu-na.org.il/?pg=show
Show pages:   https://www.tmu-na.org.il/?CategoryID=<cat>&ArticleID=<id>

The schedule page (`?pg=show`) is a flat HTML table (`table.timeTableDataTable`)
with one row per performance. Columns: date / weekday / time / title (link) /
category / price / buy-button. When several performances fall on the same
calendar date, the date+weekday cells are blank — we carry the previous date
forward.

Each show detail page contains:
  - h1                                 → title
  - .Show-Tabs-Content-Inner           → multi-paragraph description, with
                                         credits at the end (label-prefixed
                                         lines like "בימוי:", "שחקנים:")
  - <b>מופעים קרובים</b> + <table>     → all upcoming dates for that show
                                         (not just those visible on the front
                                         schedule's window)
  - <meta property="og:image">         → poster

There is no JSON-LD on the show pages, so `tickets_opened_on` is left None.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


BASE_URL = "https://www.tmu-na.org.il"
SCHEDULE_URL = "https://www.tmu-na.org.il/?pg=show"

# Schedule cells: DD/MM/YYYY and HH:MM
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")

# Hebrew credit labels — used both to detect where credits start in the
# description block, and to extract individual fields (director, performers).
# Sorted longest-first so that "עיצוב במה" matches before "עיצוב".
CREDIT_LABELS = sorted([
    "מחזה",
    "מאת",
    "בימוי",
    "במאי",
    "דרמטורגיה",
    "כוריאוגרפיה",
    "תרגום",
    "עיצוב במה",
    "עיצוב תאורה",
    "עיצוב",
    "תפאורה",
    "תלבושות",
    "תאורה",
    "מוסיקה",
    "מוזיקה",
    "ייעוץ מוסיקלי",
    "הפקה",
    "שחקנים",
    "משתתפים",
    "מבצעים",
    "בכיכובם",
    "בכיכוב",
], key=len, reverse=True)

_LABEL_OR = "|".join(re.escape(l) for l in CREDIT_LABELS)
# Match a label, optionally followed by 1-3 connector words (e.g.
# "בימוי ודרמטורגיה" or "עיצוב במה ותלבושות"), then a colon.
_LABEL_TAIL = r"(?:\s+ו?[א-ת]+){0,3}\s*[:：]"
# Generic "any credit label + colon" — used as a stop pattern in field lookups
# and to find where the credits block begins inside a description.
ANY_LABEL_COLON_RE = re.compile(rf"\b(?:{_LABEL_OR}){_LABEL_TAIL}")


class TmunaScraper(Scraper):
    source_id = "tmuna"
    source_name = "תיאטרון תמונע"
    venue = "תיאטרון תמונע"
    city = "תל אביב"

    def fetch_shows(self) -> Iterable[Show]:
        listing = self.get(SCHEDULE_URL)
        soup = BeautifulSoup(listing.text, "lxml")
        table = soup.select_one("table.timeTableDataTable")
        if not table:
            self.log.warning("Tmuna schedule: no timeTableDataTable found")
            return

        rows = [tr for tr in table.find_all("tr")
                if not tr.find("td", class_="spacerTd") and tr.find("td")]
        self.log.info("Tmuna schedule: %d performance rows", len(rows))

        # Group performances by show URL.
        # Track listing-side genre per URL too (from the "קטגוריה" column).
        grouped: dict[str, dict] = {}
        current_date: tuple[int, int, int] | None = None  # (y, m, d)

        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            date_txt = tds[0].get_text(" ", strip=True)
            time_txt = tds[2].get_text(" ", strip=True)
            link = tds[3].find("a", href=True)
            if not link:
                continue
            href = link["href"].strip()
            url = urljoin(BASE_URL, href)
            title = link.get_text(" ", strip=True)
            category = tds[4].get_text(" ", strip=True)

            # Date — carries forward when blank
            dm = DATE_RE.search(date_txt)
            if dm:
                d, mo, y = map(int, dm.groups())
                current_date = (y, mo, d)
            if current_date is None:
                continue

            tm = TIME_RE.search(time_txt)
            if not tm:
                continue
            h, mi = map(int, tm.groups())

            try:
                dt = datetime(current_date[0], current_date[1], current_date[2], h, mi)
            except ValueError:
                continue

            entry = grouped.setdefault(url, {
                "title": title,
                "category": category,
                "performances": [],
            })
            entry["performances"].append(dt)
            if not entry["title"] and title:
                entry["title"] = title
            if not entry["category"] and category:
                entry["category"] = category

        self.log.info("Tmuna unique shows: %d", len(grouped))

        for url, info in grouped.items():
            try:
                show = self._fetch_detail(
                    url,
                    fallback_title=info["title"],
                    listing_performances=info["performances"],
                    listing_genre=info["category"],
                )
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue
            if show is None:
                continue
            yield show

    # -- internals -------------------------------------------------------
    def _fetch_detail(
        self,
        url: str,
        fallback_title: str,
        listing_performances: list[datetime],
        listing_genre: str,
    ) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Title
        title_el = soup.select_one("h1.ArticleTitle, h1")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            title = fallback_title
        if not title:
            return None

        # Description + credits live in the same block — split heuristically.
        description, credits_text = self._extract_description_and_credits(soup)

        performers = self._extract_performers(credits_text)
        director = self._extract_field(credits_text, ["בימוי", "במאי"])

        duration_minutes = self._extract_duration(soup)

        # Poster — og:image is the cleanest source on this site
        poster_url = ""
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            poster_url = og["content"].strip()

        # Performances — prefer the detail page's "מופעים קרובים" table when
        # present (it includes dates beyond the front-schedule window). Fall
        # back to the listing-side dates otherwise.
        detail_performances = self._extract_detail_performances(soup)
        if detail_performances:
            performances = sorted(set(detail_performances) | set(listing_performances))
        else:
            performances = sorted(set(listing_performances))

        # Stable source_id from the ArticleID query param (falls back to URL).
        source_id = self._article_id(url) or url

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
            genre=listing_genre or "תיאטרון",
            poster_url=poster_url,
        )

    @staticmethod
    def _article_id(url: str) -> str:
        try:
            qs = parse_qs(urlparse(url).query)
            aid = qs.get("ArticleID", [""])[0]
            return aid
        except Exception:
            return ""

    @staticmethod
    def _extract_detail_performances(soup) -> list[datetime]:
        """Pull dates out of the "מופעים קרובים" table on a show page."""
        b = soup.find("b", string=re.compile(r"מופעים\s+קרובים"))
        if not b:
            return []
        table = b.find_next("table")
        if not table:
            return []
        out: list[datetime] = []
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            dm = DATE_RE.search(tds[0].get_text(" ", strip=True))
            tm = TIME_RE.search(tds[2].get_text(" ", strip=True))
            if not dm or not tm:
                continue
            d, mo, y = map(int, dm.groups())
            h, mi = map(int, tm.groups())
            try:
                out.append(datetime(y, mo, d, h, mi))
            except ValueError:
                continue
        return out

    @classmethod
    def _extract_description_and_credits(cls, soup) -> tuple[str, str]:
        """Return (description, credits_text).

        The Show-Tabs-Content-Inner block mixes prose (description, sometimes a
        review quote) with credit lines that look like
        "מחזה: ... // בימוי: ... // שחקנים: ...".
        We flatten to a single line, then split at the first credit-label.
        """
        inner = soup.select_one(".Show-Tabs-Content-Inner")
        if not inner:
            return "", ""
        # Flatten — collapse all whitespace (including the linebreaks BS4
        # inserts between inline tags) to single spaces.
        text = re.sub(r"\s+", " ", inner.get_text(" ", strip=True)).strip()
        if not text:
            return "", ""

        m = ANY_LABEL_COLON_RE.search(text)
        if m:
            description = text[: m.start()].strip(' .,"')
            credits_text = text[m.start() :].strip()
        else:
            description = text
            credits_text = ""

        return description[:600], credits_text

    @staticmethod
    def _extract_field(credits_text: str, labels: list[str]) -> str:
        """Find a labeled value (e.g. "בימוי: ...") inside flat credits text.

        Labels may carry connector words ("בימוי ודרמטורגיה:") and values run
        until the next "//" delimiter, the next labeled field, or the end of
        the string.
        """
        if not credits_text:
            return ""
        for label in labels:
            pattern = (
                rf"\b{re.escape(label)}{_LABEL_TAIL}\s*"
                rf"(.+?)(?=\s*(?://|\Z|{ANY_LABEL_COLON_RE.pattern}))"
            )
            m = re.search(pattern, credits_text)
            if not m:
                continue
            val = m.group(1).strip(' .,')
            if val:
                return val[:200]
        return ""

    @classmethod
    def _extract_performers(cls, credits_text: str) -> list[str]:
        for label in ["שחקנים", "משתתפים", "מבצעים", "בכיכובם", "בכיכוב"]:
            raw = cls._extract_field(credits_text, [label])
            if raw:
                parts = re.split(r"[,•·]|\s+ו(?=\S)", raw)
                parts = [p.strip(" .") for p in parts if p.strip()]
                if parts:
                    return parts[:8]
        return []

    @staticmethod
    def _extract_duration(soup) -> int | None:
        text = soup.get_text(" ", strip=True)
        m = re.search(
            r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*(?:כ-?\s*)?(\d{2,3})\s*דק",
            text,
        )
        if m:
            return int(m.group(1))
        m = re.search(
            r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*(?:כ-?\s*)?(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?",
            text,
        )
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        # "כשעה" / "כשעה וחצי"
        if re.search(r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*כשעה\s+וחצי", text):
            return 90
        if re.search(r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*כשעה", text):
            return 60
        return None
