"""
PostgreSQL caching layer for TradingAgents analysis results.

Cache key: (ticker, analysis_date, dimension string).
The dimension string encodes sorted analysts, language, and an LLM profile fingerprint.
On cache hit, stored events are replayed to the job stream — no LLM calls.
On cache miss, results are saved after the analysis completes.
Stale rows (past the US/Eastern midnight following ``analysis_date``) are not served
but remain in the database until overwritten by a new run.

Requires the DATABASE_URL environment variable to point at a PostgreSQL
connection string (e.g. a Neon pooler URL).  If DATABASE_URL is not set
the module degrades gracefully: get_cached_analysis() always returns None
and save_analysis() returns False without writing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any

from service.analysis_dates import analysis_cache_is_stale

logger = logging.getLogger(__name__)

# ── Optional dependency — import once, fail gracefully ───────────────────────

try:
    import psycopg2
    from psycopg2.extras import Json as PgJson, execute_values as _pg_execute_values

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False
    logger.warning("psycopg2 not installed — DB caching disabled")


# ── Connection helper ─────────────────────────────────────────────────────────

def _get_db_url() -> str | None:
    """Return the cleaned DATABASE_URL, or None if not configured."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return None
    # Neon pooler URLs include channel_binding=require which older psycopg2
    # versions don't recognise — strip it before connecting.
    # Handle both ?channel_binding=x (first param) and &channel_binding=x.
    db_url = re.sub(r"channel_binding=[^&]*&?", "", db_url)
    # Clean up any orphaned ? or trailing & left after stripping
    db_url = re.sub(r"\?&", "?", db_url)
    db_url = db_url.rstrip("?&")
    return db_url


def _connect() -> Any | None:
    """Open a fresh DB connection.

    A new connection is opened for every call so there is no risk of stale
    connections being returned from a long-lived pool.  Analysis jobs run
    infrequently (minutes apart) so the overhead is negligible.

    Returns None if DB caching is not configured or psycopg2 is unavailable.
    """
    if not _PSYCOPG2_AVAILABLE:
        return None
    db_url = _get_db_url()
    if not db_url:
        return None
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        return conn
    except Exception as exc:
        logger.error("DB connect failed: %s", exc)
        return None


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol     VARCHAR(20)  PRIMARY KEY,
    name       TEXT         NOT NULL,
    cik        INTEGER,
    updated_at TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tickers_symbol_prefix
    ON tickers (symbol text_pattern_ops);

CREATE TABLE IF NOT EXISTS analysis_cache (
    id                   SERIAL PRIMARY KEY,
    ticker               VARCHAR(20)  NOT NULL,
    analysis_date        DATE         NOT NULL,
    selected_analysts    TEXT         NOT NULL,
    decision             TEXT,
    final_trade_decision TEXT,
    human_readable_report TEXT,
    sections             JSONB,
    pdf_filename         TEXT,
    pdf_data             BYTEA,
    created_at           TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (ticker, analysis_date, selected_analysts)
);

