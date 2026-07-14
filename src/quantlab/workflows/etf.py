from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pandas as pd

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.backtest import BacktestEngine, calculate_equity_metrics
from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import AssetType, Bar, Instrument, OrderRequest, Side
from quantlab.execution import CostModel
from quantlab.factors import MomentumFactorEngine
from quantlab.llm import await_with_provider_close, build_provider
from quantlab.learning import build_predictor, cross_sectional_features
from quantlab.portfolio.etf_core import lot_aware_etf_core_weights, rebalance_period
from quantlab.strategies import (
    AdaptiveEtfRotationStrategy,
    AdaptiveEtfRotationStrategyV2,
    AdaptiveEtfRotationStrategyV3,
    EtfRotationStrategy,
)
from quantlab.workflows.research import build_price_history_evidence


ETF_METADATA = {
    "sh510300": ("沪深300ETF", True),
    "sz159915": ("创业板ETF", True),
    "sh510880": ("红利ETF", True),
    "sh518880": ("黄金ETF", False),
    "sh513100": ("纳指ETF", False),
    "sh511010": ("国债ETF", False),
}


def run_etf_workflow(
    settings: Settings,
    start: date,
    end: date,
    strategy_variant: str | None = None,
):
    cfg = resolve_etf_variant_config(settings, strategy_variant or "legacy")
    symbols = list(cfg["universe"])
    westock = WestockProvider(settings.root.parent)
    provider = CachedProvider(
        FallbackProvider([westock, AkShareProvider()]),
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, start, end)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    frame["date"] = pd.to_datetime(frame["date"])
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])
    coverage = {
        symbol: {
            "first": group.date.min().date().isoformat(),
            "last": group.date.max().date().isoformat(),
            "bars": int(len(group)),
        }
        for symbol, group in frame.groupby("symbol")
    }
    if not coverage:
        raise ValueError("no ETF data returned")
    latest_first_date = max(date.fromisoformat(item["first"]) for item in coverage.values())
    effective_start = max(start, latest_first_date)
    frame = frame[frame.date >= pd.Timestamp(effective_start)]
    signal_frame = signal_frame[signal_frame.date >= pd.Timestamp(effective_start)]
    bars = [bar for bar in bars if bar.date >= effective_start]
    backtest = run_etf_backtest(settings, bars, signal_frame, symbols, cfg)
    strategy = _etf_strategy(cfg)
    as_of = frame.date.max().date()
    final_signals = strategy.generate(as_of, signal_frame)
    decision_run = None
    if final_signals:
        candidate = final_signals[0]
        factor_report = MomentumFactorEngine().analyze(candidate.symbol, signal_frame, as_of)
        relative_factors = cross_sectional_features(signal_frame, as_of, candidate.symbol)
        price = float(
            frame[
                (frame.symbol == candidate.symbol) & (frame.date == pd.Timestamp(as_of))
            ].close.iloc[0]
        )
        fallback = provider.wrapped
        degraded = fallback.last_degraded_from if isinstance(fallback, FallbackProvider) else []
        llm = build_provider(settings.section("llm"))
        decision_run = asyncio.run(
            await_with_provider_close(
                llm,
                MultiAgentDecisionSystem(
                    llm,
                    build_predictor(settings.resolve(settings.get("system.database_path")), "etf"),
                ).run(
                    ResearchContext(
                        symbol=candidate.symbol,
                        as_of=as_of,
                        price=price,
                        strategy_signals=[candidate],
                        quant_factors=factor_report.model_dump(mode="json"),
                        price_history=build_price_history_evidence(
                            frame[frame["symbol"] == candidate.symbol], as_of
                        ),
                        cross_section_factors=relative_factors,
                        asset_type="etf",
                        market_regime=factor_report.regime.value,
                        data_quality=0.8 if degraded else 1.0,
                        degraded_sources=degraded,
                    )
                ),
            )
        )
    return {
        "backtest": backtest,
        "decision_run": decision_run,
        "bars": len(bars),
        "source": provider.name,
        "requested_start": start.isoformat(),
        "effective_start": effective_start.isoformat(),
        "coverage": coverage,
        "strategy_variant": cfg.get("strategy_variant", "legacy"),
        "research_only": cfg.get("strategy_variant") in {
            "adaptive_v1",
            "adaptive_v2",
            "adaptive_v3",
        },
    }


