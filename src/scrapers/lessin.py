"""
Scraper for Beit Lessin — תיאטרון בית ליסין (Tel Aviv).

The Hebrew listing page (https://www.lessin.co.il/הצגות/) hosts a swiper of
the current/upcoming productions; the secondary archive at
https://www.lessin.co.il/shows/ paginates through the full repertoire.
We collect show URLs from both, then visit each detail page.

Detail pages use a custom (non-Elementor) WordPress theme. Useful selectors:
  - .show_title h1               → show title
  - .show_title .text            → byline (מאת, תרגום, בימוי) — short summary
  - .show_expert .content        → free-form description (and "משך ההצגה")
  - .details_row                 → structured key/value credit rows. Each has
        ├── .detail              → label (מאת / בימוי / תרגום / משתתפים …)
        └── .dtail_answer        → value (one or more <a class="talent">)
  - .mainshow_list .mulrow       → one row per performance
        ├── .mu1.list (first)    → day-of-week letter (ש/א/ב…)
        ├── .mu1.list (second)   → DD.MM
        ├── .mu2.list            → HH:MM
        └── .mu3.list a[href]    → ticket link → lessin.pres.global/eWeb/event/{id}
  - #myImageurl[data-desk]       → wide hero image (poster)
  - meta[property="og:image"]    → fallback poster

Dates appear in DD.MM format (no year). We use the "if past, bump to next
year" logic from Habima.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


HEBREW_LISTING_URL = "https://www.lessin.co.il/%D7%94%D7%A6%D7%92%D7%95%D7%AA/"
ARCHIVE_BASE = "https://www.lessin.co.il/shows/"
ARCHIVE_MAX_PAGES = 5
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


class LessinScraper(Scraper):
    source_id = "lessin"
    source_name = "תיאטרון בית ליסין"
    venue = "בית ליסין"
    city = "תל אביב"

    def fetch_shows(self) -> Iterable[Show]:
        urls = self._collect_show_urls()
        self.log.info("Lessin: %d unique show URLs", len(urls))

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

        # Hebrew "הצגות" landing page (swiper of current shows + many links)
        try:
            r = self.get(HEBREW_LISTING_URL)
            urls.update(self._extract_show_urls(r.text))
        except Exception as e:
            self.log.warning("Lessin Hebrew listing failed: %s", e)

        # /shows/ archive (paginated)
        for page in range(1, ARCHIVE_MAX_PAGES + 1):
            page_url = ARCHIVE_BASE if page == 1 else f"{ARCHIVE_BASE}page/{page}/"
            try:
                r = self.get(page_url)
            except Exception:
                break
            new_urls = self._extract_show_urls(r.text)
            if not new_urls:
                break
            before = len(urls)
            urls.update(new_urls)
            if len(urls) == before:
                # nothing new — assume we've reached the end
                break

        return sorted(urls)

    @staticmethod
    def _extract_show_urls(html: str) -> set[str]:
        soup = BeautifulSoup(html, "lxml")
        out: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/shows/" not in href:
                continue
            if "/en/" in href:
                continue
            if "#" in href:
                continue
            # Skip the archive root and pagination
            if href.rstrip("/").endswith("/shows"):
                continue
            if "/shows/page/" in href:
                continue
            if not href.endswith("/"):
                continue
            out.add(href)
        return out

    # -- detail ----------------------------------------------------------
    def _fetch_detail(self, url: str) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Title
        title = ""
        title_el = soup.select_one(".show_title h1")
        if title_el:
            title = title_el.get_text(strip=True)
        if not title:
            t2 = soup.select_one("h1")
            if t2:
                title = t2.get_text(strip=True)
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            if og:
                title = (og.get("content") or "").split("-")[0].strip()
        if not title:
            return None

        # Description (long free-form blurb)
        desc_block = soup.select_one(".show_expert .content")
        full_desc_text = ""
        description = ""
        if desc_block:
            full_desc_text = desc_block.get_text("\n", strip=True)
            # Use the longer paragraphs (skip credit/byline lines)
            paras = [
                p.strip()
                for p in re.split(r"\n+", full_desc_text)
                if len(p.strip()) > 40
            ]
            description = " ".join(paras[:3])[:600]
        if not description:
            og = soup.select_one('meta[property="og:description"]')
            if og:
                description = (og.get("content") or "").strip()[:600]

        # Structured credit rows ("בימוי", "משתתפים", "מאת", ...)
        credits = self._parse_credit_rows(soup)
        director = credits.get("בימוי", "") or credits.get("במאי", "") or credits.get("במאית", "")
        performers = self._split_performers(
            credits.get("משתתפים")
            or credits.get("בכיכוב")
            or credits.get("שחקנים")
            or ""
        )
        # Fallback: pull credits from the short byline under the title
        if not director:
            byline = soup.select_one(".show_title .text")
            if byline:
                director = self._extract_field(byline.get_text("\n", strip=True), ["בימוי", "במאי"])
        if not performers and full_desc_text:
            performers = self._extract_performers(full_desc_text)

        # Duration
        duration_minutes = self._extract_duration(full_desc_text or soup.get_text(" ", strip=True))

        # Poster — Lessin uploads multiple variants per show (e.g. 1900x950
        # wide banner, 1000x1000 square, 800x1000 portrait). For our card grid
        # the portrait or square variants look best. Prefer them, then fall
        # back to og:image, then the wide hero, then anything.
        poster_url = self._pick_poster(r.text)
        if not poster_url:
            og = soup.select_one('meta[property="og:image"]')
            if og:
                poster_url = (og.get("content") or "").strip()
        if not poster_url:
            hero = soup.select_one("#myImageurl")
            if hero:
                poster_url = hero.get("data-desk") or hero.get("data-mob") or hero.get("src") or ""
        if not poster_url:
            img = soup.select_one(".show_banner img, img.attachment-large, img.wp-post-image")
            if img:
                poster_url = img.get("src") or ""

        # Performances
        performances = self._extract_performances(soup)

        # When did the page get published? Best proxy for "when did tickets open".
        tickets_opened_on = self._extract_tickets_opened(soup)

        # Source ID = slug
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
            genre="תיאטרון",
            poster_url=poster_url,
            tickets_opened_on=tickets_opened_on,
        )

    @staticmethod
    def _pick_poster(html: str) -> str:
        """Choose the most card-friendly Lessin image variant from the page HTML.

        Lessin uploads each show in multiple aspect ratios named e.g.
        '1900x950OR.jpg' (banner), '1000x1000OR.jpg' (square),
        '800x1000or.jpg' (portrait). We prefer portrait > square > anything-but-banner.
        """
        all_imgs = re.findall(
            r"https://www\.lessin\.co\.il/wp-content/uploads/[^\"'> ]+\.(?:jpg|jpeg|png|webp)",
            html,
            flags=re.IGNORECASE,
        )
        # Strip resize suffixes WordPress appends (e.g. -300x200)
        portrait_re = re.compile(r"800x1000", re.IGNORECASE)
        square_re = re.compile(r"1000x1000", re.IGNORECASE)
        banner_re = re.compile(r"1900x950|1200x", re.IGNORECASE)
        for u in all_imgs:
            if portrait_re.search(u):
                return u
        for u in all_imgs:
            if square_re.search(u):
                return u
        # Anything that's not a known-bad logo / favicon / wide banner
        for u in all_imgs:
            low = u.lower()
            if any(bad in low for bad in ("favicon", "flogo", "mizrahi", "logo")):
                continue
            if banner_re.search(u):
                continue
            return u
        return ""

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
                        return datetime.fromisoformat(dp.replace("Z", "+00:00")).date()
                    except (ValueError, AttributeError):
                        continue
        return None

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _parse_credit_rows(soup) -> dict[str, str]:
        """Each .details_row has a .detail (label) and .dtail_answer (value)."""
        out: dict[str, str] = {}
        for row in soup.select(".details_row"):
            label_el = row.select_one(".detail")
            value_el = row.select_one(".dtail_answer")
            if not label_el or not value_el:
                continue
            label = label_el.get_text(strip=True).rstrip(":：").strip()
            # Prefer talent links if present (they're the cleanest names)
            talents = [a.get_text(strip=True) for a in value_el.select("a.talent")]
            if talents:
                value = ", ".join(t for t in talents if t)
            else:
                value = value_el.get_text(" ", strip=True)
            value = value.strip(" ,;-")
            if label and value:
                out[label] = value
        return out

    @staticmethod
    def _split_performers(raw: str) -> list[str]:
        if not raw:
            return []
        parts = re.split(r"[,•·]|\s+ו(?=\S)", raw)
        parts = [p.strip(" .,-") for p in parts if p.strip()]
        parts = [p for p in parts if 1 < len(p) < 80]
        return parts[:8]

    @staticmethod
    def _extract_performances(soup) -> list[datetime]:
        """Each .mulrow has the day letter, DD.MM, HH:MM in separate cells."""
        performances: list[datetime] = []
        now = datetime.now()
        rows = soup.select(".mainshow_list .mulrow, .mulrow")
        for row in rows:
            text = row.get_text(" ", strip=True)
            d_match = DATE_RE.search(text)
            t_match = TIME_RE.search(text)
            if not d_match or not t_match:
                continue
            d, mo = int(d_match.group(1)), int(d_match.group(2))
            h, mi = int(t_match.group(1)), int(t_match.group(2))
            # Sanity check — reject obviously-bad values
            if not (1 <= d <= 31 and 1 <= mo <= 12 and 0 <= h <= 23 and 0 <= mi <= 59):
                continue
            year = now.year
            try:
                dt = datetime(year, mo, d, h, mi)
            except ValueError:
                continue
            if dt < now - timedelta(days=30):
                dt = dt.replace(year=year + 1)
            performances.append(dt)
        return sorted(set(performances))

    @staticmethod
    def _extract_field(text: str, labels: list[str]) -> str:
        """Pull a single value following any of the labels in flat text."""
        if not text:
            return ""
        for label in labels:
            # Match label followed by ":" or whitespace, then capture until end-of-line / 200 chars
            m = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n]+)", text)
            if m:
                value = m.group(1).strip()
                # Stop at a likely next-credit boundary
                value = re.split(r"\s{2,}|·|•", value)[0]
                return value.strip(" .,;-")[:200]
        return ""

    @staticmethod
    def _extract_performers(text: str) -> list[str]:
        if not text:
            return []
        for label in ["משתתפים", "בכיכוב", "שחקנים", "מבצעים", "השחקנים"]:
            m = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n]+)", text)
            if not m:
                continue
            raw = m.group(1).strip()
            parts = re.split(r"[,•·]|\s+ו(?=\S)", raw)
            parts = [p.strip(" .,-") for p in parts if p.strip()]
            parts = [p for p in parts if 1 < len(p) < 80]
            if parts:
                return parts[:8]
        return []

    @staticmethod
    def _extract_duration(text: str) -> int | None:
        if not text:
            return None
        # "משך ההצגה: כ-90 דקות"
        m = re.search(r"(?:משך|אורך)\s+ההצגה\s*[:：]?\s*(?:כ-?\s*)?(\d{2,3})\s*דק", text)
        if m:
            return int(m.group(1))
        # "משך ההצגה: כשעה ו-50 דקות" / "שעה וחצי" / "שעתיים"
        # First try numeric: "X שעות ו-Y דקות"
        m = re.search(r"(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            return hours * 60 + mins
        # Word-based hours
        word_hours = {"שעה": 1, "כשעה": 1, "שעתיים": 2, "כשעתיים": 2}
        word_mins = {
            "וחצי": 30, "ורבע": 15, "ושלושת רבעי": 45,
            "ועשר": 10, "ועשרים": 20, "וחמישים": 50, "וארבעים": 40, "ושלושים": 30,
        }
        m = re.search(r"משך ההצגה\s*[:：]?\s*([^\n,.]{0,80})", text)
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
