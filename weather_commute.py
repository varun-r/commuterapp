"""
Weather + Commute Alert Tool
Triggered when user sends a time + direction, e.g.:
  "now to work", "9AM to work", "6PM to home"

"to work": home (Oakland) -> work (SF)
"to home": work (SF) -> home (Oakland)
"""

import os
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


def fetch_weather_location(lat: float, lon: float, weather_key: str) -> list:
    """
    Fetch full day hourly forecast from WeatherAPI for a location.
    Returns list of hourly dicts.
    """
    url = "http://api.weatherapi.com/v1/forecast.json"
    params = {
        "key": weather_key,
        "q": f"{lat},{lon}",
        "days": 1,
        "aqi": "no",
        "alerts": "no",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["forecast"]["forecastday"][0]["hour"]


def extract_weather_at(hours: list, target_dt: datetime) -> dict:
    """Extract weather for the closest hour from WeatherAPI hourly list."""
    target_naive = target_dt.astimezone(PACIFIC).replace(tzinfo=None)
    best = None
    best_diff = float("inf")
    for h in hours:
        t = datetime.strptime(h["time"], "%Y-%m-%d %H:%M")
        diff = abs((t - target_naive).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best = h

    return {
        "temp": round(best["temp_f"]),
        "condition": best["condition"]["text"],
        "wind_mph": round(best["wind_mph"]),
        "precip_pct": best["chance_of_rain"],
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


# BART travel times (minutes) for our two routes
# Rockridge -> Civic Center (closest to 75 14th St SF): ~25 min
# 16th St Mission -> Rockridge: ~27 min
BART_TRAVEL_TIMES = {
    "ROCK": 25,  # to SF (Civic Center)
    "16TH": 27,  # to Rockridge
}

# Yellow line destination filters
# To work: Rockridge -> SF, Yellow line goes South toward SF Airport / Daly City
# To home: 16th St -> Rockridge, Yellow line goes North toward Antioch
BART_DEST_FILTER = {
    "ROCK": "South",   # SF-bound trains
    "16TH": "North",   # East Bay-bound trains (Antioch/Berryessa)
}


def get_bart_times(station: str, bart_key: str) -> str:
    """
    Get next two BART departure times with arrival times.
    Returns formatted string like '6:18 PM arrv 6:43 PM and 6:34 PM arrv 6:59 PM'
    """
    if not bart_key:
        return "[no BART key]"

    direction = BART_DEST_FILTER[station]
    travel_min = BART_TRAVEL_TIMES[station]

    try:
        url = f"https://api.bart.gov/api/etd.aspx?cmd=etd&orig={station}&key={bart_key}&json=y"
        data = None
        for _ in range(5):
            try:
                resp = requests.get(url, timeout=10)
                candidate = resp.json()
                if "station" in candidate.get("root", {}):
                    data = candidate
                    break
            except Exception:
                pass
        if data is None:
            return "check bart.gov for times"

        now = datetime.now(PACIFIC)
        departures = []

        for etd in data["root"]["station"][0]["etd"]:
            # For 16TH -> home, only include Antioch/Berryessa (Yellow line east)
            if station == "16TH" and etd["destination"] not in ("Antioch", "Berryessa"):
                continue
            for est in etd["estimate"]:
                if est["direction"] == direction:
                    mins_raw = est["minutes"]
                    if mins_raw == "Leaving":
                        mins = 0
                    else:
                        try:
                            mins = int(mins_raw)
                        except ValueError:
                            continue
                    dep_time = now + timedelta(minutes=mins)
                    arr_time = dep_time + timedelta(minutes=travel_min)
                    departures.append((dep_time, arr_time))

        departures.sort()
        departures = departures[:2]

        if not departures:
            return "no trains found"

        parts = []
        for dep, arr in departures:
            parts.append(f"{dep.strftime('%-I:%M %p')} arrv {arr.strftime('%-I:%M %p')}")
        return " and ".join(parts)

    except Exception as e:
        return f"unavailable ({e})"


def snarky_recommendation(delay_min: int) -> str:
    import random
    if delay_min > 10:
        options = [
            f"🚨 +{delay_min} min of traffic? Honestly, just take BART. Your car is not worth it today.",
            f"😬 That's {delay_min} extra minutes sitting in your car doing nothing. BART is right there.",
            f"🤦 +{delay_min} min delay. Bay Bridge traffic wins again. Maybe let BART have this one.",
            f"📻 With +{delay_min} min of traffic, you'll really get to bond with whatever's on the radio. Or just take BART.",
        ]
    else:
        options = [
            f"✅ Traffic's light today. BART would have taken longer honestly — drive it.",
            f"🚗 Only +{delay_min} min delay. You'd spend that waiting on a platform. Drive.",
            f"😌 Traffic is basically nothing. Don't even think about BART today.",
            f"🏎️ Light traffic — BART is for days like tomorrow. Today you drive.",
        ]
    return random.choice(options)


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


def build_message(trigger_text: str, google_maps_key: str = None, bart_key: str = None, weather_key: str = None) -> str:
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

    # Weather — two API calls (one per location)
    home_hours = fetch_weather_location(HOME_LAT, HOME_LON, weather_key)
    work_hours = fetch_weather_location(WORK_LAT, WORK_LON, weather_key)

    origin_hours = home_hours if direction == "work" else work_hours
    dest_hours = work_hours if direction == "work" else home_hours

    origin_now = extract_weather_at(origin_hours, departure_dt)
    dest_arrival = extract_weather_at(dest_hours, arrival_dt)
    work_8pm = extract_weather_at(work_hours, departure_dt.replace(hour=20, minute=0))
    home_8pm = extract_weather_at(home_hours, departure_dt.replace(hour=20, minute=0))

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
        f"🚇 If you want to take BART, the next two trains at {'Rockridge' if direction == 'work' else '16th St Mission'} are at {get_bart_times('ROCK' if direction == 'work' else '16TH', bart_key)}.",
        "",
        snarky_recommendation(delay_min),
    ]

    # Only show 8PM forecast if it's before 8PM
    if now.hour < 20:
        lines += [
            "",
            f"Also looking ahead — expect temperature to be {work_8pm['temp']}°F at work and {home_8pm['temp']}°F at home around 8PM tonight.",
        ]

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    gmaps_key = os.environ.get("GOOGLE_MAPS_KEY")
    bart_key = os.environ.get("BART_KEY")
    weather_key = os.environ.get("WEATHER_KEY")
    trigger = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "now to work"
    print(build_message(trigger, gmaps_key, bart_key, weather_key))