CREATE TABLE IF NOT EXISTS analysis_events (
    id           SERIAL  PRIMARY KEY,
    cache_id     INTEGER REFERENCES analysis_cache(id) ON DELETE CASCADE,
    event_order  INTEGER NOT NULL,
    event_type   TEXT    NOT NULL,
    event_data   JSONB   NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    settings   JSONB     NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


# ── Public API ────────────────────────────────────────────────────────────────


def init_db() -> None:
    """Create tables if they don't exist.  Call once at app startup."""
    conn = _connect()
    if conn is None:
        logger.info("DB caching not configured (DATABASE_URL missing or psycopg2 unavailable)")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        logger.info("DB schema ready")
    except Exception as exc:
        conn.rollback()
        logger.error("DB schema init failed: %s", exc)
    finally:
        conn.close()


def _cache_dimension_key(
    selected_analysts: list[str],
    language: str = "en",
    llm_profile: str = "",
) -> str:
    """Canonical cache dimension: analysts + language + optional LLM profile."""
    base = json.dumps(sorted(selected_analysts)) + f"|{language}"
    if llm_profile:
        return f"{base}|{llm_profile}"
    return base


def get_cached_analysis(
    ticker: str,
    analysis_date: date,
    selected_analysts: list[str],
    language: str = "en",
    llm_profile: str = "",
) -> dict[str, Any] | None:
    """Return a cached result dict, or None if not cached.

    Returned dict keys:
        decision, final_trade_decision, human_readable_report,
        sections (dict), pdf_filename (str|None), pdf_data (bytes|None),
        events (list[dict])
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, decision, final_trade_decision,
                       human_readable_report, sections,
                       pdf_filename, pdf_data
                FROM   analysis_cache
                WHERE  ticker = %s
                  AND  analysis_date = %s
                  AND  selected_analysts = %s
                """,
                (
                    ticker.upper(),
                    analysis_date,
                    _cache_dimension_key(selected_analysts, language, llm_profile),
                ),
            )
            row = cur.fetchone()
            if row is None:
                return None

            if analysis_cache_is_stale(analysis_date):
                logger.info(
                    "Cache stale (past US/Eastern midnight for next day) ticker=%s date=%s",
                    ticker.upper(),
                    analysis_date,
                )
                return None

            (
                cache_id,
                decision,
                final_trade_decision,
                human_readable_report,
                sections,
                pdf_filename,
                pdf_data_raw,
            ) = row

            cur.execute(
                """
                SELECT event_type, event_data
                FROM   analysis_events
                WHERE  cache_id = %s
                ORDER  BY event_order ASC
                """,
                (cache_id,),
            )
            events: list[dict] = []
            for event_type, event_data in cur.fetchall():
                entry: dict = {"type": event_type}
                if isinstance(event_data, dict):
                    entry.update(event_data)
                events.append(entry)

        return {
            "decision": decision or "",
            "final_trade_decision": final_trade_decision or "",
            "human_readable_report": human_readable_report or "",
            "sections": sections or {},
            "pdf_filename": pdf_filename,
            "pdf_data": bytes(pdf_data_raw) if pdf_data_raw is not None else None,
            "events": events,
        }
    except Exception as exc:
        logger.error("get_cached_analysis failed: %s", exc)
        return None
    finally:
        conn.close()


class _SafeEncoder(json.JSONEncoder):
    """Fallback encoder: converts anything non-serialisable to its str repr."""

    def default(self, obj: Any) -> Any:
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def _to_pg_json(value: Any) -> Any:
    """Serialize *value* to a psycopg2 Json adapter, tolerating non-standard types."""
    safe_str = json.dumps(value, cls=_SafeEncoder)
    return PgJson(json.loads(safe_str))


def save_analysis(
    ticker: str,
    analysis_date: date,
    selected_analysts: list[str],
    result: dict[str, Any],
    events: list[dict[str, Any]],
    pdf_path: str | None = None,
    language: str = "en",
    llm_profile: str = "",
) -> bool:
    """Persist an analysis result and its event log to the DB.

    If a row for (ticker, analysis_date, selected_analysts, language) already
    exists it is overwritten (ON CONFLICT DO UPDATE).

    ``pdf_path`` should be the on-disk path of the generated PDF so the raw
    bytes can be stored alongside the result.

    ``llm_profile`` must match the lookup used in get_cached_analysis.

    Returns True if a row was committed; False if DB caching is not configured.
    """
    conn = _connect()
    if conn is None:
        return False

    pdf_data: bytes | None = None
    if pdf_path:
        try:
            from pathlib import Path

            pdf_data = Path(pdf_path).read_bytes()
        except Exception as exc:
            logger.warning("Could not read PDF for DB storage: %s", exc)

    pdf_row_name = result.get("pdf_filename")
    if not pdf_row_name:
        pfs = result.get("pdf_filenames")
        if isinstance(pfs, list) and pfs:
            pdf_row_name = pfs[0]

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_cache
                    (ticker, analysis_date, selected_analysts,
                     decision, final_trade_decision,
                     human_readable_report, sections,
                     pdf_filename, pdf_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, analysis_date, selected_analysts)
                DO UPDATE SET
                    decision              = EXCLUDED.decision,
                    final_trade_decision  = EXCLUDED.final_trade_decision,
                    human_readable_report = EXCLUDED.human_readable_report,
                    sections              = EXCLUDED.sections,
                    pdf_filename          = EXCLUDED.pdf_filename,
                    pdf_data              = EXCLUDED.pdf_data
                RETURNING id
                """,
                (
                    ticker.upper(),
                    analysis_date,
                    _cache_dimension_key(selected_analysts, language, llm_profile),
                    result.get("decision"),
                    result.get("final_trade_decision"),
                    result.get("human_readable_report"),
                    _to_pg_json(result.get("sections") or {}),
                    pdf_row_name,
                    psycopg2.Binary(pdf_data) if pdf_data else None,
                ),
            )
            cache_id: int = cur.fetchone()[0]

            # Replace events (handles the DO UPDATE case)
            cur.execute(
                "DELETE FROM analysis_events WHERE cache_id = %s", (cache_id,)
            )

            for order, event in enumerate(events):
                event_type = event.get("type", "unknown")
                event_data = {k: v for k, v in event.items() if k != "type"}
                cur.execute(
                    """
                    INSERT INTO analysis_events
                        (cache_id, event_order, event_type, event_data)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (cache_id, order, event_type, _to_pg_json(event_data)),
                )

        conn.commit()
        logger.info(
            "Saved analysis to DB: ticker=%s date=%s analysts=%s lang=%s",
            ticker,
            analysis_date,
            selected_analysts,
            language,
        )
        return True
    except Exception as exc:
        conn.rollback()
        logger.error("save_analysis failed: %s", exc)
        raise
    finally:
        conn.close()