def run_etf_variant_research(
    settings: Settings,
    start: date,
    end: date,
    strategy_variant: str = "adaptive_v2",
    save: bool = True,
) -> dict:
    """Run a no-LLM retrospective comparison for an explicitly selected ETF variant."""

    if start >= end:
        raise ValueError("ETF research start must be before end")
    cfg = resolve_etf_variant_config(settings, strategy_variant)
    symbols = list(cfg["universe"])
    maximum_history = max(int(value) for value in cfg.get("lookbacks", (20, 60, 120)))
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, start - timedelta(days=maximum_history * 3), end)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no ETF data returned for strategy variant research")
    frame["date"] = pd.to_datetime(frame["date"])
    signal_frame = frame.copy()
    signal_frame["close"] = signal_frame["adjusted_close"].fillna(signal_frame["close"])

    strategy_result = run_etf_backtest(
        settings,
        bars,
        signal_frame,
        symbols,
        cfg,
        start,
        end,
    )
    stressed_result = run_etf_backtest(
        settings,
        bars,
        signal_frame,
        symbols,
        cfg,
        start,
        end,
        cost_multiplier=2.0,
    )
    total_exposure = float(settings.get("risk.max_total_exposure"))
    equal_weight_targets = {
        symbol: total_exposure / len(symbols)
        for symbol in symbols
    }
    benchmark_result = run_etf_static_backtest(
        settings,
        bars,
        symbols,
        equal_weight_targets,
        start,
        end,
    )
    strategy_metrics = _window_result_metrics(strategy_result, start, end)
    benchmark_metrics = _window_result_metrics(benchmark_result, start, end)
    stress_metrics = _window_result_metrics(stressed_result, start, end)
    output = {
        "status": "retrospective_exploratory_only",
        "strategy_variant": strategy_variant,
        "research_only": strategy_variant != "legacy",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "source": provider.name,
        "degraded_sources": list(fallback.last_degraded_from),
        "bars": len(bars),
        "config": {
            key: value
            for key, value in cfg.items()
            if key not in {"universe", "enabled", "min_weight", "max_weight"}
        },
        "metrics": {
            "strategy": strategy_metrics,
            "equal_weight_buy_hold": benchmark_metrics,
            "two_x_cost": stress_metrics,
        },
        "relative_to_equal_weight": {
            "total_return_delta": strategy_metrics["total_return"]
            - benchmark_metrics["total_return"],
            "sharpe_delta": strategy_metrics["sharpe"] - benchmark_metrics["sharpe"],
            "max_drawdown_delta": strategy_metrics["max_drawdown"]
            - benchmark_metrics["max_drawdown"],
        },
        "claim_boundary": (
            "This run reuses historically researched data and cannot establish strategy alpha or "
            "admission. Adaptive variants remain prospective-only challengers."
        ),
    }
    if save:
        output["reports"] = _export_etf_variant_research(settings, output)
    return output


def resolve_etf_variant_config(settings: Settings, strategy_variant: str) -> dict:
    if strategy_variant not in {"legacy", "adaptive_v1", "adaptive_v2", "adaptive_v3"}:
        raise ValueError(f"unsupported ETF strategy variant: {strategy_variant}")
    cfg = dict(settings.get("strategies.etf_rotation"))
    if strategy_variant == "legacy":
        cfg.pop("strategy_variant", None)
        return cfg
    profile = dict(settings.get(f"strategies.etf_{strategy_variant}", {}))
    return {**cfg, **profile, "strategy_variant": strategy_variant}


