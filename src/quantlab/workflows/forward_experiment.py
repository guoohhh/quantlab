from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any, Callable

from quantlab.config import Settings
from quantlab.domain import AnalysisContextPack, AssetType, DataQuality, MarketQuote
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.learning import LearningRepository
from quantlab.persistence.evidence import EvidenceRepository
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.notifications import NotificationRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.security import sanitize_for_export
from quantlab.market import QuoteService, TradingCalendarService
from quantlab.workflows.context import build_analysis_context_pack
from quantlab.workflows.forward_ablation import (
    FORWARD_GOVERNANCE_VERSION,
    FORWARD_PROMPT_VERSION,
    _asset_type,
    _execute_variants,
    create_round3_forward_cohort,
    freeze_forward_ablation_sample,
    forward_ablation_scorecard,
    run_raw_forward_llm,
)
from quantlab.workflows.llm_committee import ROLE_DOMAINS, run_context_committee


PRIMARY_PROTOCOL_VERSION = "primary-forward-v2"


class _PointInTimePoolQuoteProvider:
    """Authoritative signal-close quotes frozen inside one admitted PIT snapshot."""

    provider_name = "trusted_point_in_time_pool"
    authoritative = True

    def __init__(self, settings: Settings, snapshot: dict[str, Any]):
        self.settings = settings
        self.snapshot = snapshot
        self.provider_version = str(snapshot.get("source_version") or "unknown")
        self.members = {item["symbol"]: item for item in snapshot.get("members") or []}

    def quote(
        self,
        symbol: str,
        *,
        asset_type: AssetType,
        as_of: date,
    ) -> MarketQuote:
        if str(self.snapshot.get("snapshot_date"))[:10] != as_of.isoformat():
            raise ValueError("point-in-time quote requires the exact snapshot date")
        member = self.members.get(symbol)
        if member is None or not member.get("eligible"):
            raise ValueError("eligible point-in-time pool member is unavailable")
        payload = dict(member.get("payload") or {})
        if str(payload.get("trade_date"))[:10] != as_of.isoformat():
            raise ValueError("point-in-time member quote is not for the signal date")
        price = payload.get("latest_price")
        if price is None or float(price) <= 0:
            raise ValueError("point-in-time member has no positive signal close")
        market_date = str(payload.get("spot_provider_market_date") or "")[:10]
        if market_date != as_of.isoformat():
            raise ValueError("point-in-time member provider date does not match signal date")
        observations = dict(payload.get("field_observations") or {})
        price_observation = dict(observations.get("price") or {})
        if price_observation.get("missing_reason") or not price_observation.get(
            "available_at"
        ):
            raise ValueError("point-in-time signal close lacks an auditable observation")
        available_at = datetime.fromisoformat(
            str(price_observation["available_at"]).replace("Z", "+00:00")
        )
        if available_at.tzinfo is None:
            raise ValueError("point-in-time quote availability must be timezone-aware")
        cutoff_at = datetime.fromisoformat(
            str(self.snapshot["cutoff_at"]).replace("Z", "+00:00")
        )
        if available_at.astimezone(UTC) > cutoff_at.astimezone(UTC):
            raise ValueError("point-in-time quote observation exceeds snapshot cutoff")
        provider = str(payload.get("field_sources", {}).get("current_spot") or "")
        if not provider:
            raise ValueError("point-in-time quote provider provenance is unavailable")
        return MarketQuote(
            symbol=symbol,
            name=str(member.get("name") or symbol),
            asset_type=asset_type,
            raw_price=float(price),
            as_of=as_of,
            available_at=available_at.astimezone(UTC),
            source=provider,
            provider=provider,
            source_version=self.provider_version,
            data_quality=(
                DataQuality.AVAILABLE
                if member.get("data_quality") == "available"
                else DataQuality.DEGRADED
            ),
            suspended=bool(payload.get("suspended")),
            is_st=bool(payload.get("is_st")),
            industry=payload.get("industry") or member.get("category"),
            trade_lot=int(
                self.settings.get(
                    f"costs.{asset_type.value}.trade_lot",
                    100 if asset_type in {AssetType.STOCK, AssetType.ETF} else 10,
                )
            ),
            t_plus_one=asset_type == AssetType.STOCK,
            session_status="closed",
            quote_kind="current_close",
            authoritative=True,
            evidence_stage="production",
            trust_level=DataTrustLevel(
                str(self.snapshot.get("trust_level") or "server_observed")
            ),
            license_status="provider_terms_unverified_no_sla",
            endpoint=f"point_in_time_pool/{self.snapshot['snapshot_id']}",
            raw_payload_fingerprint=str(
                price_observation.get("raw_response_fingerprint")
                or self.snapshot.get("fingerprint")
                or ""
            ),
            actionable=False,
            actionability_reasons=["signal_close_is_not_intraday_actionable"],
            risk_metadata={
                "pool_snapshot_id": self.snapshot["snapshot_id"],
                "pool_fingerprint": self.snapshot.get("fingerprint"),
            },
        )


