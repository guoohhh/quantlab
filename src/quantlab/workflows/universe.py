from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from math import floor
from typing import Any

import pandas as pd

from quantlab.config import Settings
from quantlab.data import (
    ALIAS_REGISTRY_VERSION,
    BaoStockProvider,
    a_share_symbol_aliases,
    canonical_a_share_symbol,
)
from quantlab.persistence import AShareUniverseRepository
from quantlab.security import safe_error_detail


def refresh_a_share_security_master(
    settings: Settings,
    as_of: date | None = None,
    *,
    frames: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """Build a versioned Shanghai/Shenzhen listing and delisting master."""

    effective_as_of = as_of or date.today()
    sources = []
    degraded_sources = []
    loaded: dict[str, pd.DataFrame] = {}
    if frames is None:
        import akshare as ak

        calls = {
            "sse_main_active": lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
            "sse_star_active": lambda: ak.stock_info_sh_name_code(symbol="科创板"),
            "szse_active": lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
            "sse_delisted": lambda: ak.stock_info_sh_delist(symbol="全部"),
            "szse_delisted": lambda: ak.stock_info_sz_delist(symbol="终止上市公司"),
        }
        for name, call in calls.items():
            try:
                loaded[name] = call()
                sources.append(name)
            except Exception as exc:
                loaded[name] = pd.DataFrame()
                degraded_sources.append(f"{name}: {safe_error_detail(exc)}")
    else:
        loaded = {name: frame.copy() for name, frame in frames.items()}
        sources = sorted(name for name, frame in loaded.items() if not frame.empty)

    records: dict[str, dict[str, Any]] = {}
    for source_name, board in (("sse_main_active", "main"), ("sse_star_active", "star")):
        for row in loaded.get(source_name, pd.DataFrame()).to_dict("records"):
            code = _digits(row.get("证券代码"))
            if not _is_supported_a_share("SH", code):
                continue
            symbol = "sh" + code
            records[symbol] = _master_record(
                symbol,
                row.get("证券简称") or row.get("公司简称"),
                "SH",
                board,
                row.get("上市日期"),
                None,
                "active",
                source_name,
                row,
            )
    for row in loaded.get("szse_active", pd.DataFrame()).to_dict("records"):
        code = _digits(row.get("A股代码"))
        if not _is_supported_a_share("SZ", code):
            continue
        symbol = "sz" + code
        records[symbol] = _master_record(
            symbol,
            row.get("A股简称"),
            "SZ",
            _board(symbol),
            row.get("A股上市日期"),
            None,
            "active",
            "szse_active",
            row,
        )
    for row in loaded.get("sse_delisted", pd.DataFrame()).to_dict("records"):
        code = _digits(row.get("公司代码"))
        if not _is_supported_a_share("SH", code):
            continue
        symbol = "sh" + code
        if symbol in records:
            continue
        records[symbol] = _master_record(
            symbol,
            row.get("公司简称"),
            "SH",
            _board(symbol),
            row.get("上市日期"),
            row.get("暂停上市日期"),
            "delisted",
            "sse_delisted",
            row,
        )
    for row in loaded.get("szse_delisted", pd.DataFrame()).to_dict("records"):
        code = _digits(row.get("证券代码"))
        if not _is_supported_a_share("SZ", code):
            continue
        symbol = "sz" + code
        if symbol in records:
            continue
        records[symbol] = _master_record(
            symbol,
            row.get("证券简称"),
            "SZ",
            _board(symbol),
            row.get("上市日期"),
            row.get("终止上市日期"),
            "delisted",
            "szse_delisted",
            row,
        )
    if not records:
        raise ValueError("A-share security master returned no usable records")

    ordered = [records[symbol] for symbol in sorted(records)]
    canonical = [
        {
            key: item.get(key)
            for key in (
                "symbol",
                "name",
                "exchange",
                "board",
                "listing_date",
                "delisting_date",
                "status",
                "source",
            )
        }
        for item in ordered
    ]
    version_hash = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    status_counts = Counter(item["status"] for item in ordered)
    board_counts = Counter(item["board"] for item in ordered)
    audit = {
        "records": len(ordered),
        "active": status_counts.get("active", 0),
        "delisted": status_counts.get("delisted", 0),
        "boards": dict(board_counts),
        "missing_listing_dates": sum(item["listing_date"] is None for item in ordered),
        "missing_delisting_dates": sum(
            item["status"] == "delisted" and item["delisting_date"] is None for item in ordered
        ),
        "degraded_sources": degraded_sources,
        "symbol_alias_registry": {
            "version": ALIAS_REGISTRY_VERSION,
            "aliases": a_share_symbol_aliases(),
        },
    }
    repository = AShareUniverseRepository(settings.resolve(settings.get("system.database_path")))
    build_id = repository.save_master_build(
        as_of=effective_as_of,
        version_hash=version_hash,
        records=ordered,
        sources=sources,
        audit=audit,
    )
    return {
        "build_id": build_id,
        "as_of": effective_as_of.isoformat(),
        "version_hash": version_hash,
        "sources": sources,
        "audit": audit,
    }


def capture_point_in_time_universe(
    settings: Settings,
    snapshot_date: date,
    *,
    provider: BaoStockProvider | None = None,
    force: bool = False,
) -> dict[str, Any]:
    repository = AShareUniverseRepository(settings.resolve(settings.get("system.database_path")))
    stored = _canonicalize_snapshot_records(repository.snapshot(snapshot_date))
    source = provider or BaoStockProvider()
    master = repository.master_records()
    expected = {item["symbol"] for item in master if _active_on(item, snapshot_date)}

    def completeness(items: list[dict[str, Any]]) -> float:
        if not expected:
            return 1.0
        actual_symbols = {item["symbol"] for item in items}
        return len(actual_symbols & expected) / len(expected)

    stored_is_complete = bool(stored and completeness(stored) >= 0.90)
    capture_attempts = 0
    if stored_is_complete and not force:
        records = stored
        snapshot_source = stored[0]["source"]
        cache_hit = True
    else:
        best_records = stored
        for capture_attempts in range(1, 4):
            securities = source.point_in_time_universe(snapshot_date)
            candidate = _canonicalize_snapshot_records(
                [
                    {
                        "symbol": item.symbol,
                        "source_symbol": item.source_symbol or item.symbol,
                        "name": item.name,
                        "exchange": item.exchange,
                        "board": item.board,
                        "trade_status": item.trade_status,
                        "source": item.source,
                    }
                    for item in securities
                ]
            )
            if not best_records or completeness(candidate) > completeness(best_records):
                best_records = candidate
            if completeness(candidate) >= 0.90:
                best_records = candidate
                break
        records = best_records
        if expected and completeness(records) < 0.90:
            raise ValueError(
                f"point-in-time universe for {snapshot_date} remained incomplete after "
                f"{capture_attempts} attempts: coverage={completeness(records):.1%}"
            )
        repository.save_snapshot(snapshot_date, records, source.name)
        snapshot_source = source.name
        cache_hit = False

    actual = {item["symbol"] for item in records}
    overlap = actual & expected
    union = actual | expected
    cross_validation = {
        "master_available": bool(master),
        "master_version_hash": master[0]["version_hash"] if master else None,
        "expected_from_exchange_master": len(expected),
        "observed_in_baostock_snapshot": len(actual),
        "overlap": len(overlap),
        "jaccard": len(overlap) / len(union) if union else None,
        "missing_from_snapshot_count": len(expected - actual),
        "unexpected_in_snapshot_count": len(actual - expected),
        "missing_from_snapshot_sample": sorted(expected - actual)[:20],
        "unexpected_in_snapshot_sample": sorted(actual - expected)[:20],
        "symbol_alias_registry_version": ALIAS_REGISTRY_VERSION,
        "canonicalized_symbol_count": sum(
            (item.get("source_symbol") or item["symbol"]) != item["symbol"] for item in records
        ),
        "canonicalized_symbol_sample": [
            {
                "source_symbol": item.get("source_symbol"),
                "canonical_symbol": item["symbol"],
            }
            for item in records
            if item.get("source_symbol") and item["source_symbol"] != item["symbol"]
        ][:20],
    }
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "source": snapshot_source,
        "cache_hit": cache_hit,
        "capture_attempts": capture_attempts,
        "records": records,
        "securities": len(records),
        "tradable": sum(bool(item["trade_status"]) for item in records),
        "cross_validation": cross_validation,
    }