def run_etf_backtest(
    settings: Settings,
    bars: list[Bar],
    signal_frame: pd.DataFrame,
    symbols: list[str],
    cfg: dict,
    trade_start: date | None = None,
    trade_end: date | None = None,
    cost_multiplier: float = 1.0,
):
    strategy = _etf_strategy(cfg)
    instruments = {
        symbol: Instrument(
            symbol=symbol,
            name=ETF_METADATA.get(symbol, (symbol, True))[0],
            asset_type=AssetType.ETF,
            t_plus_one=ETF_METADATA.get(symbol, (symbol, True))[1],
        )
        for symbol in symbols
    }
    last_rebalance = None

    def signal_fn(trade_date, day_bars, account):
        nonlocal last_rebalance
        if trade_start and trade_date < trade_start:
            return []
        if trade_end and trade_date > trade_end:
            return []
        month = (trade_date.year, trade_date.month)
        if month == last_rebalance:
            return []
        last_rebalance = month
        generated = strategy.generate(
            trade_date,
            signal_frame,
            current_symbols=list(account.positions),
        )
        adaptive_types = (AdaptiveEtfRotationStrategy, AdaptiveEtfRotationStrategyV2)
        if not generated and not isinstance(strategy, adaptive_types):
            return []
        equity = account.equity()
        total_exposure = float(settings.get("risk.max_total_exposure"))
        core_weight = max(0.0, min(1.0, float(cfg.get("core_weight", 0.0))))
        if isinstance(strategy, AdaptiveEtfRotationStrategyV2):
            core_weight = max(
                0.0,
                min(1.0, float(strategy.last_diagnostics.get("effective_core_weight", 0.0))),
            )
        core_per_symbol = total_exposure * core_weight / len(symbols)
        targets = {symbol: core_per_symbol for symbol in symbols if core_per_symbol > 0}
        satellite_budget = total_exposure * (1 - core_weight)
        if isinstance(strategy, adaptive_types):
            diagnostics = strategy.last_diagnostics
            active_multiplier = float(diagnostics.get("regime_multiplier", 0.0)) * float(
                diagnostics.get("risk_scale", 0.0)
            )
            active_budget = satellite_budget * max(0.0, min(1.0, active_multiplier))
            defensive_budget = satellite_budget - active_budget
            targets[strategy.defensive_symbol] = (
                targets.get(strategy.defensive_symbol, 0.0) + defensive_budget
            )
            satellite_budget = active_budget
        for item in generated:
            targets[item.symbol] = (
                targets.get(item.symbol, 0.0) + item.target_weight * satellite_budget
            )
        orders = []
        rebalance_tolerance_weight = (
            strategy.rebalance_tolerance_weight
            if isinstance(strategy, AdaptiveEtfRotationStrategyV2)
            else 0.0
        )
        for symbol, position in list(account.positions.items()):
            if symbol not in day_bars:
                continue
            target = int(targets.get(symbol, 0) * equity / day_bars[symbol].close / 100) * 100
            if position.quantity > target:
                if not _rebalance_gap_passes(
                    position.quantity,
                    target,
                    day_bars[symbol].close,
                    equity,
                    rebalance_tolerance_weight,
                ):
                    continue
                orders.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.SELL,
                        quantity=position.quantity - target,
                        signal_date=trade_date,
                        reason="monthly rebalance",
                    )
                )
        for symbol, weight in targets.items():
            if symbol not in day_bars:
                continue
            current = account.positions.get(symbol)
            current_qty = current.quantity if current else 0
            target = int(weight * equity / day_bars[symbol].close / 100) * 100
            if target > current_qty:
                if not _rebalance_gap_passes(
                    current_qty,
                    target,
                    day_bars[symbol].close,
                    equity,
                    rebalance_tolerance_weight,
                ):
                    continue
                orders.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.BUY,
                        quantity=target - current_qty,
                        signal_date=trade_date,
                        reason="ETF rotation",
                    )
                )
        return orders

    costs = {
        AssetType.ETF.value: CostModel.from_dict(settings.get("costs.etf")).scaled(
            cost_multiplier
        )
    }
    execution_bars = _bounded_execution_bars(bars, trade_start, trade_end)
    return BacktestEngine(instruments, costs, settings.get("system.initial_capital")).run(
        execution_bars, signal_fn
    )


