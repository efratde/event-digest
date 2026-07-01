"""
Web enrichment — fill in missing description / poster_url for shows whose
source pages don't expose that data (primarily Zappa concerts, where Akamai
blocks the per-event detail pages).

Strategy per show, in order, until one returns useful data:
  1. Hebrew Wikipedia REST summary (artist / title)
  2. English Wikipedia REST summary (artist / title)
  3. DuckDuckGo Instant Answer API (keyless, free)

All results are cached in SQLite (`web_enrich_cache` table) keyed by the
search query for 30 days, so re-runs don't hammer the APIs.

We never overwrite existing description / poster_url on the Show — this is
strictly fill-in-the-blanks.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

import httpx

from .models import Show


LOG = logging.getLogger("web_enrich")

USER_AGENT = (
    "EventDigest/0.1 (personal-use script; "
    "contact: event-digest@example.com) httpx"
)

CACHE_TTL_DAYS = 30
INTER_CALL_DELAY_SEC = 1.0  # between actual network calls

WIKI_HE = "https://he.wikipedia.org"
WIKI_EN = "https://en.wikipedia.org"
DDG_URL = "https://api.duckduckgo.com/"

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_enrich_cache (
    query TEXT PRIMARY KEY,
    fetched_on TEXT NOT NULL,
    source TEXT,
    summary TEXT,
    image_url TEXT
);
"""


