import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # before ai.py reads ANTHROPIC_API_KEY

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .ai import ai_available, assess_trip, parse_trip  # noqa: E402
from .airports import search_airports  # noqa: E402
from .assess import TripError, build_evidence  # noqa: E402

log = logging.getLogger("trip_risk")
app = FastAPI(title="Trip Risk Check")

# Keep bundles briefly so /assess runs on exactly the evidence the user is looking at.
BUNDLE_TTL = 30 * 60
_bundles: dict[str, tuple[float, dict]] = {}


class ParseRequest(BaseModel):
    text: str
    today: Optional[str] = None


class TripRequest(BaseModel):
    origin: str
    destination: str
    via: list[str] = []
    date: str


class AssessRequest(BaseModel):
    id: str


@app.get("/api/health")
def health():
    return {"ai": ai_available()}


@app.get("/api/airports")
def airports(q: str = ""):
    return search_airports(q)


@app.post("/api/parse")
async def parse(req: ParseRequest):
    try:
        return await run_in_threadpool(parse_trip, req.text[:300], req.today or date.today().isoformat())
    except Exception as exc:
        log.exception("parse failed")
        raise HTTPException(502, f"Couldn't interpret that request: {exc}")


@app.post("/api/evidence")
async def evidence(req: TripRequest):
    try:
        bundle = await build_evidence(req.origin, req.destination, req.via, req.date)
    except TripError as exc:
        raise HTTPException(422, str(exc))

    now = time.time()
    for key in [k for k, (at, _) in _bundles.items() if now - at > BUNDLE_TTL]:
        del _bundles[key]
    _bundles[bundle["id"]] = (now, bundle)
    return bundle


@app.post("/api/assess")
async def assess(req: AssessRequest):
    entry = _bundles.get(req.id)
    if not entry:
        raise HTTPException(404, "Evidence expired, please run the check again.")
    try:
        return await run_in_threadpool(assess_trip, entry[1])
    except Exception as exc:
        log.exception("assessment failed")
        raise HTTPException(502, f"AI assessment failed: {exc}")


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static", html=True), name="static")
