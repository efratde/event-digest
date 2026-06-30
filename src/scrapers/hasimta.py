"""
Scraper for HaSimta Theater — small intimate theater in old Jaffa.

Listing page: https://hasimta.com/לוח-אירועים/
The site uses WordPress + Elementor + JetEngine; the calendar page renders one
`.jet-listing-grid__item` per individual upcoming performance, each carrying:
  - data-post-id="4-{post_id}-{n}"  → individual occurrence (n is 0-indexed)
  - <a href=".../shows/{slug}/">    → link to the show detail page
  - text "{Hebrew Month} {DD}, {YYYY} {HH}:{MM}"

We group performance rows by show URL, then enrich each unique show by fetching
its detail page (description, credits, poster, og:updated_time as the
"tickets opened" proxy since hasimta.com does not emit JSON-LD `datePublished`).

Each show detail page contains the show body followed by a "הצגות נוספות"
("more shows") sidebar that repeats credits for unrelated shows. We truncate the
text at the first occurrence of that heading before mining credits.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterable

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper


LISTING_URL = "https://hasimta.com/%d7%9c%d7%95%d7%97-%d7%90%d7%99%d7%a8%d7%95%d7%a2%d7%99%d7%9d/"

HEBREW_MONTHS = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "מארס": 3, "אפריל": 4,
    "מאי": 5, "יוני": 6, "יולי": 7, "אוגוסט": 8,
    "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}
DATE_RE = re.compile(
    r"(ינואר|פברואר|מרץ|מארס|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר)"
    r"\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})"
)
INDIV_PID_RE = re.compile(r"^4-\d+-\d+$")
MORE_SHOWS_HEADING = "הצגות נוספות"


class HasimtaScraper(Scraper):
    source_id = "hasimta"
    source_name = "תיאטרון הסמטה"
    venue = "תיאטרון הסמטה"
    city = "יפו"

    def fetch_shows(self) -> Iterable[Show]:
        listing = self.get(LISTING_URL)
        soup = BeautifulSoup(listing.text, "lxml")

        items = soup.select(".jet-listing-grid__item")
        # Keep only individual occurrence items (4-XXX-N) — the bare numeric
        # post IDs are aggregator wrappers that duplicate the same data.
        indiv = [it for it in items if INDIV_PID_RE.match(it.get("data-post-id", ""))]
        self.log.info("HaSimta listing: %d individual performance rows", len(indiv))

        # Group by show URL
        grouped: dict[str, dict] = {}
        for it in indiv:
            show_link = it.find("a", href=lambda h: h and "/shows/" in h)
            if not show_link:
                continue
            url = show_link["href"].strip()
            txt = it.get_text(" ", strip=True)
            dt = self._parse_listing_datetime(txt)
            entry = grouped.setdefault(url, {"performances": []})
            if dt is not None:
                entry["performances"].append(dt)

        self.log.info("HaSimta unique shows: %d", len(grouped))

        for url, info in grouped.items():
            try:
                show = self._fetch_detail(url, info["performances"])
            except Exception as e:
                self.log.warning("Failed to fetch %s: %s", url, e)
                continue
            if show is None:
                continue
            yield show

    # -- internals -------------------------------------------------------
    @staticmethod
    def _parse_listing_datetime(text: str) -> datetime | None:
        m = DATE_RE.search(text)
        if not m:
            return None
        month_he, day, year, hh, mm = m.groups()
        month = HEBREW_MONTHS.get(month_he)
        if not month:
            return None
        try:
            return datetime(int(year), month, int(day), int(hh), int(mm))
        except ValueError:
            return None

    def _fetch_detail(self, url: str, listing_performances: list[datetime]) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Title — strip the suffix " - תאטרון הסימטה" added by the site
        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = (og_title.get("content") or "").strip()
            title = re.sub(r"\s*-\s*תא?י?טרון הסימטה\s*$", "", title).strip()
        if not title:
            h1 = soup.select_one("h1")
            if h1:
                title = h1.get_text(strip=True)
        if not title:
            return None

        # Walk widgets in DOM order until the "הצגות נוספות" marker — anything
        # after that marker is a sidebar of unrelated shows.
        own_widgets = self._collect_own_widgets(soup)

        # Description — drawn from the show's own text-editor paragraphs only.
        description = self._extract_description(own_widgets)

        # Credits — labelled lines from BOTH the show's own paragraphs and any
        # jet-listing-grid credit boxes that sit on the page.
        body_text = " ".join(
            own_widgets["text_paragraphs"] + own_widgets["grid_items"]
        )
        director = self._extract_field(body_text, ["בימוי", "במאי", "כתיבה ובימוי"])
        performers = self._extract_performers(body_text)

        # Duration
        duration_minutes = self._extract_duration(body_text)

        # Poster — prefer og:image
        poster_url = ""
        og_img = soup.find("meta", property="og:image")
        if og_img:
            poster_url = (og_img.get("content") or "").strip()
        if not poster_url:
            img = soup.select_one(".elementor-widget-image img, picture img, img")
            if img:
                poster_url = img.get("src") or ""

        # Performances — from the calendar listing rows
        performances = sorted(set(listing_performances))

        # tickets_opened_on — JSON-LD datePublished if present, else
        # article:published_time meta
        tickets_opened_on = self._extract_published_date(soup)

        # source_id from /shows/{slug}/
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
    def _collect_own_widgets(soup) -> dict:
        """Walk widgets in DOM order; stop at the 'הצגות נוספות' marker.

        Returns a dict with:
          - `text_paragraphs`: paragraph strings from text-editor widgets only.
          - `grid_items`: text content of jet-listing-grid items that sit on
                          this show's page (used by some shows for credits).
        """
        text_paragraphs: list[str] = []
        grid_items: list[str] = []
        for w in soup.select(".elementor-widget"):
            wtext = w.get_text(" ", strip=True)
            if wtext == MORE_SHOWS_HEADING:
                break
            classes = w.get("class") or []
            if "elementor-widget-text-editor" in classes:
                # Treat <p>, <div dir="auto">, and <li> as paragraph blocks —
                # the editor stores narrative text in <div dir="auto"> on this
                # site, while link-only paragraphs use <p>.
                blocks = w.select("p, div[dir='auto'], li")
                seen: set[str] = set()
                if blocks:
                    for b in blocks:
                        # Skip parent containers that just wrap other blocks.
                        if b.find(["p", "div", "li"], dir=True):
                            continue
                        t = b.get_text(" ", strip=True)
                        if t and t not in seen:
                            seen.add(t)
                            text_paragraphs.append(t)
                else:
                    text_paragraphs.append(wtext)
            elif "elementor-widget-jet-listing-grid" in classes:
                items = w.select(".jet-listing-grid__item")
                for it in items:
                    t = it.get_text(" ", strip=True)
                    if t:
                        grid_items.append(t)
        return {
            "text_paragraphs": [p for p in text_paragraphs if p],
            "grid_items": grid_items,
        }

    @staticmethod
    def _extract_description(own_widgets: dict) -> str:
        """Pick the first substantial narrative paragraphs (skip credit lines)."""
        # A paragraph that contains *any* credit label is treated as credits, since
        # show pages often pack several labels into one comma-separated paragraph.
        CREDIT_LABEL = re.compile(
            r"(?<![\w֐-׿])(?:כתיבה|בימוי|מחזה|במאי|שחקנים|מבצעים|משתתפים|בכיכוב|"
            r"מוסיקה|מוזיקה|תאורה|תפאורה|תלבושות|כוריאוגרפיה|תרגום|הפקה|צילום|"
            r"עיצוב|דרמטורג|תודות|ע\.|מחזאי|ריקוד|וידאו|עריכה)"
            r"\s*(?:\([^)]*\))?\s*[:：–]"
        )
        # Notices like "לתשומת ליבכם:" or pure links/phrases.
        SKIP_PHRASES = re.compile(
            r"^(לתשומת\s+ליבכם|מן\s+הביקורות|ביקורות|ליצירת\s+קשר|לצפיה|לצפייה|לטריילר|לביקורת|קישור|להצטרפות|\*\*|\*)"
        )
        out: list[str] = []
        total = 0
        for p in own_widgets.get("text_paragraphs", []):
            if len(p) < 30:
                continue
            if CREDIT_LABEL.search(p):
                # If the paragraph also contains real narrative AFTER the
                # credits run (as in some bundled blocks), try to keep only
                # that tail. Cut after the last "label: value" group.
                tail = p
                last = 0
                for m in CREDIT_LABEL.finditer(p):
                    last = m.end()
                if last > 0:
                    # Walk past the credit value to the next sentence boundary.
                    after = p[last:]
                    # Heuristic: the credit value is comma-separated names; the
                    # next sentence starts when we see a period followed by a
                    # capital Hebrew word, or two consecutive spaces.
                    cut_m = re.search(r"\.\s+(?=[א-ת\"])", after)
                    if cut_m:
                        tail = after[cut_m.end():].strip()
                    else:
                        continue
                if not tail or len(tail) < 30:
                    continue
                p = tail
            if SKIP_PHRASES.match(p):
                continue
            out.append(p)
            total += len(p)
            if total >= 400:
                break
        return " ".join(out)[:600]

    # All credit labels we recognise — used to find label boundaries
    # while parsing free-form credits text.
    _CREDIT_LABELS = (
        "בימוי",
        "במאי",
        "כתיבה",
        "מחזה",
        "מחזאי",
        "שחקנים",
        "משחק",
        "מבצעים",
        "משתתפים",
        "בכיכובם",
        "בכיכוב",
        "מוסיקה",
        "מוזיקה",
        "תאורה",
        "תפאורה",
        "תלבושות",
        "כוריאוגרפיה",
        "תרגום",
        "הפקה",
        "צילום",
        "עיצוב",
        "דרמטורג",
        "תודות",
        "וידאו",
        "עריכה",
        "בהקלטה",
        "תפיסת",
        "ריקוד",
    )

    @classmethod
    def _parse_credits(cls, text: str) -> dict[str, str]:
        """Locate every '<label>[ <extra hebrew words>]: value' pair in the text.

        Returns a {label: value} mapping where the value is the text up to the
        next label match (or end of string). Tolerates compound forms like
        'בימוי ודרמטורגיה :' or 'שחקנים (לפי סדר הופעתם):'.
        """
        label_alt = "|".join(re.escape(l) for l in cls._CREDIT_LABELS)
        pattern = (
            rf"(?<![\w֐-׿])({label_alt})"
            rf"(?:\s+[א-ת']+){{0,3}}"  # optional extra Hebrew words
            rf"(?:\s*\([^)]*\))?"      # optional "(לפי סדר הופעתם)"
            rf"\s*[:：–\-]\s*"
        )
        positions = [
            (m.start(), m.end(), m.group(1), m.group())
            for m in re.finditer(pattern, text)
        ]
        out: dict[str, str] = {}
        for i, (start, end, label, full) in enumerate(positions):
            next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            raw = text[end:next_start]
            # Cut at obvious section dividers / dates that survived the slice.
            raw = re.split(r"\s+\d{1,2}/\d{1,2}/\d", raw, maxsplit=1)[0]
            raw = re.split(r"[\.]\s+[\"”]", raw, maxsplit=1)[0]
            # Cut at the first sentence boundary — credits values do not
            # contain prose. A period followed by space + Hebrew word starts
            # the surrounding narrative copy.
            raw = re.split(r"\.\s+(?=[א-ת])", raw, maxsplit=1)[0]
            # Cut on common narrative starters that hint at the show body.
            raw = re.split(r"\s+(?:שעת|בית\s+קפה|יום|בערב|במהלך|הפקה\s+של|המחזה|ההצגה)\s", raw, maxsplit=1)[0]
            raw = raw.strip(" .|,–-–")
            if raw and len(raw) < 250 and label not in out:
                out[label] = raw
                # Compound labels like "כתיבה ובימוי" → also key the value
                # under the secondary label so callers find it under "בימוי".
                if "ובימוי" in full and "בימוי" not in out:
                    out["בימוי"] = raw
                if "ובמאי" in full and "במאי" not in out:
                    out["במאי"] = raw
                if "ודרמטורגיה" in full and "דרמטורג" not in out:
                    out["דרמטורג"] = raw
        return out

    @classmethod
    def _extract_field(cls, text: str, labels: list[str]) -> str:
        credits = cls._parse_credits(text)
        for label in labels:
            if label in credits:
                return credits[label]
        return ""

    @classmethod
    def _extract_performers(cls, text: str) -> list[str]:
        credits = cls._parse_credits(text)
        for label in ["שחקנים", "משחק", "מבצעים", "משתתפים", "בכיכובם", "בכיכוב"]:
            raw = credits.get(label)
            if raw:
                # Strip parenthetical hints like "(לפי סדר הופעתם)" if any survived.
                raw = re.sub(r"\([^)]+\)", "", raw).strip()
                parts = re.split(r"[,•·]|\s+ו(?=\S)", raw)
                parts = [p.strip(" .") for p in parts if p.strip()]
                if parts:
                    return parts[:8]
        return []

    @staticmethod
    def _extract_duration(text: str) -> int | None:
        m = re.search(r"משך\s+(?:ה?הצגה|ה?מופע)\s*[:：]?\s*(?:כ-?\s*)?(\d{2,3})\s*דק", text)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d)\s*שע(?:ה|ות)\s*(?:ו-?\s*(\d{1,2})\s*דק)?", text)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            total = hours * 60 + mins
            if 30 <= total <= 300:
                return total
        return None

    @staticmethod
    def _extract_published_date(soup) -> date | None:
        # 1. JSON-LD datePublished, if any (HaSimta usually does not emit one)
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
        # 2. <meta property="article:published_time">
        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            try:
                return datetime.fromisoformat(meta["content"].replace("Z", "+00:00")).date()
            except ValueError:
                pass
        return None
