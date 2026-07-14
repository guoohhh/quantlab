from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any

import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.data.westock_data import WestockDataProvider
from quantlab.data.westock_tool import WestockToolProvider
from quantlab.domain.models import AssetType, MarketRegime, StrategySignal
from quantlab.domain.models import Bar
from quantlab.fundamentals import load_a_share_financial_report
from quantlab.portfolio.allocator import DynamicStrategyAllocator, StrategyStats
from quantlab.portfolio.etf_core import lot_aware_etf_core_weights
from quantlab.portfolio.regime import detect_regime
from quantlab.strategies import ConvertibleBondDoubleLowStrategy, EtfRotationStrategy


@dataclass
class ScanResult:
    signals: list[StrategySignal] = field(default_factory=list)
    degraded_sources: list[str] = field(default_factory=list)
    market_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    market_regime: MarketRegime | None = None
    bars: list[Bar] = field(default_factory=list)


def scan_etf_rotation(
    settings: Settings,
    as_of: date,
    allocation_policy: str = "momentum_rotation",
    allocation_capital: float | None = None,
    strategy_config: dict[str, Any] | None = None,
) -> ScanResult:
    if allocation_policy not in {"momentum_rotation", "equal_weight_core"}:
        raise ValueError(f"unsupported ETF allocation policy: {allocation_policy}")
    cfg = dict(settings.get("strategies.etf_rotation"))
    if strategy_config:
        cfg.update(strategy_config)
    symbols = list(cfg["universe"])
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    try:
        bars = provider.bars(symbols, as_of - timedelta(days=550), as_of)
    except Exception as exc:
        return ScanResult(degraded_sources=[f"ETF rotation data failed: {exc}"])
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        return ScanResult(degraded_sources=["ETF rotation data returned no bars"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["raw_close"] = frame["close"]
    frame["raw_open"] = frame["open"]
    frame["raw_high"] = frame["high"]
    frame["raw_low"] = frame["low"]
    frame["close"] = frame["adjusted_close"].fillna(frame["close"])
    effective_as_of = frame["date"].max().date()
    if allocation_policy == "equal_weight_core":
        core_weights = _lot_aware_etf_core_weights(
            settings,
            frame,
            symbols,
            allocation_capital or float(settings.get("system.initial_capital")),
        )
        signals = [
            StrategySignal(
                strategy="etf_rotation",
                symbol=symbol,
                as_of=effective_as_of,
                score=0.5,
                target_weight=core_weights[symbol],
                confidence=0.90,
                reasons=[
                    "evidence-first equal-weight ETF core",
                    "active rotation has not beaten this investable OOS benchmark",
                    "target is relative within the ETF strategy budget",
                    f"lot-aware relative core weight={core_weights[symbol]:.2%}",
                ],
            )
            for symbol in symbols
        ]
    else:
        signals = EtfRotationStrategy(
            cfg["lookbacks"], int(cfg["top_k"]), cfg["defensive_symbol"]
        ).generate(effective_as_of, frame)
    market_data = {}
    for symbol, group in frame.groupby("symbol"):
        ordered = group.sort_values("date")
        latest = ordered.iloc[-1]
        stop_loss = None
        stop_distance_pct = None
        risk_method = None
        if allocation_policy == "momentum_rotation":
            stop_loss, stop_distance_pct = _etf_atr_stop(ordered)
            risk_method = "20-day ATR x2, bounded to 4%-12%"
        market_data[symbol] = {
            "name": symbol,
            "price": float(latest["raw_close"]),
            "open": float(latest["raw_open"]),
            "as_of": latest["date"].date().isoformat(),
            "market_data_as_of": latest["date"].date().isoformat(),
            "market_data_freshness_required": True,
            "maximum_market_data_age_business_days": 1,
            "source": str(latest.get("source") or provider.name),
            "stop_loss": stop_loss,
            "stop_distance_pct": stop_distance_pct,
            "risk_method": risk_method,
            "asset_type": AssetType.ETF.value,
            "trade_lot": 100,
            "risk_check_complete": True,
            "allocation_policy": allocation_policy,
            "execution_protocol": (
                "semiannual_equal_weight_core_v1"
                if allocation_policy == "equal_weight_core"
                else "monthly_momentum_rotation_research_v1"
            ),
            "rebalance_frequency": (
                str(settings.get("strategies.etf_core.rebalance_frequency", "semiannual"))
                if allocation_policy == "equal_weight_core"
                else "monthly"
            ),
            "rebalance_tolerance_weight": (
                float(
                    settings.get("strategies.etf_core.rebalance_tolerance_weight", 0.02)
                )
                if allocation_policy == "equal_weight_core"
                else 0.0
            ),
        }
    benchmark = frame[frame["symbol"] == "sh510300"].sort_values("date")["close"]
    regime, _, _ = detect_regime(benchmark)
    return ScanResult(
        signals=signals,
        degraded_sources=list(fallback.last_degraded_from),
        market_data=market_data,
        market_regime=regime,
        bars=bars,
    )


def _lot_aware_etf_core_weights(
    settings: Settings,
    frame: pd.DataFrame,
    symbols: list[str],
    capital: float,
) -> dict[str, float]:
    """Keep the diversified core near equal while making 100-share lots executable."""

    total_exposure = min(
        float(settings.get("risk.max_total_exposure", 0.80)),
        float(settings.get("strategies.etf_core.target_exposure", 0.45)),
    )
    maximum_single = float(settings.get("risk.max_single_position", 0.15))
    latest_prices = {
        symbol: float(group.sort_values("date").iloc[-1]["raw_close"])
        for symbol, group in frame[frame["symbol"].isin(symbols)].groupby("symbol")
    }
    return lot_aware_etf_core_weights(
        latest_prices,
        symbols,
        capital,
        total_exposure,
        maximum_single,
    )


def scan_reversal(settings: Settings, as_of: date, limit: int = 20) -> ScanResult:
    provider = WestockToolProvider(settings.root.parent)
    try:
        rows = provider.filter(
            "intersect([Chg60D < -10, TurnoverValue > 50000000])",
            limit=max(limit * 2, 30),
            orderby="Chg60D",
            ascending=True,
        )
    except Exception as exc:
        return ScanResult(degraded_sources=[f"westock reversal scan failed: {exc}"])
    signals = []
    market_data: dict[str, dict[str, Any]] = {}
    for row in rows[:limit]:
        chg = float(row.get("Chg60D", 0))
        symbol = row["code"]
        market_data[symbol] = {
            "name": _safe_name(row.get("name"), symbol),
            "price": _float(row.get("ClosePrice")),
            "market_data_as_of": as_of.isoformat(),
            "market_data_freshness_required": True,
            "maximum_market_data_age_business_days": 1,
            "change_pct": _float(row.get("ChangePCT")),
            "asset_type": AssetType.STOCK.value,
            "trade_lot": 100,
            "risk_check_complete": False,
            "financial_check_complete": False,
        }
        signals.append(
            StrategySignal(
                strategy="stock_reversal",
                symbol=symbol,
                as_of=as_of,
                score=max(-1.0, min(1.0, -chg / 60)),
                target_weight=1 / min(limit, len(rows)),
                confidence=0.25,
                reasons=[
                    f"60d return={chg:.2f}%",
                    f"turnover={float(row.get('TurnoverValue', 0)):,.0f}",
                    "pre-risk-filter candidate; not an order",
                ],
            )
        )
    return ScanResult(signals=signals, market_data=market_data)


def enrich_stock_risks(settings: Settings, result: ScanResult) -> ScanResult:
    provider = WestockDataProvider(settings.root.parent)
    for signal in result.signals:
        metadata = result.market_data.setdefault(signal.symbol, {})
        complete = True
        for risk_type in ("specialtrade", "pledge", "unlock", "lawsuit"):
            try:
                payload = provider.stock_risk(signal.symbol, risk_type)
            except Exception as exc:
                complete = False
                result.degraded_sources.append(
                    f"westock {risk_type} check failed for {signal.symbol}: {exc}"
                )
                continue
            _merge_stock_risk(metadata, risk_type, payload)
        metadata["risk_check_complete"] = complete
    return result


def enrich_stock_fundamentals(result: ScanResult, as_of: date) -> ScanResult:
    def load(symbol: str):
        return symbol, load_a_share_financial_report(symbol, as_of)

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(result.signals)))) as executor:
        futures = {executor.submit(load, signal.symbol): signal.symbol for signal in result.signals}
        for future in as_completed(futures):
            symbol = futures[future]
            metadata = result.market_data.setdefault(symbol, {})
            try:
                _, report = future.result()
            except Exception as exc:
                metadata["financial_check_complete"] = False
                result.degraded_sources.append(
                    f"financial quality check failed for {symbol}: {exc}"
                )
                continue
            metadata.update(
                {
                    "financial_check_complete": True,
                    "financial_quality_score": report.quality_score,
                    "financial_hard_vetoes": list(report.hard_vetoes),
                    "financial_warnings": list(report.warnings),
                    "financial_as_of": report.as_of.isoformat(),
                }
            )
    return result


