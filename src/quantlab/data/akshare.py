from __future__ import annotations

from datetime import date, datetime, time
import time as time_module

import pandas as pd

from quantlab.data.base import DataProvider, ProviderError
from quantlab.domain.models import Bar, Instrument


class AkShareProvider(DataProvider):
    name = "akshare"

    def instruments(self, asset_type: str | None = None) -> list[Instrument]:
        return []

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc
        result: list[Bar] = []
        for symbol in symbols:
            code = symbol[2:] if symbol[:2] in {"sh", "sz", "bj"} else symbol
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    raw_frame = _history_frame(ak, code, start, end, adjust="")
                    adjusted_frame = _history_frame(ak, code, start, end, adjust="hfq")
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= 2 or not _is_transient_error(exc):
                        raise ProviderError(f"akshare failed for {symbol}: {exc}") from exc
                    time_module.sleep(0.5 * (attempt + 1))
            else:  # pragma: no cover - defensive; the loop either succeeds or raises
                raise ProviderError(f"akshare failed for {symbol}: {last_error}")
            raw_rows = _normalized_rows(raw_frame)
            adjusted_by_date = {row["date"]: row for row in _normalized_rows(adjusted_frame)}
            previous_close: float | None = None
            for row in raw_rows:
                day = row["date"]
                adjusted = adjusted_by_date.get(day)
                close = float(row["close"])
                opened = float(row["open"])
                high = float(row["high"])
                low = float(row["low"])
                suspended, limit_up, limit_down = _trade_flags(
                    code,
                    opened,
                    high,
                    low,
                    close,
                    float(row.get("volume") or 0),
                    previous_close,
                )
                result.append(
                    Bar(
                        symbol=symbol,
                        date=day,
                        open=opened,
                        high=high,
                        low=low,
                        close=close,
                        volume=float(row.get("volume") or 0),
                        amount=float(row.get("amount") or 0),
                        prev_close=previous_close,
                        adjusted_open=(float(adjusted["open"]) if adjusted else None),
                        adjusted_high=(float(adjusted["high"]) if adjusted else None),
                        adjusted_low=(float(adjusted["low"]) if adjusted else None),
                        adjusted_close=(float(adjusted["close"]) if adjusted else None),
                        suspended=suspended,
                        limit_up=limit_up,
                        limit_down=limit_down,
                        available_at=datetime.combine(day, time(15, 0)),
                        source=self.name,
                    )
                )
                previous_close = close
        return result


def _history_frame(ak, code: str, start: date, end: date, adjust: str) -> pd.DataFrame:
    arguments = {
        "symbol": code,
        "period": "daily",
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "adjust": adjust,
    }
    if code.startswith(("5", "1")):
        return ak.fund_etf_hist_em(**arguments)
    return ak.stock_zh_a_hist(**arguments)


def _normalized_rows(frame: pd.DataFrame) -> list[dict]:
    mapping = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    normalized = frame.rename(columns=mapping)
    rows = []
    for row in normalized.to_dict("records"):
        day = row["date"]
        if hasattr(day, "date"):
            day = day.date()
        elif not isinstance(day, date):
            day = date.fromisoformat(str(day)[:10])
        rows.append({**row, "date": day})
    return rows


def _is_transient_error(exc: Exception) -> bool:
    detail = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in detail
        for marker in (
            "connection",
            "remote disconnected",
            "timeout",
            "temporarily unavailable",
            "reset by peer",
            "proxyerror",
        )
    )


def _trade_flags(
    code: str,
    opened: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    previous_close: float | None,
) -> tuple[bool, bool, bool]:
    suspended = volume <= 0
    if suspended or previous_close is None or previous_close <= 0:
        return suspended, False, False
    change = close / previous_close - 1
    threshold = 0.195 if code.startswith(("300", "301", "302", "688")) else 0.095
    tolerance = max(1e-6, close * 1e-5)
    one_price = max(abs(opened - close), abs(high - close), abs(low - close)) <= tolerance
    return (
        suspended,
        bool(one_price and change >= threshold),
        bool(one_price and change <= -threshold),
    )
