from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import yaml

from .distance import compute_travel
from .preferences import Preferences
from .render import render_digest
from .scrapers import REGISTRY
from .store import Store
from .web_enrich import enrich_shows, needs_enrichment


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_config(path: str = "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the ticket-digest pipeline.")
    p.add_argument(
        "--no-web-enrich",
        action="store_true",
        help="Skip Wikipedia/DuckDuckGo enrichment for faster dev runs.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    log = logging.getLogger("main")

    cfg = load_config()
    db_path = "data/shows.db"
    db = Store(db_path)
    today = date.today()

    enabled_sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    log.info("Enabled sources: %s", [s["id"] for s in enabled_sources])

    all_shows = []
    perf_deltas: dict[str, int] = {}

    for src in enabled_sources:
        sid = src["id"]
        cls = REGISTRY.get(sid)
        if cls is None:
            log.warning("No scraper registered for '%s' — skipping", sid)
            continue
        log.info("--- scraping %s (%s) ---", sid, src.get("name"))
        try:
            with cls() as scraper:
                shows = list(scraper.fetch_shows())
            log.info("  → %d shows from %s", len(shows), sid)
            for show in shows:
                # Compute upcoming-perf count BEFORE upsert so we can compare to
                # the previous recorded count.
                upcoming_count = sum(
                    1 for p in show.performances if p.date() >= today
                )
                prev = db.previous_perf_count(show.stable_id, today)
                if prev is not None and upcoming_count != prev:
                    perf_deltas[show.stable_id] = upcoming_count - prev
                db.record_perf_count(show.stable_id, today, upcoming_count)

                stored = db.upsert(show, today=today)
                all_shows.append(stored)
        except Exception as e:
            log.exception("Scraper '%s' failed: %s", sid, e)

    log.info("Total shows after upsert: %d", len(all_shows))

    # ---------- Sub-genre classification (Wikidata + MusicBrainz) ----------
    # Any show that doesn't have a sub_genre yet gets classified once. Cached
    # forever in the DB. ~1-2 sec per show due to API rate limits.
    try:
        from .sub_genre import classify
        needing = [s for s in all_shows if not s.sub_genre]
        if needing:
            log.info("Classifying sub-genre for %d shows (cached afterwards)...", len(needing))
            classified = 0
            for show in needing:
                sg = classify(show)
                if sg:
                    show.sub_genre = sg
                    db.update_sub_genre(show.stable_id, sg)
                    classified += 1
            log.info("Sub-genre classification: %d/%d filled", classified, len(needing))
    except Exception as e:
        log.warning("sub-genre classification failed (non-fatal): %s", e)

    prefs = Preferences.from_file("preferences.yaml")
    log.info(
        "Preferences loaded — loved performers: %d, loved artists: %d, disliked: %d",
        len(prefs.loved_performers),
        len(prefs.loved_artists),
        len(prefs.disliked_performers) + len(prefs.disliked_artists) + len(prefs.disliked_genres),
    )

    # Web enrichment — fill in missing description/poster_url for shows whose
    # source pages don't expose that info (primarily Zappa concerts).
    if args.no_web_enrich:
        log.info("Skipping web enrichment (--no-web-enrich).")
    else:
        to_enrich = [s for s in all_shows if needs_enrichment(s)]
        log.info(
            "Web enrichment — %d/%d shows missing description or poster",
            len(to_enrich),
            len(all_shows),
        )
        if to_enrich:
            stats = enrich_shows(to_enrich, db_path=db_path)
            log.info(
                "Web enrichment done — filled desc=%d poster=%d "
                "by_source=%s errors=%d",
                stats["filled_description"],
                stats["filled_poster"],
                stats["by_source"],
                stats["errors"],
            )

    out_path = cfg.get("output", {}).get("html_path", "output/digest.html")
    fresh_days = cfg.get("freshness", {}).get("fresh_days", 7)
    warm_days = cfg.get("freshness", {}).get("warm_days", 21)
    home_address = cfg.get("recipient", {}).get("home_address") or cfg.get("recipient", {}).get("home_city", "תל אביב")
    home_origin = cfg.get("recipient", {}).get("home_city", "תל אביב")

    # Compute drive distance/time per source (geocoded once, then cached forever)
    venue_queries = {s["id"]: s.get("geocode_query") or f"{s.get('name')}, {s.get('city', '')}" for s in enabled_sources}
    log.info("Computing distances from %r ...", home_address)
    travel = compute_travel(home_address, list(venue_queries.values()), db_path=db_path)
    travel_by_source = {sid: travel.get(q, {}) for sid, q in venue_queries.items()}
    for sid, info in travel_by_source.items():
        if info.get("found"):
            log.info("  %s → %.1f km / ~%d min", sid, info["km"], info["minutes"])
        else:
            log.warning("  %s → distance unknown", sid)

    path = render_digest(
        all_shows,
        out_path,
        today=today,
        fresh_days=fresh_days,
        warm_days=warm_days,
        prefs=prefs,
        travel_by_source=travel_by_source,
        perf_deltas_by_id=perf_deltas,
        home_origin=home_origin,
    )
    log.info("Wrote digest → %s", path.resolve())
    print(f"\n✅ Digest ready: file://{path.resolve()}")

    db.close()

    # Best-effort: send the daily "what's new" email. Skips silently if
    # there are no new shows or if SMTP secrets aren't configured.
    try:
        from .notify import main as send_notify
        send_notify()
    except Exception as e:
        log.warning("notify failed (non-fatal): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
