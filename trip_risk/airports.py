"""Map what the user typed ("New York", "SFO", "O'Hare") to airports.

This is deliberately not done by the LLM. Claude only pulls the place names out
of free text; picking the airport has to be predictable and testable.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

AIRPORTS: list[dict] = json.loads((Path(__file__).resolve().parent.parent / "data" / "airports.json").read_text())
BY_IATA = {a["iata"]: a for a in AIRPORTS}
BY_ICAO = {a["icao"]: a for a in AIRPORTS if a["icao"]}

# Metro areas with more than one commercial airport. First one is the default.
# The others get assessed too so we can suggest "fly out of LGA instead".
METROS = {
    "New York": ["JFK", "LGA", "EWR"],
    "San Francisco Bay Area": ["SFO", "OAK", "SJC"],
    "Los Angeles": ["LAX", "BUR", "LGB", "SNA"],
    "Chicago": ["ORD", "MDW"],
    "Washington, DC": ["DCA", "IAD", "BWI"],
    "Dallas–Fort Worth": ["DFW", "DAL"],
    "Houston": ["IAH", "HOU"],
    "South Florida": ["MIA", "FLL", "PBI"],
    "Boston": ["BOS", "PVD", "MHT"],
    "Orlando": ["MCO", "SFB"],
}

ALIASES = {
    "new york": "New York", "nyc": "New York", "new york city": "New York", "manhattan": "New York",
    "brooklyn": "New York", "queens": "New York", "newark": "New York",
    "san francisco": "San Francisco Bay Area", "sf": "San Francisco Bay Area", "bay area": "San Francisco Bay Area",
    "san jose": "San Francisco Bay Area", "oakland": "San Francisco Bay Area",
    "los angeles": "Los Angeles", "la": "Los Angeles",
    "chicago": "Chicago",
    "washington": "Washington, DC", "washington dc": "Washington, DC", "washington, dc": "Washington, DC",
    "dc": "Washington, DC", "baltimore": "Washington, DC",
    "dallas": "Dallas–Fort Worth", "fort worth": "Dallas–Fort Worth",
    "houston": "Houston",
    "miami": "South Florida", "fort lauderdale": "South Florida",
    "boston": "Boston",
    "orlando": "Orlando",
}

SIZE_RANK = {"large": 0, "medium": 1, "small": 2}


@dataclass
class Resolved:
    primary: dict
    alternates: list[dict] = field(default_factory=list)
    metro: Optional[str] = None
    matched_by: str = ""
    note: Optional[str] = None

    def summary(self, query: str) -> dict:
        p = self.primary
        return {"query": query, "iata": p["iata"], "name": p["name"], "city": p["city"], "state": p["state"],
                "matched_by": self.matched_by, "note": self.note, "metro": self.metro,
                "alternates": [a["iata"] for a in self.alternates]}


def _with_alternates(ap: dict, matched_by: str, note: Optional[str] = None) -> Resolved:
    for metro, codes in METROS.items():
        if ap["iata"] in codes:
            alts = [BY_IATA[c] for c in codes if c != ap["iata"] and c in BY_IATA]
            return Resolved(ap, alts, metro, matched_by, note)
    return Resolved(ap, [], None, matched_by, note)


def _by_size(a):
    return SIZE_RANK.get(a["size"], 9)


def resolve_airport(query: str) -> Optional[Resolved]:
    q = (query or "").strip()
    if not q:
        return None
    lower = re.sub(r"\s+", " ", q.lower())

    # Whole input is a code ("jfk", "KSFO"), or there's an upper-case code in it
    # ("New York (LGA)"). Lower-case words are skipped so "new york" != NEW.
    codes = [q.upper()] if re.fullmatch(r"[A-Za-z]{3,4}", q) else re.findall(r"\b[A-Z]{3,4}\b", q)
    for code in codes:
        ap = BY_IATA.get(code) or BY_ICAO.get(code)
        if ap:
            return _with_alternates(ap, "airport code")

    if lower in ALIASES:
        return _with_alternates(BY_IATA[METROS[ALIASES[lower]][0]], "metro area")

    city, _, state = (s.strip() for s in lower.partition(","))
    matches = sorted(
        (a for a in AIRPORTS if a["city"].lower() == city and (not state or a["state"].lower() == state)),
        key=_by_size,
    )
    if matches:
        best = matches[0]
        states = list(dict.fromkeys(a["state"] for a in matches))
        note = None
        if len(states) > 1:
            note = (f'"{q}" matches cities in {", ".join(states)}; assumed {best["city"]}, {best["state"]}. '
                    f'Add the state (e.g. "{best["city"]}, {states[1]}") to change it.')
        return _with_alternates(best, "city", note)

    by_name = sorted((a for a in AIRPORTS if lower in a["name"].lower()), key=_by_size)
    if by_name:
        return _with_alternates(by_name[0], "airport name")
    return None


def search_airports(prefix: str, limit: int = 8) -> list[dict]:
    p = prefix.strip().lower()
    if len(p) < 2:
        return []
    hits = [a for a in AIRPORTS
            if a["iata"].lower().startswith(p) or a["city"].lower().startswith(p) or p in a["name"].lower()]
    hits.sort(key=_by_size)
    return [{k: a[k] for k in ("iata", "name", "city", "state")} for a in hits[:limit]]