# --------------------------------------------------------------------------
# Cache helpers
# --------------------------------------------------------------------------
def _open_cache(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(CACHE_SCHEMA)
    return conn


def _cache_get(
    conn: sqlite3.Connection, query: str
) -> Optional[dict]:
    row = conn.execute(
        "SELECT fetched_on, source, summary, image_url "
        "FROM web_enrich_cache WHERE query = ?",
        (query,),
    ).fetchone()
    if not row:
        return None
    try:
        fetched = date.fromisoformat(row["fetched_on"])
    except Exception:
        return None
    if (date.today() - fetched) > timedelta(days=CACHE_TTL_DAYS):
        return None
    return {
        "source": row["source"] or "none",
        "summary": row["summary"] or "",
        "image_url": row["image_url"] or "",
    }


def _cache_put(
    conn: sqlite3.Connection,
    query: str,
    source: str,
    summary: str,
    image_url: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO web_enrich_cache "
        "(query, fetched_on, source, summary, image_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            query,
            date.today().isoformat(),
            source,
            summary,
            image_url,
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Wikipedia / DDG fetchers
# --------------------------------------------------------------------------
def _wiki_summary(
    client: httpx.Client, lang_host: str, title: str
) -> Optional[dict]:
    """Hit /api/rest_v1/page/summary/{title}. Returns dict or None."""
    if not title.strip():
        return None
    encoded = quote(title.strip().replace(" ", "_"), safe="")
    url = f"{lang_host}/api/rest_v1/page/summary/{encoded}"
    try:
        r = client.get(url, timeout=15.0)
    except Exception as e:
        LOG.debug("wiki summary error %s: %s", url, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    # Disambiguation pages are not useful as a description.
    if data.get("type") == "disambiguation":
        return None
    extract = (data.get("extract") or "").strip()
    thumb = (data.get("thumbnail") or {}).get("source") or ""
    # Original (full-size) image when available
    original = (data.get("originalimage") or {}).get("source") or ""
    image = original or thumb
    if not extract and not image:
        return None
    return {"summary": extract, "image_url": image}


def _wiki_search_then_summary(
    client: httpx.Client, lang_host: str, query: str
) -> Optional[dict]:
    """
    Fall back to the MediaWiki search API to find the best matching page,
    then hit the summary endpoint for that page title.
    """
    if not query.strip():
        return None
    try:
        r = client.get(
            f"{lang_host}/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query.strip(),
                "format": "json",
                "srlimit": 3,
            },
            timeout=15.0,
        )
    except Exception as e:
        LOG.debug("wiki search error: %s", e)
        return None
    if r.status_code != 200:
        return None
    try:
        results = r.json().get("query", {}).get("search", [])
    except Exception:
        return None
    for hit in results:
        title = hit.get("title")
        if not title:
            continue
        out = _wiki_summary(client, lang_host, title)
        if out and (out["summary"] or out["image_url"]):
            return out
    return None


def _ddg_lookup(client: httpx.Client, query: str) -> Optional[dict]:
    """DuckDuckGo Instant Answer API — keyless, very tolerant."""
    if not query.strip():
        return None
    try:
        r = client.get(
            DDG_URL,
            params={
                "q": query.strip(),
                "format": "json",
                "no_html": "1",
                "no_redirect": "1",
                "skip_disambig": "1",
            },
            timeout=15.0,
        )
    except Exception as e:
        LOG.debug("ddg error: %s", e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    summary = (
        data.get("AbstractText")
        or data.get("Abstract")
        or ""
    ).strip()
    image = (data.get("Image") or "").strip()
    if image and image.startswith("/"):
        image = f"https://duckduckgo.com{image}"
    if not summary and not image:
        return None
    return {"summary": summary, "image_url": image}


# --------------------------------------------------------------------------
# Top-level lookup (with cache + multi-source fallback + retries)
# --------------------------------------------------------------------------
def _candidate_queries(show: Show) -> list[str]:
    """Build a small ordered list of queries to try."""
    seen: list[str] = []

    def add(q: str) -> None:
        q = (q or "").strip()
        if q and q not in seen:
            seen.append(q)

    if show.performers:
        add(show.performers[0])
    add(show.title)
    # If the artist name has a generic suffix, try a disambiguated form.
    # (Wikipedia frequently disambiguates performers as "X (singer)".)
    if show.performers and show.performers[0]:
        add(f"{show.performers[0]} (singer)")
        add(f"{show.performers[0]} (musician)")
    return seen


def _lookup(
    client: httpx.Client,
    cache: sqlite3.Connection,
    query: str,
    *,
    rate_limit: bool,
) -> dict:
    """
    Returns dict with keys: source, summary, image_url, cached(bool).
    Tries he-wiki direct, he-wiki search, en-wiki direct, en-wiki search, DDG.
    Result (even 'none') is cached.
    """
    cached = _cache_get(cache, query)
    if cached is not None:
        return {**cached, "cached": True}

    if rate_limit:
        time.sleep(INTER_CALL_DELAY_SEC)

    # 1. Hebrew Wikipedia, direct title
    res = _wiki_summary(client, WIKI_HE, query)
    if res and (res["summary"] or res["image_url"]):
        _cache_put(cache, query, "wikipedia_he", res["summary"], res["image_url"])
        return {"source": "wikipedia_he", **res, "cached": False}

    # 2. Hebrew Wikipedia, search-then-summary
    res = _wiki_search_then_summary(client, WIKI_HE, query)
    if res and (res["summary"] or res["image_url"]):
        _cache_put(cache, query, "wikipedia_he", res["summary"], res["image_url"])
        return {"source": "wikipedia_he", **res, "cached": False}

    # 3. English Wikipedia, direct
    res = _wiki_summary(client, WIKI_EN, query)
    if res and (res["summary"] or res["image_url"]):
        _cache_put(cache, query, "wikipedia_en", res["summary"], res["image_url"])
        return {"source": "wikipedia_en", **res, "cached": False}

    # 4. English Wikipedia, search
    res = _wiki_search_then_summary(client, WIKI_EN, query)
    if res and (res["summary"] or res["image_url"]):
        _cache_put(cache, query, "wikipedia_en", res["summary"], res["image_url"])
        return {"source": "wikipedia_en", **res, "cached": False}

    # 5. DuckDuckGo
    res = _ddg_lookup(client, query)
    if res and (res["summary"] or res["image_url"]):
        _cache_put(cache, query, "ddg", res["summary"], res["image_url"])
        return {"source": "ddg", **res, "cached": False}

    # Nothing — cache the negative result so we don't retry for 30 days.
    _cache_put(cache, query, "none", "", "")
    return {"source": "none", "summary": "", "image_url": "", "cached": False}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def needs_enrichment(show: Show) -> bool:
    return not (show.description or "").strip() or not (show.poster_url or "").strip()


def enrich_show(
    show: Show,
    db_path: str = "data/shows.db",
    *,
    client: httpx.Client | None = None,
    cache: sqlite3.Connection | None = None,
) -> Show:
    """
    Mutate `show` to fill in description / poster_url from web sources
    (Wikipedia first, DuckDuckGo as fallback). Returns the same Show.
    Existing values are preserved.
    """
    if not needs_enrichment(show):
        return show

    own_client = client is None
    own_cache = cache is None
    if client is None:
        client = httpx.Client(headers={"User-Agent": USER_AGENT})
    if cache is None:
        cache = _open_cache(db_path)

    try:
        any_call_made = False
        for q in _candidate_queries(show):
            res = _lookup(client, cache, q, rate_limit=any_call_made)
            # Mark "any call made" only when we actually hit the network.
            if not res["cached"]:
                any_call_made = True
            if res["source"] == "none":
                continue
            if not show.description and res["summary"]:
                show.description = res["summary"]
            if not show.poster_url and res["image_url"]:
                show.poster_url = res["image_url"]
            if not needs_enrichment(show):
                break
    finally:
        if own_client:
            client.close()
        if own_cache:
            cache.close()

    return show


def enrich_shows(
    shows: Iterable[Show],
    db_path: str = "data/shows.db",
    *,
    persist: bool = True,
) -> dict:
    """
    Enrich many shows. Returns a stats dict:
        {
            "considered": int,
            "filled_description": int,
            "filled_poster": int,
            "by_source": {"wikipedia_he": n, "wikipedia_en": n, "ddg": n, "none": n},
            "errors": int,
        }

    If `persist` is True, also writes the filled-in description / poster_url
    back to the `shows` table for any Show whose stable_id is found there.
    """
    stats = {
        "considered": 0,
        "filled_description": 0,
        "filled_poster": 0,
        "by_source": {
            "wikipedia_he": 0,
            "wikipedia_en": 0,
            "ddg": 0,
            "none": 0,
        },
        "errors": 0,
    }

    targets = [s for s in shows if needs_enrichment(s)]
    if not targets:
        return stats

    client = httpx.Client(headers={"User-Agent": USER_AGENT})
    cache = _open_cache(db_path)

    try:
        any_call_made = False
        for show in targets:
            stats["considered"] += 1
            try:
                had_desc = bool((show.description or "").strip())
                had_poster = bool((show.poster_url or "").strip())
                hit_source = "none"
                for q in _candidate_queries(show):
                    res = _lookup(
                        client, cache, q, rate_limit=any_call_made
                    )
                    if not res["cached"]:
                        any_call_made = True
                    if res["source"] == "none":
                        continue
                    if not show.description and res["summary"]:
                        show.description = res["summary"]
                    if not show.poster_url and res["image_url"]:
                        show.poster_url = res["image_url"]
                    hit_source = res["source"]
                    if not needs_enrichment(show):
                        break
                stats["by_source"][hit_source] = (
                    stats["by_source"].get(hit_source, 0) + 1
                )
                if not had_desc and (show.description or "").strip():
                    stats["filled_description"] += 1
                if not had_poster and (show.poster_url or "").strip():
                    stats["filled_poster"] += 1
            except Exception as e:
                LOG.warning("enrich failed for %s: %s", show.stable_id, e)
                stats["errors"] += 1

        if persist:
            _persist_enrichment(db_path, targets)
    finally:
        client.close()
        cache.close()

    return stats


def _persist_enrichment(db_path: str, shows: Iterable[Show]) -> None:
    """Write back description / poster_url for shows that exist in `shows` table."""
    p = Path(db_path)
    if not p.exists():
        return
    conn = sqlite3.connect(p)
    try:
        for show in shows:
            if not (show.description or show.poster_url):
                continue
            # Only fill empty cells in the row — never clobber existing data.
            conn.execute(
                """
                UPDATE shows
                SET description = CASE
                        WHEN description IS NULL OR description = ''
                            THEN ? ELSE description END,
                    poster_url  = CASE
                        WHEN poster_url IS NULL OR poster_url = ''
                            THEN ? ELSE poster_url END
                WHERE stable_id = ?
                """,
                (show.description or "", show.poster_url or "", show.stable_id),
            )
        conn.commit()
    finally:
        conn.close()
