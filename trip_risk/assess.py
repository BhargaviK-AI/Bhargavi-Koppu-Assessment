"""One trip check: resolve airports -> fetch sources -> numbered evidence -> rule baseline.

ai.py only ever sees the bundle built here, so anything Claude says can be
traced back to an evidence ID.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .airports import resolve_airport
from .http import Evidence, run_source
from .sources import aviationweather, faa, nws, openmeteo
from .util import SEVERITY, days_between, today_in

MAX_LEAD_DAYS = 14

LIMITATIONS = [
    "No flight-level data (specific flight status, aircraft, crew). There's no free real-time airline feed, "
    "so this looks at airports and weather, not your exact flight.",
    "En-route weather and air-traffic flow restrictions between airports aren't modeled.",
    "Thresholds are operational rules of thumb, not airline-specific minimums.",
]


class TripError(ValueError):
    pass


def guess_zone(ap: dict) -> dict:
    """Rough time zone from state/longitude, only used if Open-Meteo is down."""
    state, lon = ap["state"], ap["lon"]
    if state == "HI":
        tz = "Pacific/Honolulu"
    elif state == "AK":
        tz = "America/Anchorage"
    elif state == "AZ":
        tz = "America/Phoenix"
    elif lon > -87.5:
        tz = "America/New_York"
    elif lon > -101:
        tz = "America/Chicago"
    elif lon > -114.5:
        tz = "America/Denver"
    else:
        tz = "America/Los_Angeles"
    offset = datetime.now(ZoneInfo(tz)).utcoffset().total_seconds()
    return {"tz": tz, "offset": int(offset)}


def confidence_for(lead_days: int, failed: list[str]) -> dict:
    level = 2 if lead_days <= 1 else 1 if lead_days <= 3 else 0
    if lead_days <= 1:
        reasons = ["Travel is within ~24-48 h, when forecasts are most reliable and the aviation forecast (TAF) applies."]
    elif lead_days <= 3:
        reasons = [f"Travel is {lead_days} days out. Forecasts are useful but timing and intensity can still shift."]
    else:
        reasons = [f"Travel is {lead_days} days out. At this range forecasts show broad patterns only, "
                   "and no aviation forecast or live FAA data applies yet."]
    if failed:
        level = max(0, level - 1)
        reasons.append(f"Missing data from {', '.join(failed)}.")
    return {"level": ["low", "medium", "high"][level], "reasons": reasons}


def score_airport(ap: dict, role: str, zone: dict, lead: int, evidence: list[Evidence]) -> dict:
    drivers = sorted((e for e in evidence if e.severity >= 2), key=lambda e: -e.severity)
    sources = list(dict.fromkeys(e.source for e in drivers))
    top = max((e.severity for e in evidence), default=0)

    # Several moderate hazards that independent sources agree on (say rain + gusts
    # + low ceilings in both the model and the TAF) usually means reduced airport
    # capacity, which is worse than any one of them alone.
    compound = top == 2 and len(drivers) >= 3 and len(sources) >= 2
    if compound:
        top = 3

    return {
        "iata": ap["iata"], "name": ap["name"], "city": ap["city"], "state": ap["state"], "role": role,
        "time_zone": zone["tz"], "lead_days": lead, "rule_level": SEVERITY[top],
        "drivers": [e.id for e in drivers], "corroborated_by": sources,
        "compound": (f"{len(drivers)} moderate hazards confirmed by {len(sources)} independent sources, "
                     "raised to high.") if compound else None,
    }


async def build_evidence(origin: str, destination: str, via: list[str], day: str) -> dict:
    stops = [("origin", origin), ("destination", destination)]
    stops += [(f"connection{i + 1}", v) for i, v in enumerate(v for v in via if v)]

    resolved, errors = {}, []
    for role, query in stops:
        r = resolve_airport(query)
        if r:
            resolved[role] = (query, r)
        else:
            errors.append(f'Couldn\'t find a U.S. airport for "{query}".')
    if not day:
        errors.append("Please provide a travel date.")
    if errors:
        raise TripError(" ".join(errors))

    # Trip airports first, then metro alternates (only used to suggest swaps).
    airports, roles = [], {}
    for role, (_, r) in resolved.items():
        if r.primary["iata"] not in roles:
            roles[r.primary["iata"]] = role
            airports.append(r.primary)
    for role, (_, r) in resolved.items():
        for alt in r.alternates:
            if alt["iata"] not in roles:
                roles[alt["iata"]] = f"alternate-{role}"
                airports.append(alt)

    origin_ap = resolved["origin"][1].primary
    rough_lead = days_between(today_in(guess_zone(origin_ap)["tz"]), day)
    if rough_lead < 0:
        raise TripError("That date is in the past.")
    if rough_lead > MAX_LEAD_DAYS:
        raise TripError(f"That trip is {rough_lead} days out. Forecasts have very little skill beyond about two "
                        f"weeks, so this only checks trips up to {MAX_LEAD_DAYS} days ahead. Try again closer to the date.")

    # Open-Meteo goes first because it gives us each airport's time zone.
    om = await run_source("Open-Meteo", openmeteo.url_for(airports, day), openmeteo.fetch(airports, day))
    zones = om.extra.get("zones") or {a["iata"]: guess_zone(a) for a in airports}
    lead = {a["iata"]: days_between(today_in(zones[a["iata"]]["tz"]), day) for a in airports}
    lead_days = lead[origin_ap["iata"]]

    nws_r, awc_r, faa_r = await asyncio.gather(
        run_source("National Weather Service", "https://api.weather.gov", nws.fetch(airports, day, zones)),
        run_source("NOAA Aviation Weather Center", "https://aviationweather.gov",
                   aviationweather.fetch(airports, day, zones, lead_days == 0)),
        run_source("FAA NAS Status", faa.FAA_URL, faa.fetch(airports, day, zones, lead)),
    )
    sources = [om, nws_r, awc_r, faa_r]

    # For a same-day trip, FAA saying "nothing going on" is worth showing.
    if faa_r.status == "ok" and lead_days == 0:
        for iata in faa_r.extra["quiet"]:
            faa_r.evidence.append(Evidence(
                "FAA NAS Status", iata, "operations", "observed", 0, "No FAA delays or programs reported",
                "No ground stop, ground delay program, delay or closure listed for this airport right now.", faa.FAA_URL))

    evidence = [e for s in sources for e in s.evidence]
    for i, e in enumerate(evidence, 1):
        e.id, e.role = f"E{i}", roles[e.airport]

    scored = [score_airport(a, roles[a["iata"]], zones[a["iata"]], lead[a["iata"]],
                            [e for e in evidence if e.airport == a["iata"]]) for a in airports]
    on_trip = [a for a in scored if not a["role"].startswith("alternate")]
    trip_level = max(SEVERITY.index(a["rule_level"]) for a in on_trip)
    failed = [s.name for s in sources if s.status == "error"]

    def summary(role):
        query, r = resolved[role]
        return r.summary(query)

    return {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trip": {
            "date": day,
            "lead_days": lead_days,
            "origin": summary("origin"),
            "destination": summary("destination"),
            "connections": [summary(role) for role in resolved if role.startswith("connection")],
        },
        "rule_assessment": {"level": SEVERITY[trip_level], "confidence": confidence_for(lead_days, failed),
                            "failed_sources": failed},
        "airports": scored,
        "evidence": [e.to_dict() for e in evidence],
        "sources": [s.summary() for s in sources],
        "limitations": LIMITATIONS,
    }
