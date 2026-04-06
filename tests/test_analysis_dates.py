"""Tests for US Eastern analysis_date normalization and cache TTL."""

from __future__ import annotations

from datetime import date, datetime, timezone

from service.analysis_dates import (
    US_EASTERN,
    analysis_cache_is_stale,
    normalize_analysis_date,
)


def test_normalize_utc_today_maps_to_eastern_today() -> None:
    # 2024-07-15 06:00 UTC = 2024-07-15 02:00 Eastern (EDT) — same calendar day both zones
    now = datetime(2024, 7, 15, 6, 0, tzinfo=timezone.utc)
    assert normalize_analysis_date(date(2024, 7, 15), server_now=now) == date(2024, 7, 15)

    # 2024-07-15 08:00 UTC = 2024-07-15 04:00 Eastern — still same
    now2 = datetime(2024, 7, 15, 8, 0, tzinfo=timezone.utc)
    assert normalize_analysis_date(date(2024, 7, 15), server_now=now2) == date(2024, 7, 15)


def test_normalize_utc_today_before_eastern_midnight_rolls_et() -> None:
    # 2024-07-15 04:00 UTC = 2024-07-14 24:00 Eastern → still 2024-07-14 in ET (midnight edge)
    # 04:00 UTC July 15 = 00:00 EDT July 15 (EDT = UTC-4) → Eastern date July 15
    now = datetime(2024, 7, 15, 4, 0, tzinfo=timezone.utc)
    et_d = now.astimezone(US_EASTERN).date()
    assert et_d == date(2024, 7, 15)
    # Client sends UTC calendar "today" = July 15
    assert normalize_analysis_date(date(2024, 7, 15), server_now=now) == date(2024, 7, 15)

    # 2024-07-15 03:59 UTC = 2024-07-14 23:59 EDT — Eastern still July 14
    now_before = datetime(2024, 7, 15, 3, 59, tzinfo=timezone.utc)
    assert now_before.astimezone(US_EASTERN).date() == date(2024, 7, 14)
    assert normalize_analysis_date(date(2024, 7, 15), server_now=now_before) == date(
        2024, 7, 14
    )


def test_normalize_explicit_historical_unchanged() -> None:
    now = datetime(2024, 7, 15, 12, 0, tzinfo=timezone.utc)
    assert normalize_analysis_date(date(2020, 6, 1), server_now=now) == date(2020, 6, 1)


def test_cache_not_stale_before_eastern_midnight_next_day() -> None:
    d = date(2024, 7, 14)
    # Last instant before July 15 00:00 Eastern (summer: EDT = UTC-4)
    # July 15 00:00 EDT = July 15 04:00 UTC
    now = datetime(2024, 7, 15, 3, 59, 0, tzinfo=timezone.utc)
    assert not analysis_cache_is_stale(d, server_now=now)


def test_cache_stale_at_eastern_midnight_next_day() -> None:
    d = date(2024, 7, 14)
    now = datetime(2024, 7, 15, 4, 0, 0, tzinfo=timezone.utc)
    assert analysis_cache_is_stale(d, server_now=now)


def test_cache_never_stale_for_old_historical() -> None:
    d = date(2020, 1, 1)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert not analysis_cache_is_stale(d, server_now=now)
