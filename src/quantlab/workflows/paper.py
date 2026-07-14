from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.domain.models import Bar, OrderRequest, Side
from quantlab.execution import CostModel
from quantlab.persistence import DecisionRepository, PaperTradingRepository
from quantlab.reporting import research_persistence_context
from quantlab.strategies import AdaptiveEtfRotationStrategyV2
from quantlab.workflows.candidates import scan_etf_rotation
from quantlab.workflows.etf import resolve_etf_variant_config
from quantlab.workflows.research import analyze_symbol
from quantlab.workflows.stock_discovery import parse_stock_symbols, screen_selected_stocks


PAPER_ACCOUNTS = {
    "benchmark_hs300": {
        "label": "沪深300基准",
        "policy": "80% 沪深300ETF 买入并持有",
    },
    "benchmark_equal_weight": {
        "label": "ETF 等权基准",
        "policy": "轮动池 80% 等权买入并持有",
    },
    "etf_strategy": {
        "label": "纯 ETF 轮动策略",
        "policy": "只使用确定性 ETF 轮动信号",
    },
    "adaptive_v2_shadow": {
        "label": "Adaptive ETF V2 前瞻影子账户",
        "policy": "只使用冻结的V2.1确定性信号；研究账户，不改变正式策略预算",
    },
    "full_system": {
        "label": "QuantLab 完整系统",
        "policy": "ETF 轮动目标 + 当日多 Agent 决策闸门",
    },
}

STOCK_PAPER_ACCOUNTS = {
    "stock_radar_equal_weight": {
        "label": "A股固定研究池等权",
        "policy": "用户冻结股票池按风险预算等权、月度再平衡",
    },
    "stock_top_rank_shadow": {
        "label": "A股系统排名影子账户",
        "policy": "点时筛选与相关性去重后的前N名、次日开盘模拟成交",
    },
    "stock_full_system_shadow": {
        "label": "A股多Agent完整系统影子账户",
        "policy": "A股系统排名目标叠加同日多Agent审核；无完整审核不新增仓位",
    },
}


