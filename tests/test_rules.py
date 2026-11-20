"""Tests for the deterministic pieces: airport resolution, TAF decoding, the
FAA GA filter, scoring, and the citation guardrail. No network calls."""
from trip_risk.ai import check_citations, regex_parse
from trip_risk.airports import resolve_airport
from trip_risk.assess import confidence_for, score_airport
from trip_risk.http import Evidence
from trip_risk.sources.aviationweather import ceiling_ft, flight_category, period_hazards
from trip_risk.sources.faa import is_ga_only
from trip_risk.sources.nws import alert_severity
from trip_risk.util import hour_ranges


def ev(sev, source="Open-Meteo", airport="SFO", id_="E1"):
    return Evidence(source, airport, "weather-forecast", "forecast", sev, "t", "d", "u", id=id_)


# --- airports ---

def test_metro_alias_picks_default_and_alternates():
    r = resolve_airport("NYC")
    assert r.primary["iata"] == "JFK"
    assert {a["iata"] for a in r.alternates} == {"LGA", "EWR"}


def test_code_inside_text_and_lowercase_words_ignored():
    assert resolve_airport("New York (LGA)").primary["iata"] == "LGA"
    # "new" must not be read as the NEW airport code
    assert resolve_airport("new york").primary["iata"] == "JFK"


def test_ambiguous_city_gets_note_and_state_disambiguates():
    r = resolve_airport("Portland")
    assert r.primary["iata"] == "PDX" and "ME" in r.note
    assert resolve_airport("Portland, ME").primary["iata"] == "PWM"


def test_unknown_place():
    assert resolve_airport("Atlantis") is None


# --- aviation weather ---

def test_flight_categories():
    assert flight_category(400, 10) == "LIFR"
    assert flight_category(800, 10) == "IFR"
    assert flight_category(2500, 10) == "MVFR"
    assert flight_category(None, 10) == "VFR"
    assert flight_category(None, 0.5) == "LIFR"


def test_ceiling_ignores_scattered_layers():
    assert ceiling_ft({"clouds": [{"cover": "SCT", "base": 800}, {"cover": "BKN", "base": 2000}]}) == 2000
    assert ceiling_ft({"clouds": [{"cover": "FEW", "base": 500}]}) is None


def test_sfo_ceiling_sensitivity():
    p = {"clouds": [{"cover": "BKN", "base": 2500}], "visib": "6+"}
    sfo = {h[0]: h[1] for h in period_hazards("SFO", p)}
    oak = {h[0]: h[1] for h in period_hazards("OAK", p)}
    assert sfo["ceiling"] == 2 and oak["ceiling"] == 1


def test_tempo_thunderstorms_are_downgraded():
    firm = period_hazards("ORD", {"wxString": "TSRA", "visib": 3})
    tempo = period_hazards("ORD", {"wxString": "TSRA", "visib": 3, "fcstChange": "TEMPO"})
    sev = lambda hz: {h[0]: h[1] for h in hz}["convective"]
    assert sev(firm) == 3 and sev(tempo) == 2


# --- FAA / NWS ---

def test_ga_only_notams_filtered():
    assert is_ga_only("!LAX 05/277 LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT EXC 24HR PPR")
    assert not is_ga_only("!BOS 11/001 BOS AD AP CLSD SNOW REMOVAL")


def test_alert_mapping():
    assert alert_severity("Blizzard Warning") == 4
    assert alert_severity("Winter Weather Advisory") == 2
    assert alert_severity("Rip Current Statement") == 0


# --- scoring ---

ZONE = {"tz": "America/Los_Angeles", "offset": -28800}
AP = {"iata": "SFO", "name": "x", "city": "x", "state": "CA"}


def test_compound_hazards_raise_level():
    evidence = [ev(2), ev(2), ev(2, source="NOAA Aviation Weather Center")]
    assert score_airport(AP, "destination", ZONE, 0, evidence)["rule_level"] == "high"


def test_single_source_does_not_compound():
    evidence = [ev(2), ev(2), ev(2)]
    assert score_airport(AP, "destination", ZONE, 0, evidence)["rule_level"] == "moderate"


def test_confidence_drops_with_lead_time_and_failures():
    assert confidence_for(0, [])["level"] == "high"
    assert confidence_for(2, [])["level"] == "medium"
    assert confidence_for(6, [])["level"] == "low"
    assert confidence_for(0, ["FAA NAS Status"])["level"] == "medium"


# --- AI guardrail / fallback parser ---

def test_check_citations_drops_unknown_ids_and_flags_gap():
    bundle = {"evidence": [{"id": "E1"}], "rule_assessment": {"level": "info"}}
    a = {"overall_risk": "high",
         "risks": [{"title": "a", "evidence_ids": ["E1", "E99"]}, {"title": "b", "evidence_ids": ["E7"]}],
         "actions": []}
    out = check_citations(a, bundle)
    assert out["risks"][0]["evidence_ids"] == ["E1"]
    assert out["risks"][1]["uncited"] is True
    assert any("rule engine" in i for i in out["validation"]["issues"])


def test_regex_parse():
    p = regex_parse("New York to San Francisco tomorrow", "2026-11-17")
    assert (p["origin"], p["destination"], p["date"]) == ("New York", "San Francisco", "2026-11-18")


def test_hour_ranges():
    assert hour_ranges([6, 7, 8, 13]) == "06:00–09:00, 13:00–14:00"
