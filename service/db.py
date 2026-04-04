"""
PostgreSQL caching layer for TradingAgents analysis results.

Cache key: (ticker, analysis_date, selected_analysts sorted).
On cache hit, stored events are replayed to the job stream — no LLM calls.
On cache miss, results are saved after the analysis completes.

Requires the DATABASE_URL environment variable to point at a PostgreSQL
connection string (e.g. a Neon pooler URL).  If DATABASE_URL is not set
the module degrades gracefully: get_cached_analysis() always returns None
and save_analysis() is a no-op.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# ── Optional dependency -  import once, fail gracefully ──────────────────────

try:
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import Json as PgJson

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False
    logger.warning("psycopg2 not installed — DB caching disabled")

# ── Connection pool (lazy init) ───────────────────────────────────────────────

_pool: Any = None  # psycopg2.pool.ThreadedConnectionPool | None


def _get_pool() -> Any:
    global _pool
    if _pool is not None:
        return _pool
    if not _PSYCOPG2_AVAILABLE:
        return None
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return None
    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, db_url)
        logger.info("DB connection pool created")
    except Exception as exc:
        logger.error("Failed to create DB pool: %s", exc)
        _pool = None
    return _pool


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
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
"""


# ── Public API ────────────────────────────────────────────────────────────────


def init_db() -> None:
    """Create tables if they don't exist.  Call once at app startup."""
    pool = _get_pool()
    if pool is None:
        logger.info("DB caching not configured (DATABASE_URL missing or psycopg2 unavailable)")
        return
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        logger.info("DB schema ready")
    except Exception as exc:
        conn.rollback()
        logger.error("DB schema init failed: %s", exc)
    finally:
        pool.putconn(conn)


def _analysts_key(selected_analysts: list[str]) -> str:
    """Canonical cache key for the analyst list."""
    return json.dumps(sorted(selected_analysts))


def get_cached_analysis(
    ticker: str,
    analysis_date: date,
    selected_analysts: list[str],
) -> dict[str, Any] | None:
    """Return a cached result dict, or None if not cached.

    Returned dict keys:
        decision, final_trade_decision, human_readable_report,
        sections (dict), pdf_filename (str|None), pdf_data (bytes|None),
        events (list[dict])
    """
    pool = _get_pool()
    if pool is None:
        return None
    conn = pool.getconn()
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
                (ticker.upper(), analysis_date, _analysts_key(selected_analysts)),
            )
            row = cur.fetchone()
            if row is None:
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
        pool.putconn(conn)


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
) -> None:
    """Persist an analysis result and its event log to the DB.

    If a row for (ticker, analysis_date, selected_analysts) already exists
    it is overwritten (ON CONFLICT DO UPDATE).

    ``pdf_path`` should be the on-disk path of the generated PDF so the raw
    bytes can be stored alongside the result.
    """
    pool = _get_pool()
    if pool is None:
        return

    pdf_data: bytes | None = None
    if pdf_path:
        try:
            from pathlib import Path

            pdf_data = Path(pdf_path).read_bytes()
        except Exception as exc:
            logger.warning("Could not read PDF for DB storage: %s", exc)

    conn = pool.getconn()
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
                    _analysts_key(selected_analysts),
                    result.get("decision"),
                    result.get("final_trade_decision"),
                    result.get("human_readable_report"),
                    _to_pg_json(result.get("sections") or {}),
                    result.get("pdf_filename"),
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
            "Saved analysis to DB: ticker=%s date=%s analysts=%s",
            ticker,
            analysis_date,
            selected_analysts,
        )
    except Exception as exc:
        conn.rollback()
        logger.error("save_analysis failed: %s", exc)
        raise
    finally:
        pool.putconn(conn)
