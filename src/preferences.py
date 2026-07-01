"""
Personal taste matching — checks each show against preferences.yaml.
Returns a small set of tags that surface in the digest as ❤ badges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Show


@dataclass
class Preferences:
    loved_performers: set[str] = field(default_factory=set)
    loved_directors: set[str] = field(default_factory=set)
    loved_artists: set[str] = field(default_factory=set)
    loved_genres: set[str] = field(default_factory=set)
    disliked_performers: set[str] = field(default_factory=set)
    disliked_artists: set[str] = field(default_factory=set)
    disliked_genres: set[str] = field(default_factory=set)

    @classmethod
    def from_file(cls, path: str | Path) -> "Preferences":
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(
            loved_performers=set(data.get("loved_performers") or []),
            loved_directors=set(data.get("loved_directors") or []),
            loved_artists=set(data.get("loved_artists") or []),
            loved_genres=set(data.get("loved_genres") or []),
            disliked_performers=set(data.get("disliked_performers") or []),
            disliked_artists=set(data.get("disliked_artists") or []),
            disliked_genres=set(data.get("disliked_genres") or []),
        )


def match_tags(show: Show, prefs: Preferences) -> list[dict]:
    """
    Return a list of {label, kind} dicts where kind is 'love' or 'dislike'.
    Empty if nothing matches.
    """
    tags: list[dict] = []

    # Loved
    for p in show.performers:
        if p in prefs.loved_performers:
            tags.append({"label": f"❤ Loved actor: {p}", "kind": "love"})
        if p in prefs.loved_artists:
            tags.append({"label": f"❤ Loved artist: {p}", "kind": "love"})
    if show.director and show.director in prefs.loved_directors:
        tags.append({"label": f"❤ Loved director: {show.director}", "kind": "love"})
    # Title might match an artist (concert)
    if show.title in prefs.loved_artists:
        tags.append({"label": f"❤ Loved artist: {show.title}", "kind": "love"})
    if show.genre and show.genre in prefs.loved_genres:
        tags.append({"label": f"❤ Loved genre: {show.genre}", "kind": "love"})

    # Disliked (keep these visible so the user knows why something looks downgraded)
    for p in show.performers:
        if p in prefs.disliked_performers or p in prefs.disliked_artists:
            tags.append({"label": f"👎 Not into it: {p}", "kind": "dislike"})
    if show.title in prefs.disliked_artists:
        tags.append({"label": f"👎 Not into it: {show.title}", "kind": "dislike"})
    if show.genre and show.genre in prefs.disliked_genres:
        tags.append({"label": f"👎 Not into it: {show.genre}", "kind": "dislike"})

    return tags


def is_disliked(show: Show, prefs: Preferences) -> bool:
    """Hard filter — when set, the show is hidden from the digest entirely."""
    if show.genre and show.genre in prefs.disliked_genres:
        return True
    if show.title in prefs.disliked_artists:
        return True
    for p in show.performers:
        if p in prefs.disliked_performers or p in prefs.disliked_artists:
            return True
    return False


# -- Match scoring -----------------------------------------------------------
#
# Returns an integer 0-100 representing how strongly this show matches the user's
# declared taste in preferences.yaml. The client-side JS will further adjust
# this based on his actual pin/dismiss history (which lives in localStorage).

def match_score(show: Show, prefs: Preferences) -> int:
    """Compute a 0-100 match score from explicit preferences only."""
    score = 0
    matches: list[str] = []   # for the badge tooltip

    # Performers / artists
    for p in show.performers:
        if p in prefs.loved_performers:
            score += 30
            matches.append(f"Loved actor: {p}")
        if p in prefs.loved_artists:
            score += 30
            matches.append(f"Loved artist: {p}")
    if show.title in prefs.loved_artists:
        score += 35
        matches.append(f"Loved artist: {show.title}")

    # Director
    if show.director and show.director in prefs.loved_directors:
        score += 25
        matches.append(f"Loved director: {show.director}")

    # Genre
    if show.genre and show.genre in prefs.loved_genres:
        score += 15
        matches.append(f"Loved genre: {show.genre}")

    return min(100, score)


def score_reasons(show: Show, prefs: Preferences) -> list[str]:
    """Human-readable reasons backing the match_score, for tooltip display."""
    reasons: list[str] = []
    for p in show.performers:
        if p in prefs.loved_performers:
            reasons.append(f"❤ Loved actor: {p}")
        if p in prefs.loved_artists:
            reasons.append(f"❤ Loved artist: {p}")
    if show.title in prefs.loved_artists:
        reasons.append(f"❤ Loved artist: {show.title}")
    if show.director and show.director in prefs.loved_directors:
        reasons.append(f"❤ Loved director: {show.director}")
    if show.genre and show.genre in prefs.loved_genres:
        reasons.append(f"❤ Loved genre: {show.genre}")
    return reasons
