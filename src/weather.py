"""
Tel Aviv weather via Open-Meteo (no auth, free, reliable).
Returns None on any failure — caller treats weather as optional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("weather")

# Tel Aviv coordinates
TLV_LAT = 32.0853
TLV_LON = 34.7818

# Open-Meteo WMO weather code to Hebrew description + emoji
WEATHER_CODES = {
    0: ("שמיים בהירים", "☀️"),
    1: ("בעיקר בהיר", "🌤"),
    2: ("מעונן חלקית", "⛅"),
    3: ("מעונן", "☁️"),
    45: ("ערפל", "🌫"),
    48: ("ערפל קפוא", "🌫"),
    51: ("טפטוף קל", "🌦"),
    53: ("טפטוף", "🌦"),
    55: ("טפטוף חזק", "🌦"),
    61: ("גשם קל", "🌧"),
    63: ("גשם", "🌧"),
    65: ("גשם חזק", "🌧"),
    71: ("שלג קל", "🌨"),
    73: ("שלג", "🌨"),
    75: ("שלג חזק", "❄️"),
    80: ("ממטרים", "🌦"),
    81: ("ממטרים", "🌧"),
    82: ("ממטרים עזים", "⛈"),
    95: ("סופת רעמים", "⛈"),
    96: ("סופת רעמים עם ברד", "⛈"),
    99: ("סופת רעמים עזה", "⛈"),
}


@dataclass
class TodayWeather:
    description: str
    high_c: int
    low_c: int
    emoji: str


def fetch_tlv_today() -> Optional[TodayWeather]:
    try:
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": TLV_LAT,
                "longitude": TLV_LON,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": "Asia/Jerusalem",
                "forecast_days": 1,
            },
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        codes = daily.get("weather_code") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        if not (codes and highs and lows):
            return None
        code = int(codes[0])
        desc, emoji = WEATHER_CODES.get(code, ("מזג אוויר נעים", "🌤"))
        return TodayWeather(
            description=desc,
            high_c=int(round(highs[0])),
            low_c=int(round(lows[0])),
            emoji=emoji,
        )
    except Exception as e:
        log.warning("weather fetch failed: %s", e)
        return None
