"""Build human-readable PDF exports from analysis reports."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fpdf import FPDF

_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")


def _split_pipe_row(line: str) -> list[str] | None:
    """Split a Markdown pipe table row into cells, or None if not a pipe row."""
    s = line.strip()
    if "|" not in s:
        return None
    core = s[1:] if s.startswith("|") else s
    if core.endswith("|"):
        core = core[:-1]
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(core):
        if core[i] == "\\" and i + 1 < len(core) and core[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if core[i] == "|":
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(core[i])
        i += 1
    parts.append("".join(cur).strip())
    if len(parts) < 2:
        return None
    return parts


def _is_separator_row(cells: list[str]) -> bool:
    if len(cells) < 2:
        return False
    return all(_SEP_CELL_RE.match(c.strip() or "-") for c in cells)


def _normalize_markdown_table_rows(raw_rows: list[list[str]]) -> list[list[str]] | None:
    """Drop GFM separator row; ensure at least header + one body row."""
    if len(raw_rows) < 2:
        return None
    ncols = max(len(r) for r in raw_rows)
    for r in raw_rows:
        while len(r) < ncols:
            r.append("")
        del r[ncols:]
    if len(raw_rows) >= 2 and _is_separator_row(raw_rows[1]):
        body = raw_rows[2:]
        out = [raw_rows[0]] + body
    else:
        out = [r for r in raw_rows if not _is_separator_row(r)]
    if len(out) < 2:
        return None
    return out


def _consume_pipe_table_block(lines: list[str], start: int) -> tuple[list[list[str]], int] | None:
    """If lines[start:] begin a pipe table, return (rows for PDF/HTML, index after last row)."""
    i = start
    raw_rows: list[list[str]] = []
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            break
        cells = _split_pipe_row(lines[i])
        if cells is None:
            break
        raw_rows.append(cells)
        i += 1
    if len(raw_rows) < 2:
        return None
    data = _normalize_markdown_table_rows(raw_rows)
    if not data:
        return None
    return data, i


def _iter_text_and_pipe_tables(paragraph: str):
    """Yield ("text", str) and ("table", list[list[str]]) segments in order."""
    lines = paragraph.split("\n")
    i = 0
    text_start = 0
    while i < len(lines):
        consumed = _consume_pipe_table_block(lines, i)
        if consumed is not None:
            rows, end_i = consumed
            if i > text_start:
                chunk = "\n".join(lines[text_start:i]).strip()
                if chunk:
                    yield ("text", chunk)
            yield ("table", rows)
            text_start = end_i
            i = end_i
            continue
        i += 1
    if text_start < len(lines):
        chunk = "\n".join(lines[text_start:]).strip()
        if chunk:
            yield ("text", chunk)


def _write_pdf_pipe_table(
    pdf: FPDF,
    family: str,
    rows: list[list[str]],
    usable_w: float,
    line_h: float,
) -> None:
    """Render a rectangular Markdown-derived table using fpdf2 table()."""
    if not rows:
        return
    ncols = len(rows[0])
    col_fracs = tuple([1] * ncols)
    pdf.set_font(family, "", 9)
    line_height = max(line_h * 1.2, 4.5)
    with pdf.table(
        width=usable_w,
        col_widths=col_fracs,
        line_height=line_height,
        text_align="LEFT",
        first_row_as_headings=True,
    ) as table:
        for data_row in rows:
            table.row(data_row)
    pdf.set_font(family, "", 10)
    pdf.ln(2)


def _write_paragraph_with_tables(
    pdf: FPDF,
    family: str,
    paragraph: str,
    usable_w: float,
    line_h: float,
) -> None:
    """Write a paragraph that may contain Markdown pipe tables."""
    paragraph = paragraph.strip()
    if not paragraph:
        return
    for kind, payload in _iter_text_and_pipe_tables(paragraph):
        if kind == "text":
            pdf.set_font(family, "", 10)
            pdf.multi_cell(usable_w, line_h, payload)
            pdf.ln(2)
        else:
            _write_pdf_pipe_table(pdf, family, payload, usable_w, line_h)

_FONT_DIR = Path(__file__).resolve().parent / "fonts"


def _safe_ticker(ticker: str) -> str:
    cleaned = "".join(c for c in ticker.strip().upper() if c.isalnum())
    return cleaned[:32] or "TICKER"


def export_filename(
    ticker: str,
    analysis_date: date,
    *,
    language: str = "en",
) -> str:
    """Base filename: ``TICKER_YYYY-MM-DD_LANG.pdf``.

    ``analysis_date`` here is only the date segment in the filename; the caller may pass
    a user-local calendar date while the PDF body still uses the session as-of date.
    """
    sym = _safe_ticker(ticker)
    d = analysis_date.isoformat()
    lang_clean = re.sub(r"[^A-Za-z0-9]+", "_", (language or "en").strip()).strip("_").upper() or "EN"
    return f"{sym}_{d}_{lang_clean}.pdf"


def _register_font(pdf: FPDF) -> str:
    """Return font family name to use (DejaVu or Helvetica)."""
    reg = _FONT_DIR / "DejaVuSans.ttf"
    bold = _FONT_DIR / "DejaVuSans-Bold.ttf"
    if reg.is_file():
        pdf.add_font("DejaVu", "", str(reg))
        pdf.add_font("DejaVu", "B", str(bold if bold.is_file() else reg))
        return "DejaVu"
    return "Helvetica"


# Common paths where a CJK-capable font may be found (bundled first, then system).
_CJK_FONT_CANDIDATES = [
    _FONT_DIR / "WQY-ZenHei.ttc",          # bundled in repo (preferred)
    _FONT_DIR / "NotoSansSC-Regular.ttf",
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),          # macOS
    Path("/System/Library/Fonts/STHeiti Light.ttc"),     # macOS
]


def _register_cjk_font(pdf: FPDF) -> str:
    """Register and return a CJK-capable font family name.

    Tries bundled WQY-ZenHei first, then common system paths.
    Raises RuntimeError if no CJK font can be found.
    """
    for candidate in _CJK_FONT_CANDIDATES:
        if candidate.is_file():
            try:
                pdf.add_font("CJK", "", str(candidate))
                pdf.add_font("CJK", "B", str(candidate))
                return "CJK"
            except Exception:
                continue
    raise RuntimeError(
        "No CJK font found for Chinese PDF export. "
        "Ensure service/fonts/WQY-ZenHei.ttc is present "
        "(it is bundled in the repository)."
    )


def _require_cjk(pdf: FPDF) -> str:
    """CJK font required for Chinese / bilingual PDFs."""
    return _register_cjk_font(pdf)


def _require_dejavu(pdf: FPDF) -> str:
    """Alias kept for compatibility — now delegates to _require_cjk."""
    return _require_cjk(pdf)


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
            _write_paragraph_with_tables(pdf, family, para, usable_w, line_h)


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
        tz_note = (
            "时区说明：截至日期为美国东部时区（America/New_York）的公历日；"
            "以当地 00:00 作为下一分析日的分界。"
        )
    else:  # English
        title = "TradingAgents analysis report"
        label_ticker = "Ticker"
        label_date = "As-of session date (daily close)"
        label_analysts = "Analysts"
        label_decision = "Decision"
        label_report = "Report"
        note = "Note: OHLCV from Yahoo Finance is aligned to include this session date."
        tz_note = (
            "Timezone: the as-of date is the calendar date in US Eastern time "
            "(America/New_York). The next analysis day begins at 00:00 local Eastern."
        )

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
        tz_note,
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
        "Timezone: as-of date is the calendar date in US Eastern (America/New_York); "
        "the next analysis day starts at 00:00 local Eastern.",
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
        "时区说明：截至日期为美国东部（America/New_York）公历日；下一分析日以当地 00:00 为界。",
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
