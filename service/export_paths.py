from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException

from service.constants import APP_DIR

_EXPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$")


def exports_dir() -> Path:
    raw = os.getenv("TRADINGAGENTS_EXPORTS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return (APP_DIR.parent / "exports").resolve()


def safe_export_basename(name: str) -> bool:
    if not name or len(name) > 240:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_EXPORT_NAME_RE.match(name))


def resolve_export_file(filename: str) -> Path:
    if not safe_export_basename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    base = exports_dir().resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


def paths_for_export_filenames(filenames: list[str]) -> list[Path]:
    """Resolve existing PDF paths under exports dir; dedupe; skip invalid or missing."""
    base = exports_dir().resolve()
    out: list[Path] = []
    seen: set[str] = set()
    for name in filenames:
        if not safe_export_basename(name) or name in seen:
            continue
        seen.add(name)
        path = (base / name).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            continue
        if path.is_file() and path.suffix.lower() == ".pdf":
            out.append(path)
    return out