def scan_convertible_bonds(settings: Settings, as_of: date) -> ScanResult:
    try:
        import akshare as ak

        raw = ak.bond_cov_comparison()
    except Exception as exc:
        return ScanResult(degraded_sources=[f"akshare bond snapshot failed: {exc}"])
    if raw.empty or len(raw.columns) < 12:
        return ScanResult(degraded_sources=["akshare bond snapshot returned an incomplete schema"])
    # AkShare's documented order is stable even when a terminal decodes Chinese
    # headers incorrectly: code/name/price are 1/2/3 and premium is 11.
    frame = pd.DataFrame(
        {
            "symbol": raw.iloc[:, 1].astype(str).map(_bond_symbol),
            "name": raw.iloc[:, 2].astype(str),
            "price": pd.to_numeric(raw.iloc[:, 3], errors="coerce"),
            "premium_pct": pd.to_numeric(raw.iloc[:, 11], errors="coerce"),
            "underlying_price": pd.to_numeric(raw.iloc[:, 7], errors="coerce"),
            "redeem_trigger_price": pd.to_numeric(raw.iloc[:, 14], errors="coerce"),
        }
    ).dropna(subset=["symbol", "price", "premium_pct"])
    try:
        issuance = ak.bond_zh_cov()
        issuance_map = {str(row["债券代码"]).zfill(6): row for _, row in issuance.iterrows()}
    except Exception as exc:
        issuance_map = {}
        issuance_error = f"akshare bond reference data failed: {exc}"
    else:
        issuance_error = None
    frame["rating"] = frame["symbol"].map(
        lambda symbol: issuance_map.get(symbol[2:], {}).get("信用评级")
    )
    frame["initial_size"] = frame["symbol"].map(
        lambda symbol: _float(issuance_map.get(symbol[2:], {}).get("发行规模"))
    )
    frame["redeem_risk"] = (
        frame["underlying_price"].notna()
        & frame["redeem_trigger_price"].notna()
        & (frame["underlying_price"] >= frame["redeem_trigger_price"])
    )
    cfg = settings.get("strategies.convertible_bond_double_low")
    signals = ConvertibleBondDoubleLowStrategy(
        selection_count=int(cfg["selection_count"]),
        maximum_price=float(cfg["maximum_price"]),
        maximum_premium_pct=float(cfg["maximum_premium_pct"]),
    ).generate(as_of, frame)
    market_data: dict[str, dict[str, Any]] = {}
    detail_provider = WestockDataProvider(settings.root.parent)
    degraded = [issuance_error] if issuance_error else []
    by_symbol = frame.set_index("symbol").to_dict("index")
    for signal in signals:
        row = by_symbol[signal.symbol]
        try:
            detail = detail_provider.bond_detail(signal.symbol)
        except Exception as exc:
            detail = {}
            degraded.append(f"westock bond detail failed for {signal.symbol}: {exc}")
        market_data[signal.symbol] = {
            "name": row.get("name", ""),
            "price": row.get("price"),
            "market_data_as_of": as_of.isoformat(),
            "market_data_freshness_required": True,
            "maximum_market_data_age_business_days": 1,
            "premium_pct": row.get("premium_pct"),
            "asset_type": AssetType.CONVERTIBLE_BOND.value,
            "trade_lot": 10,
            "rating": detail.get("最新债项评级") or row.get("rating"),
            "maturity_date": detail.get("到期日"),
            "initial_size": detail.get("实际发行规模") or row.get("initial_size"),
            "remaining_size": None,
            "redeem_risk": bool(row.get("redeem_risk")),
        }
    return ScanResult(signals=signals, degraded_sources=degraded, market_data=market_data)


