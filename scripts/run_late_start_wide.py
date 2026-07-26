from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime

from quantlab.config import Settings
from quantlab.workflows.wide_forward import (
    LATE_START_REGISTRATION_ORIGIN,
    preregister_late_start_wide_experiment,
    register_wide_forward_batch,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trade_date", type=date.fromisoformat)
    args = parser.parse_args()

    settings = Settings.load()
    activation = preregister_late_start_wide_experiment(
        settings,
        trade_date=args.trade_date,
        frozen_at=datetime.now(UTC),
    )
    experiment = activation["experiment"]
    result = register_wide_forward_batch(
        settings,
        trade_date=args.trade_date,
        schedule_run_id=(
            f"operator-late-start:{args.trade_date.isoformat()}:"
            f"{experiment['experiment_id']}"
        ),
        experiment_id=experiment["experiment_id"],
        registration_origin=LATE_START_REGISTRATION_ORIGIN,
        registration_started_at=datetime.now(UTC),
    )
    print(
        json.dumps(
            {
                "batch_id": result["batch_id"],
                "experiment_id": result["experiment_id"],
                "status": result["status"],
                "member_count": result["member_count"],
                "prediction_count": result["prediction_count"],
                "llm_calls": result["llm_calls"],
                "llm_input_tokens": result["llm_input_tokens"],
                "llm_output_tokens": result["llm_output_tokens"],
                "llm_cost_usd": result["llm_cost_usd"],
                "role_completeness": result["role_completeness"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
