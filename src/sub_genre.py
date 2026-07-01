"""
Sub-genre classification.

For music: Wikidata lookup → MusicBrainz fallback. Returns a normalized
English label (Rock / Pop / Mizrahi / Mediterranean / Jazz / Folk / Rap /
Electronic / Metal / Indie / Traditional).

For theater: regex on title + description.

For standup / dance / parties: returns None (no sub-genre needed).

Cached forever in the `shows.sub_genre` DB column once classified.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx

log = logging.getLogger("sub_genre")

USER_AGENT = "EventDigest/1.0 (event-digest@example.com)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2/artist"

# Wikidata Q-id → normalized English sub-genre.
# Curated list of the most common genres for Israeli artists.
WIKIDATA_GENRE_MAP: dict[str, str] = {
    # Rock family
    "Q11399": "Rock",               # rock music
    "Q11366": "Alternative Rock",   # alternative rock
    "Q7749": "Rock",                # rock and roll
    "Q11409": "Rock",               # hard rock
    "Q1296": "Rock",                # rock (broad)
    "Q133641": "Progressive Rock",  # progressive rock
    "Q11401": "Metal",              # heavy metal music
    "Q188032": "Punk",              # punk rock
    # Pop family
    "Q37073": "Pop",                # pop music
    "Q484641": "Pop Rock",          # pop rock
    "Q484473": "Pop Rock",          # synth-pop (close enough)
    # Mediterranean / Mizrahi (Israeli specific)
    "Q2738544": "Mizrahi",          # mizrahi music
    "Q482789": "Mediterranean",     # Mediterranean music
    # Jazz
    "Q8341": "Jazz",                # jazz
    "Q21401": "Jazz",               # jazz fusion
    # Folk / world
    "Q188450": "Folk",              # folk music
    "Q189909": "Folk",              # world music
    "Q282472": "Folk",              # folk rock
    # Hip-hop / rap
    "Q11401": "Rap",                # (note: same Q-id as metal in some sources, distinguished by context)
    "Q11470": "Rap",                # rap
    "Q11366": "Alternative Rock",   # alternative rock
    "Q9759": "Rap",                 # hip hop music
    # Electronic
    "Q9778": "Electronic",          # electronic music
    "Q183564": "Electronic",        # techno
    "Q179805": "Electronic",        # house music
    # Children's
    "Q188044": "Children's",        # children's music
    # Classical (we filter these out per the user's preferences)
    "Q9730": "Classical",           # classical music
    "Q9794": "Rap",                 # rap (alternate)
    # Country / blues
    "Q42874": "Blues",              # blues
    "Q83270": "Country",            # country
    # Reggae
    "Q11202": "Reggae",             # reggae
    # Singer-songwriter genre is broad — map to "Traditional" / "Folk" depending on context
    "Q179310": "Folk",              # singer-songwriter
    # Funk / soul
    "Q186356": "Funk",              # funk
    "Q11400": "Soul",               # soul music
    "Q186946": "R&B",               # rhythm and blues
}

## Tribute / cover show patterns — detected BEFORE Wikidata so that
## "Pink Floyd Echoes Show" doesn't get classified as actual Pink Floyd rock.
TRIBUTE_PATTERN = re.compile(
    r"tribute to|tribute show|salute to|salute show|tribute concert"
    r"|tribute|celebrating|in memory of",
    re.IGNORECASE,
)

# Theater regex patterns: text → normalized sub-genre.
# Order matters: more specific first.
THEATER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"musical"), "Musical"),
    (re.compile(r"satir"),          "Satire"),
    (re.compile(r"monodrama|solo show|one[ -]?man[ -]?show|one[ -]?person[ -]?show"), "Monodrama"),
    (re.compile(r"comedy"),         "Comedy"),
    (re.compile(r"tragedy|tragic"), "Tragedy"),
    (re.compile(r"thriller|mystery"), "Thriller"),
    (re.compile(r"drama"),          "Drama"),
    (re.compile(r"fringe"),         "Fringe"),
    (re.compile(r"physical theat(?:er|re)"),   "Physical Theater"),
    (re.compile(r"documentary theat(?:er|re)"), "Documentary"),
]


# ---------- Wikidata helpers ----------
def _ws_search(name: str, lang: str = "he", limit: int = 5) -> list[dict]:
    try:
        r = httpx.get(WIKIDATA_API,
                      params={"action": "wbsearchentities", "search": name,
                              "language": lang, "type": "item",
                              "limit": limit, "format": "json"},
                      headers={"User-Agent": USER_AGENT}, timeout=15)
        return r.json().get("search", [])
    except Exception as e:
        log.warning("wikidata search failed for %r: %s", name, e)
        return []


def _ws_genre_qids(qid: str) -> list[str]:
    try:
        r = httpx.get(WIKIDATA_API,
                      params={"action": "wbgetentities", "ids": qid,
                              "props": "claims", "format": "json"},
                      headers={"User-Agent": USER_AGENT}, timeout=15)
        claims = r.json().get("entities", {}).get(qid, {}).get("claims", {})
        out = []
        for c in claims.get("P136", []):
            try:
                out.append(c["mainsnak"]["datavalue"]["value"]["id"])
            except KeyError:
                pass
        return out
    except Exception as e:
        log.warning("wikidata claims failed for %s: %s", qid, e)
        return []


def _ws_label_for(qid: str, lang: str = "he") -> Optional[str]:
    try:
        r = httpx.get(WIKIDATA_API,
                      params={"action": "wbgetentities", "ids": qid,
                              "props": "labels", "languages": f"{lang}|en",
                              "format": "json"},
                      headers={"User-Agent": USER_AGENT}, timeout=10)
        ent = r.json().get("entities", {}).get(qid, {})
        return ent.get("labels", {}).get(lang, {}).get("value") or \
               ent.get("labels", {}).get("en", {}).get("value")
    except Exception:
        return None


def _classify_music_via_wikidata(name: str) -> Optional[str]:
    """Search Wikidata, find the first match that has a P136 (genre) claim."""
    if not name:
        return None
    matches = _ws_search(name, "he") or _ws_search(name, "en")
    for m in matches[:5]:
        time.sleep(0.3)  # be polite
        genre_qids = _ws_genre_qids(m["id"])
        if not genre_qids:
            continue
        # Map first known genre Q-id to our taxonomy
        for qid in genre_qids:
            if qid in WIKIDATA_GENRE_MAP:
                return WIKIDATA_GENRE_MAP[qid]
        # No mapping — fetch the label and use it raw (best effort)
        label = _ws_label_for(genre_qids[0], "he")
        return label
    return None


# ---------- MusicBrainz fallback ----------
# MusicBrainz tag → English genre. Comprehensive map of legitimate music genres.
# Anything NOT in this map is treated as "not a genre" and skipped.
MB_TAG_TO_HEBREW = {
    # Rock family
    "rock": "Rock", "rock and roll": "Rock", "classic rock": "Rock",
    "hard rock": "Rock", "soft rock": "Rock", "indie rock": "Indie",
    "indie": "Indie", "alternative rock": "Alternative Rock",
    "alternative": "Alternative Rock", "alt rock": "Alternative Rock",
    "progressive rock": "Progressive Rock", "psychedelic rock": "Psychedelic Rock",
    "garage rock": "Rock", "stoner rock": "Rock",
    # Pop
    "pop": "Pop", "pop rock": "Pop Rock", "synth-pop": "Electro-Pop",
    "synthpop": "Electro-Pop", "art pop": "Pop", "indie pop": "Indie Pop",
    "europop": "Pop", "dance pop": "Pop",
    # Mizrahi / Mediterranean
    "mizrahi": "Mizrahi", "mediterranean": "Mediterranean",
    "muzika mizrahit": "Mizrahi",
    # Jazz
    "jazz": "Jazz", "smooth jazz": "Jazz", "jazz fusion": "Jazz",
    "bebop": "Jazz", "swing": "Jazz", "afro-cuban jazz": "Jazz",
    # Folk
    "folk": "Folk", "folk rock": "Folk", "world": "Folk",
    "world music": "Folk", "ethnic": "Folk", "singer-songwriter": "Folk",
    # Hip-hop / rap
    "hip hop": "Rap", "hip-hop": "Rap", "rap": "Rap", "trap": "Rap",
    "cloud rap": "Rap",
    # Metal
    "metal": "Metal", "heavy metal": "Metal", "thrash metal": "Metal",
    "death metal": "Metal", "black metal": "Metal", "doom metal": "Metal",
    # Punk
    "punk": "Punk", "punk rock": "Punk", "noisecore": "Punk",
    # Electronic
    "electronic": "Electronic", "techno": "Electronic", "house": "Electronic",
    "trance": "Electronic", "edm": "Electronic", "dubstep": "Electronic",
    "drum and bass": "Electronic", "ambient": "Electronic",
    "electronic rock": "Electronic Rock", "acid house": "Electronic",
    "eurodance": "Electronic",
    # Funk / soul / R&B
    "funk": "Funk", "soul": "Soul", "neo soul": "Soul",
    "r&b": "R&B", "rhythm and blues": "R&B", "blue-eyed soul": "Soul",
    # Blues / country / reggae
    "blues": "Blues", "blues rock": "Blues Rock",
    "country": "Country", "country rock": "Country",
    "reggae": "Reggae", "ska": "Reggae",
    # Classical (we generally hide these)
    "classical": "Classical", "opera": "Opera", "symphony": "Classical",
    "baroque": "Classical", "chamber music": "Classical",
    "cinematic classical": "Classical",
    # Israeli / Jewish specifics
    "israeli": "Israeli", "hebrew rock": "Israeli Rock",
    "klezmer": "Klezmer", "chazzanut": "Cantorial", "jewish": "Jewish Traditional",
    "orthodox pop": "Religious Pop",
    # Children's
    "children's music": "Children's", "kids": "Children's",
}

# Tags that are NOT genres — skip these even if they're the top tag.
MB_NON_GENRE_TAGS = {
    "hebrew", "english", "russian", "french", "spanish", "arabic", "yiddish",
    "british", "american", "canadian", "german", "french", "italian", "japanese",
    "composer", "lyricist", "producer", "songwriter", "instrumentalist",
    "vocalist", "musician", "artist", "performer",
    "special purpose artist", "anonymous",
    "awesomename", "soundtrack",
}


def _classify_music_via_musicbrainz(name: str) -> Optional[str]:
    """Look up artist in MusicBrainz, return top *real* genre tag in English."""
    if not name:
        return None
    try:
        r = httpx.get(MUSICBRAINZ_API,
                      params={"query": f"artist:{name}", "fmt": "json", "limit": 3},
                      headers={"User-Agent": USER_AGENT}, timeout=15)
        artists = r.json().get("artists", [])
        for a in artists[:3]:
            tags = sorted(a.get("tags", []), key=lambda t: -t.get("count", 0))
            for t in tags:
                tag_lower = (t.get("name") or "").lower().strip()
                if not tag_lower or tag_lower in MB_NON_GENRE_TAGS:
                    continue
                if tag_lower in MB_TAG_TO_HEBREW:
                    return MB_TAG_TO_HEBREW[tag_lower]
                # Unknown but might be a real genre — only accept if it looks
                # like one (single word, no weird casing). Skip otherwise.
                if " " not in tag_lower and len(tag_lower) <= 18:
                    # Best effort: convert to title-case English
                    continue   # actually skip, too risky
    except Exception as e:
        log.warning("musicbrainz lookup failed for %r: %s", name, e)
    return None


# ---------- Theater classifier ----------
def _classify_theater(title: str, description: str) -> Optional[str]:
    text = f" {title} {description} "
    for pat, label in THEATER_PATTERNS:
        if pat.search(text):
            return label
    return None


# ---------- Public API ----------
def classify(show) -> Optional[str]:
    """
    Classify a Show into a sub-genre. Returns None if not classifiable
    (or if not relevant — standup/dance/parties don't get sub-genres).
    """
    genre = (show.genre or "").strip()
    music_sources = {"zappa_tlv", "zappa_herzliya", "barby", "shuni",
                     "caesarea", "reading3", "hangar11"}
    theater_sources = {"habima", "cameri", "lessin", "tzavta", "gesher",
                       "tmuna", "hasimta", "yoram_loewenstein"}

    is_music = (genre in {"Music", "Israeli Music"}
                or show.source in music_sources)
    is_theater = (genre in {"Theater", "Musicals"}
                  or show.source in theater_sources)

    if is_music:
        # Tribute / cover show? — detect before Wikidata so we don't accidentally
        # classify "Pink Floyd Tribute Show" as actual Pink Floyd rock.
        haystack = f"{show.title} {show.description or ''}"
        if TRIBUTE_PATTERN.search(haystack):
            return "Tribute Shows"

        # The "talent" we look up: prefer first performer, else title (concerts often title=artist)
        name = (show.performers[0] if show.performers else show.title).strip()
        # Strip common suffixes that pollute the search
        # e.g. "Shalom Hanoch - Band Show" → "Shalom Hanoch"
        for sep in [" - ", " | ", " · ", "—", "–"]:
            if sep in name:
                name = name.split(sep)[0].strip()
        # Strip leading numbers like "15 Years of" common in tribute shows
        name = re.sub(r"^\d+\s+years\s+of\s+", "", name, flags=re.IGNORECASE)
        result = _classify_music_via_wikidata(name)
        if result:
            return result
        time.sleep(0.5)
        return _classify_music_via_musicbrainz(name)

    if is_theater:
        return _classify_theater(show.title, show.description)

    return None
