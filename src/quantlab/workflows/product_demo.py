from __future__ import annotations

import hashlib
import gc
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from quantlab.config import Settings
from quantlab.domain import AssetType, DataQuality, MarketQuote
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.market import TradingCalendarService
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.workflows.simulator import (
    create_user_paper_account,
    mark_user_paper_account,
    run_pretrade_check,
    settle_user_paper_order,
    submit_user_paper_order,
    user_simulator_repository,
)


HISTORICAL_DEMO_DATASET = "data/demo/historical-research-v1.json"


def historical_demo_dataset(settings: Settings) -> dict[str, Any]:
    path = settings.resolve(HISTORICAL_DEMO_DATASET)
    payload = json.loads(path.read_text(encoding="utf-8"))
    dataset = {
        **payload,
        "dataset_path": str(path),
        "dataset_fingerprint": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    _validate_historical_dataset(dataset)
    return dataset


def reset_historical_research_demo(settings: Settings) -> dict[str, Any]:
    """Remove only the fingerprint-bound isolated demo ledger."""

    dataset = historical_demo_dataset(settings)
    dataset_identity = (
        f"historical-research-{dataset['dataset_version']}-"
        f"{dataset['dataset_fingerprint'][:16]}"
    )
    demo_root = settings.resolve(settings.get("runtime.demo_directory", "data/demo")).resolve()
    database_path = (demo_root / f"{dataset_identity}.db").resolve()
    if database_path.parent != demo_root:
        raise ValueError("historical demo database escaped the configured demo directory")
    removed: list[str] = []
    # sqlite3 connection context managers commit but do not close. Demo calls
    # are short-lived, so collect those unreachable handles before unlinking
    # the fingerprint-bound files on Windows.
    gc.collect()
    for candidate in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        if candidate.exists():
            candidate.unlink()
            removed.append(str(candidate))
    return {
        "database_path": str(database_path),
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "removed": removed,
    }


def prepare_historical_research_demo(settings: Settings) -> dict[str, Any]:
    """Prepare the isolated demo through pre-trade without creating an order."""

    context = _historical_demo_context(settings)
    dataset = context["dataset"]
    candidate = context["candidate"]
    account = context["account"]
    repository = context["repository"]
    existing_orders = repository.orders(account["account_id"], limit=10)
    order = existing_orders[0] if existing_orders else None
    if order is not None:
        stored_check = repository.pretrade_check(order["check_id"])
        if stored_check is None:
            raise ValueError("historical demo order is missing its pre-trade check")
        _validate_historical_demo_check(
            stored_check,
            dataset=dataset,
            candidate=candidate,
            account_id=account["account_id"],
            check_id=order["check_id"],
        )
        check = stored_check["check_payload"]
    else:
        signal_quote = _historical_quote(
            candidate,
            price_key="signal_close",
            quote_date=date.fromisoformat(dataset["signal_date"]),
            session_status="closed",
            quote_kind="current_close",
        )
        check = run_pretrade_check(
            context["demo_settings"],
            account_id=account["account_id"],
            symbol=candidate["symbol"],
            side="buy",
            quantity=1_000,
            quote=signal_quote,
            requested_at=datetime(2025, 12, 31, 8, 0, tzinfo=UTC),
            user_context={
                "demo_mode": "historical_research",
                "dataset_fingerprint": dataset["dataset_fingerprint"],
            },
        )
        stored_check = repository.pretrade_check(check["check_id"])
        if stored_check is None:
            raise RuntimeError("historical demo pre-trade check was not persisted")
        _validate_historical_demo_check(
            stored_check,
            dataset=dataset,
            candidate=candidate,
            account_id=account["account_id"],
            check_id=check["check_id"],
        )

    fills = repository.fills(account["account_id"])
    overview = repository.overview(account["account_id"])
    _validate_historical_demo_entities(candidate, order, fills, overview)
    return _historical_demo_payload(
        context,
        check=check,
        order=order,
        fills=fills,
        overview=overview,
        scorecard=None,
        stage="completed" if order and order["status"] == "filled" else "pretrade_ready",
    )


def confirm_historical_research_demo(
    settings: Settings,
    *,
    check_id: str,
    dataset_fingerprint: str,
    confirmed: bool,
) -> dict[str, Any]:
    """Confirm and complete one previously prepared isolated demo lifecycle."""

    if confirmed is not True:
        raise ValueError("explicit user confirmation is required for the historical demo order")
    context = _historical_demo_context(settings)
    dataset = context["dataset"]
    candidate = context["candidate"]
    account = context["account"]
    repository = context["repository"]
    demo_settings = context["demo_settings"]
    if dataset_fingerprint != dataset["dataset_fingerprint"]:
        raise ValueError("historical demo dataset fingerprint does not match the prepared check")
    stored_check = repository.pretrade_check(check_id)
    if stored_check is None:
        raise ValueError("historical demo pre-trade check not found")
    _validate_historical_demo_check(
        stored_check,
        dataset=dataset,
        candidate=candidate,
        account_id=account["account_id"],
        check_id=check_id,
    )
    check = stored_check["check_payload"]
    if check.get("allowed_to_submit") is not True:
        raise ValueError("historical demo pre-trade check did not pass deterministic rules")

    order = submit_user_paper_order(
        demo_settings,
        check_id=check_id,
        quantity=1_000,
        idempotency_key=f"historical-demo-order:{dataset['dataset_fingerprint']}",
        requested_at=datetime(2025, 12, 31, 8, 5, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        user_confirmation={
            "confirmed": True,
            "check_id": check_id,
            "account_id": check["account_id"],
            "symbol": check["symbol"],
            "side": check["side"],
            "quantity": 1_000,
            "source": "historical_research_demo",
            "simulation_mode": "next_open_simulation",
            "close_reference_acknowledged": True,
        },
    )
    if order["status"] in {"pending", "partially_filled"}:
        fill_quote = _historical_quote(
            candidate,
            price_key="next_open",
            quote_date=date.fromisoformat(dataset["execution_date"]),
            session_status="open",
            quote_kind="realtime",
        )
        settlement = settle_user_paper_order(
            demo_settings,
            order_id=order["order_id"],
            quote=fill_quote,
            fill_key=f"historical-demo-fill:{dataset['dataset_fingerprint']}",
        )
        order = settlement["order"]
    close_quote = _historical_quote(
        candidate,
        price_key="next_close",
        quote_date=date.fromisoformat(dataset["execution_date"]),
        session_status="closed",
        quote_kind="current_close",
    )
    mark_user_paper_account(
        demo_settings,
        account_id=account["account_id"],
        snapshot_date=date.fromisoformat(dataset["execution_date"]),
        marks=[close_quote],
    )
    overview = repository.overview(account["account_id"])
    fills = repository.fills(account["account_id"])
    _validate_historical_demo_entities(candidate, order, fills, overview)
    scorecard = _save_historical_demo_scorecard(
        settings,
        dataset=dataset,
        candidate=candidate,
        order=order,
        fills=fills,
        overview=overview,
    )
    return _historical_demo_payload(
        context,
        check=check,
        order=order,
        fills=fills,
        overview=overview,
        scorecard=scorecard,
        stage="completed",
    )


def run_historical_research_demo(settings: Settings) -> dict[str, Any]:
    """Backward-compatible one-call wrapper around prepare plus confirm."""

    prepared = prepare_historical_research_demo(settings)
    return confirm_historical_research_demo(
        settings,
        check_id=prepared["pretrade"]["check_id"],
        dataset_fingerprint=prepared["dataset"]["fingerprint"],
        confirmed=True,
    )


def _historical_demo_context(settings: Settings) -> dict[str, Any]:
    dataset = historical_demo_dataset(settings)
    dataset_identity = (
        f"historical-research-{dataset['dataset_version']}-"
        f"{dataset['dataset_fingerprint'][:16]}"
    )
    demo_settings = _isolated_demo_settings(settings, dataset_identity, test_mode=True)
    calendar = TradingCalendarService.from_settings(demo_settings)
    fetched_at = datetime.fromisoformat(str(dataset["source_fetched_at"]))
    calendar.ingest(
        dataset["calendar"],
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="frozen_historical_demo",
        source=HISTORICAL_DEMO_DATASET,
        endpoint="bundled_research_only_dataset",
        source_version=dataset["dataset_version"],
        available_at=fetched_at,
        license_status=dataset["license_status"],
        raw_fingerprint=dataset["dataset_fingerprint"],
    )
    account = create_user_paper_account(
        demo_settings,
        name="黑客松历史研究 Demo",
        initial_capital=100_000.0,
        idempotency_key=f"historical-demo-account:{dataset['dataset_fingerprint']}",
    )
    candidate = max(dataset["candidates"], key=lambda item: float(item["research_score"]))
    return {
        "settings": settings,
        "demo_settings": demo_settings,
        "dataset": dataset,
        "candidate": candidate,
        "account": account,
        "repository": user_simulator_repository(demo_settings),
    }


def _validate_historical_demo_check(
    stored_check: dict[str, Any],
    *,
    dataset: dict[str, Any],
    candidate: dict[str, Any],
    account_id: str,
    check_id: str,
) -> None:
    check = stored_check.get("check_payload") or {}
    user_request = stored_check.get("user_request") or {}
    user_context = user_request.get("user_context") or {}
    if (
        stored_check.get("check_id") != check_id
        or check.get("check_id") != check_id
        or stored_check.get("account_id") != account_id
        or check.get("account_id") != account_id
        or check.get("symbol") != candidate["symbol"]
        or check.get("side") != "buy"
        or int(check.get("requested_quantity") or 0) != 1_000
        or user_context.get("demo_mode") != "historical_research"
        or user_context.get("dataset_fingerprint") != dataset["dataset_fingerprint"]
    ):
        raise ValueError("historical demo pre-trade check identity is inconsistent")


def _validate_historical_demo_entities(
    candidate: dict[str, Any],
    order: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    overview: dict[str, Any],
) -> None:
    if order is not None and order["symbol"] != candidate["symbol"]:
        raise ValueError("historical demo order does not match the selected candidate")
    if any(fill["symbol"] != candidate["symbol"] for fill in fills) or any(
        position["symbol"] != candidate["symbol"]
        for position in overview.get("positions", [])
    ):
        raise ValueError("historical demo candidate, fill and position are inconsistent")


def _save_historical_demo_scorecard(
    settings: Settings,
    *,
    dataset: dict[str, Any],
    candidate: dict[str, Any],
    order: dict[str, Any],
    fills: list[dict[str, Any]],
    overview: dict[str, Any],
) -> dict[str, Any]:
    candidate_returns = [
        {
            "symbol": item["symbol"],
            "signal_to_close_return_pct": (
                float(item["next_close"]) / float(item["signal_close"]) - 1.0
            )
            * 100.0,
            "execution_to_close_return_pct": (
                float(item["next_close"]) / float(item["next_open"]) - 1.0
            )
            * 100.0,
        }
        for item in dataset["candidates"]
    ]
    return Round9Repository(
        settings.resolve(settings.get("system.database_path"))
    ).save_historical_scorecard(
        {
            "scorecard_name": "frozen historical research demo",
            "signal_date": dataset["signal_date"],
            # The actual availability time prevents a false point-in-time claim.
            "data_cutoff": dataset["source_fetched_at"],
            "execution_date": dataset["execution_date"],
            "provider_manifest": {
                "source": dataset["source"],
                "source_fetched_at": dataset["source_fetched_at"],
                "license_status": dataset["license_status"],
                "dataset_version": dataset["dataset_version"],
            },
            "dataset_fingerprint": dataset["dataset_fingerprint"],
            "candidate_universe": dataset["candidates"],
            "metrics": {
                "candidate_returns": candidate_returns,
                "selected_symbol": candidate["symbol"],
                "selected_order_quantity": int(order["filled_quantity"]),
                "selected_account_equity": float(overview["equity"]),
                "transaction_cost": sum(
                    float(item["transaction_fees"]) + float(item["slippage"])
                    for item in fills
                ),
                "strategy_metrics": "unavailable",
                "statistical_model_metrics": "unavailable",
                "llm_fusion_metrics": "unavailable",
                "baseline_metrics": "unavailable",
            },
            "point_in_time_verified": False,
            "lookahead_risk": (
                "source observations were fetched after the historical signal date; "
                "this is a reproducible product demo, not independent PIT validation"
            ),
            "training_eligible": False,
            "forward_scorecard_eligible": False,
        }
    )


def _historical_demo_payload(
    context: dict[str, Any],
    *,
    check: dict[str, Any],
    order: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    overview: dict[str, Any],
    scorecard: dict[str, Any] | None,
    stage: str,
) -> dict[str, Any]:
    dataset = context["dataset"]
    candidate = context["candidate"]
    demo_settings = context["demo_settings"]
    formal_count = len(
        Round5Repository(
            demo_settings.resolve(demo_settings.get("system.database_path"))
        ).experiments()
    )
    return {
        "mode": "historical_research_demo",
        "stage": stage,
        "dataset": {
            "version": dataset["dataset_version"],
            "fingerprint": dataset["dataset_fingerprint"],
            "source": dataset["source"],
            "source_fetched_at": dataset["source_fetched_at"],
            "signal_date": dataset["signal_date"],
            "execution_date": dataset["execution_date"],
            "research_only": True,
            "training_eligible": False,
            "forward_scorecard_eligible": False,
        },
        "candidates": dataset["candidates"],
        "selected_candidate": candidate,
        "research": {
            "research_id": f"historical-demo:{dataset['dataset_fingerprint']}",
            "frozen": True,
            "evidence_boundary": "research_only",
            "suggested_action": candidate["suggested_action"],
            "supporting_evidence": candidate["supporting_evidence"],
            "opposing_evidence": candidate["opposing_evidence"],
            "invalidation_conditions": candidate["invalidation_conditions"],
        },
        "pretrade": check,
        "order": order,
        "fills": fills,
        "account": overview,
        "isolated_database": str(
            demo_settings.resolve(demo_settings.get("system.database_path"))
        ),
        "formal_experiments_in_demo_database": formal_count,
        "historical_scorecard": scorecard,
        "claim_boundary": (
            "This frozen historical route is research_only. It is not live data and cannot "
            "enter primary, the formal scorecard, training data or a user's normal account."
        ),
    }


def live_demo_status(settings: Settings) -> dict[str, Any]:
    from quantlab.runtime.readiness import primary_start_readiness

    readiness = primary_start_readiness(settings, require_runtime=False)
    states = readiness["data"]["source_states"]
    required = {
        "trading_calendar",
        "security_master",
        "industry_membership",
        "point_in_time_pool",
    }
    available_states = {
        name
        for name, item in states.items()
        if item.get("status") in {"completed", "available", "partial"}
        and int(item.get("symbol_count") or 0) > 0
        and float(item.get("field_coverage") or 0.0) > 0
    }
    minimum_ready_states = {
        name for name, item in states.items() if bool(item.get("minimum_ready"))
    }
    pool = readiness["data"].get("point_in_time_pool") or {}
    minimum_ready = required <= minimum_ready_states
    data_available = required <= available_states
    actionable = bool(
        data_available
        and minimum_ready
        and int(pool.get("eligible_members") or 0) > 0
        and readiness.get("is_trading_day")
    )
    return {
        "mode": "live_demo",
        "has_state_records": bool(states),
        "data_available": data_available,
        "minimum_ready": minimum_ready,
        "actionable": actionable,
        "current_data_states": {
            name: {
                "status": item["status"],
                "record_count": item["symbol_count"],
                "field_coverage": item["field_coverage"],
                "last_success_at": item["last_success_at"],
            }
            for name, item in states.items()
        },
        "primary_start_allowed": readiness["start_allowed"],
        "blockers": readiness["blockers"],
        "claim_boundary": (
            "Live Demo uses only current server data. Unavailable or non-actionable data stays "
            "blocked and is never replaced by the historical demo dataset."
        ),
    }


def _validate_historical_dataset(dataset: dict[str, Any]) -> None:
    required = {
        "dataset_version",
        "source_fetched_at",
        "signal_date",
        "execution_date",
        "calendar",
        "candidates",
        "research_only",
    }
    missing = sorted(required - set(dataset))
    if missing:
        raise ValueError(f"historical demo dataset missing fields: {', '.join(missing)}")
    if dataset.get("research_only") is not True:
        raise ValueError("historical demo dataset must be research_only")
    signal_date = date.fromisoformat(str(dataset["signal_date"]))
    execution_date = date.fromisoformat(str(dataset["execution_date"]))
    if execution_date <= signal_date:
        raise ValueError("historical demo execution date must follow signal date")
    calendar = {
        date.fromisoformat(str(item["trade_date"])): bool(item["is_open"])
        for item in dataset["calendar"]
    }
    if not calendar.get(signal_date) or not calendar.get(execution_date):
        raise ValueError("historical demo signal and execution dates must be open sessions")
    candidate_fields = {
        "symbol",
        "name",
        "category",
        "signal_close",
        "next_open",
        "next_close",
        "research_score",
        "suggested_action",
        "supporting_evidence",
        "opposing_evidence",
        "invalidation_conditions",
    }
    symbols: set[str] = set()
    for candidate in dataset["candidates"]:
        candidate_missing = sorted(candidate_fields - set(candidate))
        if candidate_missing:
            raise ValueError(
                f"historical demo candidate missing fields: {', '.join(candidate_missing)}"
            )
        symbol = str(candidate["symbol"])
        if symbol in symbols:
            raise ValueError("historical demo candidate symbols must be unique")
        symbols.add(symbol)
        if min(
            float(candidate["signal_close"]),
            float(candidate["next_open"]),
            float(candidate["next_close"]),
        ) <= 0:
            raise ValueError("historical demo prices must be positive")


def _isolated_demo_settings(settings: Settings, name: str, *, test_mode: bool) -> Settings:
    demo_root = settings.resolve(settings.get("runtime.demo_directory", "data/demo"))
    return settings.with_overrides(
        {
            "system": {
                "database_path": str(demo_root / f"{name}.db"),
                "test_mode": test_mode,
            }
        }
    )


def _historical_quote(
    candidate: dict[str, Any],
    *,
    price_key: str,
    quote_date: date,
    session_status: str,
    quote_kind: str,
) -> MarketQuote:
    available_at = datetime.combine(quote_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=7
    )
    return MarketQuote(
        symbol=candidate["symbol"],
        name=candidate["name"],
        asset_type=AssetType.ETF,
        raw_price=float(candidate[price_key]),
        as_of=quote_date,
        available_at=available_at,
        observed_at=available_at,
        source="frozen_historical_research_demo",
        provider="bundled_dataset",
        source_version="historical-research-demo-v1",
        data_quality=DataQuality.AVAILABLE,
        industry=candidate["category"],
        trade_lot=100,
        t_plus_one=True,
        session_status=session_status,
        quote_kind=quote_kind,
        authoritative=False,
        evidence_stage="test",
        trust_level=DataTrustLevel.TEST,
        license_status="unverified_no_sla",
        actionable=session_status == "open",
        actionability_reasons=[] if session_status == "open" else ["historical_close_quote"],
    )


__all__ = [
    "confirm_historical_research_demo",
    "historical_demo_dataset",
    "live_demo_status",
    "prepare_historical_research_demo",
    "reset_historical_research_demo",
    "run_historical_research_demo",
]
