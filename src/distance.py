"""
Distance + travel-time estimation without Google Maps.

- Geocodes the home address and each venue ONCE via OpenStreetMap Nominatim
  (free, no API key, requires a User-Agent and rate-limit of <=1 req/sec)
- Caches coordinates in SQLite (table: geocode_cache)
- Computes Haversine straight-line distance, multiplies by 1.3 for road distance
- Estimates driving time: 30 km/h for short urban hops, 60 km/h above 15 km

Accuracy: good enough to distinguish "5 minutes away" vs "45 minutes away".
Not accurate for live traffic — that needs Google Maps API.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import httpx


log = logging.getLogger("distance")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "DadTicketsDigest/1.0 (personal use; one geocoding call per venue)"

GEOCODE_DDL = """
CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    lat REAL,
    lon REAL,
    display_name TEXT,
    fetched_on TEXT NOT NULL,
    found INTEGER NOT NULL  -- 1 if found, 0 if not (negative cache)
);
"""


@dataclass
class GeoPoint:
    lat: float
    lon: float
    display_name: str = ""

    def __bool__(self) -> bool:
        return self.lat != 0.0 or self.lon != 0.0


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points, in km."""
    R = 6371.0
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def estimate_minutes(km: float) -> int:
    """Rough drive time estimate from straight-line distance."""
    road_km = km * 1.3  # straight-line → road approximation
    if road_km < 15:
        speed = 30  # urban / Tel Aviv traffic
    elif road_km < 50:
        speed = 60  # suburban arterial
    else:
        speed = 80  # intercity highway
    return max(1, int(round(road_km / speed * 60)))


class Geocoder:
    def __init__(self, db_path: str | Path = "data/shows.db", min_interval: float = 1.1):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(GEOCODE_DDL)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        self._min_interval = min_interval
        self._last_call_at = 0.0

    def close(self) -> None:
        self.client.close()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _from_cache(self, query: str) -> GeoPoint | None:
        cur = self.conn.execute(
            "SELECT lat, lon, display_name, found FROM geocode_cache WHERE query = ?",
            (query,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if not row["found"]:
            return GeoPoint(0.0, 0.0)  # negative cache hit
        return GeoPoint(row["lat"], row["lon"], row["display_name"] or "")

    def _save(self, query: str, point: GeoPoint | None) -> None:
        if point is None:
            self.conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (query, lat, lon, display_name, fetched_on, found) "
                "VALUES (?, NULL, NULL, NULL, ?, 0)",
                (query, date.today().isoformat()),
            )
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (query, lat, lon, display_name, fetched_on, found) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (query, point.lat, point.lon, point.display_name, date.today().isoformat()),
            )
        self.conn.commit()

    def geocode(self, query: str) -> GeoPoint | None:
        """Returns a GeoPoint (truthy) if found, GeoPoint(0,0) (falsy) for negative cache, or None on error.

        If the exact query fails, tries a small set of fallback variants
        (drop the word 'רחוב', drop the city, etc.) before giving up.
        """
        for variant in self._variants(query):
            cached = self._from_cache(variant)
            if cached is not None and bool(cached):
                return cached
            if cached is not None and not bool(cached):
                # Negative cache for this exact variant — try the next one
                continue
            point = self._geocode_one(variant)
            if point and bool(point):
                # Also cache the original query so we don't iterate variants next time
                if variant != query:
                    self._save(query, point)
                return point
        # All variants exhausted — negative-cache the original
        self._save(query, None)
        return GeoPoint(0.0, 0.0)

    @staticmethod
    def _variants(query: str) -> list[str]:
        """Generate alternate phrasings of the same query.

        IMPORTANT: do NOT drop the city suffix — that risks matching a street
        with the same name in another Israeli city (e.g. Da Vinci St in Haifa).
        Only safe transforms here.
        """
        q = query.strip()
        out = [q]
        # Drop "רחוב " prefix (Nominatim stores street names without it)
        if q.startswith("רחוב "):
            out.append(q[len("רחוב "):].strip())
        return out

    def _geocode_one(self, query: str) -> GeoPoint | None:
        cached = self._from_cache(query)
        if cached is not None:
            return cached

        # Rate-limit: Nominatim asks for ≤1 req/sec
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call_at = time.monotonic()

        try:
            r = self.client.get(
                NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "il",
                    "accept-language": "he",
                },
            )
            r.raise_for_status()
            results = r.json()
        except Exception as e:
            log.warning("Geocode failed for %r: %s", query, e)
            return None

        if not results:
            log.info("No geocode result for %r", query)
            self._save(query, None)
            return GeoPoint(0.0, 0.0)

        first = results[0]
        point = GeoPoint(
            lat=float(first["lat"]),
            lon=float(first["lon"]),
            display_name=first.get("display_name", "") or "",
        )
        self._save(query, point)
        log.info("Geocoded %r → (%.4f, %.4f)", query, point.lat, point.lon)
        return point


def compute_travel(home_address: str, venue_queries: Iterable[str], db_path: str = "data/shows.db") -> dict[str, dict]:
    """
    Returns {venue_query: {"km": float, "minutes": int, "found": bool}} for each venue.
    Geocodes once per venue (and once for home), caches in SQLite forever.
    """
    out: dict[str, dict] = {}
    with Geocoder(db_path) as g:
        home = g.geocode(home_address)
        if not home or not bool(home):
            log.error("Home address %r could not be geocoded — aborting distance computation", home_address)
            return {q: {"km": None, "minutes": None, "found": False} for q in venue_queries}

        for q in venue_queries:
            point = g.geocode(q)
            if not point or not bool(point):
                out[q] = {"km": None, "minutes": None, "found": False}
                continue
            km = haversine_km(home, point)
            out[q] = {"km": km, "minutes": estimate_minutes(km), "found": True}
    return out
