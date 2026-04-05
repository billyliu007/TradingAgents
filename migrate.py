"""
Run this once to create the DB schema in your Neon (or any Postgres) database.

Usage:
    DATABASE_URL="postgresql://..." python migrate.py
    # or with a .env file:
    python migrate.py
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

db_url = os.getenv("DATABASE_URL")
if not db_url:
    print("DATABASE_URL not set — skipping DB migration.")
    sys.exit(0)

# Neon pooler URLs include channel_binding=require which older psycopg2
# versions don't recognise — strip it so the connection doesn't fail.
import re as _re
db_url = _re.sub(r"[?&]channel_binding=[^&]*", "", db_url)

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

DDL = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol     VARCHAR(20)  PRIMARY KEY,
    name       TEXT         NOT NULL,
    cik        INTEGER,
    updated_at TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tickers_symbol_prefix
    ON tickers (symbol text_pattern_ops);

CREATE TABLE IF NOT EXISTS analysis_cache (
    id                    SERIAL       PRIMARY KEY,
    ticker                VARCHAR(20)  NOT NULL,
    analysis_date         DATE         NOT NULL,
    selected_analysts     TEXT         NOT NULL,
    decision              TEXT,
    final_trade_decision  TEXT,
    human_readable_report TEXT,
    sections              JSONB,
    pdf_filename          TEXT,
    pdf_data              BYTEA,
    created_at            TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (ticker, analysis_date, selected_analysts)
);

CREATE TABLE IF NOT EXISTS analysis_events (
    id           SERIAL   PRIMARY KEY,
    cache_id     INTEGER  REFERENCES analysis_cache(id) ON DELETE CASCADE,
    event_order  INTEGER  NOT NULL,
    event_type   TEXT     NOT NULL,
    event_data   JSONB    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analysis_cache_lookup
    ON analysis_cache (ticker, analysis_date, selected_analysts);

CREATE INDEX IF NOT EXISTS idx_analysis_events_cache_id
    ON analysis_events (cache_id, event_order);

CREATE TABLE IF NOT EXISTS app_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    settings   JSONB     NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

print(f"Connecting to: {db_url[:40]}...")
try:
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(DDL)
        # Verify tables exist
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('tickers', 'analysis_cache', 'analysis_events', 'app_settings')
            ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]
    conn.commit()
    conn.close()
    print(f"Migration complete. Tables ready: {', '.join(tables)}")
except Exception as exc:
    print(f"ERROR: {exc}")
    sys.exit(1)