def run_etf_core_protocol_backtest(
    settings: Settings,
    bars: list[Bar],
    symbols: list[str],
    trade_start: date,
    trade_end: date,
    *,
    rebalance_frequency: str = "semiannual",
    rebalance_tolerance_weight: float = 0.02,
    target_exposure: float | None = None,
    cost_multiplier: float = 1.0,
):
    """Backtest the same lot-aware periodic ETF core protocol used by portfolio plans."""

    instruments = {
        symbol: Instrument(
            symbol=symbol,
            name=ETF_METADATA.get(symbol, (symbol, True))[0],
            asset_type=AssetType.ETF,
            t_plus_one=ETF_METADATA.get(symbol, (symbol, True))[1],
        )
        for symbol in symbols
    }
    last_period: tuple[int, int] | None = None

    def signal_fn(trade_date, day_bars, account):
        nonlocal last_period
        if trade_date < trade_start or trade_date > trade_end:
            return []
        period = rebalance_period(trade_date, rebalance_frequency)
        if period == last_period:
            return []
        last_period = period
        equity = account.equity()
        total_exposure = min(
            float(settings.get("risk.max_total_exposure")),
            float(
                target_exposure
                if target_exposure is not None
                else settings.get("strategies.etf_core.target_exposure", 0.45)
            ),
        )
        maximum_single = float(settings.get("risk.max_single_position"))
        relative_weights = lot_aware_etf_core_weights(
            {symbol: float(day_bars[symbol].close) for symbol in symbols if symbol in day_bars},
            symbols,
            equity,
            total_exposure,
            maximum_single,
        )
        targets = {
            symbol: relative_weight * total_exposure
            for symbol, relative_weight in relative_weights.items()
        }
        orders = []
        for symbol, position in list(account.positions.items()):
            bar = day_bars.get(symbol)
            if bar is None:
                continue
            target = int(targets.get(symbol, 0.0) * equity / bar.close / 100) * 100
            if position.quantity <= target or not _rebalance_gap_passes(
                position.quantity,
                target,
                bar.close,
                equity,
                rebalance_tolerance_weight,
            ):
                continue
            orders.append(
                OrderRequest(
                    symbol=symbol,
                    side=Side.SELL,
                    quantity=position.quantity - target,
                    signal_date=trade_date,
                    reason=f"{rebalance_frequency} ETF core rebalance",
                )
            )
        for symbol, weight in targets.items():
            bar = day_bars.get(symbol)
            if bar is None:
                continue
            current = account.positions.get(symbol)
            current_quantity = current.quantity if current else 0
            target = int(weight * equity / bar.close / 100) * 100
            if target <= current_quantity or not _rebalance_gap_passes(
                current_quantity,
                target,
                bar.close,
                equity,
                rebalance_tolerance_weight,
            ):
                continue
            orders.append(
                OrderRequest(
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=target - current_quantity,
                    signal_date=trade_date,
                    reason=f"{rebalance_frequency} ETF core rebalance",
                )
            )
        return orders

    costs = {
        AssetType.ETF.value: CostModel.from_dict(settings.get("costs.etf")).scaled(
            cost_multiplier
        )
    }
    execution_bars = _bounded_execution_bars(bars, trade_start, trade_end)
    return BacktestEngine(instruments, costs, settings.get("system.initial_capital")).run(
        execution_bars, signal_fn
    )


def _etf_strategy(cfg: dict):
    if cfg.get("strategy_variant") == "adaptive_v1":
        return AdaptiveEtfRotationStrategy(cfg)
    if cfg.get("strategy_variant") == "adaptive_v2":
        return AdaptiveEtfRotationStrategyV2(cfg)
    if cfg.get("strategy_variant") == "adaptive_v3":
        return AdaptiveEtfRotationStrategyV3(cfg)
    return EtfRotationStrategy(cfg["lookbacks"], int(cfg["top_k"]), cfg["defensive_symbol"])


def _rebalance_gap_passes(
    current_quantity: int,
    target_quantity: int,
    price: float,
    equity: float,
    tolerance_weight: float,
) -> bool:
    gap_weight = abs(target_quantity - current_quantity) * price / max(equity, 1e-9)
    return gap_weight + 1e-12 >= tolerance_weight