def select_stratified_point_in_time_sample(
    records: list[dict[str, Any]],
    sample_size: int,
    *,
    seed: str,
    snapshot_date: date,
) -> dict[str, Any]:
    if sample_size < 4:
        raise ValueError("point-in-time market sample requires at least four securities")
    eligible = [
        item
        for item in records
        if item.get("trade_status") and not _special_treatment_name(item.get("name", ""))
    ]
    if len(eligible) < sample_size:
        raise ValueError("point-in-time market universe is smaller than requested sample")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in eligible:
        stratum = item["board"] if item["board"] != "main" else f"{item['exchange']}_main"
        groups[stratum].append(item)
    total = len(eligible)
    quotas = {}
    fractions = []
    for stratum, items in groups.items():
        ideal = sample_size * len(items) / total
        quota = min(len(items), max(1, floor(ideal)))
        quotas[stratum] = quota
        fractions.append((ideal - floor(ideal), stratum))
    while sum(quotas.values()) > sample_size:
        removable = [key for key, value in quotas.items() if value > 1]
        if not removable:
            break
        key = min(removable, key=lambda item: (len(groups[item]) / total, item))
        quotas[key] -= 1
    for _, stratum in sorted(fractions, reverse=True):
        if sum(quotas.values()) >= sample_size:
            break
        if quotas[stratum] < len(groups[stratum]):
            quotas[stratum] += 1
    selected = []
    rotation = str(snapshot_date.year)
    for stratum in sorted(groups):
        ranked = sorted(
            groups[stratum],
            key=lambda item: hashlib.sha256(
                f"{seed}:{rotation}:{item['symbol']}".encode("utf-8")
            ).hexdigest(),
        )
        selected.extend(ranked[: quotas[stratum]])
    selected = sorted(selected, key=lambda item: item["symbol"])
    return {
        "records": selected,
        "sample_size": len(selected),
        "eligible_universe": len(eligible),
        "excluded_non_trading": sum(not item.get("trade_status") for item in records),
        "excluded_special_treatment_names": sum(
            bool(item.get("trade_status")) and _special_treatment_name(item.get("name", ""))
            for item in records
        ),
        "strata": dict(
            Counter(
                item["board"] if item["board"] != "main" else f"{item['exchange']}_main"
                for item in selected
            )
        ),
        "seed": seed,
        "rotation_period": rotation,
    }


