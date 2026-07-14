from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from quantlab.config import Settings
from quantlab.domain.models import MarketRegime, Position
from quantlab.persistence import DecisionRepository, TerminalRepository
from quantlab.portfolio import DynamicStrategyAllocator, build_manual_portfolio_plan
from quantlab.portfolio.allocator import StrategyStats
from quantlab.workflows.candidates import (
    enrich_stock_risks,
    enrich_stock_fundamentals,
    scan_convertible_bonds,
    scan_etf_rotation,
    scan_reversal,
)


RISK_PROFILE_LIMITS = {
    "conservative": (0.60, 0.10),
    "balanced": (0.80, 0.15),
    "aggressive": (0.90, 0.20),
}


def generate_portfolio_plan(
    settings: Settings,
    as_of: date | None = None,
    reversal_limit: int = 10,
    check_stock_risks: bool = True,
    save: bool = True,
    etf_policy: str = "evidence_first",
    allow_unvalidated_stock: bool = False,
) -> dict[str, Any]:
    if etf_policy not in {"evidence_first", "equal_weight_core", "momentum_rotation"}:
        raise ValueError(f"unsupported ETF portfolio policy: {etf_policy}")
    plan_date = as_of or date.today()
    database_path = settings.resolve(settings.get("system.database_path"))
    terminal = TerminalRepository(database_path)
    overview = terminal.portfolio_overview(settings.get("system.initial_capital"))
    etf_validation = terminal.latest_strategy_validation("etf_rotation")
    etf_core_validation = terminal.latest_strategy_validation("etf_equal_weight_core")
    bond_validation = terminal.latest_strategy_validation("convertible_bond_double_low")
    resolved_etf_policy = (
        select_evidence_first_etf_policy(etf_validation)
        if etf_policy == "evidence_first"
        else etf_policy
    )
    active_etf_config = active_etf_deployment_config(etf_validation)
    active_etf_eligible = active_etf_config is not None
    etf_core_eligible = bool(
        etf_core_validation and etf_core_validation.get("admission", {}).get("passed")
    )
    stock_validation = _load_a_share_v3_validation(settings)
    stock_allocation_eligible = bool(
        stock_validation and stock_validation.get("locked_holdout_ready")
    )
    bond_allocation_eligible = bool(
        bond_validation and bond_validation.get("admission", {}).get("passed")
    )

    etf = scan_etf_rotation(
        settings,
        plan_date,
        allocation_policy=resolved_etf_policy,
        allocation_capital=float(overview["equity"]),
        strategy_config=(
            active_etf_config if resolved_etf_policy == "momentum_rotation" else None
        ),
    )
    reversal = scan_reversal(settings, plan_date, reversal_limit)
    if check_stock_risks and reversal.signals:
        reversal = enrich_stock_risks(settings, reversal)
        reversal = enrich_stock_fundamentals(reversal, plan_date)
    bonds = scan_convertible_bonds(settings, plan_date)
    scans = [etf, reversal, bonds]
    regime = etf.market_regime or MarketRegime.RANGE

    scopes = {
        "etf_rotation": "etf",
        "stock_reversal": "stock",
        "convertible_bond_double_low": "convertible_bond",
    }
    data_quality = {
        "etf_rotation": 0.7 if etf.degraded_sources else 1.0,
        "stock_reversal": 0.7 if reversal.degraded_sources else 1.0,
        "convertible_bond_double_low": 0.7 if bonds.degraded_sources else 1.0,
    }
    available = {
        "etf_rotation": bool(etf.signals)
        and (
            (resolved_etf_policy == "equal_weight_core" and etf_core_eligible)
            or active_etf_eligible
        ),
        "stock_reversal": bool(reversal.signals) and stock_allocation_eligible,
        "convertible_bond_double_low": bool(bonds.signals) and bond_allocation_eligible,
    }
    stats = []
    for name in scopes:
        if not available[name]:
            continue
        validation = (
            etf_validation
            if name == "etf_rotation"
            else bond_validation
            if name == "convertible_bond_double_low"
            else None
        )
        if validation and name == "etf_rotation" and resolved_etf_policy == "equal_weight_core":
            oos = validation.get("benchmark_oos", {}).get("equal_weight_buy_hold", {})
        else:
            oos = validation.get("selected_oos", {}) if validation else {}
        validated = bool(
            validation
            and oos.get("folds", 0) >= 3
            and oos.get("positive_fold_rate", 0) >= 0.50
            and oos.get("mean_sharpe", 0) > 0
            and oos.get("worst_max_drawdown", -1) > -0.25
            and (
                resolved_etf_policy == "equal_weight_core"
                or validation.get("admission", {}).get("passed", True)
            )
        )
        drawdown = float(oos.get("worst_max_drawdown", 0.0))
        annualized = float(oos.get("mean_annualized_return", 0.0))
        stats.append(
            StrategyStats(
                name=name,
                sharpe_oos=float(oos.get("mean_sharpe", 0.0)),
                calmar_oos=(annualized / abs(drawdown) if drawdown < 0 else 0.0),
                recent_return=annualized,
                max_drawdown=drawdown,
                data_quality=data_quality[name],
                calibrated=validated,
            )
        )
    bounds = {}
    for name in available:
        if not available[name]:
            continue
        config_name = (
            "etf_core"
            if name == "etf_rotation" and resolved_etf_policy == "equal_weight_core"
            else name
        )
        bounds[name] = (
            float(settings.get(f"strategies.{config_name}.min_weight")),
            float(settings.get(f"strategies.{config_name}.max_weight")),
        )
    portfolio_settings = terminal.portfolio_settings(settings.get("system.initial_capital"))
    profile = portfolio_settings["risk_profile"]
    default_total = float(settings.get("risk.max_total_exposure"))
    default_single = float(settings.get("risk.max_single_position"))
    profile_total, profile_single = RISK_PROFILE_LIMITS.get(
        profile, (default_total, default_single)
    )
    max_total = min(default_total, profile_total) if profile != "aggressive" else profile_total
    max_single = min(default_single, profile_single) if profile != "aggressive" else profile_single
    budgets = DynamicStrategyAllocator().allocate(
        stats,
        regime,
        bounds,
        total_budget=max_total,
    )

    signals = [signal for scan in scans for signal in scan.signals]
    market_data: dict[str, dict[str, Any]] = {}
    for scan in scans:
        market_data.update(scan.market_data)
    if resolved_etf_policy == "momentum_rotation" and not active_etf_eligible:
        _mark_research_only(
            etf.signals,
            market_data,
            "active ETF rotation has no admitted frozen production configuration",
        )
    if resolved_etf_policy == "equal_weight_core" and not etf_core_eligible:
        _mark_research_only(
            etf.signals,
            market_data,
            "ETF core production protocol has not passed its matching cost-aware validation",
        )
    if reversal.signals and not stock_allocation_eligible:
        _mark_research_only(
            reversal.signals,
            market_data,
            (
                "unvalidated A-share override is research-only and requires manual review"
                if allow_unvalidated_stock
                else "A-share V3 has not passed the frozen validation gate"
            ),
        )
    if bonds.signals and not bond_allocation_eligible:
        _mark_research_only(
            bonds.signals,
            market_data,
            "convertible-bond strategy has no admitted cost-aware OOS validation",
        )
    positions = {
        item["symbol"]: Position(
            symbol=item["symbol"],
            quantity=int(item["quantity"]),
            average_cost=float(item["average_cost"]),
            market_price=float(item["last_price"]),
        )
        for item in overview["positions"]
    }
    decision_repository = DecisionRepository(database_path)
    agent_gates = {}
    for signal in signals:
        record = decision_repository.latest_for_symbol(signal.symbol, plan_date.isoformat())
        if record is None or record["as_of"] != plan_date.isoformat():
            continue
        decision = record["payload"].get("decision", {})
        action = str(decision.get("action") or record["action"])
        requires_review = bool(decision.get("requires_human_review"))
        current_position = positions.get(signal.symbol, Position(symbol=signal.symbol))
        current_weight = (
            current_position.market_value / float(overview["equity"])
            if float(overview["equity"]) > 0
            else 0.0
        )
        if requires_review or action == "review_required":
            target_cap = current_weight
        elif action in {"buy", "add"}:
            target_cap = float(decision.get("target_weight") or 0.0)
        elif action in {"hold", "watch"}:
            target_cap = current_weight
        elif action == "reduce":
            target_cap = current_weight * 0.5
        else:
            target_cap = 0.0
        metadata = market_data.setdefault(signal.symbol, {})
        applied_to_orders = not (
            signal.strategy == "etf_rotation" and resolved_etf_policy == "equal_weight_core"
        )
        if applied_to_orders:
            metadata.update(
                {
                    "_agent_decision_action": action,
                    "_agent_requires_human_review": requires_review,
                    "_agent_target_cap": target_cap,
                    "_agent_run_id": record["run_id"],
                }
            )
        agent_gates[signal.symbol] = {
            "run_id": record["run_id"],
            "action": action,
            "requires_human_review": requires_review,
            "target_cap": target_cap,
            "applied_to_orders": applied_to_orders,
        }
    for item in overview["positions"]:
        market_data.setdefault(
            item["symbol"],
            {
                "name": item["symbol"],
                "price": float(item["last_price"]),
                "trade_lot": 100,
            },
        )

    previous_payload = terminal.latest_portfolio_plan()
    previous_targets = previous_payload.get("managed_targets", {}) if previous_payload else {}
    previous_plan_as_of = _payload_plan_date(previous_payload)
    degraded = [reason for scan in scans for reason in scan.degraded_sources if reason]
    plan = build_manual_portfolio_plan(
        as_of=plan_date,
        market_regime=regime,
        equity=float(overview["equity"]),
        cash=float(overview["cash"]),
        positions=positions,
        signals=signals,
        strategy_budgets=budgets,
        market_data=market_data,
        previous_targets=previous_targets,
        previous_plan_as_of=previous_plan_as_of,
        max_total_exposure=max_total,
        max_single_position=max_single,
        max_industry_exposure=float(settings.get("risk.max_industry_exposure", 0.30)),
        minimum_order_value=float(settings.get("risk.minimum_order_value", 1_000.0)),
        degraded_sources=degraded,
    )
    if etf_validation and not etf_validation.get("admission", {}).get("passed", True):
        if resolved_etf_policy == "equal_weight_core":
            plan.warnings.insert(
                0,
                "Evidence-first mode selected the investable equal-weight ETF core because active "
                "rotation failed its OOS benchmark and statistical gates.",
            )
        else:
            plan.warnings.insert(
                0,
                "Active ETF rotation was explicitly selected without an admitted frozen production "
                "configuration; candidates are research-only and receive zero new-order budget.",
            )
    if resolved_etf_policy == "equal_weight_core" and not (
        etf_core_validation and etf_core_validation.get("admission", {}).get("passed")
    ):
        plan.warnings.insert(
            0,
            "The ETF core production protocol has not completed its matching cost-aware validation; "
            "keep all generated lines in research review.",
        )
    if reversal.signals and not stock_allocation_eligible:
        plan.warnings.insert(
            0,
            "A-share candidates remain visible for research, but receive zero order budget because "
            "V3 produced positive validation returns without enough bootstrap confidence.",
        )
    if bonds.signals and not bond_allocation_eligible:
        plan.warnings.insert(
            0,
            "Convertible-bond candidates remain visible for research, but receive zero order budget "
            "until a cost-aware OOS validation passes.",
        )
    signal_map = {signal.symbol: signal for signal in signals}
    managed_targets = {
        symbol: {
            "strategy": signal_map[symbol].strategy,
            "name": market_data.get(symbol, {}).get("name", ""),
            "price": market_data.get(symbol, {}).get("price"),
            "trade_lot": market_data.get(symbol, {}).get("trade_lot", 100),
            "asset_type": market_data.get(symbol, {}).get("asset_type", "stock"),
            "target_weight": weight,
        }
        for symbol, weight in plan.target_weights.items()
        if symbol in signal_map
    }
    payload = {
        "plan": plan.model_dump(mode="json"),
        "managed_targets": managed_targets,
        "risk_profile": profile,
        "strategy_validation": {
            "etf_rotation": (
                {
                    "validation_id": etf_validation.get("validation_id"),
                    "selected_oos": etf_validation.get("selected_oos"),
                    "benchmark_oos": etf_validation.get("benchmark_oos"),
                    "relative_to_benchmarks": etf_validation.get("relative_to_benchmarks"),
                    "admission": etf_validation.get("admission"),
                }
                if etf_validation
                else None
            ),
            "etf_equal_weight_core": (
                {
                    "validation_id": etf_core_validation.get("validation_id"),
                    "production_core_protocol": etf_core_validation.get(
                        "production_core_protocol"
                    ),
                    "admission": etf_core_validation.get("admission"),
                }
                if etf_core_validation
                else None
            ),
            "a_share_v3": stock_validation,
            "convertible_bond_double_low": (
                {
                    "validation_id": bond_validation.get("validation_id"),
                    "admission": bond_validation.get("admission"),
                }
                if bond_validation
                else None
            ),
        },
        "portfolio_policy": {
            "requested_etf_policy": etf_policy,
            "resolved_etf_policy": resolved_etf_policy,
            "stock_allocation_eligible": stock_allocation_eligible,
            "active_etf_allocation_eligible": active_etf_eligible,
            "etf_core_allocation_eligible": etf_core_eligible,
            "bond_allocation_eligible": bond_allocation_eligible,
            "allow_unvalidated_stock": allow_unvalidated_stock,
            "decision_basis": (
                "use the investable OOS winner until an active strategy passes its benchmark gate"
                if etf_policy == "evidence_first"
                else "explicit user override"
            ),
        },
        "scan_counts": {
            "etf_rotation": len(etf.signals),
            "stock_reversal": len(reversal.signals),
            "convertible_bond_double_low": len(bonds.signals),
        },
        "agent_gates": agent_gates,
    }
    if save:
        payload["plan_id"] = terminal.save_portfolio_plan(plan_date, payload)
    return payload


