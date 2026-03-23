"""
Weather + Commute Alert Tool
Triggered when user sends "now" or a time like "9AM", "9:00AM", "10P" via WhatsApp.
Responds with:
- Current weather at home (440 Hudson St, Oakland, CA 94618)
- Drive time + traffic to work (75 14th Street, San Francisco, CA 94103)
- Temp at work on arrival
- Temp at work 3 hours after arrival
- Temp at work at 8PM
- Temp at home at 9PM
"""

import re
import requests
from datetime import datetime, timedelta
import pytz

PACIFIC = pytz.timezone("America/Los_Angeles")

HOME_LAT = 37.8352
HOME_LON = -122.2477

WORK_LAT = 37.7694
WORK_LON = -122.4162

HOME_ADDRESS = "440 Hudson St, Oakland, CA 94618"
WORK_ADDRESS = "75 14th Street, San Francisco, CA 94103"


def parse_time(text: str) -> datetime:
    """Parse 'now', '9AM', '9:00AM', '10P', '14:30' into a Pacific datetime (today)."""
    text = text.strip().upper()
    now = datetime.now(PACIFIC)

    if text == "NOW":
        return now

    # Normalize: "9P" -> "9PM", "10A" -> "10AM"
    text = re.sub(r"(\d)(P)$", r"\1PM", text)
    text = re.sub(r"(\d)(A)$", r"\1AM", text)

    formats = ["%I:%M%p", "%I%p", "%H:%M", "%H"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            result = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            return PACIFIC.localize(result) if result.tzinfo is None else result
        except ValueError:
            continue

    raise ValueError(f"Could not parse time: '{text}'")


def get_weather_at(lat: float, lon: float, target_dt: datetime) -> dict:
    """
    Fetch hourly weather from Open-Meteo for given coords.
    Returns temp (°F), weather description, wind speed for the hour nearest target_dt.
    """
    # Open-Meteo uses UTC
    target_utc = target_dt.astimezone(pytz.utc)
    date_str = target_utc.strftime("%Y-%m-%d")

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,weathercode,windspeed_10m,precipitation_probability",
        "temperature_unit": "fahrenheit",
        "windspeed_unit": "mph",
        "timezone": "America/Los_Angeles",
        "start_date": date_str,
        "end_date": date_str,
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    codes = data["hourly"]["weathercode"]
    winds = data["hourly"]["windspeed_10m"]
    precip = data["hourly"]["precipitation_probability"]

    # Find closest hour
    target_naive = target_dt.astimezone(PACIFIC).replace(tzinfo=None)
    best_idx = 0
    best_diff = float("inf")
    for i, t_str in enumerate(times):
        t = datetime.fromisoformat(t_str)
        diff = abs((t - target_naive).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    return {
        "temp": round(temps[best_idx]),
        "condition": wmo_to_description(codes[best_idx]),
        "wind_mph": round(winds[best_idx]),
        "precip_pct": precip[best_idx],
        "time": times[best_idx],
    }


def wmo_to_description(code: int) -> str:
    """Convert WMO weather code to human-readable string."""
    mapping = {
        0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Icy fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow",
        80: "Light showers", 81: "Showers", 82: "Heavy showers",
        95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
    }
    return mapping.get(code, f"Conditions (code {code})")


def get_drive_time(departure_dt: datetime, google_maps_key: str) -> dict:
    """Get drive time + traffic summary from home to work using Google Maps Directions API."""
    if not google_maps_key:
        return {"duration_min": None, "traffic_summary": "Traffic data unavailable (no API key)"}

    departure_ts = int(departure_dt.timestamp())
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": HOME_ADDRESS,
        "destination": WORK_ADDRESS,
        "departure_time": departure_ts,
        "traffic_model": "best_guess",
        "key": google_maps_key,
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data["status"] != "OK":
        return {"duration_min": None, "traffic_summary": f"Maps error: {data['status']}"}

    leg = data["routes"][0]["legs"][0]
    normal_sec = leg["duration"]["value"]
    traffic_sec = leg.get("duration_in_traffic", {}).get("value", normal_sec)

    normal_min = round(normal_sec / 60)
    traffic_min = round(traffic_sec / 60)
    delay = traffic_min - normal_min

    if delay <= 2:
        traffic_label = "Light traffic"
    elif delay <= 10:
        traffic_label = f"Moderate traffic (+{delay} min)"
    elif delay <= 20:
        traffic_label = f"Heavy traffic (+{delay} min)"
    else:
        traffic_label = f"Very heavy traffic (+{delay} min)"

    return {
        "duration_min": traffic_min,
        "normal_min": normal_min,
        "traffic_summary": traffic_label,
        "distance": leg["distance"]["text"],
    }


def build_message(trigger_text: str, google_maps_key: str = None) -> str:
    """Main function: parse trigger, gather data, build WhatsApp message."""
    now = datetime.now(PACIFIC)
    departure_dt = parse_time(trigger_text)

    # If the parsed time is in the past (e.g. user says "9AM" at 10AM), assume tomorrow
    if departure_dt < now - timedelta(minutes=5):
        departure_dt += timedelta(days=1)

    # --- Drive time ---
    drive = get_drive_time(departure_dt, google_maps_key)
    duration_min = drive.get("duration_min")
    arrival_dt = departure_dt + timedelta(minutes=duration_min if duration_min else 45)

    # --- Weather checkpoints ---
    home_now = get_weather_at(HOME_LAT, HOME_LON, departure_dt)
    work_arrival = get_weather_at(WORK_LAT, WORK_LON, arrival_dt)
    work_3hr = get_weather_at(WORK_LAT, WORK_LON, arrival_dt + timedelta(hours=3))
    work_8pm = get_weather_at(WORK_LAT, WORK_LON, departure_dt.replace(hour=20, minute=0))
    home_9pm = get_weather_at(HOME_LAT, HOME_LON, departure_dt.replace(hour=21, minute=0))

    # --- Format message ---
    dep_str = departure_dt.strftime("%-I:%M %p")
    arr_str = arrival_dt.strftime("%-I:%M %p")
    arr_3hr_str = (arrival_dt + timedelta(hours=3)).strftime("%-I:%M %p")

    lines = [
        f"🌤 *Weather & Commute Report*",
        f"Departure: {dep_str}",
        "",
        f"🏠 *Home now ({dep_str})*",
        f"  {home_now['temp']}°F — {home_now['condition']}",
        f"  Wind: {home_now['wind_mph']} mph | Rain chance: {home_now['precip_pct']}%",
        "",
        f"🚗 *Drive to SF*",
    ]

    if duration_min:
        lines.append(f"  {drive['traffic_summary']} — {duration_min} min ({drive.get('distance', '')})")
        lines.append(f"  Arriving ~{arr_str}")
    else:
        lines.append(f"  {drive['traffic_summary']}")
        lines.append(f"  Estimated arrival: ~{arr_str}")

    lines += [
        "",
        f"💼 *Work — SF (on arrival ~{arr_str})*",
        f"  {work_arrival['temp']}°F — {work_arrival['condition']}",
        "",
        f"💼 *Work — SF at {arr_3hr_str} (+3 hrs)*",
        f"  {work_3hr['temp']}°F — {work_3hr['condition']}",
        "",
        f"💼 *Work — SF at 8:00 PM*",
        f"  {work_8pm['temp']}°F — {work_8pm['condition']}",
        "",
        f"🏠 *Home at 9:00 PM*",
        f"  {home_9pm['temp']}°F — {home_9pm['condition']}",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    trigger = sys.argv[1] if len(sys.argv) > 1 else "now"
    gmaps_key = sys.argv[2] if len(sys.argv) > 2 else None
    print(build_message(trigger, gmaps_key))
