"""Build human-readable PDF exports from analysis reports."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import Align, WrapMode

_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")

# fpdf2 defaults multi_cell to JUSTIFY, which adds huge gaps between few Latin tokens
# on lines mixed with CJK (e.g. "3. close_200_sma（200 SMA） —— …").
_MC_LEFT = {"align": Align.L}


def _multicell_body_kwargs(body_language: str | None) -> dict:
    """Left-aligned body text; character wrap for CJK, word wrap for Latin scripts."""
    lk = (body_language or "en").strip().lower()
    if lk in ("zh", "ja"):
        return {"align": Align.L, "wrapmode": WrapMode.CHAR}
    return {"align": Align.L, "wrapmode": WrapMode.WORD}


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
    *,
    body_language: str = "en",
) -> None:
    """Write a paragraph that may contain Markdown pipe tables."""
    paragraph = paragraph.strip()
    if not paragraph:
        return
    mc_kw = _multicell_body_kwargs(body_language)
    for kind, payload in _iter_text_and_pipe_tables(paragraph):
        if kind == "text":
            pdf.set_font(family, "", 10)
            pdf.multi_cell(usable_w, line_h, payload, **mc_kw)
            pdf.ln(2)
        else:
            _write_pdf_pipe_table(pdf, family, payload, usable_w, line_h)

_FONT_DIR = Path(__file__).resolve().parent / "fonts"


def _safe_ticker(ticker: str) -> str:
    cleaned = "".join(c for c in ticker.strip().upper() if c.isalnum())
    return cleaned[:32] or "TICKER"


def _language_uses_cjk_font(language: str) -> bool:
    """Chinese and Japanese body text needs a CJK-capable font (bundled WQY-ZenHei, etc.)."""
    return (language or "").strip().lower() in ("zh", "zh-hans", "zh-hant", "ja")


# Cover / footer strings per analysis language (PDF shell is localized; report body follows the LLM).
_PDF_COVER: dict[str, dict[str, str]] = {
    "en": {
        "title": "TradingAgents analysis report",
        "label_ticker": "Ticker",
        "label_date": "Report generation date (US Eastern)",
        "label_analysts": "Analysts",
        "label_decision": "Decision",
        "label_report": "Report",
        "note": (
            "Note: Quoted OHLCV and indicators use this calendar date as the analysis "
            "horizon where applicable."
        ),
        "tz_note": (
            "This is the US Eastern (America/New_York) calendar date for this analysis run "
            "and this PDF."
        ),
        "legal_disclaimer": (
            "Research and education only. This report is an AI-generated simulation and "
            "is not investment, legal, or tax advice. Incorporates the open-source "
            "TradingAgents framework (Tauric Research), licensed under Apache-2.0."
        ),
        "empty_body": "No report content.",
    },
    "zh": {
        "title": "交易代理分析报告",
        "label_ticker": "股票代码",
        "label_date": "报告生成日期（美国东部）",
        "label_analysts": "分析师",
        "label_decision": "决策",
        "label_report": "报告",
        "note": "注：引用的 OHLCV 与指标在适用情况下以此公历日为分析截止日。",
        "tz_note": (
            "上述日期为美国东部（America/New_York）时区下本次分析与生成本 PDF 的公历日。"
        ),
        "legal_disclaimer": (
            "仅供研究与教育。本报告为 AI 生成模拟，不构成投资、法律或税务建议。"
            "使用开源 TradingAgents 框架（Tauric Research），Apache-2.0 许可。"
        ),
        "empty_body": "没有报告内容。",
    },
    "zh-hant": {
        "title": "TradingAgents 分析報告",
        "label_ticker": "股票代號",
        "label_date": "報告生成日期（美國東部）",
        "label_analysts": "分析師",
        "label_decision": "決策",
        "label_report": "報告",
        "note": "註：引用的 OHLCV 與指標在適用情況下以此曆日為分析截止日。",
        "tz_note": (
            "上述日期為美國東部（America/New_York）時區下本次分析與產生本 PDF 的曆日。"
        ),
        "legal_disclaimer": (
            "僅供研究與教育。本報告為 AI 生成模擬，不構成投資、法律或稅務建議。"
            "使用開源 TradingAgents 框架（Tauric Research），Apache-2.0 許可。"
        ),
        "empty_body": "沒有報告內容。",
    },
    "es": {
        "title": "Informe de análisis TradingAgents",
        "label_ticker": "Ticker",
        "label_date": "Fecha de generación del informe (Este de EE. UU.)",
        "label_analysts": "Analistas",
        "label_decision": "Decisión",
        "label_report": "Informe",
        "note": (
            "Nota: los OHLCV e indicadores citados usan esta fecha calendario como horizonte "
            "del análisis cuando aplica."
        ),
        "tz_note": (
            "Fecha calendario en hora del Este de EE. UU. (America/New_York) de esta ejecución "
            "de análisis y de este PDF."
        ),
        "legal_disclaimer": (
            "Solo para investigación y educación. Este informe es una simulación generada por IA y "
            "no constituye asesoramiento de inversión, legal ni fiscal. Incluye el framework "
            "open source TradingAgents (Tauric Research), bajo licencia Apache-2.0."
        ),
        "empty_body": "Sin contenido del informe.",
    },
    "ja": {
        "title": "TradingAgents 分析レポート",
        "label_ticker": "ティッカー",
        "label_date": "レポート生成日（米東部）",
        "label_analysts": "アナリスト",
        "label_decision": "結論",
        "label_report": "レポート",
        "note": (
            "注: 引用する OHLCV や指標は、必要に応じてこの暦日を分析の基準日として使用します。"
        ),
        "tz_note": (
            "米東部（America/New_York）暦日での、本分析実行および本 PDF の基準日です。"
        ),
        "legal_disclaimer": (
            "研究・教育目的のみ。本レポートは AI によるシミュレーションであり、"
            "投資・法務・税務のアドバイスではありません。オープンソースの "
            "TradingAgents フレームワーク（Tauric Research）を利用、Apache-2.0 ライセンス。"
        ),
        "empty_body": "レポートの内容がありません。",
    },
}


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
    """CJK font required for Chinese / Japanese PDF export."""
    return _register_cjk_font(pdf)


def _write_body(
    pdf: FPDF,
    family: str,
    text: str,
    usable_w: float,
    line_h: float,
    *,
    body_language: str = "en",
) -> None:
    mc_kw = _multicell_body_kwargs(body_language)
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("## "):
            pdf.set_font(family, "B", 12)
            pdf.multi_cell(usable_w, line_h + 1, para[3:].strip(), **mc_kw)
            pdf.ln(2)
            pdf.set_font(family, "", 10)
        else:
            _write_paragraph_with_tables(
                pdf, family, para, usable_w, line_h, body_language=body_language
            )


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
    lang_key = (language or "en").strip().lower()
    if _language_uses_cjk_font(lang_key):
        family = _require_cjk(pdf)
    else:
        family = _register_font(pdf)
    pdf.add_page()

    lm = rm = 18
    pdf.set_margins(lm, 18, rm)
    pdf.set_left_margin(lm)
    usable_w = pdf.w - lm - rm

    cov = _PDF_COVER.get(lang_key, _PDF_COVER["en"])
    title = cov["title"]
    label_ticker = cov["label_ticker"]
    label_date = cov["label_date"]
    label_analysts = cov["label_analysts"]
    label_decision = cov["label_decision"]
    label_report = cov["label_report"]
    note = cov["note"]
    tz_note = cov["tz_note"]
    legal_disclaimer = cov.get("legal_disclaimer", _PDF_COVER["en"]["legal_disclaimer"])
    empty_body = cov["empty_body"]

    pdf.set_font(family, "B", 16)
    pdf.multi_cell(usable_w, 10, title, **_MC_LEFT)
    pdf.ln(4)

    pdf.set_font(family, "", 10)
    meta_lines = [
        f"{label_ticker}: {ticker.strip().upper()}",
        f"{label_date}: {analysis_date.isoformat()}",
        f"{label_analysts}: {', '.join(analysts)}",
        f"{label_decision}: {decision or 'N/A'}",
        note,
        tz_note,
        legal_disclaimer,
    ]
    pdf.multi_cell(usable_w, 6, "\n".join(meta_lines), **_MC_LEFT)
    pdf.ln(6)

    pdf.set_font(family, "B", 12)
    pdf.multi_cell(usable_w, 8, label_report, **_MC_LEFT)
    pdf.ln(2)
    pdf.set_font(family, "", 10)
    _write_body(
        pdf,
        family,
        human_readable_report or empty_body,
        usable_w,
        5,
        body_language=lang_key,
    )

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
