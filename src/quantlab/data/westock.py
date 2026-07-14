from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from quantlab.data.base import DataProvider, ProviderError
from quantlab.domain.models import Bar, Instrument


class WestockProvider(DataProvider):
    name = "westock"

    def __init__(self, project_root: Path, node_executable: str | None = None) -> None:
        self.node = node_executable or os.getenv("QUANTLAB_NODE_EXECUTABLE", "node")
        self.script = (
            project_root / "third-party" / "westock" / "westock-data" / "scripts" / "index.js"
        )

    def _run(self, *args: str) -> str:
        if not self.script.exists():
            raise ProviderError(f"westock script not found: {self.script}")
        process = subprocess.run(
            [self.node, str(self.script), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            check=False,
        )
        output = process.stdout.strip()
        if process.returncode != 0:
            raise ProviderError(process.stderr.strip() or output or "westock failed")
        if output.startswith("{"):
            payload = json.loads(output)
            if payload.get("success") is False:
                raise ProviderError(payload.get("error", {}).get("message", "westock query failed"))
        return output

    def doctor(self) -> dict[str, str | bool]:
        version = subprocess.run(
            [self.node, "--version"], capture_output=True, text=True, check=False
        )
        major_match = re.search(r"(\d+)", version.stdout)
        major = int(major_match.group(1)) if major_match else 0
        return {"node": self.node, "version": version.stdout.strip(), "compatible": major >= 18}

    def instruments(self, asset_type: str | None = None) -> list[Instrument]:
        return []  # universe discovery is strategy-specific and intentionally explicit

    def bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        result: dict[tuple[str, date], Bar] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=179))
            raw_bars = self._bars_chunk(symbols, chunk_start, chunk_end, fq=None)
            adjusted = {
                (bar.symbol, bar.date): bar
                for bar in self._bars_chunk(symbols, chunk_start, chunk_end, fq="hfq")
            }
            for bar in raw_bars:
                adj = adjusted.get((bar.symbol, bar.date))
                if adj is not None:
                    bar.adjusted_open = adj.open
                    bar.adjusted_high = adj.high
                    bar.adjusted_low = adj.low
                    bar.adjusted_close = adj.close
                result[(bar.symbol, bar.date)] = bar
            chunk_start = chunk_end + timedelta(days=1)
        bars = sorted(result.values(), key=lambda item: (item.symbol, item.date))
        previous_by_symbol: dict[str, float] = {}
        for bar in bars:
            previous = previous_by_symbol.get(bar.symbol)
            bar.prev_close = previous
            bar.suspended = bar.volume <= 0
            if not bar.suspended and previous and previous > 0:
                change = bar.close / previous - 1
                code = bar.symbol[2:] if bar.symbol[:2] in {"sh", "sz", "bj"} else bar.symbol
                threshold = (
                    0.195 if code.startswith(("300", "301", "302", "688")) else 0.095
                )
                tolerance = max(1e-6, bar.close * 1e-5)
                one_price = (
                    max(
                        abs(bar.open - bar.close),
                        abs(bar.high - bar.close),
                        abs(bar.low - bar.close),
                    )
                    <= tolerance
                )
                bar.limit_up = bool(one_price and change >= threshold)
                bar.limit_down = bool(one_price and change <= -threshold)
            previous_by_symbol[bar.symbol] = bar.close
        return bars

    def _bars_chunk(self, symbols: list[str], start: date, end: date, fq: str | None) -> list[Bar]:
        args = [
            "kline",
            ",".join(symbols),
            "--period",
            "day",
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--raw",
        ]
        if fq:
            args.extend(["--fq", fq])
        output = self._run(*args)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "westock kline did not return JSON; command contract needs an adapter"
            ) from exc
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            rows = rows.get("items", rows.get("klines", []))
        result: list[Bar] = []
        for row in rows or []:
            symbol = (
                row.get("symbol") or row.get("code") or (symbols[0] if len(symbols) == 1 else None)
            )
            if symbol is None:
                raise ProviderError("westock batch kline row is missing symbol")
            close = row.get("close", row.get("last"))
            result.append(
                Bar(
                    symbol=symbol,
                    date=date.fromisoformat(str(row["date"])[:10]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(close),
                    volume=float(row.get("volume") or 0),
                    amount=float(row.get("amount") or 0),
                    source=self.name,
                    available_at=datetime.fromisoformat(str(row["date"])[:10] + "T15:00:00"),
                )
            )
        return result
