"""
Enrichment helpers — generate search URLs for songs, trailers, reviews,
encyclopedia, awards, festivals, and maps.

These are pure URL constructors. No external API calls — clicking the link in
the digest takes you to the search results, where you pick what's relevant.
This is fast, free, and avoids API key management for v1.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .models import Show


# Genres we treat as "music / live concert" (Spotify makes sense)
MUSIC_GENRES = {
    "מוזיקה", "מוזיקה ישראלית", "ג'אז", "מזרחית",
    "מוזיקה רוק/פופ", "פופ", "רוק", "ים-תיכוני",
}
# Genres we treat as "stage production" (trailers/reviews make sense)
STAGE_GENRES = {"תיאטרון", "מחזות זמר", "סטנדאפ", "מחול"}

MUSIC_SOURCES = {"zappa_tlv", "zappa_hrz", "zappa_jlm", "barby", "reading3", "hangar11"}
STAGE_SOURCES = {"habima", "cameri", "lessin", "tzavta", "gesher", "khan", "haifa_theater"}


def is_music(show: Show) -> bool:
    if (show.genre or "").strip() in MUSIC_GENRES:
        return True
    return show.source in MUSIC_SOURCES


def is_stage(show: Show) -> bool:
    if (show.genre or "").strip() in STAGE_GENRES:
        return True
    return show.source in STAGE_SOURCES


# -- URL builders ------------------------------------------------------------
def spotify_search_url(query: str) -> str:
    return f"https://open.spotify.com/search/{quote_plus(query)}"


def youtube_search_url(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def google_news_search_url(query: str) -> str:
    return (
        f"https://news.google.com/search?q={quote_plus(query)}"
        "&hl=he-IL&gl=IL&ceid=IL%3Ahe"
    )


def wikipedia_search_url(query: str, lang: str = "he") -> str:
    return f"https://{lang}.wikipedia.org/w/index.php?search={quote_plus(query)}"


def google_search_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}&hl=he"


def google_maps_directions_url(destination: str, origin: str = "תל אביב") -> str:
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}&destination={quote_plus(destination)}"
    )


# -- Composite enrichment ----------------------------------------------------
def enrichment_for(show: Show, *, home_origin: str = "תל אביב") -> list[dict]:
    """
    Return a short list of {label, url, icon} dicts — at most 4 buttons per
    show, with full Hebrew text labels (icons alone are too cryptic for the
    target user). Links open searches, no API calls.
    """
    out: list[dict] = []
    title = (show.title or "").strip()
    if not title:
        return out

    # The "talent": for concerts the title IS the artist; for theater the first performer.
    talent = (show.performers[0] if show.performers else title).strip()

    if is_music(show):
        out.append({
            "label": "ספוטיפיי",
            "icon": "🎵",
            "url": spotify_search_url(talent),
        })
        out.append({
            "label": "קליפים",
            "icon": "📺",
            "url": youtube_search_url(f"{talent} להיט"),
        })
        out.append({
            "label": "ויקיפדיה",
            "icon": "📖",
            "url": wikipedia_search_url(talent),
        })
    elif is_stage(show):
        out.append({
            "label": "טריילר",
            "icon": "📺",
            "url": youtube_search_url(f"{title} טריילר"),
        })
        out.append({
            "label": "ביקורות",
            "icon": "📰",
            "url": google_news_search_url(f'"{title}" ביקורת'),
        })
        out.append({
            "label": "ויקיפדיה",
            "icon": "📖",
            "url": wikipedia_search_url(title if not show.performers else talent),
        })
    else:
        out.append({
            "label": "חיפוש",
            "icon": "🔎",
            "url": google_search_url(title),
        })
        out.append({
            "label": "ביקורות",
            "icon": "📰",
            "url": google_news_search_url(f'"{title}"'),
        })

    # Always: navigation
    venue_q = f"{show.venue}, {show.city}" if show.city else show.venue
    out.append({
        "label": "ניווט",
        "icon": "🗺",
        "url": google_maps_directions_url(venue_q, home_origin),
    })

    return out


# -- Per-performer Wikipedia chips (rendered separately from the URL row) ---
def performer_links(show: Show) -> list[dict]:
    """A clickable Wikipedia link for each named performer."""
    return [
        {"name": name, "url": wikipedia_search_url(name)}
        for name in show.performers
    ]
