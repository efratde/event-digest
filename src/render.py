from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .enrich import enrichment_for, performer_links
from .image_cache import LocalImageCache
from .models import Show
from .preferences import Preferences, is_disliked, match_score, match_tags, score_reasons


HEBREW_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEBREW_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def he_date(d: date | datetime) -> str:
    if isinstance(d, datetime):
        d = d.date()
    return f"{HEBREW_DAYS[d.weekday()]}, {d.day} {HEBREW_MONTHS[d.month - 1]} {d.year}"


def he_short_date(d: date | datetime) -> str:
    return f"{d.day} {HEBREW_MONTHS[d.month - 1]}"


def _build_calendar(cards: list[dict], today: date, num_weeks: int = 8) -> list[list[dict]]:
    """
    Build an N-week calendar grid as a list of weeks; each week is a list of 7
    days; each day = {date_iso, day_num, day_he, is_today, is_past, events}.
    Each event is a {stable_id, title, time, genre_class, venue} dict.
    Anchored to Sunday (Israeli convention) — week starts with the Sunday on or
    before today.
    """
    # Anchor to the Sunday on or before today
    days_since_sunday = (today.weekday() + 1) % 7  # weekday(): Mon=0..Sun=6 → days from prev Sunday
    week_start = today - timedelta(days=days_since_sunday)

    # Bucket events by ISO date for fast lookup
    events_by_date: dict[str, list[dict]] = {}
    for c in cards:
        for p in c.get("upcoming", []):
            # We need the actual ISO date — re-derive from next_perf_iso isn't enough
            # because a card has multiple performances. We'll do a second pass below.
            pass
    # Second pass: use the raw performances. We need to preserve the show ↔ datetime mapping.
    # Read from the original cards: we have `upcoming` which is [{date_he, time, weekday}],
    # plus `next_perf_iso`. But we need ALL ISO dates, not just the next one.
    # Re-build from the date_he + matching back is fragile. Easier: stash the iso list
    # on the view in _to_view. For now, do the cheap thing — bucket by next_perf_iso.
    # That under-counts repeat-night runs in the calendar, but covers a card's *first* date.
    # We'll improve this if it matters.
    for c in cards:
        # Add ALL performances that fit in the calendar window
        for iso_dt in c.get("performance_isos") or [c.get("next_perf_iso")]:
            if not iso_dt:
                continue
            iso_date = iso_dt[:10] if "T" in iso_dt else iso_dt
            time_str = iso_dt[11:16] if "T" in iso_dt else ""
            events_by_date.setdefault(iso_date, []).append({
                "stable_id": c["stable_id"],
                "title": c["title"],
                "time": time_str,
                "genre_class": c["genre_class"],
                "venue": c["venue"],
            })

    he_short_days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    weeks: list[list[dict]] = []
    for w in range(num_weeks):
        week: list[dict] = []
        for d in range(7):
            day = week_start + timedelta(days=w * 7 + d)
            iso = day.isoformat()
            evs = sorted(events_by_date.get(iso, []), key=lambda e: e["time"])
            week.append({
                "date_iso": iso,
                "day_num": day.day,
                "day_he": he_short_days[d],
                "is_today": (day == today),
                "is_past": (day < today),
                "events": evs,
            })
        weeks.append(week)
    return weeks


def _refine_genre(genre: str, title: str, description: str) -> str:
    """Override / refine the raw genre based on title/description heuristics.

    Catches things like class-reunion parties and tribute nights that scrapers
    tag generically as 'Music'. Returns the refined genre string.
    """
    g = (genre or "").strip()
    text = f"{title} {description}".lower()

    # Party / nightlife — explicit idioms
    party_markers = ["class reunion", " party", "party ", "dj set", "djs",
                     "rave", "80s", "90s", "80s night", "90s night",
                     "throwback night", "nostalgia night"]
    if any(m in text for m in party_markers):
        return "Parties"

    # Tribute show — its own sub-category, but still music
    tribute_markers = ["tribute to", "tribute show", "tribute", "celebrating ",
                       "salute to", "homage show"]
    if any(m in text for m in tribute_markers):
        return "Tribute"

    return g


