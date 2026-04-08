"""
US stock ticker index — in-memory store populated at startup.

Load order (``force_refresh=False``):
  1. **Local JSON file** (``service/data/us_tickers.json`` or
     ``TRADINGAGENTS_TICKERS_FILE``) — no network; best for cold starts.
  2. **SEC EDGAR** ``company_tickers.json`` — then write the JSON file (best-effort).

Search / validation are in-memory after load. The symbol list is never read from PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from bisect import bisect_left
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── SEC EDGAR free ticker list ────────────────────────────────────────────────
_SEC_URL = "https://www.sec.gov/files/company_tickers.json"
_UA = "TradingAgents/1.0 (contact: admin@tradingagents.local)"

# Reject tiny / corrupt files (SEC feed is ~10k+ symbols).
_MIN_FILE_PAIRS = 500

# ── In-memory index ─────────────────────────────────────────────────────────
# Two parallel lists, kept sorted by symbol for O(log n) prefix lookup.
_symbols: list[str] = []          # uppercase: "AAPL", "MSFT", …
_names: list[str] = []             # "Apple Inc.", "Microsoft Corp.", …

_loaded = False   # True once the lists are populated


def is_loaded() -> bool:
    return _loaded


def count() -> int:
    return len(_symbols)


def tickers_file_path() -> Path:
    """Resolved path to the on-disk ticker cache (may not exist yet)."""
    raw = (os.environ.get("TRADINGAGENTS_TICKERS_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "us_tickers.json"


def read_tickers_file(path: Path) -> list[tuple[str, str]] | None:
    """Parse *path* into ``(symbol, name)`` pairs, or ``None`` if missing/invalid.

    Accepts ``{"version":1,"pairs":[["SYM","Name"],...]}`` or a bare JSON array
    of pairs. Symbols are uppercased; rows must pass a minimum count check.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data: Any = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Ticker file unreadable %s: %s", path, exc)
        return None

    pairs_raw: list[Any]
    if isinstance(data, dict) and isinstance(data.get("pairs"), list):
        pairs_raw = data["pairs"]
    elif isinstance(data, list):
        pairs_raw = data
    else:
        logger.warning("Ticker file has unknown shape: %s", path)
        return None

    out: list[tuple[str, str]] = []
    for row in pairs_raw:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        sym = str(row[0]).strip().upper()
        name = str(row[1]).strip()
        if sym and name:
            out.append((sym, name))

    if len(out) < _MIN_FILE_PAIRS:
        logger.warning(
            "Ticker file %s has only %d pairs (need >= %d); ignoring",
            path,
            len(out),
            _MIN_FILE_PAIRS,
        )
        return None
    return out


def _save_tickers_file(path: Path, pairs: list[tuple[str, str]]) -> bool:
    """Atomically write ticker pairs to *path*."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "source": "sec_edgar_company_tickers",
            "count": len(pairs),
            "pairs": [[s, n] for s, n in pairs],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        logger.info("Wrote %d tickers to %s", len(pairs), path)
        return True
    except OSError as exc:
        logger.warning("Could not write ticker file %s (non-fatal): %s", path, exc)
        return False


# ── Search ────────────────────────────────────────────────────────────────────


def search(q: str, limit: int = 10) -> list[dict[str, str]]:
    """Return up to *limit* tickers matching *q*.

    Symbol-prefix matches come first (sorted by symbol length then alpha),
    followed by name-substring matches.
    """
    if not _loaded or not q:
        return []
    q_up = q.strip().upper()
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
    _names = [p[1] for p in pairs_sorted]
    _loaded = True
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
        sym = str(entry.get("ticker", "")).strip().upper()
        name = str(entry.get("title", "")).strip()
        if sym and name:
            pairs.append((sym, name))
    return pairs


def load(force_refresh: bool = False) -> None:
    """Populate the in-memory index.  Call once at app startup.

    Order of preference (unless ``force_refresh``):
      1. Local JSON file (``tickers_file_path()``).
      2. SEC EDGAR download → write JSON file (best-effort).

    Pass ``force_refresh=True`` to re-download from SEC even when the file exists.
    """
    global _loaded

    cache_path = tickers_file_path()

    # ── Try local file ────────────────────────────────────────────────────────
    if not force_refresh:
        file_pairs = read_tickers_file(cache_path)
        if file_pairs:
            _set_index(file_pairs)
            return

    # ── Fetch from SEC EDGAR ─────────────────────────────────────────────────
    logger.info("Downloading ticker list from SEC EDGAR…")
    try:
        pairs = _fetch_from_sec()
    except Exception as exc:
        logger.error("SEC EDGAR fetch failed: %s", exc)
        _loaded = True  # mark loaded (empty) so the app still starts
        return

    if not pairs:
        logger.warning("SEC EDGAR returned no tickers")
        _loaded = True
        return

    _set_index(pairs)
    _save_tickers_file(cache_path, pairs)