def clear_all_analysis_cache() -> dict[str, Any]:
    """Delete every analysis cache row and associated event log rows.

    ``analysis_events`` is cleared via ``TRUNCATE ... CASCADE`` on ``analysis_cache``.
    ``tickers`` and ``app_settings`` are untouched.

    Returns:
        ``{"ok": bool, "removed": int, "detail": str | None}``
        If the database is not configured, ``ok`` is True and ``removed`` is 0.
    """
    conn = _connect()
    if conn is None:
        return {
            "ok": True,
            "removed": 0,
            "detail": "DATABASE_URL not set or psycopg2 unavailable — no PostgreSQL cache to clear.",
        }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM analysis_cache")
            n = int(cur.fetchone()[0])
            cur.execute("TRUNCATE analysis_cache RESTART IDENTITY CASCADE")
        conn.commit()
        logger.info("Cleared analysis_cache (%s rows)", n)
        return {"ok": True, "removed": n, "detail": None}
    except Exception as exc:
        conn.rollback()
        logger.error("clear_all_analysis_cache failed: %s", exc)
        return {"ok": False, "removed": 0, "detail": str(exc)}
    finally:
        conn.close()


def get_app_settings() -> dict[str, Any]:
    """Return global app settings object, or {} if DB unavailable / empty."""
    conn = _connect()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT settings FROM app_settings WHERE id = 1")
            row = cur.fetchone()
            if not row or row[0] is None:
                return {}
            raw = row[0]
            if isinstance(raw, dict):
                return dict(raw)
            if isinstance(raw, str):
                return dict(json.loads(raw))
            return {}
    except Exception as exc:
        logger.error("get_app_settings failed: %s", exc)
        return {}
    finally:
        conn.close()


def save_app_settings(settings: dict[str, Any]) -> bool:
    """Upsert the singleton app_settings row. Returns False if DB unavailable."""
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (id, settings, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    settings = EXCLUDED.settings,
                    updated_at = NOW()
                """,
                (PgJson(settings),),
            )
        conn.commit()
        logger.info("app_settings saved (%d keys)", len(settings))
        return True
    except Exception as exc:
        conn.rollback()
        logger.error("save_app_settings failed: %s", exc)
        raise
    finally:
        conn.close()


# ── Ticker helpers ────────────────────────────────────────────────────────────

def load_tickers_from_db() -> list[tuple[str, str]]:
    """Return all (symbol, name) pairs from the tickers table, or [] on failure."""
    conn = _connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, name FROM tickers ORDER BY symbol")
            return [(row[0], row[1]) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("load_tickers_from_db failed: %s", exc)
        return []
    finally:
        conn.close()


def save_tickers_to_db(pairs: list[tuple[str, str]]) -> bool:
    """Bulk-upsert (symbol, name) pairs into the tickers table.

    Returns True on success, False if DB unavailable.
    """
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            _pg_execute_values(
                cur,
                """
                INSERT INTO tickers (symbol, name)
                VALUES %s
                ON CONFLICT (symbol) DO UPDATE SET
                    name       = EXCLUDED.name,
                    updated_at = NOW()
                """,
                pairs,
                page_size=500,
            )
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        logger.error("save_tickers_to_db failed: %s", exc)
        return False
    finally:
        conn.close()