def _genre_css_class(genre: str, source: str) -> str:
    """Map a show's genre to a CSS class name used for color-coded card borders.

    Source sites use inconsistent terminology — 'entertainment' often means
    stand-up, and 'music' has several interchangeable spellings. Normalize both.
    """
    g = (genre or "").strip().lower()
    THEATER = {"theater", "theatre", "musical", "musicals"}
    MUSIC = {"music", "israeli music", "jazz", "mizrahi", "pop", "rock",
             "mediterranean", "mediterranean pop"}
    COMEDY = {"standup", "stand-up", "stand up", "entertainment", "comedy", "cabaret"}
    DANCE = {"dance", "dancing"}
    KIDS = {"kids", "children"}

    if g in THEATER: return "g-theater"
    if g in MUSIC: return "g-music"
    if g in COMEDY: return "g-comedy"
    if g in DANCE: return "g-dance"
    if g in KIDS: return "g-other"   # we treat kids as "other" — usually filtered out anyway
    if g == "parties": return "g-party"
    if g == "tribute": return "g-music"   # tribute shows behave like music

    # Source-based fallback when genre is empty or unrecognized
    if source in {"zappa_tlv", "zappa_herzliya", "zappa_jlm", "barby", "shuni",
                  "caesarea", "reading3", "hangar11"}:
        return "g-music"
    if source in {"habima", "cameri", "lessin", "tzavta", "gesher",
                  "tmuna", "hasimta", "yoram_loewenstein"}:
        return "g-theater"
    if source == "suzanne_dellal":
        return "g-dance"
    return "g-other"


def _canonical_venue(venue: str) -> str:
    """Strip per-hall suffixes so multiple halls inside one venue collapse for the filter.

    Examples:
      'Tel Aviv Culture Palace - Zucker Hall' → 'Tel Aviv Culture Palace'
      'Givatayim Culture Hall - Almozlino Hall' → 'Givatayim Culture Hall'
    """
    v = (venue or "").strip()
    # Cut at " - " or " – " separator (which precedes the hall name)
    for sep in [" - ", " – ", " — "]:
        idx = v.find(sep)
        if idx > 0:
            return v[:idx].strip()
    return v


def _bucket(show: Show, today: date, fresh_days: int, warm_days: int) -> tuple[str, date | None, str]:
    """
    Returns (bucket, signal_date, signal_label).
    Prefers tickets_opened_on; falls back to first_seen.
    Bucket: 'fresh' | 'warm' | 'old'.
    """
    signal_date = show.tickets_opened_on or show.first_seen
    label = "On sale" if show.tickets_opened_on else "First seen"
    if not signal_date:
        return "old", None, label
    age = (today - signal_date).days
    if age <= fresh_days:
        return "fresh", signal_date, label
    if age <= warm_days:
        return "warm", signal_date, label
    return "old", signal_date, label


def _run_spread(performances: list[datetime], today: date) -> dict:
    """Group upcoming performances by month for a quick run-spread badge."""
    upcoming = [p for p in performances if p.date() >= today]
    if not upcoming:
        return {"total": 0, "this_month": 0, "next_month": 0, "horizon_days": 0}
    upcoming.sort()
    this_month = sum(1 for p in upcoming if p.month == today.month and p.year == today.year)
    next_month_date = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    next_month = sum(
        1 for p in upcoming
        if p.month == next_month_date.month and p.year == next_month_date.year
    )
    horizon_days = (upcoming[-1].date() - today).days
    return {
        "total": len(upcoming),
        "this_month": this_month,
        "next_month": next_month,
        "horizon_days": horizon_days,
    }


