# Trip Risk Check

Enter a U.S. trip ("New York to San Francisco tomorrow") and get an assessment of what might disrupt it, the evidence behind each risk, and what to do about it.

## Running it

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add ANTHROPIC_API_KEY
uvicorn trip_risk.app:app --reload
# open http://localhost:8000
```

Tests: `pytest`

It runs without an API key too. The free-text box falls back to a simple parser and the summary falls back to the rule engine, and the UI says so. The airport list (`data/airports.json`) is checked in; `python scripts/build_airports.py` rebuilds it.

## How it works

```
 free text ──► Claude (parse) ──► airports.py (resolve, add metro alternates)
                                          │
              ┌───────────────┬───────────┴──────┬────────────────┐
         Open-Meteo        NWS alerts +       Aviation Weather    FAA NAS
         hourly model      forecast text      TAF / METAR         Status
              └───────────────┴──────────┬───────┴────────────────┘
                                         ▼
                 numbered evidence (E1..En) + rule-based severity per airport
                                         │
                                         ▼
                    Claude (assess) ──► citation check ──► UI
```

1. **Resolve the trip.** City names map to airports deterministically. Multi-airport metros (NYC, Bay Area, DC, ...) also pull in their alternates so the tool can say "OAK looks better than SFO tomorrow".
2. **Fetch the sources** in parallel. Each one is wrapped so a failure is recorded and shown in the UI instead of breaking the check.
3. **Normalize to evidence.** Every signal becomes an item with source, airport, time window, basis (observed / forecast / official alert), a link, and a severity from explicit rules.
4. **Rule baseline.** Each airport gets the worst severity among its items. If three or more moderate hazards are confirmed by two independent sources, it's raised to high. Confidence comes from lead time and from which sources responded.
5. **Claude writes the assessment** from the evidence bundle and nothing else. The output is structured (Pydantic schema): overall risk, confidence and why, risks with timing and likelihood, actions with priority, alternates, what would change the picture.
6. **Guardrail.** Citations to evidence IDs that don't exist are stripped, risks with no valid citation are shown as "unverified", and if Claude's level is two or more steps away from the rule baseline the UI flags it.

The UI shows the evidence and sources first (a few seconds), then fills in Claude's assessment when it arrives.

## Data sources

| Source | What it gives | Why |
|---|---|---|
| [FAA NAS Status](https://nasstatus.faa.gov) | Ground stops, ground delay programs, delays, closures, de-icing | The only feed showing what's actually happening at airports right now |
| [National Weather Service](https://www.weather.gov/documentation/services-web-api) | Active watches/warnings/advisories; forecast text | Official, written by forecasters, carries the most weight |
| [NOAA Aviation Weather Center](https://aviationweather.gov/data/api/) | TAFs (airport forecasts), METARs (current obs) | The forecast airlines and ATC plan around: ceilings, visibility, wind |
| [Open-Meteo](https://open-meteo.com) | Hourly model forecast up to 16 days | Hour-by-hour numbers for the exact date, past the TAF's ~30 h range; also supplies each airport's time zone |
| [OurAirports](https://ourairports.com/data/) | Airport codes, names, coordinates | Turning "Chicago" into ORD/MDW and coordinates |

All free and keyless. No mocked data anywhere.

## Decisions and assumptions

- **Air travel only.** The scenario is frequent U.S. business travel and the example is NYC to SF, so I scoped to flights. Driving routes would need different sources (road conditions, routing).
- **Airports and weather, not specific flights.** Real-time flight status (FlightAware, etc.) is paid. The tool is explicit about this limitation, and Claude is told not to claim knowledge of a specific flight.
- **Rules first, LLM second.** Severity is decided by code I can test and explain, and Claude handles judgment on top: weighing sources against each other, timing ("the worst is 6–11am, an afternoon flight avoids most of it"), and choosing proportionate actions. That keeps the risk level stable between runs and gives me something to check the model against.
- **FAA data is a snapshot.** A ground stop right now matters for a flight today, not one in four days. For future dates FAA items are capped at low severity and labeled as such.
- **GA-only closures are filtered.** Most "closures" in the FAA feed are NOTAMs about general aviation (LAX, LAS, SAN almost always have one). Showing them would be a false alarm, so they're filtered and counted.
- **TEMPO/PROB TAF groups count one level lower**, because the forecast itself says they're possible, not expected.
- **One airport-specific rule as an example:** SFO loses paired approaches below ~3,000 ft ceilings, so it's treated as moderate even when the standard category is only "MVFR". A production version would want a table of these per airport.
- **Time zones matter.** "The travel day" is midnight to midnight local at each airport, and lead time is computed per airport (late evening in Chicago is already tomorrow in New York).
- **14-day limit.** Forecasts have very little skill past about two weeks, so the tool refuses rather than guessing.

## Where AI is used

1. **Parsing free text** into origin / destination / connections / date (low effort setting, fast). Claude only extracts names; picking the airport is deterministic.
2. **Writing the assessment.** Claude gets the evidence bundle and a system prompt describing how much to trust each source, and returns structured JSON. Each risk carries a `basis` (`observed`, `forecast`, or `inference`), so when the model reasons past the data ("these conditions usually mean a ground delay program at SFO") it's labeled as inference.

Model: `claude-opus-5-5` (configurable through `CLAUDE_MODEL`), using server-side fallback in case a request is declined.

## Communicating uncertainty

- Confidence level with the reasons (lead time, missing sources), shown beside the verdict.
- Likelihood per risk (unlikely / possible / likely / occurring now), separate from severity.
- Basis tag per risk, so inference is visibly different from observation.
- Rule baseline shown next to Claude's verdict, with an explanation when they differ.
- Missing data is never presented as "all clear".
- A "what would change this" note that says when to check again.

## Before giving this to real users

- **Flight-level data.** Integrate an airline/flight status API (paid) and let users enter a flight number. This is the biggest gap.
- **Evaluation.** Build an eval set from historical days with known disruptions (BTS on-time data plus archived TAFs/alerts) and measure the rules and Claude's assessments against what actually happened. Use that to tune the thresholds, which are educated guesses right now.
- **En-route and hub effects:** convective SIGMETs and FAA flow programs that affect a route even when both endpoints are fine.
- **Airport sensitivity table** for more airports (like the SFO rule), ideally learned from historical delay data.
- **Monitoring instead of one-off checks.** Re-check booked trips on a schedule and alert when the risk changes (Teams/email). That's what Operations actually needs.
- **Ops:** persistent cache/storage instead of in-memory, rate limiting, retries and backoff on sources, logging of model inputs/outputs for audit, cost tracking, auth (SSO), and a proper NWS User-Agent with a contact address.
- **Prompt and output review** on a sample of real trips with the Ops team before trusting the wording.

## Project layout

```
trip_risk/
  app.py              FastAPI routes + static UI
  assess.py           orchestration, evidence numbering, rule baseline, confidence
  ai.py               Claude parsing + assessment, citation check, rule-only fallback
  airports.py         airport/metro resolution
  http.py             HTTP client, cache, Evidence / SourceResult types
  util.py             time zone and formatting helpers
  sources/            one adapter per external source
static/               HTML/CSS/JS front end
scripts/build_airports.py
tests/test_rules.py
```
