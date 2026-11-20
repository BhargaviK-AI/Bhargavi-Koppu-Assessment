"""FAA NAS Status: live ground stops, delay programs, delays, closures, de-icing.

The only source that shows what's actually happening operationally. It's a
snapshot though, so for a trip a few days out it barely counts.
"""
import re
from datetime import datetime

from ..http import Evidence, SourceResult, get_json
from ..util import fmt_local, local_day_window, overlaps

FAA_URL = "https://nasstatus.faa.gov/api/airport-events"

# Most "closures" in this feed are NOTAMs restricting general aviation /
# non-scheduled traffic (e.g. LAX, LAS, SAN almost always have one). They don't
# affect airline passengers, so treating them as closures would be a false alarm.
GA_ONLY = re.compile(r"NON SKED|TRANSIENT GA|GA ACFT|\bPPR\b|PRIOR PERMISSION", re.I)


def is_ga_only(text: str) -> bool:
    return bool(GA_ONLY.search(text or ""))


def _describe(obj: dict) -> str:
    return "; ".join(f"{k}: {v}" for k, v in (obj or {}).items()
                     if v is not None and not isinstance(v, (dict, list)) and not k.lower().endswith(("id", "at")))


def _ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _max_minutes(text: str) -> int:
    m = re.search(r"(\d+)\s*(hour|minute)", text or "", re.I)
    if not m:
        return 0
    return int(m.group(1)) * (60 if m.group(2).lower() == "hour" else 1)


async def fetch(airports: list[dict], day: str, zones: dict, lead_days: dict) -> SourceResult:
    events = await get_json(FAA_URL, ttl=120)
    wanted = {a["iata"] for a in airports}
    result = SourceResult("FAA NAS Status", FAA_URL)
    ga_filtered = 0

    for e in events:
        iata = e.get("airportId")
        if iata not in wanted:
            continue
        zone = zones[iata]
        live = lead_days[iata] == 0
        caveat = "" if live else (f" (Current snapshot. FAA programs usually last hours, so this may not "
                                  f"apply to a {day} trip.)")

        def add(sev, title, detail, window=None):
            # A ground stop right now says little about a flight days away.
            result.evidence.append(Evidence("FAA NAS Status", iata, "operations", "observed",
                                            sev if live else min(sev, 1), title, detail + caveat, FAA_URL, window))

        if e.get("groundStop"):
            add(4, "Ground stop in effect", _describe(e["groundStop"]))
        if e.get("groundDelay"):
            add(3, "Ground delay program in effect", _describe(e["groundDelay"]))
        for kind, d in (("Arrival", e.get("arrivalDelay")), ("Departure", e.get("departureDelay"))):
            if not d:
                continue
            ad = d.get("arrivalDeparture") or {}
            add(2 if _max_minutes(ad.get("max")) >= 45 else 1,
                f"{kind} delays {ad.get('min', '')}–{ad.get('max', '')}".strip(),
                f"Reason: {d.get('reason') or 'n/a'}; trend: {d.get('trend') or ad.get('trend') or 'n/a'}.")
        if e.get("deicing"):
            add(1, "De-icing in progress", _describe(e["deicing"]))

        for key in ("airportClosure", "freeForm"):
            c = e.get(key)
            if not c:
                continue
            text = c.get("simpleText") or c.get("text") or _describe(c)
            if is_ga_only(text):
                ga_filtered += 1
                continue
            frm, to = _ts(c.get("startTime")), _ts(c.get("endTime"))
            start, end = local_day_window(day, zone["offset"])
            applies = key == "airportClosure" and frm is not None and overlaps(frm, to, start, end)
            window = {"from": fmt_local(frm, zone["tz"]),
                      "to": fmt_local(to, zone["tz"]) if to else "open-ended"} if frm else None
            # Closures carry their own time window, so use it rather than the snapshot rule.
            result.evidence.append(Evidence(
                "FAA NAS Status", iata, "operations", "observed", 4 if applies else 0,
                "Airport closure notice" if key == "airportClosure" else "FAA advisory (NOTAM)",
                text, FAA_URL, window))

    quiet = sorted(wanted - {ev.airport for ev in result.evidence})
    result.extra["quiet"] = quiet
    parts = []
    if quiet:
        parts.append(f"No active FAA programs or delays at {', '.join(quiet)}.")
    if ga_filtered:
        parts.append(f"Filtered out {ga_filtered} general-aviation-only NOTAM(s), not relevant to airline passengers.")
    result.note = " ".join(parts)
    return result
