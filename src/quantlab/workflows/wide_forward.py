from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time
from statistics import median
from typing import Any, Callable
from zoneinfo import ZoneInfo

from quantlab.config import Settings
from quantlab.domain import AnalysisContextPack
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.domain.strategy_evidence import ABLATION_VARIANTS
from quantlab.market import ResearchBarService, TradingCalendarService
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.persistence.wide_research import EVIDENCE_BOUNDARY, WideResearchRepository
from quantlab.workflows.context import build_analysis_context_pack
from quantlab.workflows.forward_ablation import (
    FORWARD_GOVERNANCE_VERSION,
    FORWARD_PROMPT_VERSION,
    freeze_forward_ablation_sample,
)
from quantlab.workflows.llm_committee import run_context_committee


WIDE_PROTOCOL_VERSION = "wide-forward-stratified-v1"
WIDE_REGISTRATION_ORIGIN = "wide_forward_research"
LATE_START_PROTOCOL_PREFIX = "wide-forward-late-start"
LATE_START_REGISTRATION_ORIGIN = "wide_forward_late_start_research"


def wide_forward_protocol(settings: Settings, *, frozen_at: datetime) -> dict[str, Any]:
    target = int(settings.get("strategies.wide_forward.target_sample_size", 24))
    minimum = int(settings.get("strategies.wide_forward.minimum_sample_size", 20))
    if not 20 <= minimum <= target <= 30:
        raise ValueError("wide forward configuration requires 20..30 selected stocks")
    return {
        "protocol_version": WIDE_PROTOCOL_VERSION,
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "selection": {
            "method": "deterministic_multi_axis_round_robin_v1",
            "target_sample_size": target,
            "minimum_sample_size": minimum,
            "manual_symbol_override_allowed": False,
            "required_trend_buckets": ["strong", "neutral", "weak"],
            "required_market_cap_buckets": ["large", "mid", "small"],
            "minimum_industries": int(
                settings.get("strategies.wide_forward.minimum_industries", 6)
            ),
        },
        "horizons": [5, 20],
        "variants": [item.value for item in ABLATION_VARIANTS],
        "signal_time": "T_close",
        "research_portfolio_execution": "T_plus_1_open",
        "down_prediction_evaluation": "direction_and_avoided_buy_only",
        "portfolio": {
            "initial_nav": float(
                settings.get("strategies.wide_forward.initial_research_nav", 100.0)
            ),
            "fractional_units": True,
            "weighting": "equal_notional",
            "real_account": False,
        },
        "llm_plan": {
            "committee_runs_per_symbol": 1,
            "raw_forecasts_per_symbol": 2,
            "maximum_calls_per_batch": int(
                settings.get("strategies.wide_forward.maximum_llm_calls_per_batch", 240)
            ),
            "maximum_tokens_per_batch": int(
                settings.get("strategies.wide_forward.maximum_llm_tokens_per_batch", 3_000_000)
            ),
            "maximum_cost_usd_per_batch": float(
                settings.get("strategies.wide_forward.maximum_llm_cost_usd_per_batch", 40.0)
            ),
        },
        "governance_version": FORWARD_GOVERNANCE_VERSION,
        "prompt_version": FORWARD_PROMPT_VERSION,
        "historical_backfill_allowed": False,
        "frozen_at": frozen_at.astimezone(UTC).isoformat(),
    }