def run_paper_cycle(
    settings: Settings,
    as_of: date | None = None,
    run_research: bool = False,
    research_limit: int = 1,
) -> dict[str, Any]:
    requested_as_of = as_of or date.today()
    scan = scan_etf_rotation(settings, requested_as_of)
    if not scan.signals or not scan.market_data or not scan.bars:
        raise ValueError(
            "ETF paper cycle requires current rotation signals and bars; "
            + "; ".join(scan.degraded_sources)
        )
    effective_as_of = max(bar.date for bar in scan.bars)
    database_path = settings.resolve(settings.get("system.database_path"))
    repository = PaperTradingRepository(database_path)
    decisions = DecisionRepository(database_path)
    previous_run = repository.latest_run("etf")
    initial_capital = float(settings.get("system.initial_capital"))
    for account_id, spec in PAPER_ACCOUNTS.items():
        repository.ensure_account(account_id, spec["label"], spec["policy"], initial_capital)

    bars_by_symbol: dict[str, list[Bar]] = defaultdict(list)
    for bar in scan.bars:
        bars_by_symbol[bar.symbol].append(bar)
    for symbol in bars_by_symbol:
        bars_by_symbol[symbol].sort(key=lambda item: item.date)

    fill_events = _fill_pending_orders(
        settings,
        repository,
        bars_by_symbol,
        effective_as_of,
        account_ids=set(PAPER_ACCOUNTS),
    )
    marks = {
        symbol: {
            "price": metadata["price"],
            "as_of": metadata.get("as_of", effective_as_of.isoformat()),
            "source": metadata.get("source", "unknown"),
        }
        for symbol, metadata in scan.market_data.items()
        if metadata.get("price")
    }

    research_runs = []
    if run_research:
        for signal in scan.signals[: max(0, min(int(research_limit), len(scan.signals)))]:
            output = analyze_symbol(
                settings,
                signal.symbol,
                effective_as_of,
                asset_type="etf",
                include_events=False,
            )
            decisions.save(output["decision_run"], research_persistence_context(output))
            research_runs.append(output["decision_run"].run_id)

    symbols = list(settings.get("strategies.etf_rotation.universe"))
    exposure = float(settings.get("risk.max_total_exposure"))
    strategy_targets = {
        signal.symbol: float(signal.target_weight) * exposure for signal in scan.signals
    }
    adaptive_v2_targets, adaptive_v2_signals, adaptive_v2_diagnostics = _adaptive_v2_shadow_targets(
        settings,
        repository,
        scan.bars,
        effective_as_of,
        exposure,
        symbols,
    )
    target_sets = {
        "benchmark_hs300": {"sh510300": exposure},
        "benchmark_equal_weight": {symbol: exposure / len(symbols) for symbol in symbols},
        "etf_strategy": strategy_targets,
        "adaptive_v2_shadow": adaptive_v2_targets,
        "full_system": _full_system_targets(
            repository,
            decisions,
            strategy_targets,
            marks,
            effective_as_of,
        ),
    }
    strategy_rebalance_due = _strategy_rebalance_due(
        previous_run, effective_as_of, set(strategy_targets)
    )
    adaptive_v2_rebalance_due = _monthly_rebalance_due(previous_run, effective_as_of)

    queued_orders = []
    warnings = []
    for account_id, targets in target_sets.items():
        if account_id == "etf_strategy" and not strategy_rebalance_due:
            continue
        if account_id == "adaptive_v2_shadow" and not adaptive_v2_rebalance_due:
            continue
        queued, account_warnings = _queue_target_orders(
            settings,
            repository,
            account_id,
            targets,
            marks,
            scan.market_data,
            effective_as_of,
        )
        queued_orders.extend(queued)
        warnings.extend(account_warnings)

    overviews = {}
    for account_id in PAPER_ACCOUNTS:
        overview = repository.overview(account_id, marks)
        repository.save_snapshot(account_id, effective_as_of, overview)
        overviews[account_id] = overview

    payload = {
        "requested_as_of": requested_as_of.isoformat(),
        "run_type": "etf",
        "as_of": effective_as_of.isoformat(),
        "market_regime": scan.market_regime.value if scan.market_regime else "unknown",
        "signals": [signal.model_dump(mode="json") for signal in scan.signals],
        "adaptive_v2_signals": [signal.model_dump(mode="json") for signal in adaptive_v2_signals],
        "adaptive_v2_diagnostics": adaptive_v2_diagnostics,
        "fills": fill_events,
        "queued_orders": queued_orders,
        "research_runs": research_runs,
        "degraded_sources": scan.degraded_sources,
        "warnings": list(dict.fromkeys(warnings)),
        "strategy_rebalance_due": strategy_rebalance_due,
        "adaptive_v2_rebalance_due": adaptive_v2_rebalance_due,
    }
    payload["run_id"] = repository.record_run(effective_as_of, "ok", payload)
    return {
        **payload,
        "accounts": overviews,
        "scorecard": repository.scorecard(),
        "execution_contract": (
            "signals are frozen at T close; pending orders fill at the first available later open"
        ),
    }