def _to_view(
    show: Show,
    today: date,
    bucket: str,
    signal_date: date | None,
    signal_label: str,
    perf_delta: int | None,
    travel_info: dict,
    prefs: Preferences,
    home_origin: str,
) -> dict:
    upcoming = []
    for p in show.performances:
        if p.date() >= today:
            upcoming.append({
                "date_he": he_short_date(p),
                "time": p.strftime("%H:%M"),
                "weekday": HEBREW_DAYS[p.weekday()],
            })
    refined_genre = _refine_genre(show.genre, show.title, show.description)
    return {
        "stable_id": show.stable_id,
        "source": show.source,
        "title": show.title,
        "venue": show.venue,
        "venue_canonical": _canonical_venue(show.venue),
        "city": show.city,
        "url": show.url,
        "description": show.description,
        "performers": show.performers,
        "performers_str": "|".join(show.performers),  # for client-side scoring
        "performer_chips": performer_links(show),
        "director": show.director,
        "match_score": match_score(show, prefs),
        "score_reasons": score_reasons(show, prefs),
        "duration_minutes": show.duration_minutes,
        "genre": refined_genre,
        "genre_class": _genre_css_class(refined_genre, show.source),
        "sub_genre": show.sub_genre,
        "poster_url": show.poster_url,
        "freshness": bucket,
        "signal_date_he": he_short_date(signal_date) if signal_date else "",
        "signal_label": signal_label,
        "upcoming": upcoming,
        "next_perf_iso": (
            min(p for p in show.performances if p.date() >= today).date().isoformat()
            if any(p.date() >= today for p in show.performances) else ""
        ),
        "performance_isos": [
            p.isoformat(timespec="minutes")
            for p in sorted(show.performances)
            if p.date() >= today
        ],
        "spread": _run_spread(show.performances, today),
        "perf_delta": perf_delta,        # negative = some performances were removed
        "travel_minutes": travel_info.get("minutes"),
        "travel_km": travel_info.get("km"),
        "enrichment": enrichment_for(show, home_origin=home_origin),
        "taste_tags": match_tags(show, prefs),
    }


