from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.learning import LearningRepository, monitor_all_models, train_registered_model
from quantlab.learning.historical import generate_historical_samples
from quantlab.security import safe_error_detail


def bootstrap_learning_history(
    settings: Settings,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    step: int = 5,
    asset_scope: str = "etf",
) -> dict:
    if symbols is None and asset_scope != "etf":
        raise ValueError(
            "non-ETF learning bootstrap requires an explicit symbol list; "
            "current constituents must not be mistaken for a point-in-time universe"
        )
    universe = symbols or list(settings.get("strategies.etf_rotation.universe"))
    training_eligible = asset_scope == "etf"
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(universe, start - timedelta(days=500), end)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        raise ValueError("no historical bars returned for learning bootstrap")
    frame["date"] = pd.to_datetime(frame["date"])
    frame["close"] = frame["adjusted_close"].fillna(frame["close"])
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    output = generate_historical_samples(
        frame,
        repository,
        horizons=(5, 20),
        step=step,
        flat_threshold_pct=float(settings.get("calibration.flat_threshold_pct", 1.0)),
        sample_start=start,
        asset_scope=asset_scope,
        training_eligible=training_eligible,
        universe_provenance=(
            "configured_etf_rotation_universe"
            if asset_scope == "etf"
            else "user_supplied_current_symbols_with_selection_bias"
        ),
    )
    return {
        **output,
        "source": provider.name,
        "degraded_sources": fallback.last_degraded_from,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "asset_scope": asset_scope,
        "training_eligible": training_eligible,
        "governance_warning": (
            None
            if training_eligible
            else "samples are research-only until a point-in-time universe is available"
        ),
    }


def train_learning_models(
    settings: Settings,
    horizon_days: int | None = None,
    asset_scope: str = "etf",
    force: bool = False,
) -> list[dict]:
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    horizons = [horizon_days] if horizon_days else [5, 20]
    return [
        train_registered_model(
            repository,
            horizon,
            asset_scope,
            minimum_samples=int(settings.get("learning.minimum_samples", 100)),
            minimum_validation_samples=int(settings.get("learning.minimum_validation_samples", 20)),
            validation_fraction=float(settings.get("learning.validation_fraction", 0.20)),
            validation_folds=int(settings.get("learning.validation_folds", 3)),
            minimum_fold_pass_rate=float(settings.get("learning.minimum_fold_pass_rate", 0.67)),
            force=force,
        )
        for horizon in horizons
    ]


def learning_status(settings: Settings, include_model_history: bool = False) -> dict:
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    model_history = repository.models()
    latest: dict[tuple[str, int], dict] = {}
    for model in model_history:
        key = (model["asset_scope"], model["horizon_days"])
        if key not in latest:
            latest[key] = model
    visible_models = (
        model_history
        if include_model_history
        else [
            model
            for model in latest.values()
            if model["asset_scope"] in {"etf", "stock", "convertible_bond"}
        ]
    )
    return {
        "sample_counts": repository.sample_counts(),
        "dataset_manifests": [
            repository.dataset_manifest(horizon, scope)
            for scope in ("etf", "stock", "convertible_bond")
            for horizon in (5, 20)
        ],
        "models": [_model_summary(model, include_model_history) for model in visible_models],
        "active_models": [
            _model_summary(model, False) for model in model_history if model["active"]
        ],
        "model_history_count": len(model_history),
        "recent_attributions": repository.attributions(20),
        "attribution_summary": repository.attribution_summary(),
        "recent_events": repository.recent_events(limit=20),
        "monitoring_history": repository.monitoring_history(20),
        "champion_challenger_history": repository.challenge_history(20),
    }


def _model_summary(model: dict, full: bool) -> dict:
    if full:
        return model
    metrics = model.get("metrics", {})
    summary = {
        key: model.get(key)
        for key in (
            "model_id",
            "asset_scope",
            "horizon_days",
            "version",
            "trained_until",
            "training_samples",
            "validation_samples",
            "active",
            "deactivation_reason",
        )
    } | {
        "metrics": {
            key: metrics.get(key)
            for key in (
                "brier_score",
                "baseline_brier",
                "log_loss",
                "baseline_log_loss",
                "accuracy",
                "validation_start",
                "fold_pass_rate",
                "promotion_decision",
                "champion_challenger",
            )
        }
    }
    summary["governance_status"] = (
        "governed"
        if metrics.get("promotion_decision") in {"activated", "promoted"}
        else "legacy_awaiting_challenge"
    )
    return summary


def run_learning_cycle(settings: Settings, as_of: date | None = None) -> dict:
    from quantlab.workflows.forecast import settle_forecasts
    from quantlab.workflows.tournament import settle_candidate_tournaments

    settlement = settle_forecasts(settings, as_of)
    try:
        tournament_settlement = settle_candidate_tournaments(settings, as_of)
    except Exception as exc:
        tournament_settlement = {
            "settled": [],
            "pending": [],
            "not_comparable": [],
            "degraded_sources": [
                f"candidate tournament settlement failed: {safe_error_detail(exc)}"
            ],
        }
    training = {}
    repository = LearningRepository(settings.resolve(settings.get("system.database_path")))
    monitoring = monitor_all_models(
        repository,
        minimum_online_samples=int(settings.get("learning.minimum_online_samples", 30)),
        recent_window=int(settings.get("learning.drift_recent_window", 50)),
        degradation_factor=float(settings.get("learning.drift_degradation_factor", 1.25)),
    )
    if settlement["settled"]:
        for scope in ("etf", "stock", "convertible_bond"):
            training[scope] = train_learning_models(settings, asset_scope=scope)
    return {
        "settlement": settlement,
        "tournament_settlement": tournament_settlement,
        "monitoring": monitoring,
        "training": training,
        "status": learning_status(settings),
    }
