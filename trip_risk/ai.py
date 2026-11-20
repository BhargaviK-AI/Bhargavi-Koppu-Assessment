"""Where Claude is used:

1. parse_trip   - free text ("NYC to SF tomorrow morning") -> structured fields.
2. assess_trip  - reads the numbered evidence and writes the assessment: weighs
                  sources against each other, works out timing, picks actions
                  that fit the risk, and cites evidence IDs for every claim.

The rule engine runs either way. With no API key the app falls back to a
rule-only summary and says so in the UI.
"""
import json
import os
import re
import time
from datetime import date
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .util import SEVERITY, tomorrow

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

_client = anthropic.Anthropic() if (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")) else None


def ai_available() -> bool:
    return _client is not None


def _parse(system: str, user: str, schema: type[BaseModel], effort: str, max_tokens: int = 16000):
    resp = _client.beta.messages.parse(
        model=MODEL,
        max_tokens=max_tokens,
        # If the model declines, the API retries on a fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort},
        output_format=schema,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Model output was cut off")
    if resp.parsed_output is None:
        raise RuntimeError("Model output didn't match the schema")
    return resp


# ---- 1. Trip parsing --------------------------------------------------------

class ParsedTrip(BaseModel):
    origin: str = Field(description="Origin city or airport code, e.g. 'New York' or 'SFO'. Empty if missing.")
    destination: str = Field(description="Destination city or airport code. Empty if missing.")
    connections: list[str] = Field(description="Connection airports/cities if mentioned.")
    date: str = Field(description="Travel date YYYY-MM-DD, resolved against today. Empty if not given.")
    time_of_day: str = Field(description="Departure time preference if given ('morning', '6pm'), else empty.")
    clarification: str = Field(description="Short question to ask if origin, destination or date is missing "
                                           "or ambiguous. Empty otherwise.")


def parse_trip(text: str, today: str) -> dict:
    if not _client:
        return {**regex_parse(text, today), "parsed_by": "rules"}
    weekday = date.fromisoformat(today).strftime("%A")
    resp = _parse(
        system=("You pull U.S. air-travel itineraries out of short requests. Resolve relative dates "
                "('tomorrow', 'next Friday') against the date given. Don't invent places or dates."),
        user=f"Today is {today} ({weekday}).\nRequest: {text}",
        schema=ParsedTrip,
        effort="low",
        max_tokens=2000,
    )
    return {**resp.parsed_output.model_dump(), "parsed_by": "claude"}


def regex_parse(text: str, today: str) -> dict:
    """Bare-bones parser so the app still works without an API key."""
    m = re.match(r"^(?:from\s+)?(.+?)\s+(?:to|→|->)\s+(.+?)(?:\s+(?:on\s+)?(today|tomorrow|\d{4}-\d{2}-\d{2}))?\s*$",
                 text.strip(), re.I)
    empty = {"origin": "", "destination": "", "connections": [], "date": "", "time_of_day": ""}
    if not m:
        return {**empty, "clarification": 'Try something like "New York to San Francisco tomorrow".'}
    when = (m.group(3) or "").lower()
    day = today if when == "today" else tomorrow(today) if when == "tomorrow" else when
    return {**empty, "origin": m.group(1), "destination": m.group(2), "date": day,
            "clarification": "" if day else "What date are you traveling?"}


# ---- 2. Assessment ----------------------------------------------------------

Level = Literal["low", "moderate", "high", "severe"]


class Risk(BaseModel):
    title: str
    airport: str = Field(description="IATA code")
    phase: Literal["departure", "arrival", "connection", "whole trip"]
    severity: Level
    likelihood: Literal["unlikely", "possible", "likely", "occurring now"]
    timing: str = Field(description="When during the travel day, local time, or 'all day'.")
    explanation: str = Field(description="What could happen to the trip and why, 1-3 sentences.")
    basis: Literal["observed", "forecast", "inference"] = Field(
        description="observed = happening now (FAA/METAR); forecast = stated in a forecast or alert; "
                    "inference = your own reasoning beyond what the data says.")
    evidence_ids: list[str]


class Action(BaseModel):
    action: str
    reason: str
    priority: Literal["do now", "before departure", "monitor", "optional"]
    evidence_ids: list[str]


class Assessment(BaseModel):
    headline: str = Field(description="One plain-English sentence a busy traveler can act on.")
    overall_risk: Level
    confidence: Literal["low", "medium", "high"]
    confidence_explanation: str = Field(description="Why: lead time, whether sources agree, data gaps.")
    risks: list[Risk]
    actions: list[Action]
    alternates_note: str = Field(description="Whether a nearby alternate airport looks better or worse, "
                                             "citing IDs. Empty if not relevant.")
    what_would_change: str = Field(description="What new info would move this assessment, and when to re-check.")
    disagreement_with_rules: str = Field(description="If overall_risk differs from the rule-based level, why. "
                                                     "Empty otherwise.")


ASSESS_SYSTEM = """\
You're a travel-disruption analyst for a company's Operations team. Employees fly within the U.S. and want \
to know whether a trip is likely to be disrupted, what the risks are, what the evidence is, and what (if \
anything) they should do.

You get a JSON bundle: the trip, each airport's role (origin, destination, connection or alternate), a \
rule-based baseline level, and numbered evidence (E1, E2, ...) pulled live from FAA NAS Status (current \
airport operations), the National Weather Service (official alerts and forecast text), the NOAA Aviation \
Weather Center (TAFs/METARs) and Open-Meteo (hourly model forecast).

Guidelines:
- Only use the evidence provided, and cite the evidence IDs behind every risk and action. If you go beyond \
what the data says (e.g. "these conditions at SFO usually mean a ground delay program"), set basis to \
"inference" and say so.
- Weigh sources for what they are. FAA status is what's happening now and matters less the further out the \
trip is. NWS alerts are issued by forecasters and carry weight. TAFs are the aviation forecast and the best \
guide inside ~30 hours. Open-Meteo is a raw model: good for amounts and longer range, less authoritative. \
Sources agreeing raises confidence; sources disagreeing lowers it and is worth pointing out.
- Think about timing: which part of the day is affected and whether a different departure time avoids it. \
We don't know the exact flight times, so say what you're assuming.
- Alternate airports are there only so you can suggest a swap. They aren't part of the trip.
- Be calibrated. Rain in the forecast is not a disruption. Don't inflate risk to look thorough or play it \
down to sound reassuring. "No action needed" is a perfectly good answer.
- Keep actions concrete and in proportion: pick a morning flight, avoid connections under 90 minutes, turn \
on airline alerts, rebook under a travel waiver if the airline issues one, fly into OAK instead, etc. We have \
no airline or flight-level data, so never claim to know a specific flight's status.
- If your overall_risk differs from the rule-based level, explain why in disagreement_with_rules.
- Write for a busy, non-technical traveler. Plain English, short sentences, translate jargon ("low clouds", \
not just "IFR")."""


def _compact(bundle: dict) -> dict:
    """Strip what the model doesn't need (URLs, numeric severities)."""
    evidence = []
    for e in bundle["evidence"]:
        e = {k: v for k, v in e.items() if k not in ("url", "severity")}
        e["severity"] = e.pop("severity_label")
        evidence.append(e)
    return {
        "trip": bundle["trip"],
        "rule_assessment": bundle["rule_assessment"],
        "airports": bundle["airports"],
        "sources": [{k: s[k] for k in ("name", "status", "note", "error")} for s in bundle["sources"]],
        "evidence": evidence,
        "limitations": bundle["limitations"],
    }


def assess_trip(bundle: dict) -> dict:
    if not _client:
        return {**rule_summary(bundle), "generated_by": "rules",
                "ai_note": "AI isn't configured (no ANTHROPIC_API_KEY), so this summary comes from the rule engine only."}

    started = time.monotonic()
    resp = _parse(
        system=ASSESS_SYSTEM,
        user="Assess this trip. Evidence bundle:\n" + json.dumps(_compact(bundle)),
        schema=Assessment,
        effort="medium",
    )
    result = check_citations(resp.parsed_output.model_dump(), bundle)
    result.update(generated_by="claude", model=resp.model, ms=int((time.monotonic() - started) * 1000),
                  usage={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens})
    return result


def check_citations(a: dict, bundle: dict) -> dict:
    """Guardrail on the model output: drop citations to evidence that doesn't
    exist, mark uncited risks as unverified, and flag a big gap vs. the rules."""
    known = {e["id"] for e in bundle["evidence"]}
    issues = []
    for kind, items in (("risk", a["risks"]), ("action", a["actions"])):
        for item in items:
            bad = [i for i in item["evidence_ids"] if i not in known]
            if bad:
                issues.append(f'Removed unknown evidence IDs {", ".join(bad)} from {kind} '
                              f'"{item.get("title") or item.get("action")}".')
            item["evidence_ids"] = [i for i in item["evidence_ids"] if i in known]
            item["uncited"] = not item["evidence_ids"]

    uncited = sum(r["uncited"] for r in a["risks"])
    if uncited:
        issues.append(f"{uncited} risk(s) have no supporting evidence and are marked unverified.")

    gap = SEVERITY.index(a["overall_risk"]) - SEVERITY.index(bundle["rule_assessment"]["level"])
    if abs(gap) >= 2:
        issues.append(f'Claude rated the trip "{a["overall_risk"]}" but the rule engine rated it '
                      f'"{bundle["rule_assessment"]["level"]}". Check the evidence before relying on either.')
    a["validation"] = {"issues": issues, "rule_gap": gap}
    return a


# ---- Rule-only fallback -----------------------------------------------------

PLAYBOOK = {
    "operations": "Check your airline app. If a ground stop or delay program is active, ask about same-day changes.",
    "weather-alert": "Watch for an airline travel waiver, which lets you change flights without a fee.",
    "aviation-forecast": "Pick a departure outside the affected hours and avoid tight connections.",
    "weather-forecast": "Leave extra time and turn on flight-status notifications.",
    "current-conditions": "Check your airline app for live delays.",
}

PHASE = {"origin": "departure", "destination": "arrival"}


def rule_summary(bundle: dict) -> dict:
    by_id = {e["id"]: e for e in bundle["evidence"]}
    risks = []
    for ap in bundle["airports"]:
        if ap["role"].startswith("alternate"):
            continue
        for eid in ap["drivers"][:4]:
            e = by_id[eid]
            window = e.get("window")
            risks.append({
                "title": e["title"], "airport": ap["iata"], "phase": PHASE.get(ap["role"], "connection"),
                "severity": e["severity_label"],
                "likelihood": "occurring now" if e["basis"] == "observed" else "possible",
                "timing": f"{window['from']} → {window['to']}" if window else "see evidence",
                "explanation": e["detail"], "basis": "observed" if e["basis"] == "observed" else "forecast",
                "evidence_ids": [eid], "uncited": False,
            })

    actions = []
    for cat in dict.fromkeys(by_id[r["evidence_ids"][0]]["category"] for r in risks):
        ids = [r["evidence_ids"][0] for r in risks if by_id[r["evidence_ids"][0]]["category"] == cat]
        actions.append({"action": PLAYBOOK.get(cat, PLAYBOOK["weather-forecast"]),
                        "reason": f"Based on {cat.replace('-', ' ')} signals.", "priority": "before departure",
                        "evidence_ids": ids})
    if not actions:
        actions = [{"action": "No action needed beyond normal flight-status notifications.",
                    "reason": "No moderate or higher signals.", "priority": "optional", "evidence_ids": []}]

    level = bundle["rule_assessment"]["level"]
    level = "low" if level == "info" else level
    where = " and ".join(dict.fromkeys(r["airport"] for r in risks))
    conf = bundle["rule_assessment"]["confidence"]
    failed = bundle["rule_assessment"]["failed_sources"]
    if risks:
        headline = f"{level.capitalize()} risk: {len(risks)} signal(s) at {where}."
    elif failed:
        # Missing data isn't good news - don't present it as "all clear".
        headline = f"Couldn't fully assess this trip: {', '.join(failed)} didn't respond."
    else:
        headline = "No meaningful disruption signals for this trip."
    return {
        "headline": headline,
        "overall_risk": level,
        "confidence": conf["level"],
        "confidence_explanation": " ".join(conf["reasons"]),
        "risks": risks,
        "actions": actions,
        "alternates_note": "",
        "what_would_change": "Check again within 24 hours of departure, when the aviation forecast and live FAA status apply.",
        "disagreement_with_rules": "",
        "validation": {"issues": [], "rule_gap": 0},
    }
