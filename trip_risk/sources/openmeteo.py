"""Open-Meteo hourly model forecast (no key, up to 16 days out).

Also our source of each airport's time zone, which the other adapters need to
work out what "the travel day" means locally.
"""
from ..http import Evidence, SourceResult, get_json
from ..util import hour_ranges

HOURLY = "precipitation,snowfall,weather_code,wind_gusts_10m,visibility,precipitation_probability,temperature_2m"
THUNDER = {95, 96, 99}
FREEZING = {56, 57, 66, 67}


def url_for(airports: list[dict], day: str) -> str:
    lats = ",".join(f"{a['lat']:.3f}" for a in airports)
    lons = ",".join(f"{a['lon']:.3f}" for a in airports)
    return (f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}&hourly={HOURLY}"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
            f"&timezone=auto&start_date={day}&end_date={day}")


def rules_for(iata: str, day: str, hours: list[dict], url: str) -> list[Evidence]:
    """Turn one airport's hourly forecast into evidence. Thresholds are rules of
    thumb for airport operations, not airline minimums."""
    out = []

    def add(sev, title, detail):
        out.append(Evidence("Open-Meteo", iata, "weather-forecast", "forecast", sev, title, detail, url))

    def when(pred):
        return hour_ranges([h["hour"] for h in hours if pred(h)])

    def vals(key):
        return [h[key] for h in hours if h[key] is not None]

    max_gust = max(vals("gust"), default=0)
    rain = sum(vals("precip"))
    snow = sum(vals("snow"))
    min_vis_mi = min(vals("vis_ft"), default=52800) / 5280
    max_temp = max(vals("temp"), default=0)
    max_pop = max(vals("pop"), default=None)

    # Always include a baseline - "nothing notable" is useful evidence too.
    vis_txt = "6+" if min_vis_mi >= 6 else f"{min_vis_mi:.1f}"
    snow_txt = f" (snow {snow:.1f} in)" if snow > 0 else ""
    add(0, f"Forecast summary for {day}",
        f"Max gust {max_gust:.0f} mph, precipitation {rain:.2f} in{snow_txt}, min visibility {vis_txt} mi, "
        f"peak precip chance {max_pop if max_pop is not None else 'n/a'}%, high {max_temp:.0f}°F.")

    if max_gust >= 35:
        add(3 if max_gust >= 50 else 2, f"Strong gusts up to {max_gust:.0f} mph",
            f"Gusts ≥35 mph forecast {when(lambda h: (h['gust'] or 0) >= 35)} local. Crosswinds can force "
            "single-runway operations and cut arrival rates.")
    if snow >= 1:
        add(4 if snow >= 8 else 3 if snow >= 4 else 2, f"Snowfall {snow:.1f} in",
            f"Snow forecast {when(lambda h: (h['snow'] or 0) > 0)} local. Expect de-icing queues; heavier "
            "totals lead to pre-emptive cancellations.")
    if any(h["code"] in FREEZING for h in hours):
        add(3, "Freezing rain / drizzle forecast",
            f"Freezing precipitation {when(lambda h: h['code'] in FREEZING)} local. Icing slows de-icing and ground ops a lot.")
    storm_hours = [h for h in hours if h["code"] in THUNDER]
    if storm_hours:
        add(3 if len(storm_hours) >= 3 else 2, f"Thunderstorms forecast ({len(storm_hours)} h)",
            f"Thunderstorms {when(lambda h: h['code'] in THUNDER)} local. Lightning stops ramp work; storms "
            "cause ground stops and reroutes.")
    if rain >= 1 and not snow:
        add(2, f"Heavy rain {rain:.2f} in",
            f"Rain {when(lambda h: (h['precip'] or 0) >= 0.05)} local. Heavy rain usually brings low ceilings, "
            "which reduce arrival capacity.")
    low_vis = [h for h in hours if h["vis_ft"] is not None and h["vis_ft"] / 5280 < 1]
    if len(low_vis) >= 2:
        add(2, "Visibility under 1 mile",
            f"Visibility below 1 mi {when(lambda h: h['vis_ft'] is not None and h['vis_ft'] / 5280 < 1)} local.")
    if max_temp >= 110:
        add(2, f"Extreme heat {max_temp:.0f}°F", "Very high temperatures can trigger weight restrictions on some aircraft.")
    return out


async def fetch(airports: list[dict], day: str) -> SourceResult:
    url = url_for(airports, day)
    body = await get_json(url)
    locations = body if isinstance(body, list) else [body]

    result = SourceResult("Open-Meteo", url, note=f"Hourly model forecast for {day}, local airport time.")
    zones = {}
    for ap, loc in zip(airports, locations):
        zones[ap["iata"]] = {"tz": loc["timezone"], "offset": loc["utc_offset_seconds"]}
        h = loc["hourly"]
        hours = [
            {"hour": int(t[11:13]), "gust": h["wind_gusts_10m"][i], "precip": h["precipitation"][i],
             "snow": h["snowfall"][i], "code": h["weather_code"][i], "vis_ft": h["visibility"][i],
             "pop": h["precipitation_probability"][i], "temp": h["temperature_2m"][i]}
            for i, t in enumerate(h["time"])
        ]
        result.evidence += rules_for(ap["iata"], day, hours, url)
    result.extra["zones"] = zones
    return result
