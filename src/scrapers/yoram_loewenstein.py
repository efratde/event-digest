"""
Scraper for Studio Yoram Loewenstein (Tel Aviv).

Studio Yoram Loewenstein is a small acting school + theater in the Hatikva
neighbourhood. They produce a handful of student-led productions each season.
Two web surfaces are useful:

  1. The WordPress site (https://www.studioact.co.il/) hosts an editorial
     archive of every production at /studio-shows/. Each show has a detail page
     with credits in a `<pre>` block (play / adaptation / direction / translation) and a cast
     list in a similar `<pre>` block. JSON-LD provides datePublished and
     og:image gives a usable poster.

  2. The smarticket booking portal (https://studioact.smarticket.co.il/) is the
     authoritative source for *upcoming* dates. Each currently-bookable show
     appears as a `.show_cube`. The detail page (/<slug>) holds the full
     schedule in a `<table>` of `<tr>` rows, each with an aria-label like
     "on Monday, 4 May 2026" and a time appended in the cell text.

We use smarticket as the primary source (only currently-bookable shows are
listed there — exactly what we want for the digest), then enrich with WP
metadata when we can match a show by title prefix.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterable
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from ..models import Show
from .base import Scraper

# NOTE: source-site text-matching literals were translated from the original Hebrew for this English demo.


SMART_BASE = "https://studioact.smarticket.co.il/"
WP_LISTING_URL = (
    "https://www.studioact.co.il/"
    "%d7%94%d7%a6%d7%92%d7%95%d7%aa-%d7%a1%d7%98%d7%95%d7%93%d7%99%d7%95/"
)

HEBREW_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
_MONTHS_PATTERN = "|".join(HEBREW_MONTHS.keys())
DATE_RE = re.compile(rf"(\d{{1,2}})\s+({_MONTHS_PATTERN})\s+(\d{{4}})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
PRICE_RE = re.compile(r"(\d{2,4})\s*to\s*(\d{2,4})\s*₪")


class YoramLoewensteinScraper(Scraper):
    source_id = "yoram_loewenstein"
    source_name = "Yoram Loewenstein Studio"
    venue = "Yoram Loewenstein Studio"
    city = "Tel Aviv"

    def fetch_shows(self) -> Iterable[Show]:
        # WP archive — used for enrichment (description / credits / poster).
        wp_index = self._build_wp_index()
        self.log.info("Yoram Loewenstein WP archive: %d shows indexed", len(wp_index))

        # smarticket — authoritative list of currently-bookable productions.
        try:
            home = self.get(SMART_BASE)
        except Exception as e:
            self.log.warning("smarticket portal unreachable: %s", e)
            return

        soup = BeautifulSoup(home.text, "lxml")
        cubes = soup.select(".show_cube")
        self.log.info("Yoram Loewenstein smarticket portal: %d cubes", len(cubes))

        seen: set[str] = set()
        for cube in cubes:
            link = cube.find("a", href=True)
            if not link:
                continue
            href = link["href"].strip()
            # Skip subscription / season-pass cards.
            if not href or "season" in href.lower() or "subscription" in unquote(href):
                continue
            url = urljoin(SMART_BASE, href)
            if url in seen:
                continue
            seen.add(url)

            try:
                show = self._fetch_smarticket_detail(url, wp_index)
            except Exception as e:
                self.log.warning("Failed to fetch smarticket %s: %s", url, e)
                continue
            if show is None:
                continue
            yield show

    # -- WP enrichment ---------------------------------------------------
    def _build_wp_index(self) -> dict[str, dict]:
        """Map {normalised-title-prefix: {url, title, poster_hint}}.

        We pull this once at the start of the run so each smarticket show can
        cheaply look up its matching editorial page.
        """
        out: dict[str, dict] = {}
        try:
            r = self.get(WP_LISTING_URL)
        except Exception as e:
            self.log.warning("WP archive unreachable: %s", e)
            return out

        soup = BeautifulSoup(r.text, "lxml")
        for art in soup.select("article.elementor-post"):
            link = art.find("a", href=True)
            h = art.find(["h2", "h3"])
            if not link or not h:
                continue
            url = link["href"].strip()
            title_full = h.get_text(strip=True)
            # Title format: "<NAME>- by <author> – class of <year>"
            # Strip anything after the first '-' or '–'.
            stem = re.split(r"[-–]", title_full, maxsplit=1)[0].strip()
            stem = stem.strip(" .,:")
            if not stem:
                continue
            img = art.find("img")
            poster_hint = ""
            if img:
                src = img.get("src", "") or img.get("data-src", "")
                # Strip the WordPress -326x94 size suffix to get the original.
                poster_hint = re.sub(r"-\d+x\d+(?=\.[a-z]+$)", "", src)
            key = self._norm(stem)
            # Don't overwrite — first occurrence wins (and the listing is
            # ordered newest-first, which is what we want).
            out.setdefault(key, {"url": url, "title": title_full, "poster_hint": poster_hint})
        return out

    @staticmethod
    def _norm(s: str) -> str:
        # Strip punctuation/whitespace for a loose match.
        return re.sub(r"[\s\-–•·\.,:;\"']+", "", s)

    def _lookup_wp(self, title: str, wp_index: dict) -> dict | None:
        if not title or not wp_index:
            return None
        key = self._norm(title)
        if key in wp_index:
            return wp_index[key]
        # Try prefix match — smarticket title may be a substring of WP title.
        for k, v in wp_index.items():
            if k.startswith(key) or key.startswith(k):
                return v
        return None

    # -- smarticket detail ----------------------------------------------
    def _fetch_smarticket_detail(self, url: str, wp_index: dict) -> Show | None:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        title_el = soup.select_one("#show-title-header, h1")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            return None

        # Performances + venue + price (all live in the "all dates" table).
        performances, venue_label, price_min, price_max = self._extract_schedule(soup)

        venue = venue_label or self.venue

        # Short description from the "Additional details" section.
        description = self._extract_smarticket_description(soup)

        # Try to enrich from the WordPress page.
        wp_match = self._lookup_wp(title, wp_index)
        wp_director = ""
        wp_performers: list[str] = []
        wp_description = ""
        wp_poster = ""
        wp_tickets_opened: date | None = None
        wp_url = ""
        if wp_match:
            wp_url = wp_match.get("url", "")
            wp_poster = wp_match.get("poster_hint", "")
            try:
                meta = self._fetch_wp_detail(wp_url)
                wp_director = meta["director"]
                wp_performers = meta["performers"]
                wp_description = meta["description"]
                wp_poster = meta["poster"] or wp_poster
                wp_tickets_opened = meta["tickets_opened"]
            except Exception as e:
                self.log.debug("WP enrichment failed for %s: %s", title, e)

        # Prefer the longer description.
        final_description = wp_description if len(wp_description) > len(description) else description

        # Source ID — slug from the smarticket URL.
        slug = unquote(url.rstrip("/").rsplit("/", 1)[-1])
        source_id = re.sub(r"\s+", "-", slug).strip("-")

        return Show(
            source=self.source_id,
            source_id=source_id,
            url=wp_url or url,  # prefer the editorial page when we have one
            title=title,
            venue=venue,
            city=self.city,
            performances=performances,
            description=final_description,
            performers=wp_performers,
            director=wp_director,
            duration_minutes=None,  # not consistently published
            genre="theatre",
            price_min=price_min,
            price_max=price_max,
            poster_url=wp_poster,
            tickets_opened_on=wp_tickets_opened,
        )

    @staticmethod
    def _extract_schedule(soup) -> tuple[list[datetime], str, int | None, int | None]:
        """Pull every performance row, plus the venue label and price range.

        smarticket renders the same dates 2-3 times on the page (recent /
        upcoming / all). We dedupe and prefer the table that includes a price
        column ("all dates").
        """
        performances: dict[datetime, None] = {}
        venue_label = ""
        price_min: int | None = None
        price_max: int | None = None

        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            row_text = row.get_text(" ", strip=True)
            d_match = DATE_RE.search(row_text)
            t_match = TIME_RE.search(row_text)
            if not d_match or not t_match:
                continue
            day = int(d_match.group(1))
            month = HEBREW_MONTHS[d_match.group(2)]
            year = int(d_match.group(3))
            hour = int(t_match.group(1))
            minute = int(t_match.group(2))
            if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            try:
                dt = datetime(year, month, day, hour, minute)
            except ValueError:
                continue
            performances[dt] = None

            # Venue label is usually the second td (e.g. "Abbas" Hall - 19 Hanoch Street).
            if len(cells) >= 2 and not venue_label:
                v = cells[1].get_text(" ", strip=True)
                if v and "₪" not in v:
                    venue_label = v

            # Price column appears in the "all dates" table.
            pm = PRICE_RE.search(row_text)
            if pm:
                lo, hi = int(pm.group(1)), int(pm.group(2))
                if price_min is None or lo < price_min:
                    price_min = lo
                if price_max is None or hi > price_max:
                    price_max = hi

        return sorted(performances.keys()), venue_label, price_min, price_max

    @staticmethod
    def _extract_smarticket_description(soup) -> str:
        """Pull the prose under the 'Additional details' heading."""
        h = soup.find(
            lambda tag: tag.name in ("h2", "h3") and "Additional details" in tag.get_text()
        )
        if not h:
            return ""
        parent = h.find_parent(["section", "div"]) or h.parent
        if not parent:
            return ""
        text = parent.get_text("\n", strip=True)
        # Trim the heading itself.
        text = re.sub(r"^.*Additional details\s*", "", text, count=1)
        # Prefer the paragraph following 'Synopsis:' if present.
        m = re.search(r"Synopsis[:：]\s*(.+)", text, re.S)
        if m:
            text = m.group(1)
        # Collapse whitespace and trim to ~600 chars.
        text = re.sub(r"\s+", " ", text).strip()
        return text[:600]

    # -- WP detail enrichment -------------------------------------------
    def _fetch_wp_detail(self, url: str) -> dict:
        """Pull director / cast / description / poster / publish-date from WP."""
        out = {
            "director": "",
            "performers": [],
            "description": "",
            "poster": "",
            "tickets_opened": None,
        }
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        # Credits — the studio uses a `<pre>` with bold labels (play / direction /
        # adaptation / translation). The cast appears in a separate `<pre>` block.
        credits, cast = self._parse_pre_blocks(soup)
        out["director"] = (
            credits.get("Direction")
            or credits.get("Director")
            or credits.get("Direction and translation")
            or ""
        )[:200]
        out["performers"] = cast[:8]

        # Description — the paragraph after "Synopsis" or simply the first long
        # paragraph in the post body.
        description = ""
        for p in soup.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if len(txt) > 80 and "smarticket" not in txt.lower() and "studioact" not in txt.lower():
                # Skip the licensing footer ("This production has been licensed...").
                if txt.startswith("This production"):
                    continue
                description = txt
                break
        # Strip the optional "Synopsis:" prefix.
        description = re.sub(r"^\s*Synopsis\s*[:：]?\s*", "", description)
        out["description"] = description[:600]

        # Poster — og:image is usually the show banner.
        og = soup.select_one('meta[property="og:image"]')
        if og:
            poster = (og.get("content") or "").strip()
            # Skip the site logo / icon files.
            if poster and not any(bad in poster.lower() for bad in ("logo-trans", "cropped-icon")):
                out["poster"] = poster

        # JSON-LD datePublished — the closest proxy to "tickets opened".
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
                        out["tickets_opened"] = datetime.fromisoformat(
                            dp.replace("Z", "+00:00")
                        ).date()
                        break
                    except (ValueError, AttributeError):
                        continue
            if out["tickets_opened"]:
                break

        return out

    @staticmethod
    def _parse_pre_blocks(soup) -> tuple[dict[str, str], list[str]]:
        """Return ({label: value}, [cast_names]).

        The studio's `<pre>` blocks come in two flavours, but the markup is
        line-based — the label and value can live on the same line *or* on
        consecutive lines (BeautifulSoup's `get_text("\n")` flattens the
        `<strong>label</strong> value` pattern into "label\nvalue" because the
        WordPress source breaks after each `<span>`).

        Credits block::

            Play:
            Euripides
            Direction:
            Udi Persi

        Cast block::

            Medea-
            Einat Brener
            Jason-
            Alon Bukobza

        We walk each block line-by-line and pair "<something ending in : or ->"
        with the next non-empty line.
        """
        credit_labels = (
            "Play", "Adaptation", "Translation", "Direction", "Director",
            "Adaptation and translation", "Direction and translation",
            "Choreography", "Music", "Original music",
            "Scenery", "Costumes", "Set design",
            "Lighting", "Artistic mentoring", "Dramaturgy",
            "Assistant production manager", "Production manager",
            "Movement design", "Video art", "Stills and trailer photography",
            "Set and costume design",
        )
        credits: dict[str, str] = {}
        cast: list[str] = []

        for pre in soup.find_all("pre"):
            text = pre.get_text("\n", strip=True)
            if not text:
                continue
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            local_credits: dict[str, str] = {}
            local_cast: list[str] = []

            i = 0
            while i < len(lines):
                line = lines[i]
                # Same-line "label: value"
                m = re.match(r"^(.{2,40}?)\s*[:：]\s*(.+)$", line)
                if m:
                    label = m.group(1).strip().rstrip(":：").strip()
                    value = m.group(2).strip().strip(".,;-")
                    if any(label.startswith(lbl) for lbl in credit_labels) and value:
                        local_credits[label] = value
                        i += 1
                        continue
                # Label-only line followed by a value line ("Play:" + "\n" + "Euripides")
                if line.endswith(":") or line.endswith("："):
                    label = line.rstrip(":：").strip()
                    if any(label.startswith(lbl) for lbl in credit_labels) and i + 1 < len(lines):
                        value = lines[i + 1].strip().strip(".,;-")
                        # Make sure the next line isn't itself a label.
                        if value and not value.endswith((":", "：", "-")):
                            local_credits[label] = value
                            i += 2
                            continue
                # Crew line where the dash sits inside the label
                # ("Video art-\nYaara Nirel"). Treat as a credit when the label
                # matches a known crew role.
                m_dash = re.match(r"^(.{1,40})[\-–]\s*$", line)
                if m_dash and i + 1 < len(lines):
                    label = m_dash.group(1).strip()
                    next_line = lines[i + 1].strip()
                    if any(label.startswith(lbl) for lbl in credit_labels):
                        value = next_line.strip(".,;-")
                        if value and not value.endswith((":", "：", "-", "–")):
                            local_credits[label] = value
                            i += 2
                            continue
                # Cast line: "<role>-" followed by actor name(s).
                if re.match(r"^.{1,40}[\-–]\s*$", line) and i + 1 < len(lines):
                    actor_line = lines[i + 1]
                    if not actor_line.endswith((":", "：", "-", "–")):
                        for piece in re.split(r"\s*/\s*", actor_line):
                            piece = piece.strip().strip(".,;")
                            if 1 < len(piece) < 60:
                                local_cast.append(piece)
                        i += 2
                        continue
                # Same-line "role- actor"
                m = re.match(r"^[^\-–]{1,40}[\-–]\s+(.+)$", line)
                if m:
                    actor_line = m.group(1).strip()
                    for piece in re.split(r"\s*/\s*", actor_line):
                        piece = piece.strip().strip(".,;")
                        if 1 < len(piece) < 60:
                            local_cast.append(piece)
                    i += 1
                    continue
                i += 1

            credits.update(local_credits)
            cast.extend(local_cast)

        # Dedupe cast preserving order.
        seen: set[str] = set()
        unique_cast: list[str] = []
        for name in cast:
            if name not in seen:
                seen.add(name)
                unique_cast.append(name)
        return credits, unique_cast
