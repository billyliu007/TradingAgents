"""US Eastern calendar rules for analysis_date and DB cache TTL."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

US_EASTERN = ZoneInfo("America/New_York")

# Cache rows whose analysis_date is more than this many Eastern calendar days
# before "today" ET skip TTL — keeps long-ago / backtest dates cacheable.
_CACHE_STALE_LOOKBACK_DAYS = 365


def normalize_analysis_date(client_date: date, *, server_now: datetime | None = None) -> date:
    """Map client ``analysis_date`` to the canonical US Eastern calendar date used for cache and runs.

    - If the client sent **today's UTC calendar date** (typical web UI using ``toISOString()``),
      replace with **today's date in America/New_York** so all regions share one "session day".
    - Otherwise treat the value as an explicit **US Eastern civil calendar** as-of date
      (no UTC midnight shift).
    """
    now = server_now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    utc_today = now.astimezone(timezone.utc).date()
    et_today = now.astimezone(US_EASTERN).date()
    if client_date == utc_today:
        return et_today
    return client_date


def analysis_cache_is_stale(
    analysis_date: date,
    *,
    server_now: datetime | None = None,
) -> bool:
    """True if a cache row for ``analysis_date`` should not be served (Eastern midnight cut).

    Valid until (exclusive) the start of ``analysis_date + 1`` at 00:00 America/New_York.
    Rows for dates more than :data:`_CACHE_STALE_LOOKBACK_DAYS` before today ET are never stale,
    so historical analyses remain cacheable.
    """
    now = server_now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_et_date = now.astimezone(US_EASTERN).date()
    if (now_et_date - analysis_date).days > _CACHE_STALE_LOOKBACK_DAYS:
        return False
    expiry = datetime.combine(
        analysis_date + timedelta(days=1),
        time.min,
        tzinfo=US_EASTERN,
    )
    return now >= expiry