def render_digest(
    shows: Iterable[Show],
    out_path: str | Path,
    *,
    today: date | None = None,
    fresh_days: int = 7,
    warm_days: int = 21,
    prefs: Preferences | None = None,
    travel_by_source: dict[str, dict] | None = None,
    perf_deltas_by_id: dict[str, int] | None = None,
    home_origin: str = "Tel Aviv",
    db_path: str = "data/shows.db",
) -> Path:
    today = today or date.today()
    prefs = prefs or Preferences()
    travel_by_source = travel_by_source or {}
    perf_deltas_by_id = perf_deltas_by_id or {}

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image_cache = LocalImageCache(
        output_dir=out_path.parent / "images",
        db_path=db_path,
    )

    cards: list[dict] = []
    sources_used: set[str] = set()
    canonical_venues: set[str] = set()   # for the filter dropdown (collapsed halls)
    genre_classes_used: set[str] = set()
    sub_genres_used: set[str] = set()

    for show in shows:
        if is_disliked(show, prefs):
            continue
        # Skip shows with no upcoming performances at all — including the case
        # where the scraper returned an empty performances list (e.g. Habima's
        # "Behind the Magic" backstage tour before any tour dates are scheduled).
        if not show.performances or not any(p.date() >= today for p in show.performances):
            continue
        bucket, signal_date, signal_label = _bucket(show, today, fresh_days, warm_days)
        view = _to_view(
            show, today, bucket, signal_date, signal_label,
            perf_deltas_by_id.get(show.stable_id),
            travel_by_source.get(show.source, {}),
            prefs, home_origin,
        )
        # Replace remote poster URL with locally-cached path (fixes hotlink-blocked sites).
        if view["poster_url"]:
            local = image_cache.fetch(view["poster_url"])
            if local:
                view["poster_url"] = local
        cards.append(view)
        sources_used.add(show.source)
        canonical_venues.add(_canonical_venue(show.venue))
        genre_classes_used.add(view["genre_class"])
        if view.get("sub_genre"):
            sub_genres_used.add(view["sub_genre"])

    image_cache.close()

    # Default sort: nearest upcoming performance first, with loved items pinned ahead
    def _default_sort(v: dict):
        loved = any(t["kind"] == "love" for t in v["taste_tags"])
        return (0 if loved else 1, v["next_perf_iso"] or "zzzz")
    cards.sort(key=_default_sort)

    # Genre filter options — only show categories that have at least one show
    genre_class_labels = {
        "g-theater": "Theater",
        "g-music":   "Music",
        "g-comedy":  "Stand-up",
        "g-dance":   "Dance",
        "g-party":   "Parties",
        "g-other":   "Other",
    }
    genre_options = [
        {"class": gc, "label": genre_class_labels[gc]}
        for gc in ["g-theater", "g-music", "g-comedy", "g-dance", "g-party", "g-other"]
        if gc in genre_classes_used
    ]

    # Build filter facets for the UI
    facets = {
        "venues": sorted(canonical_venues),
        "genres": genre_options,
        "sub_genres": sorted(sub_genres_used),
        "sources": sorted(sources_used),
    }

    # Calendar grid — next 8 weeks starting from today's week (Sunday-anchored,
    # Israeli convention). Each cell = a date with a list of events on that date.
    calendar_weeks = _build_calendar(cards, today, num_weeks=8)

    # Load curated lists of Israeli artists/performers/directors/genres.
    # These are the PRIMARY suggestion source — meaningful and clean.
    curated_path = Path("data/curated_culture.yaml")
    curated: dict = {}
    if curated_path.exists():
        import yaml as _yaml
        curated = _yaml.safe_load(curated_path.read_text(encoding="utf-8")) or {}

    # Build SECONDARY suggestions from scraped data, but filter aggressively
    # so we don't surface show titles or compound headlines as "artists".
    def _looks_like_show_title(s: str) -> bool:
        if not s:
            return True
        if any(ch in s for ch in [":", "|", " - ", "–", "—", "(", ")", "[", "]", "/"]):
            return True
        # Has digits (e.g. "15 Years of Alon Eder", "1987")
        if any(ch.isdigit() for ch in s):
            return True
        # Has English words like Show, PARTY, LIVE, TRIBUTE
        if any(w in s.upper() for w in ["SHOW", "PARTY", "LIVE", "TRIBUTE", "BAND",
                                         "CONCERT", "CELEBRATING", "EXPERIENCE", "TOUR",
                                         "FEATURING", "FT.", "FT ", "VS.", "&"]):
            return True
        # Has narrative words that signal a show title rather than a name
        for word in ["show", "evening", "hit", "guests", "host", "hosting", "choir",
                     "tribute", "in honor of", "ensemble", "band", "group", "saint-", "Tzavta"]:
            if word in s:
                return True
        # Too long — real names are short
        if len(s) > 35:
            return True
        return False

    def _is_clean_genre(g: str) -> bool:
        # Keep only well-known short genre labels
        return g and len(g) < 25 and not _looks_like_show_title(g)

    scraped_performers: set[str] = set()
    scraped_directors: set[str] = set()
    scraped_artists: set[str] = set()
    scraped_genres: set[str] = set()
    music_sources = {"zappa_tlv", "zappa_herzliya", "barby", "shuni", "caesarea",
                     "reading3", "hangar11"}
    for show in shows:
        for p in show.performers:
            if not _looks_like_show_title(p):
                scraped_performers.add(p)
        if show.director and not _looks_like_show_title(show.director):
            scraped_directors.add(show.director)
        if show.genre and _is_clean_genre(show.genre):
            scraped_genres.add(show.genre)
        if show.source in music_sources and not _looks_like_show_title(show.title):
            scraped_artists.add(show.title)

    suggestions = {
        # Curated comes first, scraped supplements
        "curated": curated,
        "scraped": {
            "performers": sorted(scraped_performers),
            "directors": sorted(scraped_directors),
            "artists": sorted(scraped_artists),
            "genres": sorted(scraped_genres),
        },
    }

    # Seed preferences for first-time users (localStorage will then take over)
    prefs_seed = {
        "loved_performers": sorted(prefs.loved_performers),
        "loved_artists": sorted(prefs.loved_artists),
        "loved_directors": sorted(prefs.loved_directors),
        "loved_genres": sorted(prefs.loved_genres),
        "disliked_performers": sorted(prefs.disliked_performers),
        "disliked_artists": sorted(prefs.disliked_artists),
        "disliked_genres": sorted(prefs.disliked_genres),
    }

    template_dir = Path(__file__).parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("digest.html")
    html = template.render(
        today_he=he_date(today),
        today_iso=today.isoformat(),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        cards=cards,
        facets=facets,
        calendar_weeks=calendar_weeks,
        sources_used=sorted(sources_used),
        suggestions_json=json.dumps(suggestions, ensure_ascii=False),
        prefs_seed_json=json.dumps(prefs_seed, ensure_ascii=False),
    )

    out_path.write_text(html, encoding="utf-8")
    return out_path
