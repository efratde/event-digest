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

# Open-Meteo WMO weather code to English description + emoji
WEATHER_CODES = {
    0: ("Clear sky", "☀️"),
    1: ("Mostly clear", "🌤"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫"),
    48: ("Freezing fog", "🌫"),
    51: ("Light drizzle", "🌦"),
    53: ("Drizzle", "🌦"),
    55: ("Heavy drizzle", "🌦"),
    61: ("Light rain", "🌧"),
    63: ("Rain", "🌧"),
    65: ("Heavy rain", "🌧"),
    71: ("Light snow", "🌨"),
    73: ("Snow", "🌨"),
    75: ("Heavy snow", "❄️"),
    80: ("Showers", "🌦"),
    81: ("Showers", "🌧"),
    82: ("Heavy showers", "⛈"),
    95: ("Thunderstorm", "⛈"),
    96: ("Thunderstorm with hail", "⛈"),
    99: ("Severe thunderstorm", "⛈"),
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
        desc, emoji = WEATHER_CODES.get(code, ("Pleasant weather", "🌤"))
        return TodayWeather(
            description=desc,
            high_c=int(round(highs[0])),
            low_c=int(round(lows[0])),
            emoji=emoji,
        )
    except Exception as e:
        log.warning("weather fetch failed: %s", e)
        return None
