"""NOAA Aviation Weather Center: TAFs and METARs.

The TAF is the forecast airlines and the FAA actually plan around (ceiling,
visibility, wind, weather), usually 24-30 h ahead.
"""
import asyncio
import re
from datetime import datetime

from ..http import Evidence, SourceResult, get_json
from ..util import fmt_local, local_day_window, overlaps

# Airports that lose capacity at higher ceilings than the standard categories
# imply. SFO's parallel runways lose paired visual approaches below ~3,000 ft,
# which is why it runs ground delay programs on otherwise "MVFR" days.
CEILING_SENSITIVE = {"SFO": 3000}

HAZARD_TITLES = {
    "ceiling": "Low ceilings / visibility",
    "wind": "Strong gusts",
    "convective": "Thunderstorms",
    "icing": "Freezing precipitation",
    "snow": "Snow",
}


def parse_vis(v) -> float:
    if v is None or "+" in str(v):
        return 10.0
    return float(v)


def ceiling_ft(period: dict):
    layers = [c["base"] for c in period.get("clouds") or [] if c["cover"] in ("BKN", "OVC", "OVX") and c["base"]]
    vv = period.get("vertVis")
    if vv is not None:
        layers.append(vv * 100 if vv < 100 else vv)
    return min(layers) if layers else None


def flight_category(cig, vis) -> str:
    if (cig is not None and cig < 500) or vis < 1:
        return "LIFR"
    if (cig is not None and cig < 1000) or vis < 3:
        return "IFR"
    if (cig is not None and cig <= 3000) or vis <= 5:
        return "MVFR"
    return "VFR"


def period_hazards(iata: str, p: dict) -> list[tuple[str, int, str, bool]]:
    """(hazard, severity, label, tentative) for one TAF period."""
    cig, vis = ceiling_ft(p), parse_vis(p.get("visib"))
    cat = flight_category(cig, vis)
    wx = p.get("wxString") or ""
    gust = p.get("wgst") or 0
    # TEMPO / PROB groups are "possible", not expected - knock them down one level.
    tentative = p.get("fcstChange") in ("TEMPO", "PROB") or p.get("probability") is not None
    found = []

    def add(key, sev, label):
        found.append((key, sev - 1 if tentative and sev > 1 else sev, label, tentative))

    sensitive = CEILING_SENSITIVE.get(iata)
    if cat == "LIFR":
        add("ceiling", 3, f"LIFR (ceiling {cig or '—'} ft, vis {vis:g} SM)")
    elif cat == "IFR":
        add("ceiling", 2, f"IFR (ceiling {cig or '—'} ft, vis {vis:g} SM)")
    elif sensitive and cig is not None and cig < sensitive:
        add("ceiling", 2, f"ceiling {cig} ft, below {iata}'s ~{sensitive} ft limit for paired approaches")
    elif cat == "MVFR":
        add("ceiling", 1, f"MVFR (ceiling {cig or '—'} ft, vis {vis:g} SM)")

    if gust >= 40:
        add("wind", 3, f"gusts {gust} kt")
    elif gust >= 30:
        add("wind", 2, f"gusts {gust} kt")
    if "TS" in wx:
        add("convective", 3, f"thunderstorms ({wx})")
    if re.search(r"FZRA|FZDZ|PL", wx):
        add("icing", 3, f"freezing precipitation ({wx})")
    elif re.search(r"\+SN|BLSN", wx):
        add("snow", 3, f"heavy/blowing snow ({wx})")
    elif "SN" in wx:
        add("snow", 2, f"snow ({wx})")
    return found


def taf_evidence(ap: dict, taf: dict, day: str, zone: dict, url: str) -> list[Evidence]:
    start, end = local_day_window(day, zone["offset"])
    tz, iata = zone["tz"], ap["iata"]
    worst: dict[str, dict] = {}

    for p in taf["fcsts"]:
        if not overlaps(p["timeFrom"], p["timeTo"], start, end):
            continue
        span = f"{fmt_local(max(p['timeFrom'], start), tz)} → {fmt_local(min(p['timeTo'], end), tz)}"
        for key, sev, label, tentative in period_hazards(iata, p):
            w = worst.setdefault(key, {"sev": 0, "items": []})
            w["sev"] = max(w["sev"], sev)
            w["items"].append(f"{label}{' [possible]' if tentative else ''} {span}")

    issued = datetime.fromisoformat(taf["issueTime"].replace("Z", "+00:00")).timestamp()
    cover_end = min(taf["validTimeTo"], end)
    raw = taf["rawTAF"]
    if cover_end < end:
        raw += f" — note: this TAF only covers the travel day until {fmt_local(cover_end, tz)}."

    out = [Evidence("NOAA Aviation Weather Center", iata, "aviation-forecast", "forecast", 0,
                    f"TAF {ap['icao']} (issued {fmt_local(issued, tz)})", raw, url)]
    for key, w in worst.items():
        if w["sev"] > 0:
            out.append(Evidence("NOAA Aviation Weather Center", iata, "aviation-forecast", "forecast",
                                w["sev"], f"TAF: {HAZARD_TITLES[key]}", "; ".join(w["items"]), url))
    return out


async def fetch(airports: list[dict], day: str, zones: dict, is_today: bool) -> SourceResult:
    ids = ",".join(a["icao"] for a in airports if a["icao"])
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={ids}&format=json"
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={ids}&format=json"

    async def no_metars():
        return []

    tafs, metars = await asyncio.gather(get_json(taf_url), get_json(metar_url) if is_today else no_metars())
    tafs_by_id = {t["icaoId"]: t for t in tafs or []}
    metars_by_id = {m["icaoId"]: m for m in metars or []}

    result = SourceResult("NOAA Aviation Weather Center", taf_url)
    not_covered = []
    for ap in airports:
        zone = zones[ap["iata"]]
        start, end = local_day_window(day, zone["offset"])
        taf = tafs_by_id.get(ap["icao"])
        if taf and overlaps(taf["validTimeFrom"], taf["validTimeTo"], start, end):
            result.evidence += taf_evidence(ap, taf, day, zone, taf_url)
        else:
            not_covered.append(ap["iata"])

        metar = metars_by_id.get(ap["icao"])
        if metar:
            cat = metar.get("fltCat") or "unknown"
            sev = {"LIFR": 2, "IFR": 1}.get(cat, 0)
            result.evidence.append(Evidence("NOAA Aviation Weather Center", ap["iata"], "current-conditions",
                                            "observed", sev, f"Current conditions {cat} (METAR)",
                                            metar["rawOb"], metar_url))

    result.note = (f"TAF doesn't reach the travel date for {', '.join(not_covered)} (TAFs cover ~24-30 h)."
                   if not_covered else "TAF periods overlapping the travel day.")
    return result
