from __future__ import annotations

from datetime import date

import math


def attribute_sample(sample: dict, events: list[dict]) -> dict:
    expected = float(sample.get("expected_return_pct") or 0.0)
    realized = float(sample.get("realized_return_pct") or 0.0)
    surprise = realized - expected
    features = sample.get("features", {})
    components = sample.get("context", {}).get("forecast_components", {})
    probabilities = components.get("final") or [
        float(features.get("llm_up_probability", 0)),
        float(features.get("llm_flat_probability", 0)),
        float(features.get("llm_down_probability", 0)),
    ]
    predicted = ("up", "flat", "down")[max(range(3), key=probabilities.__getitem__)]
    actual_index = ("up", "flat", "down").index(sample["outcome"])
    assigned_to_actual = float(probabilities[actual_index])
    confidence = float(max(probabilities))
    brier = sum(
        (float(probability) - float(index == actual_index)) ** 2
        for index, probability in enumerate(probabilities)
    ) / 3
    ranked = []
    start = date.fromisoformat(sample["as_of"])
    end = date.fromisoformat(sample["evaluated_at"])
    span = max(1, (end - start).days)
    for event in events:
        event_date = date.fromisoformat(event["event_date"])
        proximity = 1 - min(1.0, (event_date - start).days / span) * 0.5
        sentiment = float(event.get("sentiment", 0))
        alignment = 1.0 if surprise == 0 or sentiment * surprise >= 0 else 0.5
        score = float(event.get("impact_score", 0.5)) * proximity * alignment
        ranked.append(
            {
                "event_id": event["id"],
                "event_date": event["event_date"],
                "event_type": event["event_type"],
                "title": event["title"],
                "source": event["source"],
                "association_score": score,
                "sentiment": sentiment,
            }
        )
    ranked.sort(key=lambda item: item["association_score"], reverse=True)
    context = sample.get("context", {})
    root_causes = _root_cause_candidates(
        sample,
        ranked,
        probabilities,
        predicted,
        assigned_to_actual,
        confidence,
        context,
    )
    return {
        "sample_key": sample["sample_key"],
        "symbol": sample["symbol"],
        "horizon_days": sample["horizon_days"],
        "expected_return_pct": expected,
        "realized_return_pct": realized,
        "surprise_pct": surprise,
        "predicted_outcome": predicted,
        "actual_outcome": sample["outcome"],
        "direction_correct": predicted == sample["outcome"],
        "probability_assigned_to_actual": assigned_to_actual,
        "forecast_confidence": confidence,
        "brier_score": brier,
        "error_severity": _error_severity(predicted == sample["outcome"], confidence, surprise),
        "candidate_event_explanations": ranked[:5],
        "root_cause_candidates": root_causes,
        "unresolved": not bool(ranked),
        "causal_claim": False,
        "method_note": (
            "Events are ranked by timing, declared impact and sentiment alignment. "
            "This is evidence association, not proof of causality."
        ),
    }


def _root_cause_candidates(
    sample: dict,
    ranked_events: list[dict],
    probabilities: list[float],
    predicted: str,
    assigned_to_actual: float,
    confidence: float,
    context: dict,
) -> list[dict]:
    candidates: list[dict] = []
    if predicted != sample["outcome"] and confidence >= 0.60:
        candidates.append(
            {
                "code": "model_overconfidence",
                "score": min(1.0, confidence + (0.5 - assigned_to_actual)),
                "evidence": f"confidence={confidence:.3f}, actual_probability={assigned_to_actual:.3f}",
            }
        )
    components = context.get("forecast_components", {})
    valid_components = []
    for name in ("raw_llm", "statistical", "final"):
        values = components.get(name)
        if isinstance(values, (list, tuple)) and len(values) == 3:
            try:
                parsed = [float(value) for value in values]
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(value) for value in parsed):
                valid_components.append((name, parsed.index(max(parsed))))
    if len({index for _, index in valid_components}) > 1:
        candidates.append(
            {
                "code": "ensemble_disagreement",
                "score": 0.65,
                "evidence": ", ".join(f"{name}={index}" for name, index in valid_components),
            }
        )
    data_quality = float(context.get("data_quality", 1.0) or 0.0)
    degraded_sources = context.get("degraded_sources", [])
    if data_quality < 0.80 or degraded_sources:
        candidates.append(
            {
                "code": "data_quality_or_fallback",
                "score": max(0.5, 1.0 - data_quality),
                "evidence": f"data_quality={data_quality:.3f}, degraded={len(degraded_sources)}",
            }
        )
    event_codes = {
        "earnings": "earnings_surprise",
        "financial": "earnings_surprise",
        "macro": "macro_event",
        "regulatory": "regulatory_event",
        "corporate_action": "corporate_action",
        "news": "news_event",
    }
    for event in ranked_events[:3]:
        candidates.append(
            {
                "code": event_codes.get(event["event_type"], "market_event"),
                "score": float(event["association_score"]),
                "evidence": f"event_id={event['event_id']}:{event['title']}",
            }
        )
    if not candidates:
        candidates.append(
            {
                "code": "unexplained_residual",
                "score": 0.25,
                "evidence": "no recorded event or diagnostic threshold explained the error",
            }
        )
    candidates.sort(key=lambda item: float(item["score"]), reverse=True)
    return candidates[:5]


def _error_severity(direction_correct: bool, confidence: float, surprise: float) -> str:
    if direction_correct and abs(surprise) < 2:
        return "low"
    if (not direction_correct and confidence >= 0.70) or abs(surprise) >= 8:
        return "high"
    return "medium"
