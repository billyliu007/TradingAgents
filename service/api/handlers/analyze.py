from __future__ import annotations

from fastapi import HTTPException

from service.analysis import cache_lookup, cache_save, execute_analysis, normalize_analyze_request
from service.schemas import AnalyzeRequest, AnalyzeResponse
from service.server_logging import log_message


def sync_analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    payload = normalize_analyze_request(payload)
    cached = cache_lookup(payload, label="sync")
    if cached is not None:
        log_message(f"[sync] Cache hit ticker={payload.ticker} lang={payload.language}")
        return AnalyzeResponse(
            decision=cached["decision"],
            final_trade_decision=cached.get("final_trade_decision", ""),
            human_readable_report=cached.get("human_readable_report", ""),
            sections=cached.get("sections") or {},
            raw_state={},
            pdf_filenames=None,
            pdf_download_urls=None,
            analysis_date=payload.analysis_date,
        )
    try:
        result = execute_analysis(payload, job_id=None)
        cache_save(payload, result, [], label="sync")
        return AnalyzeResponse(
            decision=result["decision"],
            final_trade_decision=result["final_trade_decision"],
            human_readable_report=result["human_readable_report"],
            sections=result["sections"],
            raw_state=result["raw_state"],
            pdf_filenames=result.get("pdf_filenames"),
            pdf_download_urls=result.get("pdf_download_urls"),
            analysis_date=payload.analysis_date,
        )
    except Exception as exc:
        log_message(f"Analyze failed ticker={payload.ticker} error={exc}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc
