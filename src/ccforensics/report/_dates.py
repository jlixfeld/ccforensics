from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

_N_DAYS_RE = re.compile(r"^(\d+)d$")


def _parse_token(spec: str, now: datetime) -> date:
    spec = spec.strip().lower()
    if spec == "today":
        return now.date()
    if spec == "yesterday":
        return now.date() - timedelta(days=1)
    m = _N_DAYS_RE.match(spec)
    if m:
        return now.date() - timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(spec)
    except ValueError as e:
        raise ValueError(f"unrecognized date spec: {spec!r}") from e


def parse_since(spec: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    d = _parse_token(spec, now)
    return datetime.combine(d, time.min, tzinfo=UTC)


def parse_until(spec: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    d = _parse_token(spec, now)
    return datetime.combine(d, time.max, tzinfo=UTC)
