from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .models import Show


SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    stable_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    venue TEXT NOT NULL,
    city TEXT,
    performances_json TEXT,
    description TEXT,
    performers_json TEXT,
    director TEXT,
    duration_minutes INTEGER,
    genre TEXT,
    price_min INTEGER,
    price_max INTEGER,
    poster_url TEXT,
    tickets_opened_on TEXT,    -- ISO date, scraper-provided (e.g. JSON-LD datePublished)
    sub_genre TEXT,            -- normalized sub-genre (Wikidata/MusicBrainz/regex)
    first_seen TEXT NOT NULL,  -- ISO date
    last_seen TEXT NOT NULL    -- ISO date
);
CREATE INDEX IF NOT EXISTS idx_shows_first_seen ON shows(first_seen);
CREATE INDEX IF NOT EXISTS idx_shows_last_seen ON shows(last_seen);

CREATE TABLE IF NOT EXISTS perf_count_history (
    stable_id TEXT NOT NULL,
    seen_on TEXT NOT NULL,
    upcoming_count INTEGER NOT NULL,
    PRIMARY KEY (stable_id, seen_on)
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Idempotent migration: add sub_genre column if missing (safe no-op otherwise)
        try:
            self._conn.execute("ALTER TABLE shows ADD COLUMN sub_genre TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass   # column already exists

    def close(self) -> None:
        self._conn.close()

    def update_sub_genre(self, stable_id: str, sub_genre: str) -> None:
        self._conn.execute(
            "UPDATE shows SET sub_genre = ? WHERE stable_id = ?",
            (sub_genre, stable_id),
        )
        self._conn.commit()

    def shows_needing_sub_genre(self, today: date | None = None) -> list[Show]:
        """Return active shows that don't have a sub_genre yet."""
        today = today or date.today()
        cur = self._conn.execute(
            """
            SELECT * FROM shows
            WHERE (sub_genre IS NULL OR sub_genre = '')
              AND julianday('now') - julianday(last_seen) <= 2
            """
        )
        return [self._row_to_show(r) for r in cur.fetchall()]

    def previous_perf_count(self, stable_id: str, before: date) -> int | None:
        """Most recent recorded upcoming_count strictly BEFORE `before` date."""
        cur = self._conn.execute(
            """
            SELECT upcoming_count FROM perf_count_history
            WHERE stable_id = ? AND seen_on < ?
            ORDER BY seen_on DESC LIMIT 1
            """,
            (stable_id, before.isoformat()),
        )
        row = cur.fetchone()
        return row["upcoming_count"] if row else None

    def record_perf_count(self, stable_id: str, when: date, count: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO perf_count_history (stable_id, seen_on, upcoming_count) VALUES (?, ?, ?)",
            (stable_id, when.isoformat(), count),
        )
        self._conn.commit()

    def upsert(self, show: Show, today: date | None = None) -> Show:
        """
        Insert show if new, or update mutable fields if already known.
        Sets first_seen on first insert; last_seen on every call.
        Returns the show with first_seen/last_seen populated.
        """
        today = today or date.today()
        cur = self._conn.execute(
            "SELECT first_seen FROM shows WHERE stable_id = ?", (show.stable_id,)
        )
        row = cur.fetchone()
        if row is None:
            first_seen = today
            self._conn.execute(
                """
                INSERT INTO shows (
                    stable_id, source, source_id, url, title, venue, city,
                    performances_json, description, performers_json, director,
                    duration_minutes, genre, price_min, price_max, poster_url,
                    tickets_opened_on, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    show.stable_id,
                    show.source,
                    show.source_id,
                    show.url,
                    show.title,
                    show.venue,
                    show.city,
                    json.dumps([p.isoformat() for p in show.performances]),
                    show.description,
                    json.dumps(show.performers, ensure_ascii=False),
                    show.director,
                    show.duration_minutes,
                    show.genre,
                    show.price_min,
                    show.price_max,
                    show.poster_url,
                    show.tickets_opened_on.isoformat() if show.tickets_opened_on else None,
                    first_seen.isoformat(),
                    today.isoformat(),
                ),
            )
        else:
            first_seen = date.fromisoformat(row["first_seen"])
            self._conn.execute(
                """
                UPDATE shows SET
                    url = ?, title = ?, venue = ?, city = ?,
                    performances_json = ?, description = ?, performers_json = ?,
                    director = ?, duration_minutes = ?, genre = ?,
                    price_min = ?, price_max = ?, poster_url = ?,
                    tickets_opened_on = COALESCE(?, tickets_opened_on),
                    last_seen = ?
                WHERE stable_id = ?
                """,
                (
                    show.url,
                    show.title,
                    show.venue,
                    show.city,
                    json.dumps([p.isoformat() for p in show.performances]),
                    show.description,
                    json.dumps(show.performers, ensure_ascii=False),
                    show.director,
                    show.duration_minutes,
                    show.genre,
                    show.price_min,
                    show.price_max,
                    show.poster_url,
                    show.tickets_opened_on.isoformat() if show.tickets_opened_on else None,
                    today.isoformat(),
                    show.stable_id,
                ),
            )
        self._conn.commit()
        show.first_seen = first_seen
        show.last_seen = today
        # Surface persisted-only fields back onto the in-memory Show — these
        # are written by other code paths (notably sub_genre via the
        # classifier) and should NOT be re-derived from scraper output.
        cur = self._conn.execute(
            "SELECT tickets_opened_on, sub_genre FROM shows WHERE stable_id = ?",
            (show.stable_id,),
        )
        r = cur.fetchone()
        if r:
            if show.tickets_opened_on is None and r["tickets_opened_on"]:
                show.tickets_opened_on = date.fromisoformat(r["tickets_opened_on"])
            if not show.sub_genre and r["sub_genre"]:
                show.sub_genre = r["sub_genre"]
        return show

    def all_active(self, seen_within_days: int = 14) -> list[Show]:
        """Return all shows seen at least once in the last N days."""
        cur = self._conn.execute(
            """
            SELECT * FROM shows
            WHERE julianday('now') - julianday(last_seen) <= ?
            ORDER BY first_seen DESC
            """,
            (seen_within_days,),
        )
        return [self._row_to_show(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_show(row: sqlite3.Row) -> Show:
        perfs_raw = json.loads(row["performances_json"] or "[]")
        return Show(
            source=row["source"],
            source_id=row["source_id"],
            url=row["url"],
            title=row["title"],
            venue=row["venue"],
            city=row["city"] or "",
            performances=[datetime.fromisoformat(p) for p in perfs_raw],
            description=row["description"] or "",
            performers=json.loads(row["performers_json"] or "[]"),
            director=row["director"] or "",
            duration_minutes=row["duration_minutes"],
            genre=row["genre"] or "",
            price_min=row["price_min"],
            price_max=row["price_max"],
            poster_url=row["poster_url"] or "",
            sub_genre=row["sub_genre"] if "sub_genre" in row.keys() and row["sub_genre"] else "",
            tickets_opened_on=date.fromisoformat(row["tickets_opened_on"]) if row["tickets_opened_on"] else None,
            first_seen=date.fromisoformat(row["first_seen"]),
            last_seen=date.fromisoformat(row["last_seen"]),
        )

    def upsert_many(self, shows: Iterable[Show]) -> list[Show]:
        return [self.upsert(s) for s in shows]
