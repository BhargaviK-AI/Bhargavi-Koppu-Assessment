"""National Weather Service (api.weather.gov): official alerts + forecast text.

Alerts are the strongest weather signal we have since a forecaster issued them.
"""
import asyncio
import re
from datetime import datetime

from ..http import Evidence, SourceResult, get_json
from ..util import fmt_local, local_day_window, overlaps

# NWS has ~120 alert types and most (rip currents, small craft, air quality)
# don't matter for flying. Anything not listed here shows up as info.
ALERT_SEVERITY = [
    (r"blizzard|ice storm|hurricane warning|tornado warning|extreme wind", 4),
    (r"winter storm warning|tropical storm warning|severe thunderstorm warning|high wind warning|hurricane watch", 3),
    (r"winter storm watch|winter weather advisory|wind advisory|dense fog|freezing|severe thunderstorm watch"
     r"|tornado watch|tropical storm watch|high wind watch|lake effect", 2),
    (r"flood|excessive heat|extreme heat|heat advisory|dust", 1),
]


def alert_severity(event: str) -> int:
    for pattern, sev in ALERT_SEVERITY:
        if re.search(pattern, event, re.I):
            return sev
    return 0


def _ts(value):
    return datetime.fromisoformat(value).timestamp() if value else None


async def _for_airport(ap: dict, day: str, zone: dict) -> list[Evidence]:
    point = f"{ap['lat']:.4f},{ap['lon']:.4f}"
    alerts_url = f"https://api.weather.gov/alerts/active?point={point}"
    meta, alerts = await asyncio.gather(
        get_json(f"https://api.weather.gov/points/{point}", ttl=86400),
        get_json(alerts_url),
    )
    start, end = local_day_window(day, zone["offset"])
    out = []

    for feature in alerts.get("features", []):
        p = feature["properties"]
        frm = _ts(p.get("onset") or p.get("effective"))
        to = _ts(p.get("ends") or p.get("expires"))
        if frm is None or not overlaps(frm, to, start, end):
            continue
        desc = re.sub(r"\s+", " ", p.get("description") or "")[:400]
        out.append(Evidence(
            "National Weather Service", ap["iata"], "weather-alert", "official-alert",
            alert_severity(p["event"]), p["event"],
            f"{p.get('headline') or ''} — {desc}".strip(" —"),
            p.get("@id") or alerts_url,
            window={"from": fmt_local(frm, zone["tz"]),
                    "to": fmt_local(to, zone["tz"]) if to else "until further notice"},
        ))

    # Forecast text only goes ~7 days out; past that there's nothing to add.
    forecast_url = meta.get("properties", {}).get("forecast")
    if forecast_url:
        try:
            fc = await get_json(forecast_url)
        except Exception:
            fc = {}
        periods = [p for p in fc.get("properties", {}).get("periods", [])
                   if overlaps(_ts(p["startTime"]), _ts(p["endTime"]), start, end)]
        for p in periods[:3]:
            out.append(Evidence("National Weather Service", ap["iata"], "forecast-text", "forecast", 0,
                                f"{p['name']}: {p['shortForecast']}", p["detailedForecast"], forecast_url))
    return out


async def fetch(airports: list[dict], day: str, zones: dict) -> SourceResult:
    results = await asyncio.gather(*(_for_airport(a, day, zones[a["iata"]]) for a in airports),
                                   return_exceptions=True)
    result = SourceResult("National Weather Service", "https://api.weather.gov")
    failed = []
    for ap, r in zip(airports, results):
        if isinstance(r, Exception):
            failed.append(ap["iata"])
        else:
            result.evidence += r
    if len(failed) == len(airports):
        raise RuntimeError(f"NWS unavailable for every airport ({results[0]})")
    result.note = (f"No NWS data for {', '.join(failed)}." if failed
                   else "Active alerts overlapping the travel day, plus NWS forecast text.")
    return result
