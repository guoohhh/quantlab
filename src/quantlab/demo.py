from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.backtest import BacktestEngine
from quantlab.config import Settings
from quantlab.data import DemoDataProvider
from quantlab.domain.models import AssetType, OrderRequest, Side
from quantlab.domain import ResearchProvenance
from quantlab.execution import CostModel
from quantlab.llm import await_with_provider_close, build_provider
from quantlab.persistence import DecisionRepository
from quantlab.reporting import build_research_audit_package, research_persistence_context
from quantlab.strategies import EtfRotationStrategy
from quantlab.workflows.radar import build_market_radar
from quantlab.workflows.research import analyze_symbol


def bars_to_frame(bars) -> pd.DataFrame:
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def run_demo(settings: Settings) -> dict:
    provider = DemoDataProvider()
    instruments = {item.symbol: item for item in provider.instruments()}
    start = date(2023, 1, 1)
    end = date(2025, 12, 31)
    bars = provider.bars(list(instruments), start, end)
    frame = bars_to_frame(bars)
    strategy_cfg = settings.get("strategies.etf_rotation")
    strategy = EtfRotationStrategy(
        lookbacks=strategy_cfg["lookbacks"],
        top_k=int(strategy_cfg["top_k"]),
        defensive_symbol=strategy_cfg["defensive_symbol"],
    )
    last_rebalance: tuple[int, int] | None = None

    def signals(trade_date, day_bars, account):
        nonlocal last_rebalance
        month = (trade_date.year, trade_date.month)
        if month == last_rebalance:
            return []
        last_rebalance = month
        strategy_signals = strategy.generate(trade_date, frame)
        if not strategy_signals:
            return []
        equity = account.equity()
        targets = {item.symbol: item.target_weight * 0.80 for item in strategy_signals}
        orders = []
        for symbol, position in list(account.positions.items()):
            target_qty = int(targets.get(symbol, 0) * equity / day_bars[symbol].close / 100) * 100
            if position.quantity > target_qty:
                orders.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.SELL,
                        quantity=position.quantity - target_qty,
                        signal_date=trade_date,
                        reason="monthly rebalance",
                    )
                )
        for symbol, target_weight in targets.items():
            current = account.positions.get(symbol)
            current_qty = current.quantity if current else 0
            target_qty = int(target_weight * equity / day_bars[symbol].close / 100) * 100
            if target_qty > current_qty:
                orders.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.BUY,
                        quantity=target_qty - current_qty,
                        signal_date=trade_date,
                        reason="ETF rotation",
                    )
                )
        return orders

    costs = {
        AssetType.ETF.value: CostModel.from_dict(settings.get("costs.etf")),
        AssetType.STOCK.value: CostModel.from_dict(settings.get("costs.stock")),
    }
    result = BacktestEngine(instruments, costs, settings.get("system.initial_capital")).run(
        bars, signals
    )
    last_date = frame.date.max().date()
    final_signals = strategy.generate(last_date, frame)
    candidate = final_signals[0]
    latest_price = float(
        frame[
            (frame.symbol == candidate.symbol) & (frame.date == pd.Timestamp(last_date))
        ].close.iloc[0]
    )
    llm = build_provider(settings.section("llm"))
    decision_run = asyncio.run(
        await_with_provider_close(
            llm,
            MultiAgentDecisionSystem(llm).run(
                ResearchContext(
                    symbol=candidate.symbol,
                    as_of=last_date,
                    price=latest_price,
                    strategy_signals=[candidate],
                    market_regime="range",
                    data_quality=0.5,
                    degraded_sources=["synthetic demo data"],
                )
            ),
        )
    )
    return {"backtest": result, "decision_run": decision_run}


def run_live_demo(settings: Settings, include_sectors: bool = False) -> dict:
    """Run the real-data hackathon path: radar -> ETF committee -> audit package."""

    radar = build_market_radar(settings, include_sectors=include_sectors)
    candidates = [item for item in radar["instruments"] if int(item.get("observations", 0)) >= 120]
    if not candidates:
        raise ValueError("market radar has no ETF with enough history for live research")
    candidate = candidates[0]
    research = analyze_symbol(
        settings,
        candidate["symbol"],
        date.fromisoformat(radar["as_of"]),
        asset_type="etf",
        include_events=False,
    )
    DecisionRepository(settings.resolve(settings.get("system.database_path"))).save(
        research["decision_run"],
        research_persistence_context(research),
        provenance=ResearchProvenance(
            origin="demo_research",
            requested_as_of=radar["as_of"],
            evidence_stage="demo",
        ),
    )
    return {
        "mode": "real_market_data",
        "radar": radar,
        "selected_candidate": candidate,
        "research": research,
        "audit_package": build_research_audit_package(research),
    }
