"""
Scraper for Gesher Theater — תיאטרון גשר (Jaffa).

The repertoire listing lives at
    https://www.gesher-theatre.co.il/he/repertoire/a/main/
and the full schedule listing (with "next show" snippets) at
    https://www.gesher-theatre.co.il/he/repertoire/a/shows/

Both pages link to detail pages in the form
    https://www.gesher-theatre.co.il/he/repertoire/a/view/?ContentID=<id>

Detail-page selectors (custom non-WordPress CMS):
  - h1                                  → title
  - h2 (first one)                      → subtitle / byline
  - .mainBody > div:first-of-type       → free-form description (<p> tags)
  - .leftShowsContainer ul.subtitle-mode li
                                        → one <li> per performance with
        ├── span.dayOfTheWeek           → e.g. "יום רביעי"
        ├── span.fullDate               → DD/MM/YYYY
        ├── span.DateTime               → HH:MM
        └── div.purchaseLink a          → ticket link
  - .showTeam (multiple)                → "יוצרים" (creators) and "שחקנים" (cast)
  - meta[property="og:image"]           → poster (often malformed — see below)

Poster note: og:image on Gesher pages is sometimes built incorrectly as
  http://www.gesher-theatre.co.ilhttps://1407132350.rsc.cdn77.org/...
We strip the bad prefix. The canonical CDN pattern is
  https://1407132350.rsc.cdn77.org/Warehouse/content/pics/pic_<ContentID>_a.webp

JSON-LD on Gesher does NOT expose `datePublished`, so tickets_opened_on stays
None and the store falls back to first_seen.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


BASE = "https://www.gesher-theatre.co.il"
LISTING_URLS = [
    f"{BASE}/he/repertoire/a/main/",
    f"{BASE}/he/repertoire/a/shows/",
]
DETAIL_PATH = "/he/repertoire/a/view/"
# DD/MM/YYYY
FULL_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
# DD.MM (no year) — fallback used by the "next show" snippets on /shows/
SHORT_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")
# Recover a real CDN URL from a malformed og:image like
# "http://www.gesher-theatre.co.ilhttps://1407132350.rsc.cdn77.org/..."
CDN_RECOVER_RE = re.compile(r"https?://1407132350\.rsc\.cdn77\.org/[^\"'> ]+")


class GesherScraper(Scraper):
    source_id = "gesher"
    source_name = "תיאטרון גשר"
    venue = "תיאטרון גשר"
    city = "יפו"

    def fetch_shows(self) -> Iterable[Show]:
        urls = self._collect_show_urls()
        self.log.info("Gesher: %d unique show URLs", len(urls))

        for url in urls:
            try:
                show = self._fetch_detail(url)
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue
            if show is None:
                continue
            yield show

    # -- listing ---------------------------------------------------------
    def _collect_show_urls(self) -> list[str]:
        urls: set[str] = set()
        for listing in LISTING_URLS:
            try:
                r = self.get(listing)
            except Exception as e:
                self.log.warning("Gesher listing %s failed: %s", listing, e)
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if "ContentID" not in href:
                    continue
                full = urljoin(listing, href)
                if DETAIL_PATH not in full:
                    continue
                # Strip fragments / extra params we don't need
                cid = self._content_id(full)
                if not cid:
                    continue
                # Re-canonicalise so we don't have dupes from /repertoire/../view/?...
                canonical = f"{BASE}{DETAIL_PATH}?ContentID={cid}"
                urls.add(canonical)
        return sorted(urls)

    @staticmethod
    def _content_id(url: str) -> str:
        try:
            qs = parse_qs(urlparse(url).query)
            v = qs.get("ContentID", [""])[0]
            return v.strip()
        except Exception:
            return ""

    # -- detail ----------------------------------------------------------
    def _fetch_detail(self, url: str) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")
        html = r.text

        # Title
        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            if og:
                # og:title is "הצגה לילדים - <Title> - תיאטרון גשר, ..."
                content = (og.get("content") or "").strip()
                # Pull the chunk between the first " - " and " - תיאטרון גשר"
                m = re.search(r"-\s*(.+?)\s*-\s*תיאטרון גשר", content)
                if m:
                    title = m.group(1).strip()
        if not title:
            return None

        # Description — first <div> child of .mainBody, filtered to <p> text
        description = self._extract_description(soup)

        # Credits — parsed from .showTeam <ul><li> with <strong> values
        creators_map, cast_names = self._extract_team_structured(soup)
        director = self._pick_director(creators_map)
        performers = cast_names

        # Duration (somewhere in the page text)
        duration_minutes = self._extract_duration(soup.get_text(" ", strip=True))

        # Poster
        poster_url = self._extract_poster(soup, html, url)

        # Performances
        performances = self._extract_performances(soup)

        # ContentID acts as a clean source_id
        cid = self._content_id(url) or url.rstrip("/").split("/")[-1]

        # No JSON-LD datePublished on Gesher → leave None, store will use first_seen.
        return Show(
            source=self.source_id,
            source_id=cid,
            url=url,
            title=title,
            venue=self.venue,
            city=self.city,
            performances=performances,
            description=description,
            performers=performers,
            director=director,
            duration_minutes=duration_minutes,
            genre="תיאטרון",
            poster_url=poster_url,
            tickets_opened_on=None,
        )

    # -- description ----------------------------------------------------
    @staticmethod
    def _extract_description(soup) -> str:
        """The first child div of .mainBody holds the show blurb (<p> tags)."""
        body = soup.select_one(".mainShowContent .mainBody, .mainBody")
        if body:
            for div in body.find_all("div", recursive=False):
                paras = [p.get_text(" ", strip=True) for p in div.find_all("p")]
                paras = [p for p in paras if len(p) > 30]
                if paras:
                    return " ".join(paras[:3])[:600]
        # Fallback: any reasonably-long <p>
        paras = []
        for p in soup.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if 60 < len(txt) < 800 and "browser" not in txt.lower():
                paras.append(txt)
            if len(paras) >= 3:
                break
        if paras:
            return " ".join(paras)[:600]
        og = soup.select_one('meta[property="og:description"]')
        if og:
            return (og.get("content") or "").strip()[:600]
        return ""

    @staticmethod
    def _pick_director(creators: dict[str, str]) -> str:
        """Find a creator role that contains 'בימוי' / 'במאי' / 'במאית'."""
        keys = list(creators.keys())
        # Exact-ish match first
        for exact in ("בימוי", "במאי", "במאית"):
            if exact in creators:
                return creators[exact]
        # Substring match (handles 'בימוי ועיבוד', 'בימוי משותף')
        for k in keys:
            if any(word in k for word in ("בימוי", "במאי", "במאית")):
                return creators[k]
        return ""

    # -- credits ---------------------------------------------------------
    @staticmethod
    def _extract_team_structured(soup) -> tuple[dict[str, str], list[str]]:
        """Parse .showTeam blocks. Each <li> usually has '<role>:' + <strong>name</strong>.

        Sometimes Gesher splits a multi-person role across <li>s: the first <li>
        carries the role label with no <strong> ('בימוי ועיבוד:'), and the next
        few <li>s carry only the <strong> names. We track the last seen role so
        those orphan names attach to it.

        Returns (creators_map, cast_names).
        """
        creators: dict[str, str] = {}
        cast: list[str] = []
        cast_seen: set[str] = set()

        for tb in soup.select(".showTeam"):
            title_el = tb.select_one(".showTeamTitle")
            block_kind = (title_el.get_text(strip=True) if title_el else "").strip()
            last_role = ""
            for li in tb.select("li"):
                strongs = [s.get_text(" ", strip=True) for s in li.find_all("strong")]
                strongs = [s for s in strongs if s]
                # Role label: li text minus strong contents, minus trailing punctuation
                full_text = li.get_text(" ", strip=True)
                role_candidate = full_text
                for s in strongs:
                    role_candidate = role_candidate.replace(s, "")
                role_candidate = role_candidate.strip(" :：\n\t.,-")

                # If the li has a non-empty role label, latch it
                if role_candidate:
                    last_role = role_candidate

                if not strongs:
                    # Pure role-label row (e.g. 'בימוי ועיבוד:' on its own) — skip
                    continue

                role = last_role
                value = ", ".join(strongs)

                if block_kind.startswith("יוצרים"):
                    if role:
                        if role in creators:
                            # Append additional creators under the same role
                            existing = creators[role]
                            for s in strongs:
                                if s and s not in existing:
                                    existing = f"{existing}, {s}"
                            creators[role] = existing
                        else:
                            creators[role] = value
                elif block_kind.startswith("שחקנים"):
                    for name in strongs:
                        for piece in re.split(r"\s*,\s*", name):
                            piece = piece.strip(" .,-")
                            if 1 < len(piece) < 80 and piece not in cast_seen:
                                cast_seen.add(piece)
                                cast.append(piece)

        return creators, cast[:8]

    # -- duration -------------------------------------------------------
    @staticmethod
    def _extract_duration(text: str) -> int | None:
        if not text:
            return None
        m = re.search(r"(?:משך|אורך)\s+ההצגה\s*[:：]?\s*(?:כ-?\s*)?(\d{2,3})\s*דק", text)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        word_hours = {"שעה": 1, "כשעה": 1, "שעתיים": 2, "כשעתיים": 2}
        word_mins = {
            "וחצי": 30, "ורבע": 15, "ושלושת רבעי": 45,
            "ועשר": 10, "ועשרים": 20, "וחמישים": 50, "וארבעים": 40, "ושלושים": 30,
        }
        m = re.search(r"(?:משך|אורך)\s+ההצגה\s*[:：]?\s*([^\n,.]{0,80})", text)
        if m:
            phrase = m.group(1)
            hours = 0
            mins = 0
            for w, h in word_hours.items():
                if w in phrase:
                    hours = h
                    break
            for w, mn in word_mins.items():
                if w in phrase:
                    mins = mn
                    break
            if hours or mins:
                return hours * 60 + mins
        return None

    # -- poster ---------------------------------------------------------
    @staticmethod
    def _extract_poster(soup, html: str, url: str) -> str:
        """Build a clean CDN URL.

        Gesher's og:image is often double-prefixed (http://www.gesher-theatre.co.il+https://...);
        we strip the bad prefix and prefer the canonical
        https://1407132350.rsc.cdn77.org/Warehouse/content/pics/pic_<id>_a.webp.
        """
        cid = ""
        try:
            cid = parse_qs(urlparse(url).query).get("ContentID", [""])[0].strip()
        except Exception:
            pass

        # 1. Direct CDN guess based on ContentID — most reliable
        if cid:
            return f"https://1407132350.rsc.cdn77.org/Warehouse/content/pics/pic_{cid}_a.webp"

        # 2. Recover from og:image
        og = soup.select_one('meta[property="og:image"]')
        if og:
            content = (og.get("content") or "").strip()
            m = CDN_RECOVER_RE.search(content)
            if m:
                return m.group(0)

        # 3. Find any pic_<id>_a.webp in the page HTML
        m = CDN_RECOVER_RE.search(html or "")
        if m:
            return m.group(0)
        return ""

    # -- performances ---------------------------------------------------
    @staticmethod
    def _extract_performances(soup) -> list[datetime]:
        performances: list[datetime] = []
        now = datetime.now()
        # Primary: clean ul.subtitle-mode > li with span.fullDate / span.DateTime
        items = soup.select("ul.subtitle-mode li, .leftShowsContainer li")
        for li in items:
            full = li.select_one("span.fullDate")
            time_el = li.select_one("span.DateTime")
            if full:
                m = FULL_DATE_RE.search(full.get_text(strip=True))
                if not m:
                    continue
                d, mo, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                h, mi = 19, 30  # safe default — most evening shows; will be overridden
                if time_el:
                    tm = TIME_RE.search(time_el.get_text(strip=True))
                    if tm:
                        h, mi = int(tm.group(1)), int(tm.group(2))
                try:
                    dt = datetime(year, mo, d, h, mi)
                except ValueError:
                    continue
                performances.append(dt)
                continue

            # Fallback: parse from li text — accept either DD/MM/YYYY or DD.MM
            text = li.get_text(" ", strip=True)
            tm = TIME_RE.search(text)
            if not tm:
                continue
            h, mi = int(tm.group(1)), int(tm.group(2))
            m = FULL_DATE_RE.search(text)
            if m:
                d, mo, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                m2 = SHORT_DATE_RE.search(text)
                if not m2:
                    continue
                d, mo = int(m2.group(1)), int(m2.group(2))
                year = now.year
                try:
                    dt = datetime(year, mo, d, h, mi)
                except ValueError:
                    continue
                if dt < now - timedelta(days=30):
                    dt = dt.replace(year=year + 1)
                performances.append(dt)
                continue
            try:
                dt = datetime(year, mo, d, h, mi)
            except ValueError:
                continue
            performances.append(dt)

        # Final filter: drop obviously-bogus values and dedupe
        cleaned = sorted({p for p in performances if 2024 <= p.year <= now.year + 3})
        return cleaned