def latest_portfolio_plan(settings: Settings) -> dict[str, Any] | None:
    return TerminalRepository(
        settings.resolve(settings.get("system.database_path"))
    ).latest_portfolio_plan()


def select_evidence_first_etf_policy(validation: dict[str, Any] | None) -> str:
    if not validation:
        return "equal_weight_core"
    if active_etf_deployment_config(validation) is not None:
        return "momentum_rotation"
    equal_weight = validation.get("benchmark_oos", {}).get("equal_weight_buy_hold", {})
    if equal_weight:
        return "equal_weight_core"
    return "equal_weight_core"


def active_etf_deployment_config(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not validation or not validation.get("admission", {}).get("passed"):
        return None
    deployment = validation.get("production_deployment", {})
    config = deployment.get("config")
    if (
        deployment.get("status") != "admitted"
        or deployment.get("policy") != "momentum_rotation"
        or not isinstance(config, dict)
    ):
        return None
    return dict(config)


def _mark_research_only(
    signals: list,
    market_data: dict[str, dict[str, Any]],
    reason: str,
) -> None:
    for signal in signals:
        market_data.setdefault(signal.symbol, {}).update(
            {
                "_research_only": True,
                "_research_only_reason": reason,
            }
        )


def _payload_plan_date(payload: dict[str, Any] | None) -> date | None:
    if not payload:
        return None
    value = payload.get("plan", {}).get("as_of")
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _load_a_share_v3_validation(settings: Settings) -> dict[str, Any] | None:
    path = (
        settings.resolve(settings.get("system.data_dir"))
        / "reports"
        / "a-share-strategy-lab-v3-validation.json"
    )
    if not path.exists():
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = payload.get("validation_result", {})
    return {
        "status": payload.get("status"),
        "protocol_hash": payload.get("protocol_hash"),
        "policy_hash": payload.get("policy_hash"),
        "locked_holdout_ready": bool(payload.get("locked_holdout_ready")),
        "total_return": result.get("total_return"),
        "benchmark_total_return": result.get("benchmark_total_return"),
        "mean_rank_ic": result.get("mean_rank_ic"),
        "max_drawdown": result.get("max_drawdown"),
        "bootstrap_probability_mean_excess_positive": result.get(
            "paired_comparison", {}
        ).get("probability_mean_excess_positive"),
        "admission": result.get("admission"),
    }