def strategy_budgets(has_reversal: bool, has_bonds: bool) -> dict[str, float]:
    stats = [StrategyStats("etf_rotation", data_quality=0.9)]
    if has_reversal:
        stats.append(StrategyStats("stock_reversal", data_quality=0.5))
    if has_bonds:
        stats.append(StrategyStats("convertible_bond_double_low", data_quality=0.5))
    bounds = {
        "etf_rotation": (0.20, 0.65),
        "stock_reversal": (0.10, 0.55),
        "convertible_bond_double_low": (0.05, 0.35),
    }
    return DynamicStrategyAllocator().allocate(stats, MarketRegime.RANGE, bounds)


def _bond_symbol(code: str) -> str:
    code = code.split(".")[0].zfill(6)
    return ("sh" if code.startswith(("11", "13")) else "sz") + code


def _float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if pd.notna(output) else None


def _records(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        return [payload]
    return []


def _merge_stock_risk(metadata: dict[str, Any], risk_type: str, payload: Any) -> None:
    records = _records(payload)
    if risk_type == "specialtrade":
        metadata["is_st"] = any(
            "ST" in str(row.get("type") or row.get("类型") or row.get("特别处理类型") or "").upper()
            and "撤销" not in str(row)
            for row in records
        )
    elif risk_type == "pledge":
        ratios = [_percent(row.get("pledgeRatio") or row.get("质押比例")) for row in records]
        known = [value for value in ratios if value is not None]
        metadata["pledge_ratio"] = max(known) if known else 0.0
    elif risk_type == "unlock":
        metadata["unlock_risk_level"] = _risk_level(records)
    elif risk_type == "lawsuit":
        metadata["lawsuit_risk_level"] = _risk_level(records)


def _percent(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        return None
    return number / 100 if "%" in text or number > 1 else number


def _risk_level(records: list[dict[str, Any]]) -> str:
    levels = [str(row.get("riskLevel") or row.get("风险等级") or "").lower() for row in records]
    if "high" in levels or "高" in levels:
        return "high"
    if records and ("medium" in levels or "中" in levels):
        return "medium"
    return "medium" if records else "low"


def _safe_name(value: Any, symbol: str) -> str:
    name = str(value or "").strip()
    return symbol if not name or "�" in name else name


def _etf_atr_stop(frame: pd.DataFrame) -> tuple[float, float]:
    close = pd.to_numeric(frame["raw_close"], errors="coerce")
    high = pd.to_numeric(frame["raw_high"], errors="coerce")
    low = pd.to_numeric(frame["raw_low"], errors="coerce")
    previous = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    price = float(close.iloc[-1])
    atr = float(true_range.tail(20).mean())
    raw_distance = 2 * atr / price if price > 0 and pd.notna(atr) else 0.08
    distance = max(0.04, min(0.12, raw_distance))
    return round(price * (1 - distance), 4), round(distance, 4)