def preregister_wide_forward_experiment(
    settings: Settings,
    *,
    frozen_at: datetime | None = None,
    signal_start_date: date | None = None,
) -> dict[str, Any]:
    repository = WideResearchRepository(settings.resolve(settings.get("system.database_path")))
    existing = repository.experiment_by_protocol_version(WIDE_PROTOCOL_VERSION)
    if existing is not None:
        portfolios = repository.ensure_portfolios(
            experiment=existing,
            cost_rules=settings.get("costs.stock", {}),
            initial_nav=float(existing["payload"]["portfolio"]["initial_nav"]),
        )
        with repository.connect() as db:
            row = db.execute(
                "SELECT * FROM forward_ablation_cohorts WHERE cohort_id=?",
                (existing["cohort_id"],),
            ).fetchone()
        cohort = dict(row) if row else {"cohort_id": existing["cohort_id"]}
        if isinstance(cohort.get("payload"), str):
            cohort["payload"] = json.loads(cohort["payload"])
        return {
            "experiment": existing,
            "cohort": cohort,
            "research_portfolios": portfolios,
            "activation": {
                "status": existing["status"],
                "signal_start_date": existing["signal_start_date"],
                "historical_date_registration_allowed": False,
                "idempotent": True,
            },
        }
    observed = (frozen_at or datetime.now(UTC)).astimezone(UTC)
    protocol = wide_forward_protocol(settings, frozen_at=observed)
    protocol_hash = _fingerprint(protocol)
    strategy_repository = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    cohort = strategy_repository.create_forward_cohort(
        protocol_version=WIDE_PROTOCOL_VERSION,
        protocol_hash=protocol_hash,
        frozen_at=observed,
        minimum_matured_samples=30,
        payload=protocol,
    )
    start = signal_start_date or TradingCalendarService.from_settings(settings).next_open_day(
        observed.astimezone(
            ZoneInfo(str(settings.get("system.timezone", "Asia/Shanghai")))
        ).date()
    )
    experiment = repository.create_experiment(
        protocol_version=WIDE_PROTOCOL_VERSION,
        cohort_id=cohort["cohort_id"],
        target_sample_size=int(protocol["selection"]["target_sample_size"]),
        minimum_sample_size=int(protocol["selection"]["minimum_sample_size"]),
        signal_start_date=start,
        frozen_at=observed,
        payload=protocol,
    )
    portfolios = repository.ensure_portfolios(
        experiment=experiment,
        cost_rules=settings.get("costs.stock", {}),
        initial_nav=float(protocol["portfolio"]["initial_nav"]),
    )
    return {
        "experiment": experiment,
        "cohort": cohort,
        "research_portfolios": portfolios,
        "activation": {
            "status": "preregistered",
            "signal_start_date": start.isoformat(),
            "historical_date_registration_allowed": False,
        },
    }


def preregister_late_start_wide_experiment(
    settings: Settings,
    *,
    trade_date: date,
    frozen_at: datetime | None = None,
) -> dict[str, Any]:
    """Freeze a same-day post-close research cohort without altering the strict cohort.

    This is prospective for future outcomes, but it is intentionally not described as a
    before-signal preregistration because the signal-day close is already observable.
    """

    observed = (frozen_at or datetime.now(UTC)).astimezone(UTC)
    market_tz = ZoneInfo(str(settings.get("system.timezone", "Asia/Shanghai")))
    local_observed = observed.astimezone(market_tz)
    if trade_date != local_observed.date():
        raise ValueError("late-start wide research must use the current market date")
    if local_observed.time() < time(15, 0):
        raise ValueError("late-start wide research is only allowed after the market close")
    if not TradingCalendarService.from_settings(settings).is_open(
        trade_date,
        cutoff_at=observed,
        formal=True,
        minimum_trust=DataTrustLevel.SERVER_OBSERVED,
    ):
        raise ValueError("late-start wide research requires an open production trading day")

    protocol_version = f"{LATE_START_PROTOCOL_PREFIX}-{trade_date.isoformat()}-v2"
    repository = WideResearchRepository(settings.resolve(settings.get("system.database_path")))
    existing = repository.experiment_by_protocol_version(protocol_version)
    if existing is not None:
        portfolios = repository.ensure_portfolios(
            experiment=existing,
            cost_rules=settings.get("costs.stock", {}),
            initial_nav=float(existing["payload"]["portfolio"]["initial_nav"]),
        )
        return {
            "experiment": existing,
            "research_portfolios": portfolios,
            "activation": {
                "status": existing["status"],
                "signal_start_date": existing["signal_start_date"],
                "evidence_grade": "research_only_late_start_forward",
                "idempotent": True,
            },
        }

    protocol = wide_forward_protocol(settings, frozen_at=observed)
    protocol.update(
        {
            "protocol_version": protocol_version,
            "activation_mode": "operator_post_close_before_next_open",
            "evidence_grade": "research_only_late_start_forward",
            "strict_preregistered_before_signal_close": False,
            "formal_primary_scorecard_eligible": False,
            "late_start_trade_date": trade_date.isoformat(),
            "claim_boundary": (
                "The signal-day close was observable when this cohort was frozen. Future "
                "5/20-session outcomes and the next open were not observable. This is a "
                "prospective research observation, not strict before-signal preregistration."
            ),
        }
    )
    protocol_hash = _fingerprint(protocol)
    strategy_repository = StrategyEvidenceRepository(repository.path)
    cohort = strategy_repository.create_forward_cohort(
        protocol_version=protocol_version,
        protocol_hash=protocol_hash,
        frozen_at=observed,
        minimum_matured_samples=30,
        payload=protocol,
    )
    experiment = repository.create_experiment(
        protocol_version=protocol_version,
        cohort_id=cohort["cohort_id"],
        target_sample_size=int(protocol["selection"]["target_sample_size"]),
        minimum_sample_size=int(protocol["selection"]["minimum_sample_size"]),
        signal_start_date=trade_date,
        frozen_at=observed,
        payload=protocol,
    )
    portfolios = repository.ensure_portfolios(
        experiment=experiment,
        cost_rules=settings.get("costs.stock", {}),
        initial_nav=float(protocol["portfolio"]["initial_nav"]),
    )
    return {
        "experiment": experiment,
        "cohort": cohort,
        "research_portfolios": portfolios,
        "activation": {
            "status": "preregistered",
            "signal_start_date": trade_date.isoformat(),
            "evidence_grade": "research_only_late_start_forward",
            "historical_date_registration_allowed": False,
        },
    }


