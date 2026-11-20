from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Coarse on purpose - the inputs are mostly forecasts.
SEVERITY = ["info", "low", "moderate", "high", "severe"]


def local_day_window(day: str, utc_offset_s: int) -> tuple[float, float]:
    """Epoch seconds for local midnight -> midnight on `day` (YYYY-MM-DD)."""
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() - utc_offset_s
    return start, start + 86400


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and (a_end if a_end is not None else float("inf")) > b_start


def today_in(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def days_between(d1: str, d2: str) -> int:
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def tomorrow(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def fmt_local(ts: float, tz: str) -> str:
    dt = datetime.fromtimestamp(ts, ZoneInfo(tz))
    return dt.strftime("%a %-I:%M %p %Z")


def hour_ranges(hours: list[int]) -> str:
    """[6, 7, 8, 13] -> '06:00-09:00, 13:00-14:00'"""
    ranges: list[list[int]] = []
    for h in hours:
        if ranges and ranges[-1][1] == h:
            ranges[-1][1] = h + 1
        else:
            ranges.append([h, h + 1])
    return ", ".join(f"{a:02d}:00–{b:02d}:00" for a, b in ranges)
