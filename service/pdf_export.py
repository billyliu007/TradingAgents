"""Build human-readable PDF exports from analysis reports."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from fpdf import FPDF

_FONT_DIR = Path(__file__).resolve().parent / "fonts"


def _safe_ticker(ticker: str) -> str:
    cleaned = "".join(c for c in ticker.strip().upper() if c.isalnum())
    return cleaned[:32] or "TICKER"


def export_filename(
    ticker: str,
    analysis_date: date,
    analysts: list[Literal["market", "social", "news", "fundamentals"]],
) -> str:
    """Base filename without path: ASSET_DATE_analyst1-analyst2.pdf"""
    sym = _safe_ticker(ticker)
    d = analysis_date.isoformat()
    parts = sorted(set(analysts))
    analyst_part = "-".join(parts) if parts else "none"
    return f"{sym}_{d}_{analyst_part}.pdf"


def _register_font(pdf: FPDF) -> str:
    """Return font family name to use (DejaVu or Helvetica)."""
    reg = _FONT_DIR / "DejaVuSans.ttf"
    bold = _FONT_DIR / "DejaVuSans-Bold.ttf"
    if reg.is_file():
        pdf.add_font("DejaVu", "", str(reg))
        pdf.add_font("DejaVu", "B", str(bold if bold.is_file() else reg))
        return "DejaVu"
    return "Helvetica"


def _write_body(pdf: FPDF, family: str, text: str, usable_w: float, line_h: float) -> None:
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("## "):
            pdf.set_font(family, "B", 12)
            pdf.multi_cell(usable_w, line_h + 1, para[3:].strip())
            pdf.ln(2)
            pdf.set_font(family, "", 10)
        else:
            pdf.set_font(family, "", 10)
            pdf.multi_cell(usable_w, line_h, para)
            pdf.ln(2)


def write_analysis_pdf(
    path: Path,
    *,
    ticker: str,
    analysis_date: date,
    analysts: list[str],
    decision: str,
    human_readable_report: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    family = _register_font(pdf)
    pdf.add_page()

    lm = rm = 18
    pdf.set_margins(lm, 18, rm)
    pdf.set_left_margin(lm)
    usable_w = pdf.w - lm - rm

    pdf.set_font(family, "B", 16)
    pdf.multi_cell(usable_w, 10, "TradingAgents analysis report")
    pdf.ln(4)

    pdf.set_font(family, "", 10)
    meta_lines = [
        f"Ticker: {ticker.strip().upper()}",
        f"Analysis date: {analysis_date.isoformat()}",
        f"Analysts: {', '.join(analysts)}",
        f"Decision: {decision or 'N/A'}",
    ]
    pdf.multi_cell(usable_w, 6, "\n".join(meta_lines))
    pdf.ln(6)

    pdf.set_font(family, "B", 12)
    pdf.multi_cell(usable_w, 8, "Report")
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    _write_body(pdf, family, human_readable_report or "No report content.", usable_w, 5)

    pdf.output(str(path))


def unique_path(directory: Path, filename: str) -> Path:
    """If filename exists, append _2, _3, ... before the extension."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = filename.rsplit(".", 1) if "." in filename else (filename, "pdf")
    candidate = directory / f"{stem}.{suffix}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        alt = directory / f"{stem}_{n}.{suffix}"
        if not alt.exists():
            return alt
        n += 1
