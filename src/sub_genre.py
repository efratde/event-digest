"""
Sub-genre classification.

For music: Wikidata lookup → MusicBrainz fallback. Returns a normalized
Hebrew label (רוק / פופ / מזרחית / ים-תיכוני / ג'אז / פולק / ראפ /
אלקטרוני / מטאל / אינדי / מסורתית).

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

USER_AGENT = "DadTicketsDigest/1.0 (dad-tickets@example.com)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2/artist"

# Wikidata Q-id → normalized Hebrew sub-genre.
# Curated list of the most common genres for Israeli artists.
WIKIDATA_GENRE_MAP: dict[str, str] = {
    # Rock family
    "Q11399": "רוק",            # rock music
    "Q11366": "רוק אלטרנטיבי",   # alternative rock
    "Q7749": "רוק",              # rock and roll
    "Q11409": "רוק",             # hard rock
    "Q1296": "רוק",              # rock (broad)
    "Q133641": "רוק פרוגרסיבי",  # progressive rock
    "Q11401": "מטאל",            # heavy metal music
    "Q188032": "פאנק",           # punk rock
    # Pop family
    "Q37073": "פופ",             # pop music
    "Q484641": "פופ-רוק",        # pop rock
    "Q484473": "פופ-רוק",        # synth-pop (close enough)
    # Mediterranean / Mizrahi (Israeli specific)
    "Q2738544": "מזרחית",        # mizrahi music
    "Q482789": "ים-תיכוני",      # Mediterranean music
    # Jazz
    "Q8341": "ג'אז",             # jazz
    "Q21401": "ג'אז",            # jazz fusion
    # Folk / world
    "Q188450": "פולק",           # folk music
    "Q189909": "פולק",           # world music
    "Q282472": "פולק",           # folk rock
    # Hip-hop / rap
    "Q11401": "ראפ",             # (note: same Q-id as metal in some sources, distinguished by context)
    "Q11470": "ראפ",             # rap
    "Q11366": "רוק אלטרנטיבי",   # alternative rock
    "Q9759": "ראפ",              # hip hop music
    # Electronic
    "Q9778": "אלקטרוני",         # electronic music
    "Q183564": "אלקטרוני",       # techno
    "Q179805": "אלקטרוני",       # house music
    # Children's
    "Q188044": "ילדים",          # children's music
    # Classical (we filter these out per Dad's preferences)
    "Q9730": "קלאסי",            # classical music
    "Q9794": "ראפ",              # rap (alternate)
    # Country / blues
    "Q42874": "בלוז",            # blues
    "Q83270": "קאנטרי",          # country
    # Reggae
    "Q11202": "רגאיי",           # reggae
    # Singer-songwriter genre is broad — map to "מסורתית" / "פולק" depending on context
    "Q179310": "פולק",           # singer-songwriter
    # Funk / soul
    "Q186356": "פאנק",           # funk
    "Q11400": "סול",             # soul music
    "Q186946": "R&B",            # rhythm and blues
}

## Tribute / cover show patterns — detected BEFORE Wikidata so that
## "Pink Floyd Echoes Show" doesn't get classified as actual Pink Floyd rock.
TRIBUTE_PATTERN = re.compile(
    r"מחווה ל|מופע מחווה|הצדעה ל|מופע הצדעה|מחווה ל"  # Hebrew
    r"|tribute|celebrating|in memory of|הופעת מחווה",
    re.IGNORECASE,
)

# Theater regex patterns: text → normalized sub-genre.
# Order matters: more specific first.
THEATER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"מחזמר|מיוזיקל"), "מחזמר"),
    (re.compile(r"סאטיר"),          "סאטירה"),
    (re.compile(r"מונודרמה|מופע יחיד|one[ -]?man[ -]?show"), "מונודרמה"),
    (re.compile(r"קומדיה"),         "קומדיה"),
    (re.compile(r"טרגדיה|טרגי"),    "טרגדיה"),
    (re.compile(r"מותחן|מסתורין"), "מותחן"),
    (re.compile(r"דרמה"),           "דרמה"),
    (re.compile(r"פרינג'"),         "פרינג'"),
    (re.compile(r"תיאטרון פיזי"),   "תיאטרון פיזי"),
    (re.compile(r"תיאטרון תיעודי"), "תיעודי"),
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
# MusicBrainz tag → Hebrew genre. Comprehensive map of legitimate music genres.
# Anything NOT in this map is treated as "not a genre" and skipped.
MB_TAG_TO_HEBREW = {
    # Rock family
    "rock": "רוק", "rock and roll": "רוק", "classic rock": "רוק",
    "hard rock": "רוק", "soft rock": "רוק", "indie rock": "אינדי",
    "indie": "אינדי", "alternative rock": "רוק אלטרנטיבי",
    "alternative": "רוק אלטרנטיבי", "alt rock": "רוק אלטרנטיבי",
    "progressive rock": "רוק פרוגרסיבי", "psychedelic rock": "רוק פסיכדלי",
    "garage rock": "רוק", "stoner rock": "רוק",
    # Pop
    "pop": "פופ", "pop rock": "פופ-רוק", "synth-pop": "פופ אלקטרוני",
    "synthpop": "פופ אלקטרוני", "art pop": "פופ", "indie pop": "פופ אינדי",
    "europop": "פופ", "dance pop": "פופ",
    # Mizrahi / Mediterranean
    "mizrahi": "מזרחית", "mediterranean": "ים-תיכוני",
    "muzika mizrahit": "מזרחית",
    # Jazz
    "jazz": "ג'אז", "smooth jazz": "ג'אז", "jazz fusion": "ג'אז",
    "bebop": "ג'אז", "swing": "ג'אז", "afro-cuban jazz": "ג'אז",
    # Folk
    "folk": "פולק", "folk rock": "פולק", "world": "פולק",
    "world music": "פולק", "ethnic": "פולק", "singer-songwriter": "פולק",
    # Hip-hop / rap
    "hip hop": "ראפ", "hip-hop": "ראפ", "rap": "ראפ", "trap": "ראפ",
    "cloud rap": "ראפ",
    # Metal
    "metal": "מטאל", "heavy metal": "מטאל", "thrash metal": "מטאל",
    "death metal": "מטאל", "black metal": "מטאל", "doom metal": "מטאל",
    # Punk
    "punk": "פאנק", "punk rock": "פאנק", "noisecore": "פאנק",
    # Electronic
    "electronic": "אלקטרוני", "techno": "אלקטרוני", "house": "אלקטרוני",
    "trance": "אלקטרוני", "edm": "אלקטרוני", "dubstep": "אלקטרוני",
    "drum and bass": "אלקטרוני", "ambient": "אלקטרוני",
    "electronic rock": "רוק אלקטרוני", "acid house": "אלקטרוני",
    "eurodance": "אלקטרוני",
    # Funk / soul / R&B
    "funk": "פאנק", "soul": "סול", "neo soul": "סול",
    "r&b": "R&B", "rhythm and blues": "R&B", "blue-eyed soul": "סול",
    # Blues / country / reggae
    "blues": "בלוז", "blues rock": "בלוז רוק",
    "country": "קאנטרי", "country rock": "קאנטרי",
    "reggae": "רגאיי", "ska": "רגאיי",
    # Classical (we generally hide these)
    "classical": "קלאסי", "opera": "אופרה", "symphony": "קלאסי",
    "baroque": "קלאסי", "chamber music": "קלאסי",
    "cinematic classical": "קלאסי",
    # Israeli / Jewish specifics
    "israeli": "ישראלי", "hebrew rock": "רוק ישראלי",
    "klezmer": "כליזמר", "chazzanut": "חזנות", "jewish": "מסורתי-יהודי",
    "orthodox pop": "פופ דתי",
    # Children's
    "children's music": "ילדים", "kids": "ילדים",
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
    """Look up artist in MusicBrainz, return top *real* genre tag in Hebrew."""
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

    is_music = (genre in {"מוזיקה", "מוסיקה", "מוזיקה ישראלית"}
                or show.source in music_sources)
    is_theater = (genre in {"תיאטרון", "מחזות זמר"}
                  or show.source in theater_sources)

    if is_music:
        # Tribute / cover show? — detect before Wikidata so we don't accidentally
        # classify "Pink Floyd Tribute Show" as actual Pink Floyd rock.
        haystack = f"{show.title} {show.description or ''}"
        if TRIBUTE_PATTERN.search(haystack):
            return "מופעי מחווה"

        # The "talent" we look up: prefer first performer, else title (concerts often title=artist)
        name = (show.performers[0] if show.performers else show.title).strip()
        # Strip common suffixes that pollute the search
        # e.g. "שלום חנוך - מופע להקה" → "שלום חנוך"
        for sep in [" - ", " | ", " · ", "—", "–"]:
            if sep in name:
                name = name.split(sep)[0].strip()
        # Strip leading numbers like "15 שנות" common in tribute shows
        name = re.sub(r"^\d+\s+שנות\s+", "", name)
        result = _classify_music_via_wikidata(name)
        if result:
            return result
        time.sleep(0.5)
        return _classify_music_via_musicbrainz(name)

    if is_theater:
        return _classify_theater(show.title, show.description)

    return None