def _master_record(
    symbol: str,
    name: Any,
    exchange: str,
    board: str,
    listing_date: Any,
    delisting_date: Any,
    status: str,
    source: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": str(name or symbol).strip(),
        "exchange": exchange,
        "board": board,
        "listing_date": _date_value(listing_date),
        "delisting_date": _date_value(delisting_date),
        "status": status,
        "source": source,
        "payload": {key: _json_value(value) for key, value in payload.items()},
    }


def _active_on(item: dict[str, Any], day: date) -> bool:
    listing = _date_value(item.get("listing_date"))
    delisting = _date_value(item.get("delisting_date"))
    return bool(listing and listing <= day and (delisting is None or day <= delisting))


def _digits(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    return text[-6:].zfill(6) if text else ""


def _is_supported_a_share(exchange: str, code: str) -> bool:
    if len(code) != 6:
        return False
    if exchange == "SH":
        return code.startswith("6")
    return code.startswith(("000", "001", "002", "003", "300", "301", "302"))


def _board(symbol: str) -> str:
    code = symbol[2:]
    if symbol.startswith("sh") and code.startswith(("688", "689")):
        return "star"
    if symbol.startswith("sz") and code.startswith(("300", "301", "302")):
        return "chinext"
    return "main"


def _special_treatment_name(name: str) -> bool:
    compact = str(name or "").upper().replace(" ", "")
    return compact.startswith(("ST", "*ST", "PT")) or "退" in compact


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _json_value(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _canonicalize_snapshot_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for record in records:
        source_symbol = record.get("source_symbol") or record["symbol"]
        symbol = canonical_a_share_symbol(record["symbol"])
        item = {**record, "symbol": symbol, "source_symbol": source_symbol}
        existing = normalized.get(symbol)
        if existing is None or source_symbol == symbol:
            normalized[symbol] = item
    return [normalized[symbol] for symbol in sorted(normalized)]
