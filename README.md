# Event Digest — a daily personalized performance digest

> **Portfolio demonstration — synthetic data.** The taste profile, seed preferences, home location used for travel-distance, and pin/hide history in this demo are fictional and de-identified, and represent no real person or address. Any performers, venues, or shows are drawn from public event listings only and reflect no real individual's private preferences. This is a portfolio piece, not a real recommendation service.

A Python pipeline that scrapes 17 Israeli ticketing sites, filters by a taste profile (theater/music/standup, excluding classical/opera/ballet), and builds a daily HTML page with:

- Streaming-app-style cards, with a genre-colored border
- Sorting by "taste match" (based on preferences.yaml + pin/hide actions in localStorage)
- An alternative calendar view
- Travel cost from home (Nominatim, local)
- Automatic enrichment from Wikipedia + a links panel for Spotify/YouTube/review searches
- A hierarchical taste questionnaire + a modal preferences editor

## Running locally

```bash
uv sync
uv run python -m src.main
open output/digest.html
```

The `--no-web-enrich` option skips fetching descriptions/images from Wikipedia (saves about a minute on a local run).

## Architecture

```
src/
  main.py              orchestrator
  scrapers/            17 site-specific scrapers
  models.py            Show dataclass
  store.py             SQLite (first_seen, perf-count history)
  distance.py          OSM Nominatim geocoder + Haversine
  image_cache.py       local image cache (Referer hotlink fix)
  web_enrich.py        Wikipedia/DDG description+poster fallback
  enrich.py            search-URL builders (Spotify, YouTube, …)
  preferences.py       match_score + dislike filter
  render.py            Jinja2 → HTML

templates/
  digest.html          single-page app, vanilla JS, RTL Hebrew

data/
  curated_culture.yaml   curated lists of Israeli artists/actors/directors
  venues_candidates.yaml geocoded venue list (45-min radius from home)

config.yaml              sources, freshness windows, output path
preferences.yaml         seed prefs (overridden by browser localStorage)
```

## Adding a source (new site)

1. Write `src/scrapers/X.py` implementing `Scraper` from `base.py`
2. Register it in `src/scrapers/__init__.py` under `REGISTRY`
3. Add it to `config.yaml` under `sources:` (including `geocode_query`)

## Automated deployment (Claude Routine)

See the routine prompt in `.claude/routine_prompt.md`.
