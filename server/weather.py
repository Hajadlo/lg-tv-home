"""Weather from Open-Meteo for the location in config.toml ([weather] latitude/longitude)."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger("tvhome.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NOTE_HOURS = 12
WET_PROBABILITY = 50
WET_MM = 0.2

# WMO weather code -> (contract code, label)
WMO: dict[int, tuple[str, str]] = {
    0: ("clear", "Clear"),
    1: ("partly", "Mainly clear"),
    2: ("partly", "Partly cloudy"),
    3: ("cloudy", "Overcast"),
    45: ("fog", "Fog"),
    48: ("fog", "Freezing fog"),
    51: ("drizzle", "Light drizzle"),
    53: ("drizzle", "Drizzle"),
    55: ("drizzle", "Heavy drizzle"),
    56: ("drizzle", "Freezing drizzle"),
    57: ("drizzle", "Freezing drizzle"),
    61: ("rain", "Light rain"),
    63: ("rain", "Rain"),
    65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"),
    67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"),
    73: ("snow", "Snow"),
    75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("rain", "Light showers"),
    81: ("rain", "Showers"),
    82: ("rain", "Heavy showers"),
    85: ("snow", "Snow showers"),
    86: ("snow", "Heavy snow showers"),
    95: ("storm", "Thunderstorm"),
    96: ("storm", "Thunderstorm with hail"),
    99: ("storm", "Thunderstorm with hail"),
}
WET = {"drizzle", "rain", "snow", "storm"}
NOUN = {"drizzle": "Rain", "rain": "Rain", "snow": "Snow", "storm": "Storms"}


def map_code(wmo: int | None) -> tuple[str, str]:
    if wmo in WMO:
        return WMO[wmo]
    return ("cloudy", "Cloudy")


def fetch_forecast(lat: float, lon: float, timeout: float = 10) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,weather_code",
        "hourly": "weather_code,precipitation,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min",
    }
    url = f"{FORECAST_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _hour_is_wet(code: int | None, mm: float | None, prob: float | None) -> bool:
    return map_code(code)[0] in WET and ((mm or 0) >= WET_MM or (prob or 0) >= WET_PROBABILITY)


def rain_note(hourly: dict, now: datetime) -> str | None:
    """'Rain from 17:00', 'Rain until 21:00', 'Rain all day' or None, over the next hours."""
    times = hourly.get("time") or []
    codes = hourly.get("weather_code") or []
    mm = hourly.get("precipitation") or []
    prob = hourly.get("precipitation_probability") or []
    hour = now.strftime("%Y-%m-%dT%H:00")
    try:
        start = times.index(hour)
    except ValueError:
        return None
    window = range(start, min(start + NOTE_HOURS, len(times)))

    def at(seq: list, i: int) -> Any:
        return seq[i] if i < len(seq) else None

    wet = [_hour_is_wet(at(codes, i), at(mm, i), at(prob, i)) for i in window]
    if not wet:
        return None
    hhmm = lambda i: times[i][11:16]  # noqa: E731
    if wet[0]:
        noun = NOUN[map_code(at(codes, start))[0]]
        for offset, is_wet in enumerate(wet):
            if not is_wet:
                return f"{noun} until {hhmm(start + offset)}"
        return f"{noun} all day" if len(wet) >= NOTE_HOURS else None
    for offset, is_wet in enumerate(wet):
        if is_wet:
            i = start + offset
            return f"{NOUN[map_code(at(codes, i))[0]]} from {hhmm(i)}"
    return None


def summarize(forecast: dict, now: datetime | None = None) -> dict:
    """Open-Meteo response -> the contract's weather object."""
    current = forecast["current"]
    daily = forecast.get("daily") or {}
    if now is None:
        now = datetime.fromisoformat(current["time"])
    code, label = map_code(current.get("weather_code"))

    def first(key: str) -> int | None:
        values = daily.get(key) or []
        return round(values[0]) if values and values[0] is not None else None

    return {
        "tempC": round(current["temperature_2m"]),
        "code": code,
        "label": label,
        "highC": first("temperature_2m_max"),
        "lowC": first("temperature_2m_min"),
        "note": rain_note(forecast.get("hourly") or {}, now),
    }


def make_fetch(location: Callable[[], dict], forecast: Callable[[float, float], dict] = fetch_forecast):
    def fetch() -> dict:
        loc = location()
        return summarize(forecast(loc["latitude"], loc["longitude"]))
    return fetch
