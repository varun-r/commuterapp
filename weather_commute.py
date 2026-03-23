"""
Weather + Commute Alert Tool
Triggered when user sends a time + direction, e.g.:
  "now to work", "9AM to work", "6PM to home"

"to work": home (Oakland) -> work (SF)
"to home": work (SF) -> home (Oakland)
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


def parse_trigger(text: str):
    """
    Parse trigger text like '9AM to work', 'now to home', '6:30P to work',
    or shorthand 'now work', '9AM home', 'now to work'.
    Returns (departure_dt, direction) where direction is 'work' or 'home'.
    """
    text = text.strip().lower()

    # Strip 'to' so 'to work', 'to home', 'work', 'home' all match
    text = re.sub(r'\bto\b', '', text).strip()
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text)

    if text.endswith('work'):
        direction = 'work'
        time_part = text[:-4].strip()
    elif text.endswith('home'):
        direction = 'home'
        time_part = text[:-4].strip()
    elif text.startswith('work'):
        direction = 'work'
        time_part = text[4:].strip()
    elif text.startswith('home'):
        direction = 'home'
        time_part = text[4:].strip()
    else:
        raise ValueError("Message must include 'work' or 'home'. E.g. 'now to work', '9AM home', '6PM to work'")

    departure_dt = parse_time(time_part if time_part else "now")
    return departure_dt, direction


def parse_time(text: str) -> datetime:
    """Parse 'now', '9AM', '9:00AM', '10P', '14:30' into a Pacific datetime (today)."""
    text = text.strip().upper()
    now = datetime.now(PACIFIC)

    if text in ("NOW", ""):
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
    """Fetch hourly weather from Open-Meteo for given coords."""
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
    }


def wmo_to_description(code: int) -> str:
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


def weather_emoji(condition: str) -> str:
    c = condition.lower()
    if "thunderstorm" in c: return "⛈"
    if "heavy rain" in c or "heavy shower" in c: return "🌧"
    if "rain" in c or "drizzle" in c or "shower" in c: return "🌦"
    if "snow" in c: return "❄️"
    if "fog" in c: return "🌫"
    if "overcast" in c: return "☁️"
    if "partly cloudy" in c: return "⛅"
    if "mostly clear" in c or "clear" in c: return "☀️"
    return "🌤"


def get_drive_time(origin: str, destination: str, departure_dt: datetime, google_maps_key: str) -> dict:
    if not google_maps_key:
        return {"duration_min": None, "delay_min": 0, "distance": ""}

    departure_ts = int(departure_dt.timestamp())
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_time": departure_ts,
        "traffic_model": "best_guess",
        "key": google_maps_key,
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data["status"] != "OK":
        return {"duration_min": None, "delay_min": 0, "distance": ""}

    leg = data["routes"][0]["legs"][0]
    normal_sec = leg["duration"]["value"]
    traffic_sec = leg.get("duration_in_traffic", {}).get("value", normal_sec)

    normal_min = round(normal_sec / 60)
    traffic_min = round(traffic_sec / 60)
    delay = max(0, traffic_min - normal_min)

    return {
        "duration_min": traffic_min,
        "delay_min": delay,
        "distance": leg["distance"]["text"],
    }


def build_message(trigger_text: str, google_maps_key: str = None) -> str:
    now = datetime.now(PACIFIC)
    departure_dt, direction = parse_trigger(trigger_text)

    # If time is in the past, assume tomorrow
    if departure_dt < now - timedelta(minutes=5):
        departure_dt += timedelta(days=1)

    # Set origin/destination based on direction
    if direction == "work":
        origin_addr = HOME_ADDRESS
        dest_addr = WORK_ADDRESS
        origin_lat, origin_lon = HOME_LAT, HOME_LON
        dest_lat, dest_lon = WORK_LAT, WORK_LON
        origin_label = "home"
        dest_label = "work"
    else:
        origin_addr = WORK_ADDRESS
        dest_addr = HOME_ADDRESS
        origin_lat, origin_lon = WORK_LAT, WORK_LON
        dest_lat, dest_lon = HOME_LAT, HOME_LON
        origin_label = "work"
        dest_label = "home"

    # Drive time
    drive = get_drive_time(origin_addr, dest_addr, departure_dt, google_maps_key)
    duration_min = drive.get("duration_min") or 45
    delay_min = drive.get("delay_min", 0)
    arrival_dt = departure_dt + timedelta(minutes=duration_min)

    # Weather
    origin_now = get_weather_at(origin_lat, origin_lon, departure_dt)
    dest_arrival = get_weather_at(dest_lat, dest_lon, arrival_dt)
    work_8pm = get_weather_at(WORK_LAT, WORK_LON, departure_dt.replace(hour=20, minute=0))
    home_8pm = get_weather_at(HOME_LAT, HOME_LON, departure_dt.replace(hour=20, minute=0))

    dep_str = departure_dt.strftime("%-I:%M %p")
    arr_str = arrival_dt.strftime("%-I:%M %p")

    emoji = weather_emoji(origin_now["condition"])

    # Traffic string
    if delay_min <= 2:
        traffic_str = "no delays"
    else:
        traffic_str = f"+{delay_min} min due to traffic"

    lines = [
        f"{emoji} At {origin_label} it's {origin_now['temp']}°F at {dep_str} with {origin_now['precip_pct']}% chance of rain and {origin_now['wind_mph']} mph wind.",
        "",
        f"🚗 It's gonna take you approx {duration_min} min to get to {dest_label} ({traffic_str}). You'll get there at around {arr_str} and it'll be {dest_arrival['temp']}°F with {dest_arrival['precip_pct']}% chance of rain and winds {dest_arrival['wind_mph']} mph.",
        "",
        f"Expect temperature to be {work_8pm['temp']}°F at work and {home_8pm['temp']}°F at home around 8PM tonight.",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    gmaps_key = sys.argv[-1] if len(sys.argv) > 1 and sys.argv[-1].startswith("AIza") else None
    args = sys.argv[1:-1] if gmaps_key else sys.argv[1:]
    trigger = " ".join(args) if args else "now to work"
    print(build_message(trigger, gmaps_key))
