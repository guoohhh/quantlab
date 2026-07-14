from __future__ import annotations

from datetime import date

import pandas as pd

from quantlab.config import Settings
from quantlab.data import AkShareProvider, CachedProvider, FallbackProvider, WestockProvider
from quantlab.persistence import DecisionRepository
from .events import collect_all_events


def settle_forecasts(settings: Settings, as_of: date | None = None) -> dict:
    repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    pending = repository.pending_forecasts()
    if not pending:
        return {"settled": [], "pending": [], "degraded_sources": []}

    cutoff = as_of or date.today()
    eligible = [item for item in pending if date.fromisoformat(item["as_of"]) < cutoff]
    if not eligible:
        return {"settled": [], "pending": pending, "degraded_sources": []}

    start = min(date.fromisoformat(item["as_of"]) for item in eligible)
    symbols = sorted({item["symbol"] for item in eligible})
    fallback = FallbackProvider([WestockProvider(settings.root.parent), AkShareProvider()])
    provider = CachedProvider(
        fallback,
        settings.resolve(settings.get("system.data_dir")) / "cache",
    )
    bars = provider.bars(symbols, start, cutoff)
    frame = pd.DataFrame([bar.model_dump() for bar in bars])
    if frame.empty:
        return {
            "settled": [],
            "pending": eligible,
            "degraded_sources": fallback.last_degraded_from + ["no settlement bars returned"],
        }
    frame["date"] = pd.to_datetime(frame["date"])
    settled = []
    still_pending = []
    event_degraded = []
    flat_threshold = float(settings.get("calibration.flat_threshold_pct", 1.0))
    for item in eligible:
        symbol_bars = frame[frame.symbol == item["symbol"]].sort_values("date")
        origin_rows = symbol_bars[symbol_bars.date >= pd.Timestamp(item["as_of"])]
        horizon = int(item["horizon_days"])
        if len(origin_rows) <= horizon:
            still_pending.append(item)
            continue
        start_price = float(origin_rows.iloc[0].close)
        end_row = origin_rows.iloc[horizon]
        try:
            event_output = collect_all_events(
                settings,
                item["symbol"],
                date.fromisoformat(item["as_of"]),
                end_row.date.date(),
            )
            event_degraded.extend(event_output["degraded_sources"])
        except Exception as exc:
            event_degraded.append(f"event collection failed for {item['symbol']}: {exc}")
        realized = (float(end_row.close) / start_price - 1.0) * 100
        outcome = repository.record_forecast_outcome(
            item["run_id"],
            horizon,
            realized,
            end_row.date.date().isoformat(),
            flat_threshold,
        )
        settled.append(outcome.model_dump(mode="json"))
    return {
        "settled": settled,
        "pending": still_pending,
        "degraded_sources": fallback.last_degraded_from + event_degraded,
    }
