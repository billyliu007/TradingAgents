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
    language: str = "en",
) -> str:
    """Base filename: ASSET_asof_DATE_analyst1-analyst2_LANG.pdf (session / close date).

    ``language`` is typically ``en``, ``zh``, or ``en_zh`` (bilingual PDF).
    """
    sym = _safe_ticker(ticker)
    d = analysis_date.isoformat()
    parts = sorted(set(analysts))
    analyst_part = "-".join(parts) if parts else "none"
    lang_suffix = language.upper()
    return f"{sym}_asof_{d}_{analyst_part}_{lang_suffix}.pdf"


def _register_font(pdf: FPDF) -> str:
    """Return font family name to use (DejaVu or Helvetica)."""
    reg = _FONT_DIR / "DejaVuSans.ttf"
    bold = _FONT_DIR / "DejaVuSans-Bold.ttf"
    if reg.is_file():
        pdf.add_font("DejaVu", "", str(reg))
        pdf.add_font("DejaVu", "B", str(bold if bold.is_file() else reg))
        return "DejaVu"
    return "Helvetica"


def _require_dejavu(pdf: FPDF) -> str:
    """DejaVu is required for Chinese and bilingual PDFs (Helvetica shows garbage)."""
    family = _register_font(pdf)
    if family != "DejaVu":
        raise RuntimeError(
            "Chinese PDF export needs DejaVu fonts. Add DejaVuSans.ttf and "
            "DejaVuSans-Bold.ttf under service/fonts/ (see service/fonts/README.txt)."
        )
    return family


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
    language: str = "en",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    if language == "zh":
        family = _require_dejavu(pdf)
    else:
        family = _register_font(pdf)
    pdf.add_page()

    lm = rm = 18
    pdf.set_margins(lm, 18, rm)
    pdf.set_left_margin(lm)
    usable_w = pdf.w - lm - rm

    # Localized strings
    if language == "zh":
        title = "交易代理分析报告"
        label_ticker = "股票代码"
        label_date = "截至会话日期（日收盘）"
        label_analysts = "分析师"
        label_decision = "决策"
        label_report = "报告"
        note = "注：雅虎财经的OHLCV数据与此会话日期对齐。"
    else:  # English
        title = "TradingAgents analysis report"
        label_ticker = "Ticker"
        label_date = "As-of session date (daily close)"
        label_analysts = "Analysts"
        label_decision = "Decision"
        label_report = "Report"
        note = "Note: OHLCV from Yahoo Finance is aligned to include this session date."

    pdf.set_font(family, "B", 16)
    pdf.multi_cell(usable_w, 10, title)
    pdf.ln(4)

    pdf.set_font(family, "", 10)
    meta_lines = [
        f"{label_ticker}: {ticker.strip().upper()}",
        f"{label_date}: {analysis_date.isoformat()}",
        f"{label_analysts}: {', '.join(analysts)}",
        f"{label_decision}: {decision or 'N/A'}",
        note,
    ]
    pdf.multi_cell(usable_w, 6, "\n".join(meta_lines))
    pdf.ln(6)

    pdf.set_font(family, "B", 12)
    pdf.multi_cell(usable_w, 8, label_report)
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    _write_body(pdf, family, human_readable_report or ("No report content." if language == "en" else "没有报告内容。"), usable_w, 5)

    pdf.output(str(path))


def write_bilingual_analysis_pdf(
    path: Path,
    *,
    ticker: str,
    analysis_date: date,
    analysts: list[str],
    decision_en: str,
    report_en: str,
    decision_zh: str,
    report_zh: str,
) -> None:
    """One PDF: English report first, then Chinese on a new page (same ticker/date)."""
    path.parent.mkdir(parents=True, exist_ok=True)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    family = _require_dejavu(pdf)
    pdf.add_page()

    lm = rm = 18
    pdf.set_margins(lm, 18, rm)
    pdf.set_left_margin(lm)
    usable_w = pdf.w - lm - rm
    sym = ticker.strip().upper()
    analyst_line = ", ".join(analysts)

    pdf.set_font(family, "B", 15)
    pdf.multi_cell(usable_w, 8, "TradingAgents — Bilingual analysis report")
    pdf.set_font(family, "", 10)
    pdf.multi_cell(usable_w, 6, "English section first, followed by 中文报告")
    pdf.ln(5)

    # ── English (front) ─────────────────────────────────────────────────────
    pdf.set_font(family, "B", 13)
    pdf.multi_cell(usable_w, 8, "English")
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    en_meta = [
        f"Ticker: {sym}",
        f"As-of session date (daily close): {analysis_date.isoformat()}",
        f"Analysts: {analyst_line}",
        f"Decision: {decision_en or 'N/A'}",
        "Note: OHLCV from Yahoo Finance is aligned to include this session date.",
    ]
    pdf.multi_cell(usable_w, 6, "\n".join(en_meta))
    pdf.ln(4)
    pdf.set_font(family, "B", 12)
    pdf.multi_cell(usable_w, 8, "Report")
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    _write_body(pdf, family, report_en or "No report content.", usable_w, 5)

    pdf.add_page()

    # ── Chinese (after English) ─────────────────────────────────────────────
    pdf.set_font(family, "B", 13)
    pdf.multi_cell(usable_w, 8, "中文")
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    zh_meta = [
        f"股票代码: {sym}",
        f"截至会话日期（日收盘）: {analysis_date.isoformat()}",
        f"分析师: {analyst_line}",
        f"决策: {decision_zh or 'N/A'}",
        "注：雅虎财经的 OHLCV 数据与此会话日期对齐。",
    ]
    pdf.multi_cell(usable_w, 6, "\n".join(zh_meta))
    pdf.ln(4)
    pdf.set_font(family, "B", 12)
    pdf.multi_cell(usable_w, 8, "报告")
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    _write_body(pdf, family, report_zh or "没有报告内容。", usable_w, 5)

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
