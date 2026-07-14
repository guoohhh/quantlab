from __future__ import annotations

import hashlib
import json
import socket
import time as time_module
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from quantlab.data.a_share_symbols import canonical_a_share_symbol
from quantlab.data.base import DataProvider, ProviderError
from quantlab.domain.models import Bar, Instrument


_BAOSTOCK_LOCK = Lock()


@dataclass(frozen=True)
class PointInTimeSecurity:
    symbol: str
    name: str
    exchange: str
    board: str
    trade_status: bool
    source: str = "baostock"
    source_symbol: str | None = None


class BaoStockProvider(DataProvider):
    """Anonymous free source for historical universes, ST state and delisted prices."""

    name = "baostock"

    def __init__(
        self,
        client: Any | None = None,
        *,
        cache_dir: str | Path | None = None,
        max_retries: int = 3,
        request_timeout_seconds: float = 30.0,
    ):
        self._client = client
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max(1, int(max_retries))
        self.request_timeout_seconds = float(request_timeout_seconds)
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")

    @property
    def client(self):
        if self._client is None:
            try:
                import baostock as bs
            except ImportError as exc:  # pragma: no cover - guarded by optional dependency
                raise ProviderError("baostock is not installed") from exc
            self._client = bs
        return self._client

    def instruments(self, asset_type: str | None = None) -> list[Instrument]:
        return []

    def point_in_time_universe(self, day: date) -> list[PointInTimeSecurity]:
        with self._session():
            result = self.client.query_all_stock(day=day.isoformat())
            rows = self._collect(result, f"point-in-time universe for {day}")
        fields = {name: index for index, name in enumerate(result.fields)}
        output: dict[str, PointInTimeSecurity] = {}
        for row in rows:
            raw_code = row[fields["code"]]
            source_symbol = _from_baostock_code(raw_code, canonicalize=False)
            if source_symbol is None:
                continue
            symbol = canonical_a_share_symbol(source_symbol)
            item = PointInTimeSecurity(
                symbol=symbol,
                name=str(row[fields["code_name"]]).strip() or symbol,
                exchange="SH" if symbol.startswith("sh") else "SZ",
                board=_board(symbol),
                trade_status=str(row[fields["tradeStatus"]]) == "1",
                source_symbol=source_symbol,
            )
            existing = output.get(symbol)
            if existing is None or source_symbol == symbol:
                output[symbol] = item
        return sorted(output.values(), key=lambda item: item.symbol)

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        if start > end:
            raise ValueError("bar start date must not be after end date")
        output = []
        canonical_symbols = list(dict.fromkeys(canonical_a_share_symbol(item) for item in symbols))
        for symbol in canonical_symbols:
            cached = self._load_cached_bars(symbol, start, end)
            if cached is not None:
                output.extend(cached)
                continue
            error: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    with self._session():
                        fetched = self._symbol_bars(symbol, start, end)
                    self._save_cached_bars(symbol, start, end, fetched)
                    output.extend(fetched)
                    error = None
                    break
                except Exception as exc:
                    error = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError(
                            f"baostock history for {symbol} failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    )
                    if attempt + 1 < self.max_retries:
                        time_module.sleep(0.5 * (2**attempt))
            if error is not None:
                raise error
        return sorted(output, key=lambda item: (item.symbol, item.date))

    def _cache_path(self, symbol: str, start: date, end: date) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(f"v1:{symbol}:{start}:{end}".encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{symbol}-{digest}.json"

    def _load_cached_bars(self, symbol: str, start: date, end: date) -> list[Bar] | None:
        exact = self._cache_path(symbol, start, end)
        if exact is None:
            return None
        candidates = [exact]
        candidates.extend(
            path
            for path in sorted(
                self.cache_dir.glob(f"{symbol}-*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if path != exact
        )
        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                request = payload.get("request", {})
                if request.get("symbol") != symbol:
                    continue
                cached_start = date.fromisoformat(str(request["start"]))
                cached_end = date.fromisoformat(str(request["end"]))
                if cached_start > start or cached_end < end:
                    continue
                return [
                    bar
                    for bar in (Bar.model_validate(item) for item in payload["items"])
                    if start <= bar.date <= end
                ]
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
        return None

    def cached_bars(self, symbols: list[str] | None = None) -> list[Bar]:
        """Merge valid per-symbol cache fragments without making a network request."""

        if self.cache_dir is None:
            return []
        selected = (
            {canonical_a_share_symbol(item) for item in symbols}
            if symbols is not None
            else None
        )
        merged: dict[tuple[str, date], Bar] = {}
        for path in sorted(self.cache_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                symbol = canonical_a_share_symbol(payload["request"]["symbol"])
                if selected is not None and symbol not in selected:
                    continue
                for item in payload["items"]:
                    bar = Bar.model_validate(item)
                    merged[(bar.symbol, bar.date)] = bar
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(merged.values(), key=lambda item: (item.symbol, item.date))

    def _save_cached_bars(
        self, symbol: str, start: date, end: date, bars: list[Bar]
    ) -> None:
        path = self._cache_path(symbol, start, end)
        if path is None:
            return
        payload = {
            "provider": self.name,
            "request": {
                "symbol": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "items": [bar.model_dump(mode="json") for bar in bars],
        }
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _symbol_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        code = _to_baostock_code(symbol)
        raw_fields = "date,code,open,high,low,close,preclose,volume,amount,tradestatus,pctChg,isST"
        raw_result = self.client.query_history_k_data_plus(
            code,
            raw_fields,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag="3",
        )
        raw_rows = self._collect(raw_result, f"raw history for {symbol}")
        adjusted_result = self.client.query_history_k_data_plus(
            code,
            "date,open,high,low,close",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag="1",
        )
        adjusted_rows = self._collect(adjusted_result, f"adjusted history for {symbol}")
        adjusted = {
            row[0]: {
                "open": _float(row[1]),
                "high": _float(row[2]),
                "low": _float(row[3]),
                "close": _float(row[4]),
            }
            for row in adjusted_rows
            if row and row[0]
        }
        fields = {name: index for index, name in enumerate(raw_result.fields)}
        output = []
        for row in raw_rows:
            day_text = row[fields["date"]]
            opened = _float(row[fields["open"]])
            high = _float(row[fields["high"]])
            low = _float(row[fields["low"]])
            close = _float(row[fields["close"]])
            previous_close = _optional_float(row[fields["preclose"]])
            if min(opened, high, low, close) <= 0:
                continue
            volume = _float(row[fields["volume"]])
            amount = _float(row[fields["amount"]])
            trade_status = str(row[fields["tradestatus"]]) == "1"
            is_st = str(row[fields["isST"]]) == "1"
            limit_up, limit_down = _limit_flags(
                symbol,
                opened,
                high,
                low,
                close,
                previous_close,
                is_st,
                trade_status,
            )
            adjusted_row = adjusted.get(day_text, {})
            day = date.fromisoformat(day_text)
            output.append(
                Bar(
                    symbol=symbol,
                    date=day,
                    open=opened,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                    prev_close=previous_close,
                    adjusted_open=adjusted_row.get("open"),
                    adjusted_high=adjusted_row.get("high"),
                    adjusted_low=adjusted_row.get("low"),
                    adjusted_close=adjusted_row.get("close"),
                    suspended=not trade_status,
                    limit_up=limit_up,
                    limit_down=limit_down,
                    is_st=is_st,
                    available_at=datetime.combine(day, time(15, 0)),
                    source=self.name,
                )
            )
        return output

    @contextmanager
    def _session(self) -> Iterator[None]:
        with _BAOSTOCK_LOCK:
            previous_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(self.request_timeout_seconds)
            try:
                login = self.client.login()
                if login is None:
                    raise ProviderError("baostock login failed: empty response")
                if str(login.error_code) != "0":
                    raise ProviderError(f"baostock login failed: {login.error_msg}")
                try:
                    yield
                finally:
                    self.client.logout()
            finally:
                socket.setdefaulttimeout(previous_timeout)

    @staticmethod
    def _collect(result, label: str) -> list[list[str]]:
        if str(result.error_code) != "0":
            raise ProviderError(f"baostock {label} failed: {result.error_msg}")
        rows = []
        while result.next():
            rows.append(result.get_row_data())
        return rows


def _from_baostock_code(value: str, *, canonicalize: bool = True) -> str | None:
    text = str(value).strip().lower()
    if text.startswith("sh.") and len(text) == 9 and text[3:].startswith("6"):
        symbol = "sh" + text[3:]
        return canonical_a_share_symbol(symbol) if canonicalize else symbol
    if text.startswith("sz.") and len(text) == 9:
        code = text[3:]
        if code.startswith(("000", "001", "002", "003", "300", "301", "302")):
            symbol = "sz" + code
            return canonical_a_share_symbol(symbol) if canonicalize else symbol
    return None


def _to_baostock_code(symbol: str) -> str:
    text = str(symbol).strip().lower()
    if len(text) != 8 or text[:2] not in {"sh", "sz"} or not text[2:].isdigit():
        raise ValueError(f"unsupported BaoStock symbol: {symbol}")
    return f"{text[:2]}.{text[2:]}"


def _board(symbol: str) -> str:
    code = symbol[2:]
    if symbol.startswith("sh") and code.startswith(("688", "689")):
        return "star"
    if symbol.startswith("sz") and code.startswith(("300", "301", "302")):
        return "chinext"
    return "main"


def _limit_flags(
    symbol: str,
    opened: float,
    high: float,
    low: float,
    close: float,
    previous_close: float | None,
    is_st: bool,
    trade_status: bool,
) -> tuple[bool, bool]:
    if not trade_status or previous_close is None or previous_close <= 0:
        return False, False
    code = symbol[2:]
    threshold = (
        0.045 if is_st else 0.195 if code.startswith(("300", "301", "302", "688")) else 0.095
    )
    change = close / previous_close - 1
    tolerance = max(1e-6, close * 1e-5)
    one_price = max(abs(opened - close), abs(high - close), abs(low - close)) <= tolerance
    return bool(one_price and change >= threshold), bool(one_price and change <= -threshold)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if output > 0 else None