def run_stock_paper_cycle(
    settings: Settings,
    symbols: list[str] | str,
    as_of: date | None = None,
    *,
    top_n: int = 3,
    max_correlation: float = 0.85,
    run_research: bool = False,
    research_limit: int = 2,
    bars: list[Bar] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run prospective A-share shadow accounts against a frozen user research pool."""

    universe = parse_stock_symbols(symbols, maximum=20)
    if len(universe) < 2:
        raise ValueError("stock paper cycle requires at least two A-share symbols")
    if not 1 <= top_n <= min(5, len(universe)):
        raise ValueError("top_n must be between 1 and min(5, universe size)")
    if not 0 <= max_correlation <= 1:
        raise ValueError("max_correlation must be between 0 and 1")
    requested_as_of = as_of or date.today()
    database_path = settings.resolve(settings.get("system.database_path"))
    repository = PaperTradingRepository(database_path)
    decisions = DecisionRepository(database_path)
    initial_capital = float(settings.get("system.initial_capital"))
    for account_id, spec in STOCK_PAPER_ACCOUNTS.items():
        repository.ensure_account(account_id, spec["label"], spec["policy"], initial_capital)

    tracked_symbols = set(universe)
    for account_id in STOCK_PAPER_ACCOUNTS:
        tracked_symbols.update(
            item["symbol"] for item in repository.overview(account_id).get("positions", [])
        )
        tracked_symbols.update(item["symbol"] for item in repository.pending_orders(account_id))
    degraded_sources: list[str] = []
    source = "provided_bars"
    if bars is None:
        fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
        provider = CachedProvider(
            fallback,
            settings.resolve(settings.get("system.data_dir")) / "cache",
        )
        bars = provider.bars(
            sorted(tracked_symbols),
            requested_as_of - timedelta(days=550),
            requested_as_of,
        )
        degraded_sources = list(fallback.last_degraded_from)
        source = provider.name
    normalized_bars = [
        item if isinstance(item, Bar) else Bar.model_validate(item)
        for item in (bars or [])
        if (item.date if isinstance(item, Bar) else date.fromisoformat(str(item["date"])[:10]))
        <= requested_as_of
    ]
    if not normalized_bars:
        raise ValueError("stock paper cycle returned no market bars")
    effective_as_of = max(bar.date for bar in normalized_bars if bar.symbol in set(universe))
    bars_by_symbol: dict[str, list[Bar]] = defaultdict(list)
    for bar in normalized_bars:
        bars_by_symbol[bar.symbol].append(bar)
    for symbol in bars_by_symbol:
        bars_by_symbol[symbol].sort(key=lambda item: item.date)

    fills = _fill_pending_orders(
        settings,
        repository,
        bars_by_symbol,
        effective_as_of,
        account_ids=set(STOCK_PAPER_ACCOUNTS),
    )
    marks = {
        symbol: {
            "price": history[-1].close,
            "as_of": history[-1].date.isoformat(),
            "source": history[-1].source,
        }
        for symbol, history in bars_by_symbol.items()
        if history and history[-1].date <= effective_as_of
    }
    market_data = {
        symbol: {
            "name": symbol,
            "trade_lot": int(settings.get("costs.stock.trade_lot", 100)),
            "asset_type": "stock",
            "price": marks.get(symbol, {}).get("price"),
            "as_of": marks.get(symbol, {}).get("as_of"),
            "source": marks.get(symbol, {}).get("source", source),
        }
        for symbol in tracked_symbols
    }
    screen = screen_selected_stocks(
        settings,
        universe,
        effective_as_of,
        top_n=len(universe),
        max_correlation=max_correlation,
        save=True,
        bars=normalized_bars,
        run_type="stock_paper_signal",
    )
    ranked = [
        item for item in screen["candidates"] if item.get("status") == "ok" and item.get("eligible")
    ]
    shortlist = [item for item in screen["diversified_shortlist"] if item in ranked][:top_n]
    if not shortlist:
        raise ValueError("stock paper cycle found no eligible ranked candidates")

    research_runs = []
    if run_research:
        for candidate in shortlist[: max(0, min(int(research_limit), len(shortlist)))]:
            output = analyze_symbol(
                settings,
                candidate["symbol"],
                effective_as_of,
                asset_type="stock",
                include_events=True,
            )
            decisions.save(output["decision_run"], research_persistence_context(output))
            research_runs.append(output["decision_run"].run_id)

    exposure = float(settings.get("risk.max_total_exposure"))
    max_single = float(settings.get("risk.max_single_position"))
    pool_budget = min(exposure, max_single * len(universe))
    pool_targets = _equal_weight_targets(universe, pool_budget, max_single)
    ranked_symbols = [item["symbol"] for item in shortlist]
    ranked_budget = min(exposure, max_single * len(ranked_symbols))
    ranked_targets = _equal_weight_targets(ranked_symbols, ranked_budget, max_single)
    full_system_targets = _full_system_targets(
        repository,
        decisions,
        ranked_targets,
        marks,
        effective_as_of,
        account_id="stock_full_system_shadow",
    )
    previous_run = repository.latest_run("stock")
    rebalance_due = _stock_rebalance_due(previous_run, effective_as_of, set(universe))
    target_sets = {
        "stock_radar_equal_weight": pool_targets,
        "stock_top_rank_shadow": ranked_targets,
        "stock_full_system_shadow": full_system_targets,
    }
    queued_orders = []
    warnings = []
    for account_id, targets in target_sets.items():
        if not rebalance_due and not (account_id == "stock_full_system_shadow" and run_research):
            continue
        queued, account_warnings = _queue_target_orders(
            settings,
            repository,
            account_id,
            targets,
            marks,
            market_data,
            effective_as_of,
            asset_type="stock",
        )
        queued_orders.extend(queued)
        warnings.extend(account_warnings)

    overviews = {}
    for account_id in STOCK_PAPER_ACCOUNTS:
        overview = repository.overview(account_id, marks)
        repository.save_snapshot(account_id, effective_as_of, overview)
        overviews[account_id] = overview
    payload = {
        "run_type": "stock",
        "requested_as_of": requested_as_of.isoformat(),
        "as_of": effective_as_of.isoformat(),
        "universe": universe,
        "screen_discovery_id": screen.get("discovery_id"),
        "ranked_candidates": [
            {
                "symbol": item["symbol"],
                "screen_rank": item["screen_rank"],
                "screen_score": item["screen_score"],
            }
            for item in ranked
        ],
        "diversified_top_n": ranked_symbols,
        "fills": fills,
        "queued_orders": queued_orders,
        "research_runs": research_runs,
        "degraded_sources": degraded_sources,
        "warnings": list(dict.fromkeys(warnings)),
        "rebalance_due": rebalance_due,
    }
    payload["run_id"] = repository.record_run(effective_as_of, "ok", payload)
    return {
        **payload,
        "accounts": overviews,
        "scorecard": repository.scorecard(),
        "execution_contract": (
            "A-share signals freeze at T close; buys/sells use the first later executable open, "
            "100-share lots, T+1 sellability, limit/suspension checks and stock transaction costs"
        ),
    }


def paper_scorecard(settings: Settings) -> dict[str, Any]:
    return PaperTradingRepository(
        settings.resolve(settings.get("system.database_path"))
    ).scorecard()


def paper_account_overview(settings: Settings, account_id: str) -> dict[str, Any]:
    return PaperTradingRepository(settings.resolve(settings.get("system.database_path"))).overview(
        account_id
    )


def _fill_pending_orders(
    settings: Settings,
    repository: PaperTradingRepository,
    bars_by_symbol: dict[str, list[Bar]],
    effective_as_of: date,
    account_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    events = []
    for order in repository.pending_orders():
        if account_ids is not None and order["account_id"] not in account_ids:
            continue
        signal_date = date.fromisoformat(order["signal_date"])
        asset_type = str(
            order.get("payload", {}).get("asset_type")
            or ("stock" if order["account_id"].startswith("stock_") else "etf")
        )
        cost_model = CostModel.from_dict(settings.get(f"costs.{asset_type}"))
        execution_bar = next(
            (
                bar
                for bar in bars_by_symbol.get(order["symbol"], [])
                if signal_date < bar.date <= effective_as_of
                and not bar.suspended
                and not (asset_type == "stock" and order["side"] == "buy" and bar.limit_up)
                and not (asset_type == "stock" and order["side"] == "sell" and bar.limit_down)
            ),
            None,
        )
        if execution_bar is None:
            continue
        execution_marks = {
            symbol: {
                "price": next(
                    (bar.open for bar in bars if bar.date == execution_bar.date),
                    bars[-1].close,
                ),
                "as_of": execution_bar.date.isoformat(),
                "source": execution_bar.source,
            }
            for symbol, bars in bars_by_symbol.items()
            if bars
        }
        overview = repository.overview(order["account_id"], execution_marks)
        positions = {item["symbol"]: item for item in overview["positions"]}
        if order["side"] == "sell":
            available = int(positions.get(order["symbol"], {}).get("quantity", 0))
            if asset_type == "stock":
                available = min(
                    available,
                    _sellable_quantity_before(
                        repository,
                        order["account_id"],
                        order["symbol"],
                        execution_bar.date,
                    ),
                )
            if int(order["quantity"]) > available:
                repository.reject_order(order["id"], "insufficient_position")
                events.append({"order_id": order["id"], "status": "rejected"})
                continue
        request = OrderRequest(
            symbol=order["symbol"],
            side=Side(order["side"]),
            quantity=int(order["quantity"]),
            signal_date=signal_date,
            reason=f"paper:{order['strategy']}",
        )
        fill = cost_model.fill(request, execution_bar.open, execution_bar.date)
        fees = fill.commission + fill.stamp_duty + fill.transfer_fee
        if order["side"] == "buy" and fill.gross_value + fees > overview["cash"]:
            repository.reject_order(order["id"], "insufficient_cash")
            events.append({"order_id": order["id"], "status": "rejected"})
            continue
        repository.fill_order(
            order["id"],
            trade_date=execution_bar.date,
            price=fill.price,
            fees=fees,
            gross_value=fill.gross_value,
            payload={
                "raw_open": execution_bar.open,
                "slippage": fill.slippage,
                "source": execution_bar.source,
            },
        )
        events.append(
            {
                "order_id": order["id"],
                "account_id": order["account_id"],
                "symbol": order["symbol"],
                "side": order["side"],
                "quantity": order["quantity"],
                "trade_date": execution_bar.date.isoformat(),
                "price": fill.price,
                "fees": fees,
                "status": "filled",
            }
        )
    return events


def _full_system_targets(
    repository: PaperTradingRepository,
    decisions: DecisionRepository,
    strategy_targets: dict[str, float],
    marks: dict[str, dict[str, Any]],
    as_of: date,
    account_id: str = "full_system",
) -> dict[str, float]:
    overview = repository.overview(account_id, marks)
    current_weights = {item["symbol"]: float(item["weight"]) for item in overview["positions"]}
    targets: dict[str, float] = {}
    for symbol, strategy_weight in strategy_targets.items():
        record = decisions.latest_for_symbol(symbol, as_of.isoformat())
        current = current_weights.get(symbol, 0.0)
        if record is None or record["as_of"] != as_of.isoformat():
            targets[symbol] = min(current, strategy_weight) if current else 0.0
            continue
        decision = record["payload"].get("decision", {})
        action = decision.get("action", record.get("action"))
        if action in {"buy", "add"} and not decision.get("requires_human_review"):
            targets[symbol] = min(
                strategy_weight, float(decision.get("target_weight") or strategy_weight)
            )
        elif action in {"hold", "watch"}:
            targets[symbol] = min(current, strategy_weight) if current else 0.0
        elif action == "reduce":
            targets[symbol] = min(current * 0.5, strategy_weight)
        else:
            targets[symbol] = 0.0
    return targets


def _queue_target_orders(
    settings: Settings,
    repository: PaperTradingRepository,
    account_id: str,
    targets: dict[str, float],
    marks: dict[str, dict[str, Any]],
    market_data: dict[str, dict[str, Any]],
    signal_date: date,
    asset_type: str = "etf",
) -> tuple[list[dict[str, Any]], list[str]]:
    overview = repository.overview(account_id, marks)
    existing_pending = repository.pending_orders(account_id)
    if account_id.startswith("benchmark_") and repository.trades(account_id):
        return [], []
    pending_symbols = {item["symbol"] for item in existing_pending}
    current = {item["symbol"]: int(item["quantity"]) for item in overview["positions"]}
    equity = float(overview["equity"])
    minimum_order = float(settings.get("risk.minimum_order_value", 1_000.0))
    queued = []
    warnings = []
    for symbol in sorted(set(current) | set(targets)):
        if symbol in pending_symbols:
            continue
        metadata = market_data.get(symbol, {})
        price = float(marks.get(symbol, {}).get("price") or 0)
        if price <= 0:
            warnings.append(f"{account_id}:{symbol} missing current price")
            continue
        lot = max(1, int(metadata.get("trade_lot") or 100))
        target_weight = max(0.0, float(targets.get(symbol, 0.0)))
        target_value = target_weight * equity
        target_quantity = int(target_value / price / lot) * lot
        if target_quantity == 0 and target_value >= 0.8 * price * lot:
            target_quantity = lot
        current_quantity = current.get(symbol, 0)
        delta = target_quantity - current_quantity
        if delta == 0:
            continue
        side = "buy" if delta > 0 else "sell"
        quantity = abs(delta)
        if side == "sell" and target_quantity == 0:
            quantity = current_quantity
        elif side == "sell":
            quantity = quantity // lot * lot
        if quantity <= 0:
            continue
        gross = quantity * price
        if gross < minimum_order:
            warnings.append(f"{account_id}:{symbol} target delta below minimum order value")
            continue
        repository.cancel_pending(account_id, symbol, signal_date, except_side=side)
        order_id = repository.queue_order(
            account_id=account_id,
            symbol=symbol,
            name=str(metadata.get("name") or symbol),
            strategy=("full_system" if account_id == "full_system" else account_id),
            side=side,
            quantity=quantity,
            signal_date=signal_date,
            reference_price=price,
            target_weight=target_weight,
            payload={
                "current_quantity": current_quantity,
                "target_quantity": target_quantity,
                "execution": "next_available_open",
                "asset_type": asset_type,
            },
        )
        queued.append(
            {
                "order_id": order_id,
                "account_id": account_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "reference_price": price,
                "target_weight": target_weight,
                "status": "pending_next_open",
            }
        )
    return queued, warnings


def _strategy_rebalance_due(
    previous_run: dict[str, Any] | None,
    current_date: date,
    current_symbols: set[str],
) -> bool:
    if previous_run is None:
        return True
    previous_date = date.fromisoformat(previous_run["as_of"])
    previous_signals = previous_run.get("payload", {}).get("signals", [])
    previous_symbols = {item.get("symbol") for item in previous_signals}
    return (previous_date.year, previous_date.month) != (
        current_date.year,
        current_date.month,
    ) or previous_symbols != current_symbols


def _monthly_rebalance_due(previous_run: dict[str, Any] | None, current_date: date) -> bool:
    if previous_run is None:
        return True
    previous_date = date.fromisoformat(previous_run["as_of"])
    return (previous_date.year, previous_date.month) != (current_date.year, current_date.month)


def _stock_rebalance_due(
    previous_run: dict[str, Any] | None,
    current_date: date,
    universe: set[str],
) -> bool:
    if previous_run is None:
        return True
    if _monthly_rebalance_due(previous_run, current_date):
        return True
    previous_universe = set(previous_run.get("payload", {}).get("universe", []))
    return previous_universe != universe


def _equal_weight_targets(
    symbols: list[str], total_budget: float, max_single: float
) -> dict[str, float]:
    if not symbols or total_budget <= 0:
        return {}
    per_symbol = min(max_single, total_budget / len(symbols))
    return {symbol: per_symbol for symbol in symbols}


def _sellable_quantity_before(
    repository: PaperTradingRepository,
    account_id: str,
    symbol: str,
    trade_date: date,
) -> int:
    quantity = 0
    for trade in repository.trades(account_id):
        if trade["symbol"] != symbol or date.fromisoformat(trade["trade_date"]) >= trade_date:
            continue
        quantity += int(trade["quantity"]) if trade["side"] == "buy" else -int(trade["quantity"])
    return max(0, quantity)


def _adaptive_v2_shadow_targets(
    settings: Settings,
    repository: PaperTradingRepository,
    bars: list[Bar],
    as_of: date,
    exposure: float,
    symbols: list[str],
) -> tuple[dict[str, float], list, dict[str, Any]]:
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["close"] = frame["adjusted_close"].fillna(frame["close"])
    config = resolve_etf_variant_config(settings, "adaptive_v2")
    strategy = AdaptiveEtfRotationStrategyV2(config)
    overview = repository.overview("adaptive_v2_shadow")
    current_symbols = [item["symbol"] for item in overview["positions"]]
    signals = strategy.generate(as_of, frame, current_symbols=current_symbols)
    diagnostics = dict(strategy.last_diagnostics)
    core_weight = max(
        0.0,
        min(1.0, float(diagnostics.get("effective_core_weight", 0.0))),
    )
    core_per_symbol = exposure * core_weight / len(symbols)
    targets = {symbol: core_per_symbol for symbol in symbols if core_per_symbol > 0}
    satellite_budget = exposure * (1.0 - core_weight)
    active_multiplier = max(
        0.0,
        min(
            1.0,
            float(diagnostics.get("regime_multiplier", 0.0))
            * float(diagnostics.get("risk_scale", 0.0)),
        ),
    )
    active_budget = satellite_budget * active_multiplier
    defensive_symbol = str(config["defensive_symbol"])
    targets[defensive_symbol] = (
        targets.get(defensive_symbol, 0.0) + satellite_budget - active_budget
    )
    for signal in signals:
        targets[signal.symbol] = (
            targets.get(signal.symbol, 0.0) + float(signal.target_weight) * active_budget
        )
    diagnostics["paper_target_exposure"] = sum(targets.values())
    return targets, signals, diagnostics
