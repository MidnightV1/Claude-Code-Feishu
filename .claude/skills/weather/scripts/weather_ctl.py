#!/usr/bin/env python3
"""Weather CLI — current conditions, multi-day forecast, location persistence.

Data sources (all free, no API key):
  - wttr.in: current weather with Chinese descriptions (city name lookup)
  - Open-Meteo: current + forecast with coordinate precision (geocoding fallback)
  - Open-Meteo AQI: air quality (best-effort)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# ── Location persistence ──────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
LOCATION_FILE = DATA_DIR / "location.json"
DEFAULT_LOCATION = "Beijing"


def _load_location() -> dict | None:
    """Load persisted location. Returns None if not set."""
    if LOCATION_FILE.exists():
        try:
            return json.loads(LOCATION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_location(name: str, lat: float, lng: float):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    LOCATION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return data


# ── Weather sources ───────────────────────────────────────────────────
def _weather_wttr(location: str) -> dict | None:
    """wttr.in — free, no key, Chinese weather descriptions."""
    import requests as req
    try:
        r = req.get(
            f"https://wttr.in/{location}",
            params={"format": "j1"},
            headers={"User-Agent": "curl/8.0"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    current = data.get("current_condition", [{}])[0]
    forecast = data.get("weather", [{}])[0]
    return {
        "source": "wttr.in",
        "location": location,
        "temp_c": current.get("temp_C"),
        "feels_like_c": current.get("FeelsLikeC"),
        "humidity": current.get("humidity"),
        "weather_desc": (current.get("lang_zh", [{}])[0].get("value")
                         or current.get("weatherDesc", [{}])[0].get("value")),
        "wind_speed_kmph": current.get("windspeedKmph"),
        "uv_index": current.get("uvIndex"),
        "max_temp_c": forecast.get("maxtempC") if forecast else None,
        "min_temp_c": forecast.get("mintempC") if forecast else None,
    }


# City → (lat, lon) for Open-Meteo geocoding shortcut
_CITY_COORDS = {
    "beijing": (39.9, 116.4), "shanghai": (31.2, 121.5),
    "guangzhou": (23.1, 113.3), "shenzhen": (22.5, 114.1),
    "hangzhou": (30.3, 120.2), "chengdu": (30.6, 104.1),
    "nanjing": (32.1, 118.8), "wuhan": (30.6, 114.3),
    "xian": (34.3, 108.9), "montreal": (45.5, -73.6),
    "tokyo": (35.7, 139.7), "seoul": (37.6, 127.0),
    "singapore": (1.3, 103.8), "london": (51.5, -0.1),
    "new york": (40.7, -74.0), "san francisco": (37.8, -122.4),
}


def _resolve_coords(location: str) -> tuple[float, float] | None:
    """Resolve city name to (lat, lng). Returns None on failure."""
    coords = _CITY_COORDS.get(location.lower())
    if coords:
        return coords
    import requests as req
    try:
        r = req.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1},
            timeout=5,
        )
        results = r.json().get("results", [])
        if results:
            return (results[0]["latitude"], results[0]["longitude"])
    except Exception:
        pass
    return None


def _weather_open_meteo(lat: float, lng: float, forecast_days: int = 1) -> dict | None:
    """Open-Meteo — free, no key, global coverage."""
    import requests as req
    try:
        r = req.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lng,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,uv_index_max,"
                         "weather_code,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": forecast_days,
            },
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    current = data.get("current", {})
    daily = data.get("daily", {})

    result = {
        "source": "open-meteo",
        "coords": {"lat": lat, "lng": lng},
        "current": {
            "temp_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "weather_desc": _wmo_code_to_zh(current.get("weather_code")),
            "wind_speed_kmph": current.get("wind_speed_10m"),
        },
    }

    # Daily forecast array
    dates = daily.get("time", [])
    if dates:
        result["daily"] = []
        for i, d in enumerate(dates):
            day = {"date": d}
            if daily.get("temperature_2m_max"):
                day["max_temp_c"] = daily["temperature_2m_max"][i]
            if daily.get("temperature_2m_min"):
                day["min_temp_c"] = daily["temperature_2m_min"][i]
            if daily.get("uv_index_max"):
                day["uv_index"] = daily["uv_index_max"][i]
            if daily.get("weather_code"):
                day["weather_desc"] = _wmo_code_to_zh(daily["weather_code"][i])
            if daily.get("precipitation_probability_max"):
                day["precip_prob_pct"] = daily["precipitation_probability_max"][i]
            result["daily"].append(day)

    # AQI (best-effort)
    _attach_aqi(result, lat, lng)

    return result


def _attach_aqi(result: dict, lat: float, lng: float):
    """Attach AQI data to result dict. Best-effort, never fails."""
    import requests as req
    try:
        r = req.get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={
                "latitude": lat,
                "longitude": lng,
                "current": "pm2_5,pm10,us_aqi",
                "timezone": "auto",
            },
            timeout=5,
        )
        aqi_data = r.json().get("current", {})
        result["aqi"] = {
            "us_aqi": aqi_data.get("us_aqi"),
            "pm2_5": aqi_data.get("pm2_5"),
            "pm10": aqi_data.get("pm10"),
        }
    except Exception:
        pass


def _wmo_code_to_zh(code) -> str:
    """Convert WMO weather code to Chinese description."""
    if code is None:
        return "未知"
    _MAP = {
        0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
        45: "雾", 48: "冻雾",
        51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
        61: "小雨", 63: "中雨", 65: "大雨",
        71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "小阵雨", 81: "阵雨", 82: "大阵雨",
        85: "小阵雪", 86: "大阵雪",
        95: "雷暴", 96: "雷暴+小冰雹", 99: "雷暴+大冰雹",
    }
    return _MAP.get(int(code), f"天气代码{code}")


# ── Commands ──────────────────────────────────────────────────────────
def cmd_current(args):
    """Fetch current weather."""
    lat, lng, loc_name = _resolve_location(args)

    # City name → prefer wttr.in (better Chinese descriptions)
    if args.location and not (args.lat and args.lng):
        result = _weather_wttr(args.location)
        if result:
            _attach_aqi_by_name(result, args.location)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

    # Coordinates → Open-Meteo
    if lat and lng:
        result = _weather_open_meteo(lat, lng, forecast_days=1)
        if result:
            if loc_name:
                result["location"] = loc_name
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

    # Last resort: wttr.in with default
    fallback_name = loc_name or DEFAULT_LOCATION
    result = _weather_wttr(fallback_name)
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"error": "All weather sources failed"}))


def cmd_forecast(args):
    """Fetch multi-day forecast (Open-Meteo only)."""
    lat, lng, loc_name = _resolve_location(args)

    if not (lat and lng):
        # Try geocoding the location name
        name = getattr(args, "location", None) or loc_name or DEFAULT_LOCATION
        coords = _resolve_coords(name)
        if coords:
            lat, lng = coords
            loc_name = loc_name or name
        else:
            print(json.dumps({"error": f"Cannot resolve location: {name}"}))
            return

    days = min(max(int(args.days), 1), 16)  # Open-Meteo max 16 days
    result = _weather_open_meteo(lat, lng, forecast_days=days)
    if result:
        if loc_name:
            result["location"] = loc_name
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"error": "Forecast fetch failed"}))


def cmd_location(args):
    """Show or set persisted location."""
    if args.set:
        if not (args.lat and args.lng):
            print(json.dumps({"error": "--set requires --lat and --lng"}))
            sys.exit(1)
        data = _save_location(args.name or "", args.lat, args.lng)
        print(json.dumps({"ok": True, "location": data}, ensure_ascii=False, indent=2))
    else:
        loc = _load_location()
        if loc:
            print(json.dumps({"location": loc}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"location": None, "default": DEFAULT_LOCATION}))


# ── Helpers ───────────────────────────────────────────────────────────
def _resolve_location(args) -> tuple[float | None, float | None, str | None]:
    """Resolve location from args → persisted → default. Returns (lat, lng, name)."""
    # Explicit coordinates
    lat = getattr(args, "lat", None)
    lng = getattr(args, "lng", None)
    if lat and lng:
        return (float(lat), float(lng), getattr(args, "location", None))

    # Explicit city name → resolve to coords
    loc_name = getattr(args, "location", None)
    if loc_name:
        coords = _resolve_coords(loc_name)
        if coords:
            return (coords[0], coords[1], loc_name)
        return (None, None, loc_name)  # wttr.in can still use city name

    # Persisted location
    saved = _load_location()
    if saved and saved.get("lat") and saved.get("lng"):
        return (saved["lat"], saved["lng"], saved.get("name"))

    # Default
    coords = _CITY_COORDS.get(DEFAULT_LOCATION.lower())
    if coords:
        return (coords[0], coords[1], DEFAULT_LOCATION)
    return (None, None, DEFAULT_LOCATION)


def _attach_aqi_by_name(result: dict, location: str):
    """Attach AQI by resolving city name to coords first."""
    coords = _resolve_coords(location)
    if coords:
        _attach_aqi(result, coords[0], coords[1])


# ── CLI ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Weather CLI")
    sub = parser.add_subparsers(dest="command")

    # current
    p_current = sub.add_parser("current", help="Current weather")
    p_current.add_argument("--location", help="City name")
    p_current.add_argument("--lat", type=float, help="Latitude")
    p_current.add_argument("--lng", type=float, help="Longitude")

    # forecast
    p_forecast = sub.add_parser("forecast", help="Multi-day forecast")
    p_forecast.add_argument("--days", type=int, default=3, help="Number of days (max 16)")
    p_forecast.add_argument("--location", help="City name")
    p_forecast.add_argument("--lat", type=float, help="Latitude")
    p_forecast.add_argument("--lng", type=float, help="Longitude")

    # location
    p_loc = sub.add_parser("location", help="Manage persisted location")
    p_loc.add_argument("--show", action="store_true", default=True, help="Show current location")
    p_loc.add_argument("--set", action="store_true", help="Set location")
    p_loc.add_argument("--name", help="Location display name")
    p_loc.add_argument("--lat", type=float, help="Latitude")
    p_loc.add_argument("--lng", type=float, help="Longitude")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "current": cmd_current,
        "forecast": cmd_forecast,
        "location": cmd_location,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
