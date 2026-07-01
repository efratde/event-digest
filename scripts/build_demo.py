"""
Build a SYNTHETIC ENGLISH portfolio demo of the Event Digest.

No scraping, no network enrichment: a hand-built set of ~24 invented English
shows spread across the real public venues, rendered through the production
render_digest() pipeline so every UI feature is exercised.

Run from the repo root:
    uv run python scripts/build_demo.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make `src` importable when run from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.models import Show
from src.preferences import Preferences
from src.render import render_digest


TODAY = date.today()
NOW_YEAR = TODAY.year


def dt(days_from_today: int, hour: int, minute: int = 0) -> datetime:
    """A performance datetime `days_from_today` days out, at the given time."""
    d = TODAY + timedelta(days=days_from_today)
    return datetime(d.year, d.month, d.day, hour, minute)


def opened(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


def make_show(**kw) -> Show:
    # Default first_seen to tickets_opened_on so the freshness bucket lines up.
    kw.setdefault("first_seen", kw.get("tickets_opened_on"))
    kw.setdefault("last_seen", TODAY)
    kw.setdefault("poster_url", "")
    return Show(**kw)


# ---------------------------------------------------------------------------
# The synthetic catalogue. Titles/descriptions are invented English; venues
# and (some) performer names are real public ones used only as taste data.
# ---------------------------------------------------------------------------
SHOWS: list[Show] = [
    # ---- THEATER (g-theater) ------------------------------------------------
    make_show(
        source="habima", source_id="hb-101",
        url="#",
        title="The Glass Menagerie Revisited",
        venue="Habima Theatre - Rovina Hall", city="Tel Aviv",
        genre="Theater",
        description="A luminous new staging of a fragile family, where memory "
                    "and glass both threaten to shatter under a single touch.",
        performers=["Gila Almagor", "Yoav Ronen"],
        director="Hanan Snir",
        duration_minutes=115, price_min=140, price_max=320,
        tickets_opened_on=opened(2),
        performances=[dt(4, 20, 30), dt(9, 20, 30), dt(16, 20, 30), dt(23, 20, 30)],
    ),
    make_show(
        source="cameri", source_id="cm-210",
        url="#",
        title="A Simple Story",
        venue="Cameri Theatre - Hall 1", city="Tel Aviv",
        genre="Theater",
        description="A quiet village, an impossible love, and a season that "
                    "refuses to end well. Adapted for the stage with restraint.",
        performers=["Sasson Gabai", "Neta Shpigelman"],
        director="Noa Barkai",
        duration_minutes=130, price_min=120, price_max=290,
        tickets_opened_on=opened(13),
        performances=[dt(6, 20, 0), dt(13, 20, 0), dt(20, 20, 0), dt(34, 20, 0)],
    ),
    make_show(
        source="gesher", source_id="gs-330",
        url="#",
        title="The Dybbuk Reimagined",
        venue="Gesher Theatre - Main Stage", city="Jaffa",
        genre="Theater",
        description="A restless spirit crosses between worlds in this bold, "
                    "movement-driven reinvention of the classic folk drama.",
        performers=["Doron Tavory", "Efrat Ben-Zur"],
        director="Michael Zats",
        duration_minutes=105, price_min=110, price_max=260,
        tickets_opened_on=opened(27),
        performances=[dt(3, 19, 0), dt(10, 19, 0), dt(24, 19, 0)],
    ),
    make_show(
        source="lessin", source_id="ls-140",
        url="#",
        title="Kitchen Conversations",
        venue="Lessin Theatre - Studio", city="Tel Aviv",
        genre="Theater",
        description="Three generations argue, cook and forgive across one long "
                    "Friday night. A warm, sharp domestic comedy-drama.",
        performers=["Rivka Michaeli", "Assi Levy"],
        director="Hanan Snir",
        duration_minutes=95, price_min=95, price_max=210,
        tickets_opened_on=opened(5),
        performances=[dt(5, 20, 30), dt(12, 20, 30), dt(19, 20, 30),
                      dt(26, 20, 30), dt(40, 20, 30)],
    ),
    make_show(
        source="cameri", source_id="cm-410",
        url="#",
        title="City of Dreams",
        venue="Cameri Theatre - Hall 2", city="Tel Aviv",
        genre="Musical",
        description="A sweeping original musical about a night-shift city that "
                    "only comes alive after midnight. Big band, bigger heart.",
        performers=["Maya Dagan", "Idan Alterman"],
        director="Tamar Keenan",
        duration_minutes=150, price_min=160, price_max=360,
        tickets_opened_on=opened(16),
        performances=[dt(8, 20, 0), dt(15, 20, 0), dt(22, 20, 0), dt(29, 20, 0)],
    ),
    make_show(
        source="habima", source_id="hb-155",
        url="#",
        title="Waiting Rooms",
        venue="Habima Theatre - Bertonov Hall", city="Tel Aviv",
        genre="Theater",
        description="Strangers share a clinic waiting room and, slowly, their "
                    "whole lives. A tender ensemble piece about patience.",
        performers=["Yona Elian", "Dov Navon"],
        director="Ilan Ronen",
        duration_minutes=100, price_min=105, price_max=240,
        tickets_opened_on=opened(1),
        performances=[dt(7, 20, 30), dt(14, 20, 30), dt(28, 20, 30)],
    ),
    make_show(
        source="yoram_loewenstein", source_id="yl-060",
        url="#",
        title="Graduation Stage",
        venue="Yoram Loewenstein Studio - Black Box", city="Tel Aviv",
        genre="Theater",
        description="The graduating class presents an evening of short original "
                    "works — raw, fearless and often unexpectedly funny.",
        performers=["The Graduating Ensemble"],
        director="Ronit Kaplan",
        duration_minutes=90, price_min=60, price_max=90,
        tickets_opened_on=opened(30),
        performances=[dt(11, 19, 30), dt(18, 19, 30)],
    ),
    make_show(
        source="hasimta", source_id="hs-072",
        url="#",
        title="Alleyway Tales",
        venue="HaSimta Theatre", city="Jaffa",
        genre="Theater",
        description="An intimate storytelling evening staged in Old Jaffa's "
                    "smallest theatre, where the walls do half the acting.",
        performers=["Gil Frank", "Shira Naor"],
        director="Hanan Snir",
        duration_minutes=80, price_min=85, price_max=150,
        tickets_opened_on=opened(17),
        performances=[dt(9, 21, 0), dt(23, 21, 0), dt(37, 21, 0)],
    ),

    # ---- MUSIC (g-music) ----------------------------------------------------
    make_show(
        source="zappa_tlv", source_id="zt-501",
        url="#",
        title="Shlomo Artzi: Summer Songs",
        venue="Zappa Tel Aviv", city="Tel Aviv",
        genre="Music", sub_genre="Israeli Rock",
        description="An intimate club evening of the songs that soundtracked a "
                    "generation, stripped back to guitar, piano and voice.",
        performers=["Shlomo Artzi"],
        director="",
        duration_minutes=120, price_min=180, price_max=340,
        tickets_opened_on=opened(3),
        performances=[dt(6, 21, 0), dt(7, 21, 0), dt(20, 21, 0)],
    ),
    make_show(
        source="zappa_herzliya", source_id="zh-512",
        url="#",
        title="Rita: Under One Sky",
        venue="Zappa Herzliya", city="Herzliya",
        genre="Pop", sub_genre="Mediterranean Pop",
        description="A powerhouse voice returns with a new set spanning three "
                    "decades of hits and a handful of brand-new songs.",
        performers=["Rita"],
        director="",
        duration_minutes=110, price_min=200, price_max=380,
        tickets_opened_on=opened(14),
        performances=[dt(10, 21, 0), dt(24, 21, 0)],
    ),
    make_show(
        source="barby", source_id="bb-620",
        url="#",
        title="Electric Tram",
        venue="Barby Club", city="Tel Aviv",
        genre="Rock",
        description="Four-piece indie rock with wall-of-sound guitars and a "
                    "reputation for sweaty, sold-out hometown shows.",
        performers=["Electric Tram"],
        director="",
        duration_minutes=95, price_min=90, price_max=140,
        tickets_opened_on=opened(4),
        performances=[dt(5, 21, 30), dt(12, 21, 30), dt(33, 21, 30)],
    ),
    make_show(
        source="caesarea", source_id="cs-700",
        url="#",
        title="Summer Under the Stars",
        venue="Caesarea Amphitheatre", city="Caesarea",
        genre="Music",
        description="An open-air night in the Roman amphitheatre by the sea, "
                    "with a full band and a string section under the stars.",
        performers=["Ninet Tayeb"],
        director="",
        duration_minutes=130, price_min=220, price_max=480,
        tickets_opened_on=opened(15),
        performances=[dt(21, 20, 30), dt(28, 20, 30)],
    ),
    make_show(
        source="shuni", source_id="sh-710",
        url="#",
        title="Mediterranean Nights",
        venue="Shuni Amphitheatre", city="Binyamina",
        genre="Mizrahi",
        description="A warm evening of Mediterranean and Mizrahi favourites in "
                    "the historic courtyard amphitheatre near Binyamina.",
        performers=["Dudu Aharon"],
        director="",
        duration_minutes=115, price_min=130, price_max=260,
        tickets_opened_on=opened(26),
        performances=[dt(19, 20, 0), dt(40, 20, 0)],
    ),
    make_show(
        source="reading3", source_id="r3-810",
        url="#",
        title="Blue Note Jazz Quartet",
        venue="Reading 3", city="Tel Aviv",
        genre="Jazz",
        description="Late-night standards and original charts from a tight "
                    "piano-led quartet in the intimate port-side room.",
        performers=["Anat Cohen", "Omer Klein"],
        director="",
        duration_minutes=100, price_min=110, price_max=180,
        tickets_opened_on=opened(6),
        performances=[dt(4, 22, 0), dt(18, 22, 0), dt(32, 22, 0)],
    ),
    make_show(
        source="zappa_tlv", source_id="zt-820",
        url="#",
        title="Midnight in Jaffa Jazz",
        venue="Zappa Tel Aviv", city="Tel Aviv",
        genre="Jazz",
        description="A monthly late set of cool, coastal jazz — brushes, "
                    "upright bass and a trumpet that knows when to whisper.",
        performers=["Avishai Cohen Trio"],
        director="",
        duration_minutes=90, price_min=100, price_max=170,
        tickets_opened_on=opened(12),
        performances=[dt(13, 22, 30), dt(41, 22, 30)],
    ),

    # ---- COMEDY / STAND-UP (g-comedy) --------------------------------------
    make_show(
        source="tzavta", source_id="tz-901",
        url="#",
        title="Laugh Lines",
        venue="Tzavta - Hall 3", city="Tel Aviv",
        genre="Stand-up",
        description="An hour of sharp, observational stand-up about parking, "
                    "parents and the impossibility of a quiet coffee.",
        performers=["Shahar Hason"],
        director="",
        duration_minutes=75, price_min=95, price_max=160,
        tickets_opened_on=opened(3),
        performances=[dt(2, 21, 0), dt(9, 21, 0), dt(16, 21, 0), dt(30, 21, 0)],
    ),
    make_show(
        source="heichal_tlv", source_id="ht-910",
        url="#",
        title="The Roast of Everything",
        venue="Tel Aviv Culture Palace (Bronfman)", city="Tel Aviv",
        genre="Comedy",
        description="A rotating line-up of comedians take turns roasting the "
                    "week's news, each other, and the front row.",
        performers=["Adir Miller", "Orna Banai"],
        director="",
        duration_minutes=85, price_min=120, price_max=220,
        tickets_opened_on=opened(18),
        performances=[dt(8, 20, 30), dt(22, 20, 30)],
    ),
    make_show(
        source="tzavta", source_id="tz-920",
        url="#",
        title="Improv Republic",
        venue="Tzavta - Hall 1", city="Tel Aviv",
        genre="cabaret",
        description="A fast, unscripted cabaret night built entirely from "
                    "audience suggestions. No two shows are ever the same.",
        performers=["The Republic Troupe"],
        director="",
        duration_minutes=90, price_min=80, price_max=130,
        tickets_opened_on=opened(28),
        performances=[dt(11, 21, 30), dt(25, 21, 30), dt(39, 21, 30)],
    ),

    # ---- DANCE (g-dance) ----------------------------------------------------
    make_show(
        source="suzanne_dellal", source_id="sd-301",
        url="#",
        title="Kinetic Horizons",
        venue="Suzanne Dellal Center - Yerushalmi Hall", city="Tel Aviv",
        genre="Dance",
        description="A striking new contemporary work for ten dancers exploring "
                    "the line where the body ends and the horizon begins.",
        performers=["Batsheva Dancers"],
        director="Maya Levy",
        duration_minutes=70, price_min=120, price_max=210,
        tickets_opened_on=opened(2),
        performances=[dt(3, 20, 0), dt(10, 20, 0), dt(17, 20, 0), dt(31, 20, 0)],
    ),
    make_show(
        source="suzanne_dellal", source_id="sd-320",
        url="#",
        title="Bodies in Motion",
        venue="Suzanne Dellal Center - Main Hall", city="Tel Aviv",
        genre="Dance",
        description="A double bill pairing a delicate duet with a thunderous "
                    "full-company finale. Percussion played live on stage.",
        performers=["Vertigo Company"],
        director="Noa Wertheim",
        duration_minutes=80, price_min=130, price_max=230,
        tickets_opened_on=opened(15),
        performances=[dt(9, 20, 30), dt(23, 20, 30)],
    ),
    make_show(
        source="tmuna", source_id="tm-330",
        url="#",
        title="Contemporary Fragments",
        venue="Tmuna Theatre", city="Tel Aviv",
        genre="Dance",
        description="Five short experimental pieces from emerging choreographers "
                    "in the fringe scene's most beloved small stage.",
        performers=["Fresh Paint Collective"],
        director="Iris Erez",
        duration_minutes=65, price_min=70, price_max=110,
        tickets_opened_on=opened(29),
        performances=[dt(12, 21, 0), dt(26, 21, 0)],
    ),

    # ---- PARTIES (g-party) --------------------------------------------------
    make_show(
        source="reading3", source_id="r3-950",
        url="#",
        title="90s Throwback Night",
        venue="Reading 3", city="Tel Aviv",
        genre="Parties", sub_genre="90s Night",
        description="A full-on 90s night of the decade's biggest floor-fillers, "
                    "with a live DJ set and a very generous fog machine.",
        performers=["DJ Retro"],
        director="",
        duration_minutes=240, price_min=70, price_max=120,
        tickets_opened_on=opened(4),
        performances=[dt(5, 23, 0), dt(19, 23, 0), dt(33, 23, 0)],
    ),
    make_show(
        source="barby", source_id="bb-960",
        url="#",
        title="Class Reunion Rave",
        venue="Barby Club", city="Tel Aviv",
        genre="Parties", sub_genre="Reunion",
        description="A class reunion dance night for everyone who still knows "
                    "every word — expect confetti and questionable choreography.",
        performers=["DJ Tapes"],
        director="",
        duration_minutes=210, price_min=60, price_max=100,
        tickets_opened_on=opened(16),
        performances=[dt(15, 22, 30), dt(29, 22, 30)],
    ),

    # ---- OTHER (g-other) ----------------------------------------------------
    make_show(
        source="heichal_givatayim", source_id="hg-970",
        url="#",
        title="Family Circus Spectacular",
        venue="Givatayim Culture Hall - Almozlino Hall", city="Givatayim",
        genre="Family Show",
        description="Acrobats, jugglers and a very patient clown in a bright "
                    "matinee built for the whole family. Runs 60 minutes.",
        performers=["The Big Top Company"],
        director="",
        duration_minutes=60, price_min=70, price_max=120,
        tickets_opened_on=opened(19),
        performances=[dt(6, 11, 0), dt(13, 11, 0), dt(20, 11, 0)],
    ),
]


def build_travel_by_source() -> dict[str, dict]:
    """Plausible drive distances from Dizengoff Center, Tel Aviv for every
    enabled source id in config.yaml."""
    km_min = {
        "habima": (1.5, 7), "cameri": (1.8, 8), "lessin": (2.0, 8),
        "tzavta": (1.0, 6), "zappa_tlv": (4.0, 12), "zappa_herzliya": (15.0, 18),
        "shuni": (48.0, 38), "heichal_tlv": (2.0, 9), "caesarea": (50.0, 40),
        "gesher": (4.0, 13), "barby": (5.0, 14), "suzanne_dellal": (3.0, 12),
        "heichal_givatayim": (5.0, 15), "reading3": (6.0, 15), "tmuna": (2.5, 10),
        "yoram_loewenstein": (4.0, 13), "hasimta": (4.5, 14),
    }
    return {
        src: {"found": True, "km": km, "minutes": minutes}
        for src, (km, minutes) in km_min.items()
    }


def main() -> None:
    prefs = Preferences.from_file(str(REPO_ROOT / "preferences.yaml"))
    travel = build_travel_by_source()

    out_path = REPO_ROOT / "output" / "digest.html"
    db_path = str(REPO_ROOT / "data" / "_demo_imgcache.db")  # gitignored (data/*.db); no images fetched (poster_url="")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    render_digest(
        SHOWS,
        out_path=str(out_path),
        today=TODAY,
        fresh_days=7,
        warm_days=21,
        prefs=prefs,
        travel_by_source=travel,
        home_origin="Tel Aviv",
        db_path=db_path,
    )
    print(f"Rendered {len(SHOWS)} shows -> {out_path}")


if __name__ == "__main__":
    main()
