"""
US stock ticker index — in-memory store populated from SEC EDGAR on startup.

Works with or without a database:
  - With DATABASE_URL: loads from DB on first hit (fast), refreshes from SEC if
    empty, persists new data back to DB.
  - Without DATABASE_URL: fetches directly from SEC EDGAR every startup (~1-2 s).

The module-level list is the authoritative source for search and validation while
the process is running.  All searches are in-memory (no DB query per request).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from bisect import bisect_left
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── SEC EDGAR free ticker list ────────────────────────────────────────────────
_SEC_URL = "https://www.sec.gov/files/company_tickers.json"
_UA = "TradingAgents/1.0 (contact: admin@tradingagents.local)"

# ── In-memory index ───────────────────────────────────────────────────────────
# Two parallel lists, kept sorted by symbol for O(log n) prefix lookup.
_symbols: list[str] = []          # uppercase: "AAPL", "MSFT", …
_names:   list[str] = []          # "Apple Inc.", "Microsoft Corp.", …

_loaded = False   # True once the lists are populated


def is_loaded() -> bool:
    return _loaded


def count() -> int:
    return len(_symbols)


# ── Search ────────────────────────────────────────────────────────────────────

def search(q: str, limit: int = 10) -> list[dict[str, str]]:
    """Return up to *limit* tickers matching *q*.

    Symbol-prefix matches come first (sorted by symbol length then alpha),
    followed by name-substring matches.
    """
    if not _loaded or not q:
        return []
    q_up    = q.strip().upper()
    q_lower = q_up.lower()
    if not q_up:
        return []

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    # 1. Symbol prefix — bisect to the start position, scan forward
    pos = bisect_left(_symbols, q_up)
    for i in range(pos, len(_symbols)):
        if not _symbols[i].startswith(q_up):
            break
        results.append({"symbol": _symbols[i], "name": _names[i]})
        seen.add(_symbols[i])
        if len(results) >= limit:
            return results

    # 2. Name contains — linear but bounded by list size (~10k)
    for sym, name in zip(_symbols, _names):
        if sym in seen:
            continue
        if q_lower in name.lower():
            results.append({"symbol": sym, "name": name})
            seen.add(sym)
            if len(results) >= limit:
                break

    return results


def exists(symbol: str) -> bool | None:
    """True/False if index is loaded; None if index not yet available."""
    if not _loaded:
        return None
    return symbol.strip().upper() in set(_symbols)


# ── Load ──────────────────────────────────────────────────────────────────────

def _set_index(pairs: list[tuple[str, str]]) -> None:
    global _symbols, _names, _loaded
    pairs_sorted = sorted(pairs, key=lambda x: x[0])
    _symbols = [p[0] for p in pairs_sorted]
    _names   = [p[1] for p in pairs_sorted]
    _loaded  = True
    logger.info("Ticker index ready: %d symbols", len(_symbols))


def _fetch_from_sec() -> list[tuple[str, str]]:
    """Download company_tickers.json from SEC EDGAR.
    Returns list of (symbol, name) pairs.
    """
    req = urllib.request.Request(_SEC_URL, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read())
    pairs: list[tuple[str, str]] = []
    for entry in raw.values():
        sym  = str(entry.get("ticker", "")).strip().upper()
        name = str(entry.get("title", "")).strip()
        if sym and name:
            pairs.append((sym, name))
    return pairs


def load(force_refresh: bool = False) -> None:
    """Populate the in-memory index.  Call once at app startup.

    Order of preference:
      1. If DB available and table has data → load from DB (fast).
      2. Otherwise → fetch from SEC EDGAR and persist to DB if available.

    Pass ``force_refresh=True`` to re-download from SEC even when DB is
    populated (useful for periodic refresh).
    """
    global _loaded

    from service import db  # local import — avoids circular imports at module level

    # ── Try loading from DB ───────────────────────────────────────────────────
    if not force_refresh:
        try:
            db_rows = db.load_tickers_from_db()
            if db_rows:
                _set_index(db_rows)
                return
        except Exception as exc:
            logger.warning("Could not load tickers from DB: %s", exc)

    # ── Fetch from SEC EDGAR ──────────────────────────────────────────────────
    logger.info("Downloading ticker list from SEC EDGAR…")
    try:
        pairs = _fetch_from_sec()
    except Exception as exc:
        logger.error("SEC EDGAR fetch failed: %s", exc)
        _loaded = True   # mark loaded (empty) so the app still starts
        return

    if not pairs:
        logger.warning("SEC EDGAR returned no tickers")
        _loaded = True
        return

    _set_index(pairs)

    # ── Persist to DB if available ────────────────────────────────────────────
    try:
        saved = db.save_tickers_to_db(pairs)
        if saved:
            logger.info("Persisted %d tickers to DB", len(pairs))
    except Exception as exc:
        logger.warning("Could not persist tickers to DB (non-fatal): %s", exc)