def ensure_primary_forward_experiment(
    settings: Settings,
    *,
    activation_origin: str = "internal",
    activation_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    existing = repository.primary_experiment()
    if existing and existing["protocol_version"] == PRIMARY_PROTOCOL_VERSION:
        if existing.get("status") != "active" or not existing.get("is_primary"):
            raise ValueError("the frozen primary forward experiment is no longer active")
        if (
            not bool(settings.get("system.test_mode", False))
            and existing["frozen_payload"].get("activation_origin") != "scheduler"
        ):
            raise ValueError(
                "the existing primary cohort lacks scheduler activation provenance; "
                "create a new protocol version before collecting formal evidence"
            )
        repository.ensure_shadow_accounts(existing)
        return existing
    test_mode = bool(settings.get("system.test_mode", False))
    activation_readiness: dict[str, Any] | None = None
    if not test_mode and activation_origin != "scheduler":
        raise ValueError(
            "a production primary forward experiment can only be activated by the "
            "forward-sample registration schedule"
        )
    if not test_mode:
        from quantlab.runtime.readiness import primary_start_readiness

        reference = activation_reference or {}
        run_date = date.fromisoformat(str(reference.get("run_date"))) if reference.get(
            "run_date"
        ) else datetime.now(UTC).date()
        activation_readiness = primary_start_readiness(settings, trade_date=run_date)
        if not activation_readiness["sample_registration_allowed"]:
            raise ValueError(
                "primary forward readiness blocked: "
                + ",".join(activation_readiness["blockers"])
            )
    resolved_activation_origin = "test_internal" if test_mode else "scheduler"
    cohort = create_round3_forward_cohort(settings)
    model_configuration = _forward_model_configuration(settings)
    statistical_models = _freeze_statistical_models(settings)
    role_policies = _freeze_role_policies(settings)
    variant_policy = _variant_policy(settings)
    frozen_payload = {
        "started_at": datetime.now(UTC).isoformat(),
        "activation_origin": resolved_activation_origin,
        "activation_reference": sanitize_for_export(activation_reference or {}),
        "activation_readiness": sanitize_for_export(activation_readiness)
        if activation_readiness is not None
        else None,
        "asset_scope": list(settings.get("strategies.forward_primary.asset_scope", ["a_share"])),
        "daily_sampling_rule": str(
            settings.get("strategies.forward_primary.daily_sampling_rule", "pit_representative_rank")
        ),
        "candidate_count": int(settings.get("strategies.forward_primary.candidate_count", 3)),
        "horizons": [5, 20],
        "model_version": hashlib.sha256(
            json.dumps(model_configuration, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "model_configuration": model_configuration,
        "statistical_models": statistical_models,
        "role_policies": role_policies,
        "prompt_version": FORWARD_PROMPT_VERSION,
        "strategy_version": str(
            settings.get("strategies.a_share_v4.protocol_version", "unknown")
        ),
        "governance_version": FORWARD_GOVERNANCE_VERSION,
        "initial_capital": float(
            settings.get("strategies.forward_primary.initial_capital", 100_000.0)
        ),
        "cost_rules": {
            "stock": settings.get("costs.stock", {}),
            "signal_at": "T_close",
            "execute_at": "T_plus_1_open",
        },
        "matching_rules": {
            "partial_fill": "supported",
            "missing_open": "remain_pending",
            "lot_aware": True,
            "order_expiry_open_sessions": 5,
        },
        "missing_data_rule": "fail_closed_and_record_every_expected_sample",
        "minimum_trust_level": str(
            settings.get(
                "strategies.forward_primary.minimum_trust_level",
                DataTrustLevel.SERVER_OBSERVED.value,
            )
        ),
        "upgrade_conditions": {"milestones": [10, 20, 30], "measured_minimum": 30},
        "stop_conditions": {
            "manual_deletion": "forbidden",
            "protocol_change": "new_cohort_and_governance_record",
        },
        "variant_policy": variant_policy,
    }
    experiment = repository.create_experiment(
        protocol_version=PRIMARY_PROTOCOL_VERSION,
        cohort_id=cohort["cohort_id"],
        frozen_payload=frozen_payload,
        make_primary=True,
    )
    repository.ensure_shadow_accounts(experiment)
    return experiment


def register_primary_forward_samples(
    settings: Settings,
    *,
    trade_date: date,
    quote_service: QuoteService | None = None,
    committee_runner: Callable[..., dict[str, Any]] | None = None,
    raw_llm_runner: Callable[..., dict[str, Any]] | None = None,
    server_now: datetime | None = None,
    activation_origin: str = "internal",
    activation_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    readiness_snapshot: dict[str, Any] | None = None
    if not bool(settings.get("system.test_mode", False)):
        from quantlab.runtime.readiness import primary_start_readiness

        readiness_snapshot = primary_start_readiness(
            settings,
            trade_date=trade_date,
            now=server_now,
        )
        if not readiness_snapshot["sample_registration_allowed"]:
            return {
                "status": "skipped",
                "trade_date": trade_date.isoformat(),
                "reason": "primary_readiness_failed",
                "blockers": readiness_snapshot["blockers"],
                "readiness": readiness_snapshot,
                "formal_evidence_created": False,
            }
    experiment = ensure_primary_forward_experiment(
        settings,
        activation_origin=activation_origin,
        activation_reference=activation_reference,
    )
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    registrations_for_date = [
        item
        for item in repository.registration_runs(experiment["experiment_id"])
        if item["trade_date"] == trade_date.isoformat()
    ]
    existing_registration = (
        max(registrations_for_date, key=lambda item: int(item.get("attempt_number") or 1))
        if registrations_for_date
        else None
    )
    scheduler_recovery = bool(
        activation_origin == "scheduler"
        and int((activation_reference or {}).get("attempt_number") or 1) > 1
        and (activation_reference or {}).get("recovery_of_schedule_run_id")
    )
    if existing_registration and (
        existing_registration["status"] != "failed" or not scheduler_recovery
    ):
        return {
            **existing_registration,
            "samples": repository.registration_samples(
                existing_registration["registration_id"]
            ),
            "idempotent": True,
        }
    minimum_trust = DataTrustLevel(experiment["minimum_trust_level"])
    observed_now = (server_now or datetime.now(UTC)).astimezone(UTC)
    if trade_date > observed_now.date():
        return _failed_registration(
            settings,
            experiment,
            trade_date,
            reason="future_trade_date_cannot_be_registered",
        )
    if _forward_model_configuration(settings) != experiment["frozen_payload"].get(
        "model_configuration"
    ):
        return _failed_registration(
            settings,
            experiment,
            trade_date,
            reason="frozen_llm_model_configuration_changed_create_a_new_cohort",
        )
    try:
        calendar_day = TradingCalendarService.from_settings(settings).day(
            trade_date,
            formal=True,
            minimum_trust=minimum_trust,
        )
    except ValueError as exc:
        return _failed_registration(
            settings,
            experiment,
            trade_date,
            reason=f"trusted_calendar_unavailable:{exc}",
        )
    if not calendar_day["is_open"]:
        return _failed_registration(
            settings,
            experiment,
            trade_date,
            reason="formal_registration_requires_an_open_trading_day",
        )
    snapshot = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).latest_pool_snapshot(
        "a_share",
        trade_date,
        namespace=DataNamespace.PRODUCTION,
        minimum_trust=minimum_trust,
    )
    if snapshot is None:
        return _failed_registration(
            settings,
            experiment,
            trade_date,
            reason="trusted_point_in_time_pool_unavailable",
        )
    from quantlab.workflows.trusted_data import snapshot_time_invariant

    timing = snapshot_time_invariant(
        snapshot,
        registration_started_at=observed_now,
    )
    if not timing["invariant_holds"]:
        return _failed_registration(
            settings,
            experiment,
            trade_date,
            reason="formal_snapshot_time_invariant_failed",
        )
    registration = repository.begin_registration(
        experiment,
        trade_date,
        pool_snapshot_id=snapshot["snapshot_id"],
        pool_fingerprint=snapshot["fingerprint"],
        manifest_id=snapshot.get("manifest_id"),
        force_new_attempt=bool(existing_registration and scheduler_recovery),
        recovery_of_registration_id=(
            existing_registration["registration_id"] if existing_registration else None
        ),
        recovery_reason=(activation_reference or {}).get("recovery_reason"),
    )
    if registration["status"] != "running":
        return {
            **registration,
            "samples": repository.registration_samples(registration["registration_id"]),
            "idempotent": True,
        }
    eligible = [item for item in snapshot["members"] if item.get("eligible")]
    eligible.sort(
        key=lambda item: (
            not bool(item.get("representative")),
            item.get("representative_rank") or 10_000,
            item["symbol"],
        )
    )
    candidate_count = int(experiment["candidate_count"])
    selected = eligible[:candidate_count]
    resolved_quote_service = quote_service or QuoteService(
        _PointInTimePoolQuoteProvider(settings, snapshot),
        repository=repository,
    )
    expected = candidate_count * len(experiment["horizons"])
    registered = failed = skipped = 0
    for ordinal in range(1, candidate_count + 1):
        member = selected[ordinal - 1] if ordinal <= len(selected) else None
        for horizon in experiment["horizons"]:
            if member is None:
                repository.record_registration_sample(
                    registration,
                    symbol=f"__missing_candidate_{ordinal}",
                    horizon_days=int(horizon),
                    ordinal=ordinal,
                    status="skipped",
                    reason="candidate_pool_smaller_than_frozen_candidate_count",
                )
                skipped += 1
                continue
            symbol = member["symbol"]
            try:
                rows = freeze_forward_ablation_sample(
                    settings,
                    cohort_id=experiment["cohort_id"],
                    symbol=symbol,
                    horizon_days=int(horizon),
                    quote_service=resolved_quote_service,
                    committee_runner=committee_runner,
                    raw_llm_runner=raw_llm_runner,
                    registration_origin="automatic_primary",
                    formal=True,
                    minimum_trust_level=minimum_trust,
                    as_of=trade_date,
                    frozen_role_policy=experiment["frozen_payload"].get("role_policies"),
                    frozen_statistical_model_id=(
                        experiment["frozen_payload"]
                        .get("statistical_models", {})
                        .get(str(horizon), {})
                        .get("model_id")
                    ),
                    use_active_statistical_model=False,
                    variant_policy=experiment["frozen_payload"].get("variant_policy"),
                )
                sample_key = rows[0]["sample_key"]
                repository.record_registration_sample(
                    registration,
                    symbol=symbol,
                    horizon_days=int(horizon),
                    ordinal=ordinal,
                    status="registered",
                    sample_key=sample_key,
                    context_fingerprint=rows[0]["context_fingerprint"],
                    prediction_fingerprints={
                        row["variant"]: row["prediction_fingerprint"] for row in rows
                    },
                )
                registered += 1
            except Exception as exc:
                repository.record_registration_sample(
                    registration,
                    symbol=symbol,
                    horizon_days=int(horizon),
                    ordinal=ordinal,
                    status="failed",
                    reason=f"{type(exc).__name__}:{exc}",
                )
                failed += 1
    status = "completed" if registered else "failed"
    result = repository.finish_registration(
        registration["registration_id"],
        status=status,
        expected=expected,
        registered=registered,
        failed=failed,
        skipped=skipped,
        failure_reason=None if registered else "no_formal_samples_registered",
        payload={
            "candidate_symbols": [item["symbol"] for item in selected],
            "pool_snapshot_id": snapshot["snapshot_id"],
            "minimum_trust_level": minimum_trust.value,
            "readiness": readiness_snapshot,
            "readiness_persisted_at": datetime.now(UTC).isoformat()
            if readiness_snapshot is not None
            else None,
            "activation_reference": sanitize_for_export(activation_reference or {}),
        },
    )
    if registered == 0:
        _emit_registration_failure(settings, experiment, trade_date, result["failure_reason"])
    else:
        from quantlab.workflows.shadow_trading import create_shadow_orders_for_registration

        create_shadow_orders_for_registration(settings, registration["registration_id"])
    return {
        **result,
        "samples": repository.registration_samples(registration["registration_id"]),
    }


def save_manual_forward_exploration(
    settings: Settings,
    *,
    symbol: str,
    horizon_days: int,
    account_id: str | None = None,
    quote_service: QuoteService | None = None,
    committee_runner: Callable[..., dict[str, Any]] | None = None,
    raw_llm_runner: Callable[..., dict[str, Any]] | None = None,
    server_now: datetime | None = None,
) -> dict[str, Any]:
    service = quote_service or QuoteService.from_settings(settings)
    asset_type = _asset_type(settings, symbol)
    observed_now = (server_now or datetime.now(UTC)).astimezone(UTC)
    quote = service.get(
        symbol,
        asset_type=asset_type,
        as_of=observed_now.date(),
        require_authoritative=True,
    )
    existing = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).latest_context(symbol, as_of=quote.as_of.isoformat())
    payload = existing or build_analysis_context_pack(
        settings,
        symbol=symbol,
        as_of=quote.as_of,
        asset_type=asset_type.value,
        account_id=account_id,
        include_events=True,
        save=True,
    )
    pack = AnalysisContextPack.model_validate(payload)
    committee = (committee_runner or run_context_committee)(
        settings,
        pack=pack,
        deterministic_max_weight=float(settings.get("risk.max_single_position", 0.15)),
        idempotency_key=f"manual-exploration:{symbol}:{quote.as_of}:{horizon_days}",
    )
    raw = (raw_llm_runner or run_raw_forward_llm)(
        settings,
        pack=pack,
        horizon_days=horizon_days,
        idempotency_key=f"manual-raw:{symbol}:{quote.as_of}:{horizon_days}",
    )
    predictions = _execute_variants(
        settings,
        pack=pack,
        committee=committee,
        horizon_days=horizon_days,
        deterministic_max_weight=float(settings.get("risk.max_single_position", 0.15)),
        raw_llm=raw,
    )
    return Round5Repository(
        settings.resolve(settings.get("system.database_path"))
    ).save_manual_exploration(
        symbol=symbol,
        account_id=account_id,
        horizon_days=horizon_days,
        context_fingerprint=pack.fingerprint,
        predictions=[item.model_dump(mode="json") for item in predictions],
        payload={
            "quote_fingerprint": quote.quote_fingerprint,
            "formal_scorecard_eligible": False,
            "research_only": True,
        },
    )


def update_forward_milestones(settings: Settings) -> list[dict[str, Any]]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    experiment = repository.primary_experiment()
    if experiment is None:
        return []
    output: list[dict[str, Any]] = []
    for horizon in experiment["horizons"]:
        scorecard = forward_ablation_scorecard(
            settings,
            cohort_id=experiment["cohort_id"],
            horizon_days=int(horizon),
            registration_origin="automatic_primary",
        )
        matured = int(scorecard["variants"]["full_system"]["matured_samples"])
        for threshold in (10, 20, 30):
            if matured >= threshold:
                output.append(
                    repository.save_milestone(
                        experiment_id=experiment["experiment_id"],
                        cohort_id=experiment["cohort_id"],
                        horizon_days=int(horizon),
                        threshold=threshold,
                        matured_samples=matured,
                        scorecard=scorecard,
                    )
                )
    return output


def formal_forward_scorecard(settings: Settings) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    experiments = repository.experiments()
    primary = repository.primary_experiment()
    if primary is None:
        return {"primary": None, "experiments": experiments, "formal_samples": 0}
    scorecards = {
        str(horizon): forward_ablation_scorecard(
            settings,
            cohort_id=primary["cohort_id"],
            horizon_days=int(horizon),
            registration_origin="automatic_primary",
        )
        for horizon in primary["horizons"]
    }
    runs = repository.registration_runs(primary["experiment_id"])
    formal_samples = sum(item["registered_samples"] for item in runs)
    return {
        "primary": primary,
        "experiments": experiments,
        "registration_runs": runs,
        "scorecards": scorecards,
        "milestones": repository.milestones(primary["experiment_id"]),
        "formal_samples": formal_samples,
        "claim_boundary": (
            "Only automatically registered primary-cohort samples are included. Manual "
            "explorations, research imports and non-primary cohorts are excluded."
        ),
    }


def _failed_registration(
    settings: Settings,
    experiment: dict[str, Any],
    trade_date: date,
    *,
    reason: str,
) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    registration = repository.begin_registration(
        experiment,
        trade_date,
        pool_snapshot_id=None,
        pool_fingerprint=None,
        manifest_id=None,
    )
    if registration["status"] == "running":
        expected = int(experiment["candidate_count"]) * len(experiment["horizons"])
        for ordinal in range(1, int(experiment["candidate_count"]) + 1):
            for horizon in experiment["horizons"]:
                repository.record_registration_sample(
                    registration,
                    symbol=f"__unavailable_candidate_{ordinal}",
                    horizon_days=int(horizon),
                    ordinal=ordinal,
                    status="failed",
                    reason=reason,
                )
        registration = repository.finish_registration(
            registration["registration_id"],
            status="failed",
            expected=expected,
            registered=0,
            failed=expected,
            skipped=0,
            failure_reason=reason,
        )
    _emit_registration_failure(settings, experiment, trade_date, reason)
    return registration


def _emit_registration_failure(
    settings: Settings,
    experiment: dict[str, Any],
    trade_date: date,
    reason: str,
) -> None:
    path = settings.resolve(settings.get("system.database_path"))
    consecutive = Round5Repository(path).consecutive_registration_failures(
        experiment["experiment_id"]
    )
    JobRepository(path).record_runtime_failure(
        source_type="forward_sample_registration",
        source_id=f"{experiment['experiment_id']}:{trade_date.isoformat()}",
        severity="warning",
        error_detail=reason,
        payload={
            "experiment_id": experiment["experiment_id"],
            "trade_date": trade_date.isoformat(),
            "consecutive_failed_trading_days": consecutive,
        },
    )
    NotificationRepository(path).emit(
        event_type="forward_registration_failed",
        aggregate_type="forward_experiment",
        aggregate_id=experiment["experiment_id"],
        payload={
            "severity": "warning",
            "content": (
                f"No formal forward samples were registered for {trade_date}: {reason}; "
                f"consecutive failures={consecutive}"
            ),
            "triggered_at": datetime.now(UTC).isoformat(),
            "source": "forward_sample_registration",
        },
        dedup_key=f"forward-registration-failed:{experiment['experiment_id']}:{trade_date}",
    )


def protocol_fingerprint(settings: Settings) -> str:
    experiment = ensure_primary_forward_experiment(settings)
    return hashlib.sha256(
        json.dumps(experiment["frozen_payload"], sort_keys=True).encode("utf-8")
    ).hexdigest()


def _forward_model_configuration(settings: Settings) -> dict[str, Any]:
    llm = settings.section("llm")
    keys = (
        "provider",
        "model",
        "base_url",
        "openai_model",
        "openai_base_url",
        "openai_reasoning_effort",
        "openai_role_models",
        "openai_role_reasoning_effort",
        "openai_enabled",
        "deepseek_model",
        "deepseek_base_url",
        "deepseek_enabled",
        "local_model",
        "local_base_url",
        "role_models",
        "role_preferences",
        "temperature",
        "timeout_seconds",
        "connect_timeout_seconds",
        "max_retries",
        "max_concurrency_per_endpoint",
        "failure_threshold",
        "failure_cooldown_seconds",
        "allow_mock_fallback",
        "maximum_committee_roles",
        "maximum_committee_rounds",
    )
    return {key: llm.get(key) for key in keys}


def _freeze_statistical_models(settings: Settings) -> dict[str, dict[str, Any]]:
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    output: dict[str, dict[str, Any]] = {}
    for horizon in (5, 20):
        record = repository.active_model(horizon, "stock")
        if record is None:
            output[str(horizon)] = {
                "model_id": None,
                "status": "cash_until_new_cohort",
            }
            continue
        metrics = record.get("metrics", {})
        governed = metrics.get("promotion_decision") in {"activated", "promoted"}
        output[str(horizon)] = {
            "model_id": record["model_id"] if governed else None,
            "registered_model_id": record["model_id"],
            "version": record["version"],
            "trained_until": record["trained_until"],
            "status": "governed" if governed else "cash_until_new_cohort",
        }
    return output


def _freeze_role_policies(settings: Settings) -> dict[str, dict[str, Any]]:
    repository = EvidenceRepository(settings.resolve(settings.get("system.database_path")))
    roles = list(ROLE_DOMAINS)
    minimum = int(settings.get("llm.role_minimum_matured_samples", 30))
    regimes: tuple[str | None, ...] = (
        None,
        "bull",
        "bear",
        "range",
        "risk_on",
        "risk_off",
        "volatile",
        "balanced",
    )
    return {
        "default" if regime is None else regime: repository.active_role_policy(
            roles,
            market_regime=regime,
            default_minimum_samples=minimum,
        )
        for regime in regimes
    }


def _variant_policy(settings: Settings) -> dict[str, Any]:
    return {
        "baseline_probabilities": {"up": 0.45, "flat": 0.30, "down": 0.25},
        "baseline_weight": float(
            settings.get("strategies.forward_primary.simple_baseline_weight", 0.05)
        ),
        "entry_score_threshold": 0.20,
        "avoid_score_threshold": -0.20,
        "llm_stat_statistical_weight": 0.50,
        "full_committee_weight": 0.35,
        "full_statistical_weight": 0.25,
        "full_baseline_weight": 0.05,
        "llm_gate_pass_actions": ["buy", "add", "hold"],
        "full_block_actions": ["avoid", "reduce", "review_required"],
    }


__all__ = [
    "PRIMARY_PROTOCOL_VERSION",
    "ensure_primary_forward_experiment",
    "formal_forward_scorecard",
    "protocol_fingerprint",
    "register_primary_forward_samples",
    "save_manual_forward_exploration",
    "update_forward_milestones",
]