def select_wide_forward_sample(
    members: list[dict[str, Any]],
    *,
    target_sample_size: int,
    minimum_sample_size: int,
    minimum_industries: int,
    seed: str,
) -> dict[str, Any]:
    if not 20 <= minimum_sample_size <= target_sample_size <= 30:
        raise ValueError("wide forward sample size must be between 20 and 30")
    eligible = [item for item in members if _eligible_member(item)]
    if len(eligible) < target_sample_size:
        raise ValueError("trusted point-in-time universe is smaller than the wide sample target")
    caps = sorted(float(item["fund_size"]) for item in eligible)
    lower_cap = caps[len(caps) // 3]
    upper_cap = caps[(len(caps) * 2) // 3]
    turnovers = [float(item["payload"]["turnover_rate"]) for item in eligible]
    turnover_median = median(turnovers)
    amounts = sorted(float(item["amount"]) for item in eligible)
    annotations = [
        _annotate_member(
            item,
            lower_cap=lower_cap,
            upper_cap=upper_cap,
            turnover_median=turnover_median,
            amount_rank=_percentile_rank(amounts, float(item["amount"])),
            seed=seed,
        )
        for item in eligible
    ]
    strata: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in annotations:
        key = (
            item["industry"],
            item["market_cap_bucket"],
            item["trend_bucket"],
            item["style_bucket"],
        )
        strata[key].append(item)
    for items in strata.values():
        items.sort(key=lambda item: (-abs(item["quant_score"]), item["tie_breaker"]))
    ordered_keys = sorted(
        strata,
        key=lambda key: hashlib.sha256(f"{seed}:{key}".encode()).hexdigest(),
    )
    industry_limit = max(2, math.ceil(target_sample_size * 0.20))
    dimension_limit = math.ceil(target_sample_size * 0.45)
    selected: list[dict[str, Any]] = []
    industry_counts: Counter[str] = Counter()
    cap_counts: Counter[str] = Counter()
    trend_counts: Counter[str] = Counter()
    while len(selected) < target_sample_size:
        progressed = False
        for key in ordered_keys:
            bucket = strata[key]
            if not bucket:
                continue
            candidate = bucket[0]
            if industry_counts[candidate["industry"]] >= industry_limit:
                continue
            if cap_counts[candidate["market_cap_bucket"]] >= dimension_limit:
                continue
            if trend_counts[candidate["trend_bucket"]] >= dimension_limit:
                continue
            bucket.pop(0)
            selected.append(candidate)
            industry_counts[candidate["industry"]] += 1
            cap_counts[candidate["market_cap_bucket"]] += 1
            trend_counts[candidate["trend_bucket"]] += 1
            progressed = True
            if len(selected) >= target_sample_size:
                break
        if not progressed:
            break
    if len(selected) < minimum_sample_size:
        raise ValueError("diversity constraints left fewer than 20 trusted wide members")
    required_trends = {"strong", "neutral", "weak"}
    required_caps = {"large", "mid", "small"}
    observed_trends = {item["trend_bucket"] for item in selected}
    observed_caps = {item["market_cap_bucket"] for item in selected}
    observed_industries = {item["industry"] for item in selected}
    if not required_trends.issubset(observed_trends):
        raise ValueError("wide sample cannot represent strong, neutral and weak stocks")
    if not required_caps.issubset(observed_caps):
        raise ValueError("wide sample cannot represent large, mid and small caps")
    if len(observed_industries) < minimum_industries:
        raise ValueError("wide sample does not meet the frozen industry diversity requirement")
    ranked = sorted(
        selected,
        key=lambda item: (
            item["trend_bucket"],
            item["market_cap_bucket"],
            item["industry"],
            -abs(item["quant_score"]),
            item["symbol"],
        ),
    )
    for index, item in enumerate(ranked, start=1):
        item["selection_rank"] = index
        item.pop("tie_breaker", None)
    return {
        "members": ranked,
        "eligible_universe": len(eligible),
        "selected_stocks": len(ranked),
        "strata": {
            "industries": dict(Counter(item["industry"] for item in ranked)),
            "market_cap": dict(Counter(item["market_cap_bucket"] for item in ranked)),
            "trend": dict(Counter(item["trend_bucket"] for item in ranked)),
            "style": dict(Counter(item["style_bucket"] for item in ranked)),
            "price_change_state": dict(
                Counter(item["price_change_state"] for item in ranked)
            ),
        },
        "seed": seed,
        "manual_selection": False,
    }


def register_wide_forward_batch(
    settings: Settings,
    *,
    trade_date: date,
    schedule_run_id: str,
    experiment_id: str | None = None,
    registration_origin: str = WIDE_REGISTRATION_ORIGIN,
    registration_started_at: datetime | None = None,
    prediction_freezer: Callable[..., list[dict[str, Any]]] = freeze_forward_ablation_sample,
    context_builder: Callable[..., dict[str, Any]] = build_analysis_context_pack,
    committee_runner: Callable[..., dict[str, Any]] = run_context_committee,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    started = (registration_started_at or datetime.now(UTC)).astimezone(UTC)
    repository = WideResearchRepository(settings.resolve(settings.get("system.database_path")))
    experiment = (
        repository.experiment(experiment_id)
        if experiment_id
        else repository.experiment_by_protocol_version(WIDE_PROTOCOL_VERSION)
    )
    if experiment is None:
        raise ValueError("wide forward experiment must be preregistered before scheduling")
    if trade_date < date.fromisoformat(experiment["signal_start_date"]):
        raise ValueError("wide forward batches cannot predate protocol registration")
    strategy_repository = StrategyEvidenceRepository(repository.path)
    snapshot = strategy_repository.latest_pool_snapshot(
        "a_share",
        on_or_before=trade_date,
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=DataTrustLevel.SERVER_OBSERVED,
    )
    if snapshot is None or snapshot["snapshot_date"] != trade_date.isoformat():
        raise ValueError("exact-date trusted production point-in-time pool is unavailable")
    _validate_snapshot_temporal_identity(snapshot, trade_date, started)
    protocol = experiment["payload"]
    selection = select_wide_forward_sample(
        snapshot["members"],
        target_sample_size=int(experiment["target_sample_size"]),
        minimum_sample_size=int(experiment["minimum_sample_size"]),
        minimum_industries=int(protocol["selection"]["minimum_industries"]),
        seed=f"{experiment['protocol_fingerprint']}:{trade_date.isoformat()}",
    )
    _validate_batch_budget(settings, selection["selected_stocks"], protocol["llm_plan"])
    batch = repository.begin_batch(
        experiment=experiment,
        trade_date=trade_date,
        snapshot=snapshot,
        schedule_run_id=schedule_run_id,
        started_at=started,
    )
    newly_created = bool(batch.pop("_newly_created", False))
    if batch["status"] == "completed":
        return repository.batch(batch["batch_id"]) or batch
    if batch["status"] == "running" and not newly_created:
        return {
            **batch,
            "idempotent": True,
            "in_progress": True,
            "reason": "wide_forward_batch_already_running",
        }
    usage_started_at = datetime.fromisoformat(batch["registration_started_at"])
    repository.save_members(batch["batch_id"], selection["members"])
    if progress_callback:
        progress_callback(0.10, "wide sample frozen; starting context and prediction runs")
    prediction_count = 0
    role_completeness: list[float] = []
    try:
        for index, member in enumerate(selection["members"], start=1):
            existing = _existing_wide_predictions(
                strategy_repository,
                batch_id=batch["batch_id"],
                cohort_id=experiment["cohort_id"],
                symbol=member["symbol"],
                as_of=trade_date,
                registration_origin=registration_origin,
            )
            if len(existing) == len(ABLATION_VARIANTS) * 2:
                role_completeness.extend(
                    float(item.get("role_completeness", 0.0)) for item in existing
                )
                for horizon in (5, 20):
                    linked = [row for row in existing if int(row["horizon_days"]) == horizon]
                    prediction_count += repository.link_predictions(
                        batch_id=batch["batch_id"],
                        symbol=member["symbol"],
                        horizon_days=horizon,
                        predictions=linked,
                    )
                continue
            context_payload = context_builder(
                settings,
                symbol=member["symbol"],
                as_of=trade_date,
                asset_type="stock",
                include_events=True,
                save=True,
            )
            pack = AnalysisContextPack.model_validate(context_payload)
            if pack.symbol != member["symbol"] or pack.as_of != trade_date:
                raise ValueError("wide-forward ContextPack identity mismatch")
            try:
                committee = committee_runner(
                    settings,
                    pack=pack,
                    deterministic_max_weight=float(
                        settings.get("risk.max_single_position", 0.15)
                    ),
                    idempotency_key=(
                        f"wide-committee:{experiment['cohort_id']}:"
                        f"{member['symbol']}:{trade_date.isoformat()}"
                    ),
                )
            except Exception as exc:
                committee = {
                    "action": "review_required",
                    "confidence": 0.0,
                    "suggested_weight_max": 0.0,
                    "degraded_roles": ["committee"],
                    "failure_type": type(exc).__name__,
                }

            def frozen_committee(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return committee

            for horizon in (5, 20):
                predictions = prediction_freezer(
                    settings,
                    cohort_id=experiment["cohort_id"],
                    symbol=member["symbol"],
                    horizon_days=horizon,
                    context_pack=pack,
                    committee_runner=frozen_committee,
                    registration_origin=registration_origin,
                    formal=True,
                    minimum_trust_level=DataTrustLevel.SERVER_OBSERVED,
                    as_of=trade_date,
                    sample_key_context_fingerprint=_fingerprint(
                        {
                            "experiment_id": experiment["experiment_id"],
                            "snapshot_fingerprint": batch["snapshot_fingerprint"],
                            "registration_origin": registration_origin,
                        }
                    ),
                )
                prediction_count += repository.link_predictions(
                    batch_id=batch["batch_id"],
                    symbol=member["symbol"],
                    horizon_days=horizon,
                    predictions=predictions,
                )
                role_completeness.extend(
                    float(item.get("role_completeness", 0.0)) for item in predictions
                )
            if progress_callback:
                progress_callback(
                    0.10 + 0.80 * index / selection["selected_stocks"],
                    f"wide predictions completed {index}/{selection['selected_stocks']}",
                )
    except Exception as exc:
        usage = _llm_usage(
            repository.path,
            experiment["cohort_id"],
            usage_started_at,
        )
        repository.finish_batch(
            batch["batch_id"],
            status="failed",
            member_count=selection["selected_stocks"],
            prediction_count=prediction_count,
            llm_usage=usage,
            role_completeness=_mean(role_completeness) or 0.0,
            failure_reason=f"{type(exc).__name__}:{exc}",
            payload={"selection": selection},
        )
        raise
    expected_predictions = selection["selected_stocks"] * 2 * len(ABLATION_VARIANTS)
    status = "completed" if prediction_count == expected_predictions else "failed"
    failure_reason = None if status == "completed" else "incomplete_prediction_matrix"
    usage = _llm_usage(
        repository.path,
        experiment["cohort_id"],
        usage_started_at,
    )
    result = repository.finish_batch(
        batch["batch_id"],
        status=status,
        member_count=selection["selected_stocks"],
        prediction_count=prediction_count,
        llm_usage=usage,
        role_completeness=_mean(role_completeness) or 0.0,
        failure_reason=failure_reason,
        payload={
            "selection": selection,
            "llm_budget_plan": protocol["llm_plan"],
            "same_day_samples_are_correlated": True,
            "independent_trade_days": 1,
        },
    )
    if status != "completed":
        raise RuntimeError("wide forward prediction matrix is incomplete")
    return repository.batch(result["batch_id"]) or result


def mark_wide_research_portfolios(
    settings: Settings,
    *,
    experiment_id: str | None = None,
    bar_service: ResearchBarService | None = None,
) -> dict[str, Any]:
    repository = WideResearchRepository(settings.resolve(settings.get("system.database_path")))
    experiments = (
        [repository.experiment(experiment_id)]
        if experiment_id
        else [
            item
            for item in repository.experiments()
            if item["status"] in {"preregistered", "active"}
        ]
    )
    experiments = [item for item in experiments if item is not None]
    if not experiments:
        return {"status": "skipped", "reason": "wide_forward_experiment_unavailable"}
    service = bar_service or ResearchBarService.from_settings(settings)
    results = [
        _mark_wide_research_portfolio(
            settings,
            repository=repository,
            experiment=experiment,
            bar_service=service,
        )
        for experiment in experiments
    ]
    return {
        "inserted_positions": sum(int(item["inserted_positions"]) for item in results),
        "portfolios": sum(int(item["portfolios"]) for item in results),
        "experiments": results,
    }


def _mark_wide_research_portfolio(
    settings: Settings,
    *,
    repository: WideResearchRepository,
    experiment: dict[str, Any],
    bar_service: ResearchBarService,
) -> dict[str, Any]:
    standardized: dict[tuple[str, str, int], dict[str, Any]] = {}
    benchmarks: dict[tuple[str, int], float] = {}
    with repository.connect() as db:
        rows = db.execute(
            """SELECT DISTINCT l.symbol,b.trade_date,l.horizon_days,p.due_at
               FROM wide_forward_prediction_links l
               JOIN wide_forward_batches b ON b.batch_id=l.batch_id
               JOIN forward_ablation_predictions p ON p.prediction_id=l.prediction_id
               JOIN forward_ablation_outcomes o ON o.prediction_id=p.prediction_id
               WHERE b.experiment_id=? AND b.status='completed'
               ORDER BY b.trade_date,l.horizon_days,l.symbol""",
            (experiment["experiment_id"],),
        ).fetchall()
    calendar = TradingCalendarService.from_settings(settings)
    for row in rows:
        signal_date = date.fromisoformat(row["trade_date"])
        execution_date = calendar.next_open_day(signal_date)
        due_date = datetime.fromisoformat(row["due_at"]).date()
        try:
            entry = bar_service.get(
                row["symbol"],
                as_of=execution_date,
                minimum_trust=DataTrustLevel.SERVER_OBSERVED,
                exact=True,
            )
            outcome = bar_service.get(
                row["symbol"],
                as_of=due_date,
                minimum_trust=DataTrustLevel.SERVER_OBSERVED,
                exact=True,
            )
            benchmark_entry = bar_service.get(
                "sh000300",
                as_of=execution_date,
                minimum_trust=DataTrustLevel.SERVER_OBSERVED,
                exact=True,
            )
            benchmark_outcome = bar_service.get(
                "sh000300",
                as_of=due_date,
                minimum_trust=DataTrustLevel.SERVER_OBSERVED,
                exact=True,
            )
        except Exception:
            continue
        if entry.bar.open <= 0 or benchmark_entry.bar.open <= 0:
            continue
        standardized[(row["symbol"], row["trade_date"], int(row["horizon_days"]))] = {
            "execution_date": execution_date.isoformat(),
            "entry_price": entry.bar.open,
            "exit_price": outcome.bar.close,
            "realized_return_pct": (outcome.bar.close / entry.bar.open - 1.0) * 100.0,
            "entry_fingerprint": entry.payload_fingerprint,
            "outcome_fingerprint": outcome.payload_fingerprint,
        }
        benchmarks[(row["trade_date"], int(row["horizon_days"]))] = (
            benchmark_outcome.bar.close / benchmark_entry.bar.open - 1.0
        ) * 100.0
    result = repository.mark_settled_positions(
        experiment_id=experiment["experiment_id"],
        benchmark_returns=benchmarks,
        standardized_returns=standardized,
    )
    return {"experiment_id": experiment["experiment_id"], **result}


def reconcile_wide_forward_batch_usage(
    settings: Settings, *, batch_id: str
) -> dict[str, Any]:
    """Reconcile persisted LLM telemetry while preserving frozen predictions and timings."""
    repository = WideResearchRepository(settings.resolve(settings.get("system.database_path")))
    batch = repository.batch(batch_id)
    if batch is None:
        raise ValueError("wide forward batch not found")
    if batch["status"] not in {"completed", "failed", "blocked"}:
        raise ValueError("wide forward batch must be terminal before usage reconciliation")
    experiment = repository.experiment(batch["experiment_id"])
    if experiment is None or experiment["evidence_boundary"] != EVIDENCE_BOUNDARY:
        raise ValueError("wide forward experiment is unavailable")
    completed_at = batch.get("registration_completed_at")
    if not completed_at:
        raise ValueError("wide forward batch completion time is unavailable")
    usage = _llm_usage(
        repository.path,
        experiment["cohort_id"],
        datetime.fromisoformat(batch["registration_started_at"]),
        ended_at=datetime.fromisoformat(completed_at),
    )
    return repository.reconcile_batch_usage(batch_id, llm_usage=usage)


def _eligible_member(item: dict[str, Any]) -> bool:
    payload = item.get("payload") or {}
    observations = payload.get("field_observations") or {}
    required = ("price", "daily_return_pct", "amount", "turnover_rate", "market_cap")
    return bool(
        item.get("eligible")
        and not item.get("missing_fields")
        and item.get("amount") not in {None, 0}
        and item.get("fund_size") not in {None, 0}
        and payload.get("industry")
        and payload.get("latest_price") is not None
        and payload.get("daily_return_pct") is not None
        and payload.get("turnover_rate") is not None
        and payload.get("market_cap") is not None
        and all(
            (observations.get(field) or {}).get("missing_reason") is None
            for field in required
        )
    )


def _annotate_member(
    item: dict[str, Any],
    *,
    lower_cap: float,
    upper_cap: float,
    turnover_median: float,
    amount_rank: float,
    seed: str,
) -> dict[str, Any]:
    payload = item["payload"]
    cap = float(item["fund_size"])
    daily_return = float(payload["daily_return_pct"])
    turnover = float(payload["turnover_rate"])
    cap_bucket = "small" if cap <= lower_cap else "large" if cap >= upper_cap else "mid"
    trend = "strong" if daily_return >= 1.0 else "weak" if daily_return <= -1.0 else "neutral"
    state = "up" if daily_return > 0.3 else "down" if daily_return < -0.3 else "flat"
    style = (
        "large_defensive"
        if cap_bucket == "large" and turnover <= turnover_median
        else "small_active"
        if cap_bucket == "small" and turnover > turnover_median
        else "momentum"
        if trend == "strong"
        else "contrarian"
        if trend == "weak"
        else "balanced"
    )
    return_component = max(-1.0, min(1.0, daily_return / 8.0))
    turnover_component = max(-1.0, min(1.0, turnover / max(1.0, turnover_median) - 1.0))
    liquidity_component = amount_rank * 2.0 - 1.0
    score = 0.60 * return_component + 0.20 * turnover_component + 0.20 * liquidity_component
    direction = "up" if score >= 0.15 else "down" if score <= -0.15 else "neutral"
    observations = payload["field_observations"]
    included = [value for value in observations.values() if value.get("missing_reason") is None]
    source_fingerprint = _fingerprint(
        sorted(
            {
                value.get("raw_response_fingerprint")
                for value in included
                if value.get("raw_response_fingerprint")
            }
        )
    )
    return {
        "symbol": item["symbol"],
        "industry": str(payload["industry"]),
        "market_cap_bucket": cap_bucket,
        "trend_bucket": trend,
        "price_change_state": state,
        "style_bucket": style,
        "quant_score": score,
        "quant_direction": direction,
        "observed_at": item["available_at"],
        "source": item["source"],
        "source_fingerprint": source_fingerprint,
        "missing_reasons": [],
        "payload": {
            "name": item.get("name"),
            "amount": item["amount"],
            "market_cap": item["fund_size"],
            "turnover_rate": turnover,
            "latest_price": payload["latest_price"],
            "previous_close": payload["previous_close"],
            "daily_return_pct": daily_return,
            "field_observations": observations,
            "data_source_manifest_id": item.get("manifest_id"),
        },
        "tie_breaker": hashlib.sha256(
            f"{seed}:{item['symbol']}".encode("utf-8")
        ).hexdigest(),
    }


def _percentile_rank(values: list[float], value: float) -> float:
    if len(values) <= 1:
        return 0.5
    below = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return (below + 0.5 * equal) / len(values)


def _validate_batch_budget(
    settings: Settings, stocks: int, plan: dict[str, Any]
) -> None:
    roles = int(settings.get("llm.maximum_committee_roles", 6))
    expected_calls = stocks * (roles + 2)
    if expected_calls > int(plan["maximum_calls_per_batch"]):
        raise ValueError("wide-forward LLM call plan exceeds the frozen batch budget")
    minimum_tokens = expected_calls * int(
        settings.get("strategies.wide_forward.reserved_tokens_per_call", 8_000)
    )
    if minimum_tokens > int(plan["maximum_tokens_per_batch"]):
        raise ValueError("wide-forward token reservation cannot complete the core role graph")
    reserved_cost = expected_calls * float(
        settings.get("strategies.wide_forward.reserved_cost_usd_per_call", 0.15)
    )
    if reserved_cost > float(plan["maximum_cost_usd_per_batch"]):
        raise ValueError("wide-forward cost reservation exceeds the frozen batch budget")


def _validate_snapshot_temporal_identity(
    snapshot: dict[str, Any], trade_date: date, registration_started_at: datetime
) -> None:
    cutoff = datetime.fromisoformat(snapshot["cutoff_at"]).astimezone(UTC)
    if cutoff > registration_started_at.astimezone(UTC):
        raise ValueError("wide forward snapshot cutoff is after registration start")
    violations: list[str] = []
    for member in snapshot["members"]:
        observations = (member.get("payload") or {}).get("field_observations") or {}
        for field, observation in observations.items():
            if observation.get("missing_reason") is not None:
                continue
            market_date = observation.get("market_date")
            available_at = observation.get("available_at")
            if market_date and market_date != trade_date.isoformat():
                violations.append(f"{member['symbol']}:{field}:market_date")
            if available_at and datetime.fromisoformat(available_at).astimezone(UTC) > cutoff:
                violations.append(f"{member['symbol']}:{field}:available_at")
    if violations:
        raise ValueError(
            "wide forward snapshot violates point-in-time field identity: "
            + ",".join(violations[:10])
        )


def _existing_wide_predictions(
    repository: StrategyEvidenceRepository,
    *,
    batch_id: str,
    cohort_id: str,
    symbol: str,
    as_of: date,
    registration_origin: str,
) -> list[dict[str, Any]]:
    with repository.connect() as db:
        rows = db.execute(
            """SELECT p.* FROM wide_forward_prediction_links l
               JOIN forward_ablation_predictions p ON p.prediction_id=l.prediction_id
               WHERE l.batch_id=? AND l.symbol=? AND p.cohort_id=? AND p.as_of=?
                 AND p.registration_origin=?
               ORDER BY p.horizon_days,p.variant""",
            (batch_id, symbol, cohort_id, as_of.isoformat(), registration_origin),
        ).fetchall()
        if len(rows) != len(ABLATION_VARIANTS) * 2:
            candidates = db.execute(
                """SELECT * FROM forward_ablation_predictions
                   WHERE cohort_id=? AND symbol=? AND as_of=? AND registration_origin=?
                   ORDER BY registered_at,sample_key,horizon_days,variant""",
                (cohort_id, symbol, as_of.isoformat(), registration_origin),
            ).fetchall()
            selected: list[Any] = []
            linked_horizons = {int(row["horizon_days"]) for row in rows}
            for horizon in (5, 20):
                if horizon in linked_horizons:
                    selected.extend(row for row in rows if int(row["horizon_days"]) == horizon)
                    continue
                groups: dict[str, list[Any]] = {}
                for row in candidates:
                    if int(row["horizon_days"]) == horizon:
                        groups.setdefault(str(row["sample_key"]), []).append(row)
                complete = next(
                    (
                        group
                        for group in groups.values()
                        if {str(item["variant"]) for item in group}
                        == {variant.value for variant in ABLATION_VARIANTS}
                    ),
                    [],
                )
                selected.extend(complete)
            rows = selected
    output = []
    for row in rows:
        item = dict(row)
        item["probabilities"] = json.loads(item["probabilities"])
        item["payload"] = json.loads(item["payload"])
        output.append(item)
    return output


def _llm_usage(
    path: Any,
    cohort_id: str,
    started_at: datetime,
    *,
    ended_at: datetime | None = None,
) -> dict[str, Any]:
    with StrategyEvidenceRepository(path).connect() as db:
        row = db.execute(
            """SELECT COUNT(*) calls,COALESCE(SUM(input_tokens),0) input_tokens,
                      COALESCE(SUM(output_tokens),0) output_tokens,
                      COALESCE(SUM(estimated_cost_usd),0) cost_usd,
                      COALESCE(SUM(latency_ms),0) latency_ms
               FROM llm_governed_calls
               WHERE created_at>=? AND (? IS NULL OR created_at<=?)
                 AND (task_id LIKE ? OR task_id LIKE ? OR task_id LIKE ? OR task_id LIKE ?)""",
            (
                started_at.astimezone(UTC).isoformat(),
                ended_at.astimezone(UTC).isoformat() if ended_at else None,
                ended_at.astimezone(UTC).isoformat() if ended_at else None,
                f"forward:{cohort_id}:%",
                f"raw-forward:{cohort_id}:%",
                f"wide-committee:{cohort_id}:%",
                f"context-committee:wide-committee:{cohort_id}:%",
            ),
        ).fetchone()
    return dict(row)


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


__all__ = [
    "LATE_START_PROTOCOL_PREFIX",
    "LATE_START_REGISTRATION_ORIGIN",
    "WIDE_PROTOCOL_VERSION",
    "mark_wide_research_portfolios",
    "preregister_late_start_wide_experiment",
    "preregister_wide_forward_experiment",
    "reconcile_wide_forward_batch_usage",
    "register_wide_forward_batch",
    "select_wide_forward_sample",
    "wide_forward_protocol",
]
