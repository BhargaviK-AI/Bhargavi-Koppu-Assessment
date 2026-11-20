import asyncio
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import httpx

from .util import SEVERITY

USER_AGENT = os.getenv("NWS_USER_AGENT", "TripRiskCheck/0.1 (prototype; ops@example.com)")

_client: Optional[httpx.AsyncClient] = None
_client_loop = None
_cache: dict[str, tuple[float, object]] = {}


def _http() -> httpx.AsyncClient:
    # One pooled client per event loop (uvicorn has one; scripts/tests may make several).
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"},
        )
        _client_loop = loop
    return _client


async def get_json(url: str, ttl: int = 300):
    # Small cache so repeated checks (and the alternates sharing a metro) don't
    # hammer the public APIs. NWS in particular asks clients to cache.
    hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    resp = await _http().get(url)
    resp.raise_for_status()
    data = resp.json()
    _cache[url] = (time.monotonic(), data)
    return data


@dataclass
class Evidence:
    source: str
    airport: str
    category: str       # operations | weather-alert | aviation-forecast | weather-forecast | forecast-text | current-conditions
    basis: str          # observed | forecast | official-alert
    severity: int
    title: str
    detail: str
    url: str
    window: Optional[dict] = None
    id: str = ""
    role: str = ""

    def to_dict(self):
        d = asdict(self)
        d["severity_label"] = SEVERITY[self.severity]
        return d


@dataclass
class SourceResult:
    name: str
    url: str
    status: str = "ok"
    ms: int = 0
    note: str = ""
    error: Optional[str] = None
    evidence: list[Evidence] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def summary(self):
        return {"name": self.name, "url": self.url, "status": self.status, "ms": self.ms,
                "note": self.note, "error": self.error, "evidence_count": len(self.evidence)}


async def run_source(name: str, url: str, coro) -> SourceResult:
    """Run one adapter. A failing source is recorded, never fatal."""
    started = time.monotonic()
    try:
        result: SourceResult = await coro
        result.name, result.url = name, url
    except Exception as exc:  # noqa: BLE001 - we want every failure surfaced in the UI
        result = SourceResult(name=name, url=url, status="error", error=str(exc) or type(exc).__name__)
    result.ms = int((time.monotonic() - started) * 1000)
    return result
