from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


@dataclass
class Show:
    # Identity
    source: str                    # e.g. "habima"
    source_id: str                 # site-specific show ID (or URL hash)
    url: str                       # canonical show / booking page

    # Display
    title: str
    venue: str                     # human-readable venue/hall name
    city: str = ""

    # Schedule — list of upcoming performance datetimes
    performances: list[datetime] = field(default_factory=list)

    # Editorial / metadata
    description: str = ""          # short blurb (1-3 sentences)
    performers: list[str] = field(default_factory=list)
    director: str = ""
    duration_minutes: Optional[int] = None
    genre: str = ""                # raw category from the source
    sub_genre: str = ""            # normalized sub-genre (Wikidata/MusicBrainz/regex)
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    poster_url: str = ""

    # On-sale date — extracted by the scraper from the source (e.g. JSON-LD datePublished).
    # Falls back to first_seen if not set. This is the *real* "tickets went on sale" signal.
    tickets_opened_on: Optional[date] = None

    # Tracking (set by the store, not the scraper)
    first_seen: Optional[date] = None
    last_seen: Optional[date] = None

    @property
    def stable_id(self) -> str:
        """Globally unique ID across all sources."""
        return f"{self.source}:{self.source_id}"

    @property
    def next_performance(self) -> Optional[datetime]:
        future = [p for p in self.performances if p >= datetime.now()]
        return min(future) if future else (min(self.performances) if self.performances else None)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Make datetimes JSON-friendly
        d["performances"] = [p.isoformat() for p in self.performances]
        if self.first_seen:
            d["first_seen"] = self.first_seen.isoformat()
        if self.last_seen:
            d["last_seen"] = self.last_seen.isoformat()
        return d
