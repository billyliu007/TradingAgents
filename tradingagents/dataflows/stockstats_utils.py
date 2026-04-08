import time
import logging

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from stockstats import wrap
from typing import Annotated
import os
from datetime import timedelta
from datetime import datetime as _dt
from zoneinfo import ZoneInfo
from .config import get_config

logger = logging.getLogger(__name__)

US_EASTERN = ZoneInfo("America/New_York")
_CASH_CLOSE_BUFFER_MIN = 20

def _effective_last_close_day(session_day):
    """Return last completed US cash session day relative to session_day."""
    now_et = _dt.now(US_EASTERN)
    if now_et.date() != session_day:
        return session_day
    close_cutoff = _dt.combine(session_day, _dt.min.time().replace(hour=16, minute=0), tzinfo=US_EASTERN) + timedelta(minutes=_CASH_CLOSE_BUFFER_MIN)
    return session_day if now_et >= close_cutoff else (session_day - timedelta(days=1))

def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        config = get_config()

        curr_date_dt = pd.to_datetime(curr_date)
        # Anchor the fetch window to the session date (US/Eastern calendar day provided by the service/UI),
        # not the server's local wall-clock date.
        end_date = curr_date_dt + pd.Timedelta(days=1)  # yfinance end is exclusive
        start_date = curr_date_dt - pd.DateOffset(years=15)
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")

        # Ensure cache directory exists
        os.makedirs(config["data_cache_dir"], exist_ok=True)

        data_file = os.path.join(
            config["data_cache_dir"],
            f"{symbol}-YFin-data-{start_date_str}-{end_date_str}.csv",
        )

        if os.path.exists(data_file):
            data = pd.read_csv(data_file, on_bad_lines="skip")
        else:
            data = yf_retry(lambda: yf.download(
                symbol,
                start=start_date_str,
                end=end_date_str,
                multi_level_index=False,
                progress=False,
                auto_adjust=True,
            ))
            # Patch last-day NaN bar (wide query) by re-fetching the single session day.
            try:
                if isinstance(data.index, pd.DatetimeIndex):
                    idx_dates = data.index.date
                    target_day = _effective_last_close_day(curr_date_dt.date())
                    if target_day in idx_dates:
                        day_rows = data.loc[idx_dates == target_day]
                        if not day_rows.empty and ("Close" in day_rows.columns) and day_rows["Close"].isna().all():
                            day_start = target_day.strftime("%Y-%m-%d")
                            day_end_excl = (target_day + timedelta(days=1)).strftime("%Y-%m-%d")
                            day_data = yf_retry(lambda: yf.download(
                                symbol,
                                start=day_start,
                                end=day_end_excl,
                                multi_level_index=False,
                                progress=False,
                                auto_adjust=True,
                            ))
                            if not day_data.empty:
                                data = pd.concat([data, day_data])
                                data = data[~data.index.duplicated(keep="last")].sort_index()
            except Exception:
                pass
            data = data.reset_index()
            data.to_csv(data_file, index=False)

        data = _clean_dataframe(data)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        target_day = _effective_last_close_day(curr_date_dt.date())
        curr_date_str = target_day.strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
