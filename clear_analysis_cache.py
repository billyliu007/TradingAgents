#!/usr/bin/env python3
"""
Empty the PostgreSQL analysis_cache table (and analysis_events via CASCADE).

Usage:
    DATABASE_URL="postgresql://..." python clear_analysis_cache.py
    # or with .env in the repo root:
    python clear_analysis_cache.py

Does nothing if DATABASE_URL is unset (prints a message and exits 0).
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from service import db  # noqa: E402  (after load_dotenv)


def main() -> int:
    out = db.clear_all_analysis_cache()
    detail = out.get("detail")
    if detail:
        print(detail)
    else:
        print(f"Removed {out.get('removed', 0)} cached analysis row(s).")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
