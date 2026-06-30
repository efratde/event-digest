"""
Scraper for Heichal Hatarbut Tel Aviv (היכל התרבות תל אביב — Charles Bronfman
Auditorium / "Mann Auditorium"). The venue hosts the Israel Philharmonic but
also a heavy rotation of pop/rock/Israeli music, musicals, and tribute shows.

Listing page: https://www.hatarbut.co.il/events/
The site is a WordPress build with the EventOrganiser plugin. All upcoming
events render on a single page (no pagination at the time of writing) — about
60 cards. Each card carries:
  - article.event                          → wrapper, with classes like
                                             event-category-133 (the "season"
                                             taxonomy id, not genre)
  - h3.eo-event-title a                    → title + detail URL
  - .eo-event-date span                    → DD.MM.YY (one or more, space-
                                             separated when the show recurs)
  - .eo-event-thumbnail img                → poster

Detail pages are Elementor templates. Useful selectors / cues:
  - h1.elementor-heading-title             → title
  - span.event-date-occurrence             → "DD.MM.YY | יום X" (for the
                                             specific occurrence the URL hits)
  - <meta property="og:description">       → starts with
        "תאריך:  אולם: <hall> תחילת המופע: HH:MM ..."
        — the most reliable place to get start time, hall, and a short blurb.
  - .elementor-widget-text-editor          → free-form description widgets
                                             (also lots of boilerplate ones)
  - JSON-LD @graph                         → datePublished (≈ tickets-opened)

Time strategy: parse the og:description for "תחילת המופע: HH:MM" — that field
exists on every event we sampled. Fall back to scanning small headings for
HH:MM if the meta tag is missing.

Date strategy: dates on the listing card are in DD.MM.YY (Israeli 2-digit-year)
form; we add the 2000 century. Multiple dates in the same .eo-event-date span
mean a multi-night run; we collapse them into a single Show with multiple
performance datetimes.

Classical filter: the venue programs heavy classical/symphony/opera content
(Philharmonic, baroque orchestras, "ensemble", "concerto"…). The user has
explicitly excluded these genres; we drop a show when its title matches any of
the classical-keyword regexes below.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


LISTING_URL = "https://www.hatarbut.co.il/events/"
BASE = "https://www.hatarbut.co.il"

# DD.MM.YY (2-digit year, sometimes appears as DD.MM.YYYY in older posts)
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
# Time of show, taken from the og:description "תחילת המופע: HH:MM"
START_TIME_RE = re.compile(r"תחילת\s+המופע\s*[:：]?\s*(\d{1,2}):(\d{2})")
# Hall name, taken from the og:description "אולם: <name>"
HALL_RE = re.compile(r"אולם\s*[:：]?\s*([^\s][^\n]{0,40}?)\s+תחילת")

# Classical / opera / ballet exclusion keywords (Hebrew). The user's father
# has explicitly excluded these genres. We match on the show title.
CLASSICAL_PATTERNS = [
    r"סימפונ",            # סימפונית, סימפוניה
    r"פילהרמונ",          # הפילהרמונית הישראלית
    r"אופרה",
    r"בלט",
    r"קלאסי",
    r"קונצ['׳]?רט",       # קונצ'רט / קונצרט / קונצ'רטו
    r"סונט",
    r"קוורטט",
    r"רביעי(י|)ה",        # רביעיית מיתרים, הרביעייה הירושלמית
    r"חמישי(י|)ה",        # חמישייה
    r"אנסמבל",            # most ensembles here are classical/chamber
    r"תזמורת",            # תזמורת הבארוק / תזמורת המהפכה
    r"בארוק",
    r"קאמרי(?!ת)",        # "קאמרי" (chamber); avoid matching "בקאמרית" alone
    r"ברנשטיין",
    r"בטהובן|מוצרט|באך|שופן|ברהמס|מאהלר",
]
CLASSICAL_RE = re.compile("|".join(CLASSICAL_PATTERNS))


class HeichalTlvScraper(Scraper):
    source_id = "heichal_tlv"
    source_name = "היכל התרבות תל אביב"
    venue = "היכל התרבות תל אביב"
    city = "תל אביב"

    def fetch_shows(self) -> Iterable[Show]:
        listing = self.get(LISTING_URL)
        soup = BeautifulSoup(listing.text, "lxml")
        cards = soup.select("article.event")
        self.log.info("Heichal TLV listing: %d cards", len(cards))

        seen_urls: set[str] = set()
        kept = 0
        skipped_classical = 0
        for card in cards:
            link = card.select_one("h3.eo-event-title a, a[itemprop='url']")
            if not link:
                continue
            url = (link.get("href") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title_el = card.select_one("h3.eo-event-title, .eo-event-title")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title:
                continue

            # Cheap pre-filter on the title before fetching the detail page —
            # saves a round-trip for the ~third of cards that are clearly
            # classical/symphony/opera.
            if self._is_classical(title):
                skipped_classical += 1
                self.log.debug("classical, skipped: %s", title)
                continue

            try:
                show = self._fetch_detail(url, card)
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue

            if show is None:
                continue
            kept += 1
            yield show

        self.log.info(
            "Heichal TLV: kept %d, dropped %d as classical",
            kept,
            skipped_classical,
        )

    # -- internals -------------------------------------------------------
    def _fetch_detail(self, url: str, listing_card) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Title: detail H1 first, fall back to listing card.
        title = ""
        h1 = soup.select_one("h1.elementor-heading-title, h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        if not title:
            tcard = listing_card.select_one(".eo-event-title")
            if tcard:
                title = tcard.get_text(" ", strip=True)
        if not title:
            return None

        # Re-check classical filter against the (possibly fuller) detail title.
        if self._is_classical(title):
            return None

        # og:description — single best source for start time, hall, and a
        # short blurb. Format always starts with:
        #   "תאריך:  אולם: <hall> תחילת המופע: HH:MM <free-text...>"
        og_desc = ""
        og = soup.select_one('meta[property="og:description"]')
        if og:
            og_desc = (og.get("content") or "").strip()

        start_time = self._extract_start_time(og_desc, soup)
        hall = self._extract_hall(og_desc)

        # Dates: prefer the listing card span (it's authoritative for the
        # whole run); fall back to the detail page's event-date-occurrence.
        date_strs = self._extract_date_strings(listing_card, soup)
        performances = self._build_performances(date_strs, start_time)

        # Description — strip the structured prefix off og:description, then
        # take the first chunk of meaningful text.
        description = self._build_description(og_desc, soup)

        # Poster — try the listing card's thumbnail first (the WordPress
        # canonical featured image), then og:image.
        poster_url = ""
        img_el = listing_card.select_one(".eo-event-thumbnail img, img.wp-post-image, img")
        if img_el:
            poster_url = (img_el.get("src") or "").strip()
        if not poster_url:
            ogi = soup.select_one('meta[property="og:image"]')
            if ogi:
                poster_url = (ogi.get("content") or "").strip()
        if poster_url:
            poster_url = urljoin(BASE, poster_url)

        # When did the page first get published? Best proxy for "tickets
        # went on sale" on the WordPress side.
        tickets_opened_on = self._extract_tickets_opened(soup)

        # Stable id = the URL slug (the bit between /events/event/ and the
        # trailing slash). Use a hash if the slug is the URL-encoded Hebrew
        # form to avoid storing raw bytes.
        source_id = url.rstrip("/").split("/")[-1]

        venue_name = self.venue
        if hall:
            # Append the hall name (e.g. "צוקר", "לאוי") for context. Both
            # are inside the same building, so city stays the same.
            venue_name = f"{self.venue} - אולם {hall}"

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=url,
            title=title,
            venue=venue_name,
            city=self.city,
            performances=performances,
            description=description,
            performers=[],
            director="",
            duration_minutes=self._extract_duration(og_desc + " " + soup.get_text(" ", strip=True)),
            genre="מוזיקה",
            poster_url=poster_url,
            tickets_opened_on=tickets_opened_on,
        )

    # -- extraction helpers ---------------------------------------------
    @staticmethod
    def _is_classical(title: str) -> bool:
        return bool(CLASSICAL_RE.search(title or ""))

    @staticmethod
    def _extract_start_time(og_desc: str, soup) -> tuple[int, int] | None:
        m = START_TIME_RE.search(og_desc or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        # Fallback: any small heading that's exactly HH:MM. The detail page
        # often duplicates the start time as a standalone <h2>/<span>.
        for el in soup.find_all(["h1", "h2", "h3", "span"]):
            t = el.get_text(" ", strip=True)
            if not t or len(t) > 8:
                continue
            mm = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
            if mm:
                h, mi = int(mm.group(1)), int(mm.group(2))
                if 0 <= h <= 23 and 0 <= mi <= 59:
                    return h, mi
        return None

    @staticmethod
    def _extract_hall(og_desc: str) -> str:
        if not og_desc:
            return ""
        m = HALL_RE.search(og_desc)
        if m:
            return m.group(1).strip(" :־-")
        return ""

    @staticmethod
    def _extract_date_strings(listing_card, soup) -> list[str]:
        """Collect raw 'DD.MM.YY' strings from the listing card and detail."""
        out: list[str] = []
        date_el = listing_card.select_one(".eo-event-date")
        if date_el:
            text = date_el.get_text(" ", strip=True)
            out.extend(DATE_RE.findall(text))
            # `findall` returns tuples — convert to strings later.

        # Fallback: detail page's per-occurrence date span.
        if not out:
            for el in soup.select(".event-date-occurrence"):
                text = el.get_text(" ", strip=True)
                out.extend(DATE_RE.findall(text))

        # Convert (d, m, y) tuples to a flat list of "d.m.y" strings.
        return [".".join(t) for t in out]

    @staticmethod
    def _build_performances(
        date_strs: list[str], start_time: tuple[int, int] | None
    ) -> list[datetime]:
        """Combine each date with the show's start time.

        If we don't have a start time we still emit a midnight placeholder so
        downstream code at least has the date — the digest renderer can
        handle that case.
        """
        h, mi = start_time if start_time else (0, 0)
        out: list[datetime] = []
        for s in date_strs:
            m = DATE_RE.fullmatch(s) or DATE_RE.search(s)
            if not m:
                continue
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            if not (1 <= d <= 31 and 1 <= mo <= 12):
                continue
            try:
                out.append(datetime(y, mo, d, h, mi))
            except ValueError:
                continue
        return sorted(set(out))

    @staticmethod
    def _build_description(og_desc: str, soup) -> str:
        """Pull a short blurb. The og:description prefix is structured
        boilerplate ('תאריך: אולם: <h> תחילת המופע: HH:MM לרכישת כרטיסים …')
        — we cut it off and take whatever real prose follows."""
        if og_desc:
            # Trim everything up to the first sentence that looks like prose,
            # i.e. drop the structured prefix that always begins with "תאריך:".
            cleaned = re.sub(
                r"^.*?(?:תחילת\s+המופע\s*[:：]?\s*\d{1,2}:\d{2}\s*)",
                "",
                og_desc,
                count=1,
            )
            # Drop common ticketing/membership boilerplate phrases.
            for boilerplate in [
                r"לרכישת כרטיסים",
                r"חברי ההיכל נהנים יותר",
                r"הצטרפו לקבוצה השקטה ותהנו!?",
                r"פתיחת דלתות שעה לפני תחילת המופע",
                r"נא הקדימו הגעתכם/?ן.{0,80}",
                r"כרטיסים שנרכשו לתאריך .{0,30} הינם בתוקף למועד זה",
                r"שימו לב!?",
            ]:
                cleaned = re.sub(boilerplate, " ", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
            if len(cleaned) > 30:
                return cleaned[:600]
        # Fallback: scan elementor text widgets for the longest reasonable
        # paragraph that isn't obviously boilerplate.
        candidates: list[str] = []
        for el in soup.select(".elementor-widget-text-editor .elementor-widget-container"):
            t = el.get_text(" ", strip=True)
            if not t or len(t) < 60 or len(t) > 1500:
                continue
            low = t
            if any(
                bad in low
                for bad in (
                    "חברי ההיכל",
                    "מדיניות הפרטיות",
                    "פיקוד העורף",
                    "Cookies",
                    "עוגיות",
                    "פתיחת דלתות",
                )
            ):
                continue
            candidates.append(t)
        if candidates:
            # Longest candidate tends to be the actual show blurb.
            return max(candidates, key=len)[:600]
        return ""

    @staticmethod
    def _extract_duration(text: str) -> int | None:
        if not text:
            return None
        m = re.search(r"משך\s+ה?מופע\s*[:：]?\s*(?:כ-?\s*)?(\d{2,3})\s*דק", text)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        return None

    @staticmethod
    def _extract_tickets_opened(soup) -> date | None:
        """Pull `datePublished` out of the page's JSON-LD graph (WordPress)."""
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
                        return datetime.fromisoformat(
                            dp.replace("Z", "+00:00")
                        ).date()
                    except (ValueError, AttributeError):
                        continue
        return None