def _window_result_metrics(result, start: date, end: date) -> dict[str, float]:
    curve = [(day, value) for day, value in result.equity_curve if start <= day <= end]
    turnover = sum(1 for fill in result.fills if start <= fill.trade_date <= end)
    return calculate_equity_metrics(curve, turnover)


def _export_etf_variant_research(settings: Settings, output: dict) -> dict[str, str]:
    reports_dir = settings.resolve(settings.get("system.data_dir")) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    variant = str(output["strategy_variant"])
    report_stem = (
        "adaptive-v2-diagnostic-latest"
        if variant == "adaptive_v2"
        else f"etf-{variant.replace('_', '-')}-diagnostic-latest"
    )
    json_path = reports_dir / f"{report_stem}.json"
    markdown_path = reports_dir / f"{report_stem}.md"
    metrics = output["metrics"]
    relative = output["relative_to_equal_weight"]
    markdown = "\n".join(
        [
            "# Adaptive ETF 策略版本回顾性诊断",
            "",
            f"- 策略版本：{output['strategy_variant']}",
            f"- 区间：{output['period']['start']} 至 {output['period']['end']}",
            f"- 状态：{output['status']}",
            f"- 策略收益：{metrics['strategy']['total_return']:.2%}",
            f"- 策略 Sharpe：{metrics['strategy']['sharpe']:.3f}",
            f"- 策略最大回撤：{metrics['strategy']['max_drawdown']:.2%}",
            f"- 策略成交次数：{metrics['strategy']['turnover_count']:.0f}",
            f"- ETF 等权收益：{metrics['equal_weight_buy_hold']['total_return']:.2%}",
            f"- 相对 ETF 等权收益：{relative['total_return_delta']:.2%}",
            f"- 相对 ETF 等权 Sharpe：{relative['sharpe_delta']:.3f}",
            f"- 相对 ETF 等权回撤改善：{relative['max_drawdown_delta']:.2%}",
            f"- 2 倍成本收益：{metrics['two_x_cost']['total_return']:.2%}",
            "",
            f"> {output['claim_boundary']}",
        ]
    )
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def run_etf_static_backtest(
    settings: Settings,
    bars: list[Bar],
    symbols: list[str],
    target_weights: dict[str, float],
    trade_start: date,
    trade_end: date,
    cost_multiplier: float = 1.0,
):
    """Buy a fixed ETF basket once and hold it as a cost-aware reference portfolio."""

    instruments = {
        symbol: Instrument(
            symbol=symbol,
            name=ETF_METADATA.get(symbol, (symbol, True))[0],
            asset_type=AssetType.ETF,
            t_plus_one=ETF_METADATA.get(symbol, (symbol, True))[1],
        )
        for symbol in symbols
    }
    submitted = False

    def signal_fn(trade_date, day_bars, account):
        nonlocal submitted
        if submitted or trade_date < trade_start or trade_date > trade_end:
            return []
        submitted = True
        equity = account.equity()
        orders = []
        for symbol, weight in target_weights.items():
            bar = day_bars.get(symbol)
            if bar is None or weight <= 0:
                continue
            target_value = weight * equity
            quantity = int(target_value / bar.close / 100) * 100
            if quantity == 0 and target_value >= 0.8 * bar.close * 100:
                quantity = 100
            if quantity <= 0:
                continue
            orders.append(
                OrderRequest(
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    signal_date=trade_date,
                    reason="static reference portfolio",
                )
            )
        return orders

    costs = {
        AssetType.ETF.value: CostModel.from_dict(settings.get("costs.etf")).scaled(
            cost_multiplier
        )
    }
    execution_bars = _bounded_execution_bars(bars, trade_start, trade_end)
    return BacktestEngine(instruments, costs, settings.get("system.initial_capital")).run(
        execution_bars, signal_fn
    )


def _bounded_execution_bars(
    bars: list[Bar],
    trade_start: date | None,
    trade_end: date | None,
) -> list[Bar]:
    if trade_start is None and trade_end is None:
        return bars
    return [
        bar
        for bar in bars
        if (trade_start is None or bar.date >= trade_start)
        and (trade_end is None or bar.date <= trade_end)
    ]
