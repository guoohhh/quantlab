from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import uuid
from datetime import UTC, date, datetime
from typing import Any, Callable

from quantlab.config import Settings
from quantlab.domain import AnalysisContextPack, AssetType
from quantlab.market import ExecutionQuoteService, ResearchBarService, TradingCalendarService
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round7 import Round7Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.security import sanitize_for_export
from quantlab.workflows.context import build_analysis_context_pack
from quantlab.workflows.llm_committee import run_context_committee


POSITION_CSV_COLUMNS = (
    "symbol",
    "name",
    "asset_type",
    "quantity",
    "average_cost",
    "industry",
)
TRADE_CSV_COLUMNS = (
    "symbol",
    "asset_type",
    "side",
    "quantity",
    "price",
    "transaction_cost",
    "trade_date",
)


def investor_csv_template(import_type: str) -> str:
    if import_type not in {"positions", "trades"}:
        raise ValueError("investor import type must be positions or trades")
    columns = POSITION_CSV_COLUMNS if import_type == "positions" else TRADE_CSV_COLUMNS
    return ",".join(columns) + "\n"


def create_investor_portfolio(
    settings: Settings,
    *,
    name: str,
    cash: float,
) -> dict[str, Any]:
    return _repository(settings).create_investor_portfolio(name=name, cash=cash)


def preview_investor_csv(
    settings: Settings,
    *,
    portfolio_id: str,
    import_type: str,
    csv_content: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if import_type not in {"positions", "trades"}:
        raise ValueError("investor import type must be positions or trades")
    reader = csv.DictReader(io.StringIO(csv_content.lstrip("\ufeff")))
    required = POSITION_CSV_COLUMNS if import_type == "positions" else TRADE_CSV_COLUMNS
    if reader.fieldnames is None or not set(required) <= set(reader.fieldnames):
        raise ValueError("CSV columns do not match the published investor import template")
    repository = _repository(settings)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    with repository.connect() as db:
        prior = {
            row[0]
            for row in db.execute(
                """SELECT r.row_fingerprint FROM investor_import_rows r
                   JOIN investor_imports i ON i.import_id=r.import_id
                   WHERE i.portfolio_id=? AND i.status='confirmed'""",
                (portfolio_id,),
            ).fetchall()
        }
    for source_row in reader:
        try:
            payload = _normalize_import_row(import_type, source_row)
            fingerprint = _row_fingerprint(import_type, payload)
            status = "duplicate" if fingerprint in seen or fingerprint in prior else "valid"
            error = None
            seen.add(fingerprint)
        except (ValueError, KeyError) as exc:
            payload = {key: source_row.get(key) for key in required}
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            status = "error"
            error = str(exc)
        normalized.append(
            {
                "row_fingerprint": fingerprint,
                "status": status,
                "payload": payload,
                "error": error,
            }
        )
    file_fingerprint = hashlib.sha256(csv_content.encode("utf-8")).hexdigest()
    preview = repository.save_import_preview(
        portfolio_id=portfolio_id,
        import_type=import_type,
        idempotency_key=idempotency_key,
        file_fingerprint=file_fingerprint,
        rows=normalized,
    )
    return {**preview, "rows": repository.import_rows(preview["import_id"])}


def confirm_investor_import(
    settings: Settings,
    *,
    import_id: str,
    confirm: bool,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("investor import requires explicit confirmation")
    repository = _repository(settings)
    with repository.transaction() as db:
        batch = db.execute(
            "SELECT * FROM investor_imports WHERE import_id=?", (import_id,)
        ).fetchone()
        if batch is None:
            raise ValueError("investor import not found")
        if batch["status"] == "confirmed":
            return {**dict(batch), "idempotent": True}
        if int(batch["error_count"]) > 0:
            raise ValueError("investor import contains invalid rows")
        rows = db.execute(
            """SELECT * FROM investor_import_rows
               WHERE import_id=? AND status='valid' ORDER BY row_number""",
            (import_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if batch["import_type"] == "positions":
                db.execute(
                    """INSERT INTO investor_positions(
                        portfolio_id,symbol,name,asset_type,industry,quantity,average_cost,
                        latest_price,latest_price_at,price_status,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'stale',?)
                    ON CONFLICT(portfolio_id,symbol) DO UPDATE SET
                      name=excluded.name,asset_type=excluded.asset_type,
                      industry=excluded.industry,quantity=excluded.quantity,
                      average_cost=excluded.average_cost,updated_at=excluded.updated_at""",
                    (
                        batch["portfolio_id"],
                        payload["symbol"],
                        payload["name"],
                        payload["asset_type"],
                        payload.get("industry"),
                        payload["quantity"],
                        payload["average_cost"],
                        payload["average_cost"],
                        None,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            else:
                _record_trade_in_tx(
                    db,
                    portfolio_id=batch["portfolio_id"],
                    payload=payload,
                    idempotency_key=f"import:{import_id}:{row['row_fingerprint']}",
                    import_id=import_id,
                    source="confirmed_csv_import",
                )
        if batch["import_type"] == "positions":
            has_activity = db.execute(
                """SELECT
                     EXISTS(SELECT 1 FROM investor_nav WHERE portfolio_id=?),
                     EXISTS(SELECT 1 FROM investor_trades WHERE portfolio_id=?)""",
                (batch["portfolio_id"], batch["portfolio_id"]),
            ).fetchone()
            if not any(bool(value) for value in has_activity):
                position_cost = float(
                    db.execute(
                        """SELECT COALESCE(SUM(quantity*average_cost),0)
                           FROM investor_positions WHERE portfolio_id=?""",
                        (batch["portfolio_id"],),
                    ).fetchone()[0]
                    or 0.0
                )
                db.execute(
                    """UPDATE investor_portfolios
                       SET initial_equity=cash+?,updated_at=? WHERE portfolio_id=?""",
                    (position_cost, datetime.now(UTC).isoformat(), batch["portfolio_id"]),
                )
        db.execute(
            "UPDATE investor_imports SET status='confirmed',confirmed_at=? WHERE import_id=?",
            (datetime.now(UTC).isoformat(), import_id),
        )
        result = db.execute(
            "SELECT * FROM investor_imports WHERE import_id=?", (import_id,)
        ).fetchone()
    return {**dict(result), "rows_applied": len(rows), "idempotent": False}


def record_investor_trade(
    settings: Settings,
    *,
    portfolio_id: str,
    symbol: str,
    asset_type: str,
    side: str,
    quantity: int,
    price: float,
    transaction_cost: float,
    trade_date: date,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = _normalize_import_row(
        "trades",
        {
            "symbol": symbol,
            "asset_type": asset_type,
            "side": side,
            "quantity": quantity,
            "price": price,
            "transaction_cost": transaction_cost,
            "trade_date": trade_date.isoformat(),
        },
    )
    repository = _repository(settings)
    with repository.transaction() as db:
        trade = _record_trade_in_tx(
            db,
            portfolio_id=portfolio_id,
            payload=payload,
            idempotency_key=idempotency_key,
            import_id=None,
            source="manual_external_trade_record",
        )
    return trade


def mark_investor_portfolios(
    settings: Settings,
    *,
    as_of: date,
    portfolio_id: str | None = None,
    quote_service: ExecutionQuoteService | None = None,
) -> dict[str, Any]:
    repository = _repository(settings)
    service = quote_service or ExecutionQuoteService.from_settings(settings)
    portfolios = repository.investor_portfolios()
    if portfolio_id:
        portfolios = [item for item in portfolios if item["portfolio_id"] == portfolio_id]
    output: list[dict[str, Any]] = []
    with repository.transaction() as db:
        for portfolio in portfolios:
            positions = db.execute(
                "SELECT * FROM investor_positions WHERE portfolio_id=? AND quantity>0",
                (portfolio["portfolio_id"],),
            ).fetchall()
            stale: list[str] = []
            market_value = unrealized = 0.0
            industry_values: dict[str, float] = {}
            symbol_values: list[float] = []
            for position in positions:
                price = float(position["latest_price"] or position["average_cost"])
                status = "stale"
                industry = str(position["industry"] or "unclassified")
                price_at = position["latest_price_at"]
                try:
                    quote = service.get(
                        position["symbol"],
                        asset_type=position["asset_type"],
                        as_of=as_of,
                        require_authoritative=True,
                    )
                    if quote.as_of == as_of:
                        price = quote.raw_price
                        status = "available"
                        price_at = (
                            quote.available_at.astimezone(UTC).isoformat()
                            if quote.available_at
                            else as_of.isoformat()
                        )
                        if quote.industry:
                            industry = quote.industry
                            db.execute(
                                "UPDATE investor_positions SET industry=? WHERE portfolio_id=? AND symbol=?",
                                (quote.industry, portfolio["portfolio_id"], position["symbol"]),
                            )
                except Exception:
                    pass
                if status == "stale":
                    stale.append(position["symbol"])
                db.execute(
                    """UPDATE investor_positions SET latest_price=?,latest_price_at=?,
                       price_status=?,updated_at=? WHERE portfolio_id=? AND symbol=?""",
                    (
                        price,
                        price_at,
                        status,
                        datetime.now(UTC).isoformat(),
                        portfolio["portfolio_id"],
                        position["symbol"],
                    ),
                )
                value = int(position["quantity"]) * price
                market_value += value
                unrealized += int(position["quantity"]) * (
                    price - float(position["average_cost"])
                )
                symbol_values.append(value)
                industry_values[industry] = industry_values.get(industry, 0.0) + value
            equity = float(portfolio["cash"]) + market_value
            previous = db.execute(
                """SELECT * FROM investor_nav
                   WHERE portfolio_id=? AND nav_date<? ORDER BY nav_date DESC LIMIT 1""",
                (portfolio["portfolio_id"], as_of.isoformat()),
            ).fetchone()
            previous_equity = float(previous["equity"]) if previous else float(
                portfolio["initial_equity"]
            )
            today_pnl = equity - previous_equity
            realized = float(
                db.execute(
                    "SELECT COALESCE(SUM(realized_pnl),0) FROM investor_positions WHERE portfolio_id=?",
                    (portfolio["portfolio_id"],),
                ).fetchone()[0]
                or 0.0
            )
            costs = float(
                db.execute(
                    """SELECT COALESCE(SUM(transaction_cost),0) FROM investor_trades
                       WHERE portfolio_id=? AND source NOT LIKE 'superseded_%'""",
                    (portfolio["portfolio_id"],),
                ).fetchone()[0]
                or 0.0
            )
            peak = max(
                [float(portfolio["initial_equity"]), equity]
                + [
                    float(row[0])
                    for row in db.execute(
                        "SELECT equity FROM investor_nav WHERE portfolio_id=? AND nav_date<?",
                        (portfolio["portfolio_id"], as_of.isoformat()),
                    ).fetchall()
                ]
            )
            drawdown = equity / peak - 1.0 if peak else 0.0
            maximum_drawdown = min(
                float(previous["maximum_drawdown"]) if previous else 0.0,
                drawdown,
            )
            concentration = max(symbol_values, default=0.0) / max(equity, 0.01)
            exposures = {
                key: value / max(equity, 0.01) for key, value in industry_values.items()
            }
            db.execute(
                """INSERT INTO investor_nav(
                    nav_id,portfolio_id,nav_date,cash,market_value,equity,today_pnl,
                    cumulative_pnl,realized_pnl,unrealized_pnl,cumulative_cost,
                    concentration,industry_exposure,drawdown,maximum_drawdown,
                    stale_symbols,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(portfolio_id,nav_date) DO UPDATE SET
                  cash=excluded.cash,market_value=excluded.market_value,equity=excluded.equity,
                  today_pnl=excluded.today_pnl,cumulative_pnl=excluded.cumulative_pnl,
                  realized_pnl=excluded.realized_pnl,unrealized_pnl=excluded.unrealized_pnl,
                  cumulative_cost=excluded.cumulative_cost,concentration=excluded.concentration,
                  industry_exposure=excluded.industry_exposure,drawdown=excluded.drawdown,
                  maximum_drawdown=excluded.maximum_drawdown,stale_symbols=excluded.stale_symbols""",
                (
                    str(uuid.uuid4()),
                    portfolio["portfolio_id"],
                    as_of.isoformat(),
                    portfolio["cash"],
                    market_value,
                    equity,
                    today_pnl,
                    equity - float(portfolio["initial_equity"]),
                    realized,
                    unrealized,
                    costs,
                    concentration,
                    json.dumps(exposures, ensure_ascii=False),
                    drawdown,
                    maximum_drawdown,
                    json.dumps(stale),
                    datetime.now(UTC).isoformat(),
                ),
            )
            output.append(
                {
                    "portfolio_id": portfolio["portfolio_id"],
                    "equity": equity,
                    "today_pnl": today_pnl,
                    "stale_symbols": stale,
                }
            )
    return {"as_of": as_of.isoformat(), "portfolios": output}


def build_investor_recommendation(
    settings: Settings,
    *,
    portfolio_id: str,
    symbol: str,
    side_hint: str | None = None,
    quote_service: ExecutionQuoteService | None = None,
    committee_runner: Callable[..., dict[str, Any]] | None = None,
    server_now: datetime | None = None,
) -> dict[str, Any]:
    repository = _repository(settings)
    overview = repository.investor_overview(portfolio_id)
    portfolio = overview["portfolio"]
    position = next((item for item in overview["positions"] if item["symbol"] == symbol), None)
    asset_type = AssetType(position["asset_type"] if position else "stock")
    observed_now = (server_now or datetime.now(UTC)).astimezone(UTC)
    quote = (quote_service or ExecutionQuoteService.from_settings(settings)).get(
        symbol,
        asset_type=asset_type,
        as_of=observed_now.date(),
        require_authoritative=True,
    )
    context_payload = build_analysis_context_pack(
        settings,
        symbol=symbol,
        as_of=quote.as_of,
        asset_type=asset_type.value,
        account_id=None,
        include_events=True,
        save=True,
    )
    pack = AnalysisContextPack.model_validate(context_payload)
    committee = (committee_runner or run_context_committee)(
        settings,
        pack=pack,
        deterministic_max_weight=float(settings.get("risk.max_single_position", 0.15)),
        idempotency_key=f"investor:{portfolio_id}:{symbol}:{quote.as_of}",
    )
    latest_nav = overview["nav"][-1] if overview["nav"] else None
    equity = float(latest_nav["equity"] if latest_nav else portfolio["initial_equity"])
    current_quantity = int(position["quantity"]) if position else 0
    current_value = current_quantity * quote.raw_price
    maximum_weight = min(
        float(settings.get("risk.max_single_position", 0.15)),
        float(committee.get("suggested_weight_max") or 0.0),
    )
    lot = int(settings.get(f"costs.{asset_type.value}.trade_lot", 100))
    maximum_quantity = int(math.floor(equity * maximum_weight / quote.raw_price / lot) * lot)
    action = str(committee.get("action") or side_hint or "review_required")
    trade_actions = {"buy", "add", "reduce", "avoid"}
    if action in {"reduce", "avoid"}:
        quantity_max = current_quantity
        quantity_min = min(lot, current_quantity) if current_quantity else 0
        post_value = max(0.0, current_value - quantity_max * quote.raw_price)
        cash_change = quantity_max * quote.raw_price
    elif action in {"buy", "add"}:
        cash_limited_quantity = int(
            math.floor(float(portfolio["cash"]) / (quote.raw_price * 1.02) / lot) * lot
        )
        quantity_max = min(
            max(0, maximum_quantity - current_quantity),
            cash_limited_quantity,
        )
        quantity_min = min(lot, quantity_max) if quantity_max else 0
        post_value = current_value + quantity_max * quote.raw_price
        cash_change = -quantity_max * quote.raw_price
    else:
        quantity_min = quantity_max = 0
        post_value = current_value
        cash_change = 0.0
    actionable = bool(
        quote.actionable
        and pack.quality_score >= 0.7
        and action in trade_actions
        and quantity_max > 0
    )
    suggested_action = (
        action if action not in trade_actions or actionable else "review_required"
    )
    card = {
        "suggested_action": suggested_action,
        "suggested_quantity_range": [quantity_min, quantity_max],
        "post_trade_weight": post_value / max(equity, 0.01),
        "cash_change": cash_change,
        "maximum_planned_loss": quantity_max * quote.raw_price * 0.15,
        "supporting_evidence": committee.get("supporting_evidence", []),
        "opposing_evidence": committee.get("opposing_evidence", []),
        "invalidation_conditions": committee.get("invalidation_conditions", []),
        "data_reliability": {
            "context_quality": pack.quality_score,
            "quote_trust_level": quote.trust_level.value,
            "quote_actionability_reasons": quote.actionability_reasons,
        },
        "actionable": actionable,
        "user_decides": True,
        "broker_order_sent": False,
        "start_price": quote.raw_price,
        "quote_fingerprint": quote.quote_fingerprint,
        "asset_type": asset_type.value,
        "due_dates": _investor_due_dates(settings, quote.as_of),
    }
    recommendation_id = str(uuid.uuid4())
    with repository.connect() as db:
        db.execute(
            """INSERT INTO investor_recommendations(
                recommendation_id,portfolio_id,symbol,as_of,action,quantity_min,quantity_max,
                actionable,context_id,context_fingerprint,payload,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                recommendation_id,
                portfolio_id,
                symbol,
                quote.as_of.isoformat(),
                card["suggested_action"],
                quantity_min,
                quantity_max,
                int(actionable),
                pack.context_id,
                pack.fingerprint,
                json.dumps(sanitize_for_export(card), ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )
    return repository.recommendation(recommendation_id) or {}


def record_recommendation_adoption(
    settings: Settings,
    *,
    recommendation_id: str,
    decision: str,
    actual_quantity: int | None = None,
    actual_price: float | None = None,
    actual_trade_date: date | None = None,
    trade_side: str | None = None,
    transaction_cost: float = 0.0,
    note: str | None = None,
) -> dict[str, Any]:
    if decision not in {"adopted", "partially_adopted", "rejected", "user_override"}:
        raise ValueError("invalid recommendation adoption decision")
    trade_fields = (trade_side, actual_quantity, actual_price, actual_trade_date)
    has_trade = any(value is not None for value in trade_fields)
    if has_trade and any(value is None for value in trade_fields):
        raise ValueError("external trade requires explicit side, quantity, price and trade date")
    if trade_side not in {None, "buy", "sell"}:
        raise ValueError("external trade side must be buy or sell")
    if decision == "rejected" and has_trade:
        raise ValueError("rejected recommendation cannot carry an external trade")
    if decision == "user_override" and not has_trade:
        raise ValueError("user_override requires an explicit external trade")
    if transaction_cost < 0:
        raise ValueError("transaction cost cannot be negative")
    repository = _repository(settings)
    round7 = Round7Repository(repository.path)
    Round8Repository(repository.path)
    recommendation = repository.recommendation(recommendation_id)
    if recommendation is None:
        raise ValueError("investor recommendation not found")
    recommendation_payload = recommendation.get("payload", {})
    action = str(recommendation.get("action") or "review_required")
    recommended_side = (
        "buy" if action in {"buy", "add"} else "sell" if action in {"reduce", "avoid"} else None
    )
    actionable = bool(recommendation.get("actionable"))
    quantity_max = int(recommendation.get("quantity_max") or 0)
    if has_trade and decision in {"adopted", "partially_adopted"}:
        if not actionable or quantity_max <= 0 or recommended_side is None:
            raise ValueError("non-actionable recommendation trade must be recorded as user_override")
        if trade_side != recommended_side:
            raise ValueError("trade against recommendation direction must be recorded as user_override")
        if int(actual_quantity or 0) > quantity_max:
            raise ValueError("external trade quantity exceeds the recommendation range")
    decision_relation = "user_override" if decision == "user_override" else "aligned"
    resolved_trade_date = actual_trade_date.isoformat() if actual_trade_date else None
    requested_revision = {
        "decision": decision,
        "actual_quantity": actual_quantity,
        "actual_price": actual_price,
        "actual_trade_date": resolved_trade_date,
        "trade_side": trade_side,
        "decision_relation": decision_relation,
        "transaction_cost": float(transaction_cost),
        "note": note,
    }
    with repository.transaction() as db:
        latest_revision = db.execute(
            """SELECT * FROM investor_adoption_revisions
               WHERE recommendation_id=? ORDER BY revision_number DESC LIMIT 1""",
            (recommendation_id,),
        ).fetchone()
        if latest_revision is not None:
            comparable = {
                key: latest_revision[key]
                for key in (
                    "decision",
                    "actual_quantity",
                    "actual_price",
                    "actual_trade_date",
                    "trade_side",
                    "decision_relation",
                    "transaction_cost",
                    "note",
                )
            }
            if comparable == requested_revision:
                return {**dict(latest_revision), "idempotent": True}
        if db.execute(
            "SELECT 1 FROM investor_recommendation_outcomes WHERE recommendation_id=? LIMIT 1",
            (recommendation_id,),
        ).fetchone():
            raise ValueError("recommendation adoption cannot be revised after outcome settlement")
        if latest_revision is not None:
            _reverse_recommendation_trade_in_tx(
                db,
                recommendation=recommendation,
                recommendation_id=recommendation_id,
            )
        revision_number = int(latest_revision["revision_number"] if latest_revision else 0) + 1
        revision_id = str(uuid.uuid4())
        recorded_at = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO investor_adoption_revisions(
                   revision_id,recommendation_id,revision_number,decision,
                   actual_quantity,actual_price,actual_trade_date,transaction_cost,
                   note,supersedes_revision_id,settled,recorded_at,trade_side,decision_relation
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
            (
                revision_id,
                recommendation_id,
                revision_number,
                decision,
                actual_quantity,
                actual_price,
                resolved_trade_date,
                transaction_cost,
                note,
                latest_revision["revision_id"] if latest_revision else None,
                recorded_at,
                trade_side,
                decision_relation,
            ),
        )
        existing_adoption = db.execute(
            "SELECT * FROM investor_recommendation_adoptions WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        adoption_id = (
            str(existing_adoption["adoption_id"]) if existing_adoption else str(uuid.uuid4())
        )
        db.execute(
            """INSERT INTO investor_recommendation_adoptions(
                adoption_id,recommendation_id,decision,actual_quantity,actual_price,note,recorded_at,
                actual_trade_side,actual_trade_date,transaction_cost,decision_relation
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(recommendation_id) DO UPDATE SET
              decision=excluded.decision,actual_quantity=excluded.actual_quantity,
              actual_price=excluded.actual_price,note=excluded.note,
              recorded_at=excluded.recorded_at,
              actual_trade_side=excluded.actual_trade_side,
              actual_trade_date=excluded.actual_trade_date,
              transaction_cost=excluded.transaction_cost,
              decision_relation=excluded.decision_relation""",
            (
                adoption_id,
                recommendation_id,
                decision,
                actual_quantity,
                actual_price,
                note,
                recorded_at,
                trade_side,
                resolved_trade_date,
                transaction_cost,
                decision_relation,
            ),
        )
        trade = None
        if has_trade:
            trade = _record_trade_in_tx(
                db,
                portfolio_id=recommendation["portfolio_id"],
                payload={
                    "symbol": recommendation["symbol"],
                    "asset_type": recommendation_payload.get("asset_type", "stock"),
                    "side": trade_side,
                    "quantity": actual_quantity,
                    "price": actual_price,
                    "transaction_cost": transaction_cost,
                    "trade_date": resolved_trade_date,
                },
                idempotency_key=(
                    f"recommendation-adoption:{recommendation_id}:revision:{revision_number}"
                ),
                import_id=None,
                source="user_reported_external_fill",
            )
        row = db.execute(
            "SELECT * FROM investor_adoption_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
    thesis = None
    if decision in {"adopted", "partially_adopted"}:
        from quantlab.workflows.investment_thesis import (
            create_investment_thesis_from_recommendation,
        )

        thesis = create_investment_thesis_from_recommendation(
            settings,
            recommendation_id=recommendation_id,
            user_decision=decision,
            external_trade_id=trade["trade_id"] if trade else None,
        )
    else:
        thesis = Round8Repository(repository.path).revise_thesis_decision(
            recommendation_id,
            user_decision=decision,
            external_trade_id=trade["trade_id"] if trade else None,
        )
    return {
        **dict(row),
        "idempotent": False,
        "revision_history": round7.adoption_revisions(recommendation_id),
        "thesis": thesis,
    }


def investor_recommendation_detail(
    settings: Settings,
    *,
    recommendation_id: str,
) -> dict[str, Any]:
    repository = _repository(settings)
    recommendation = repository.recommendation(recommendation_id)
    if recommendation is None:
        raise ValueError("investor recommendation not found")
    with repository.connect() as db:
        adoption = db.execute(
            "SELECT * FROM investor_recommendation_adoptions WHERE recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        outcomes = db.execute(
            """SELECT * FROM investor_recommendation_outcomes
               WHERE recommendation_id=? ORDER BY horizon_days""",
            (recommendation_id,),
        ).fetchall()
    return {
        "recommendation": recommendation,
        "adoption": dict(adoption) if adoption else None,
        "revisions": Round7Repository(repository.path).adoption_revisions(recommendation_id),
        "outcomes": [
            {**dict(outcome), "payload": json.loads(outcome["payload"])}
            for outcome in outcomes
        ],
        "training_eligible": False,
        "forward_scorecard_eligible": False,
    }


def investor_recommendation_effects(
    settings: Settings,
    *,
    portfolio_id: str,
) -> dict[str, Any]:
    repository = _repository(settings)
    with repository.connect() as db:
        rows = db.execute(
            """SELECT r.recommendation_id,r.symbol,r.as_of,r.action,
                      a.decision,o.horizon_days,o.realized_return_pct,o.due_date
               FROM investor_recommendations r
               LEFT JOIN investor_recommendation_adoptions a
                 ON a.recommendation_id=r.recommendation_id
               LEFT JOIN investor_recommendation_outcomes o
                 ON o.recommendation_id=r.recommendation_id
               WHERE r.portfolio_id=?
               ORDER BY r.created_at,o.horizon_days""",
            (portfolio_id,),
        ).fetchall()
    records = [dict(row) for row in rows]
    comparison: dict[str, dict[str, Any]] = {}
    for horizon in (5, 20):
        for group, decisions in (
            ("accepted", {"adopted", "partially_adopted"}),
            ("rejected", {"rejected"}),
        ):
            values = [
                float(item["realized_return_pct"])
                for item in records
                if item["horizon_days"] == horizon
                and item["decision"] in decisions
                and item["realized_return_pct"] is not None
            ]
            comparison[f"{group}_{horizon}d"] = {
                "samples": len(values),
                "average_return_pct": sum(values) / len(values) if values else None,
            }
    return {
        "portfolio_id": portfolio_id,
        "records": records,
        "comparison": comparison,
        "claim_boundary": (
            "This is product-effect evidence only. It is excluded from model training and "
            "the formal forward scorecard."
        ),
        "training_eligible": False,
        "forward_scorecard_eligible": False,
    }


def settle_investor_recommendation_outcomes(
    settings: Settings,
    *,
    as_of: date,
    bar_service: ResearchBarService | None = None,
) -> dict[str, Any]:
    repository = _repository(settings)
    service = bar_service or ResearchBarService.from_settings(settings)
    settled = pending = 0
    with repository.transaction() as db:
        recommendations = db.execute(
            "SELECT * FROM investor_recommendations ORDER BY created_at"
        ).fetchall()
        for row in recommendations:
            payload = json.loads(row["payload"])
            for horizon in (5, 20):
                due_date = date.fromisoformat(payload["due_dates"][str(horizon)])
                if due_date > as_of:
                    continue
                if db.execute(
                    """SELECT 1 FROM investor_recommendation_outcomes
                       WHERE recommendation_id=? AND horizon_days=?""",
                    (row["recommendation_id"], horizon),
                ).fetchone():
                    continue
                try:
                    observation = service.get(row["symbol"], as_of=due_date, exact=True)
                except Exception:
                    pending += 1
                    continue
                start_price = float(payload["start_price"])
                realized = (observation.bar.close / start_price - 1.0) * 100.0
                db.execute(
                    """INSERT INTO investor_recommendation_outcomes(
                        outcome_id,recommendation_id,horizon_days,due_date,observed_at,
                        start_price,end_price,realized_return_pct,source,payload,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        row["recommendation_id"],
                        horizon,
                        due_date.isoformat(),
                        datetime.now(UTC).isoformat(),
                        start_price,
                        observation.bar.close,
                        realized,
                        f"{observation.provider}:{observation.source_version}",
                        json.dumps({"research_only_product_effect": True}),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                db.execute(
                    """UPDATE investor_adoption_revisions SET settled=1
                       WHERE recommendation_id=?""",
                    (row["recommendation_id"],),
                )
                settled += 1
    return {"settled": settled, "pending": pending, "as_of": as_of.isoformat()}


def _reverse_recommendation_trade_in_tx(
    db: Any,
    *,
    recommendation: dict[str, Any],
    recommendation_id: str,
) -> None:
    trade = db.execute(
        """SELECT * FROM investor_trades
           WHERE portfolio_id=? AND symbol=?
             AND source='user_reported_external_fill'
             AND (idempotency_key=? OR idempotency_key LIKE ?)
           ORDER BY created_at DESC LIMIT 1""",
        (
            recommendation["portfolio_id"],
            recommendation["symbol"],
            f"recommendation-adoption:{recommendation_id}",
            f"recommendation-adoption:{recommendation_id}:revision:%",
        ),
    ).fetchone()
    if trade is None:
        return
    later = db.execute(
        """SELECT 1 FROM investor_trades
           WHERE portfolio_id=? AND symbol=? AND created_at>?
             AND source NOT LIKE 'superseded_%' LIMIT 1""",
        (trade["portfolio_id"], trade["symbol"], trade["created_at"]),
    ).fetchone()
    if later is not None:
        raise ValueError(
            "cannot revise an external fill after a later trade in the same symbol"
        )
    position = db.execute(
        "SELECT * FROM investor_positions WHERE portfolio_id=? AND symbol=?",
        (trade["portfolio_id"], trade["symbol"]),
    ).fetchone()
    if position is None:
        raise ValueError("cannot reverse recommendation fill because position is missing")
    quantity = int(trade["quantity"])
    price = float(trade["price"])
    cost = float(trade["transaction_cost"])
    current_quantity = int(position["quantity"])
    current_average = float(position["average_cost"])
    current_realized = float(position["realized_pnl"])
    if trade["side"] == "buy":
        if current_quantity < quantity:
            raise ValueError("cannot revise external buy after the position was reduced")
        new_quantity = current_quantity - quantity
        remaining_book = current_quantity * current_average - (quantity * price + cost)
        new_average = max(0.0, remaining_book / new_quantity) if new_quantity else 0.0
        cash_delta = quantity * price + cost
        realized = current_realized
    else:
        new_quantity = current_quantity + quantity
        new_average = current_average
        realized_delta = (price - current_average) * quantity - cost
        realized = current_realized - realized_delta
        cash_delta = -(quantity * price - cost)
    db.execute(
        """UPDATE investor_positions
           SET quantity=?,average_cost=?,realized_pnl=?,updated_at=?
           WHERE portfolio_id=? AND symbol=?""",
        (
            new_quantity,
            new_average,
            realized,
            datetime.now(UTC).isoformat(),
            trade["portfolio_id"],
            trade["symbol"],
        ),
    )
    db.execute(
        "UPDATE investor_portfolios SET cash=cash+?,updated_at=? WHERE portfolio_id=?",
        (cash_delta, datetime.now(UTC).isoformat(), trade["portfolio_id"]),
    )
    db.execute(
        """UPDATE investor_trades
           SET source='superseded_user_reported_external_fill' WHERE trade_id=?""",
        (trade["trade_id"],),
    )


def _record_trade_in_tx(
    db: Any,
    *,
    portfolio_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
    import_id: str | None,
    source: str,
) -> dict[str, Any]:
    existing = db.execute(
        "SELECT * FROM investor_trades WHERE portfolio_id=? AND idempotency_key=?",
        (portfolio_id, idempotency_key),
    ).fetchone()
    if existing:
        return dict(existing)
    portfolio = db.execute(
        "SELECT * FROM investor_portfolios WHERE portfolio_id=?", (portfolio_id,)
    ).fetchone()
    if portfolio is None:
        raise ValueError("investor portfolio not found")
    position = db.execute(
        "SELECT * FROM investor_positions WHERE portfolio_id=? AND symbol=?",
        (portfolio_id, payload["symbol"]),
    ).fetchone()
    quantity = int(payload["quantity"])
    price = float(payload["price"])
    cost = float(payload["transaction_cost"])
    if payload["side"] == "buy":
        cash_effect = quantity * price + cost
        if cash_effect > float(portfolio["cash"]) + 1e-6:
            raise ValueError("recorded investor buy exceeds portfolio cash")
        previous_quantity = int(position["quantity"]) if position else 0
        previous_cost = float(position["average_cost"]) if position else 0.0
        new_quantity = previous_quantity + quantity
        average_cost = (previous_quantity * previous_cost + cash_effect) / new_quantity
        realized_delta = 0.0
        cash_delta = -cash_effect
    else:
        if position is None or quantity > int(position["quantity"]):
            raise ValueError("recorded investor sell exceeds position")
        new_quantity = int(position["quantity"]) - quantity
        average_cost = float(position["average_cost"])
        realized_delta = (price - average_cost) * quantity - cost
        cash_delta = quantity * price - cost
    db.execute(
        """INSERT INTO investor_positions(
            portfolio_id,symbol,asset_type,quantity,average_cost,latest_price,
            price_status,realized_pnl,updated_at
        ) VALUES(?,?,?,?,?,?, 'stale',?,?)
        ON CONFLICT(portfolio_id,symbol) DO UPDATE SET
          quantity=excluded.quantity,average_cost=excluded.average_cost,
          latest_price=excluded.latest_price,
          realized_pnl=investor_positions.realized_pnl+excluded.realized_pnl,
          updated_at=excluded.updated_at""",
        (
            portfolio_id,
            payload["symbol"],
            payload["asset_type"],
            new_quantity,
            average_cost,
            price,
            realized_delta,
            datetime.now(UTC).isoformat(),
        ),
    )
    db.execute(
        "UPDATE investor_portfolios SET cash=cash+?,updated_at=? WHERE portfolio_id=?",
        (cash_delta, datetime.now(UTC).isoformat(), portfolio_id),
    )
    trade_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO investor_trades(
            trade_id,portfolio_id,import_id,idempotency_key,symbol,asset_type,side,
            quantity,price,transaction_cost,trade_date,source,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            trade_id,
            portfolio_id,
            import_id,
            idempotency_key,
            payload["symbol"],
            payload["asset_type"],
            payload["side"],
            quantity,
            price,
            cost,
            payload["trade_date"],
            source,
            datetime.now(UTC).isoformat(),
        ),
    )
    return dict(
        db.execute("SELECT * FROM investor_trades WHERE trade_id=?", (trade_id,)).fetchone()
    )


def _normalize_import_row(import_type: str, row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    asset_type = AssetType(str(row.get("asset_type") or "stock")).value
    quantity = int(row.get("quantity") or 0)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if import_type == "positions":
        average_cost = float(row.get("average_cost") or 0)
        if average_cost <= 0:
            raise ValueError("average_cost must be positive")
        return {
            "symbol": symbol,
            "name": str(row.get("name") or ""),
            "asset_type": asset_type,
            "quantity": quantity,
            "average_cost": average_cost,
            "industry": str(row.get("industry") or "") or None,
        }
    side = str(row.get("side") or "").lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    price = float(row.get("price") or 0)
    if price <= 0:
        raise ValueError("price must be positive")
    trade_date = date.fromisoformat(str(row.get("trade_date"))[:10])
    cost = float(row.get("transaction_cost") or 0)
    if cost < 0:
        raise ValueError("transaction_cost cannot be negative")
    return {
        "symbol": symbol,
        "asset_type": asset_type,
        "side": side,
        "quantity": quantity,
        "price": price,
        "transaction_cost": cost,
        "trade_date": trade_date.isoformat(),
    }


def _row_fingerprint(import_type: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([import_type, payload], sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _investor_due_dates(settings: Settings, start: date) -> dict[str, str]:
    calendar = TradingCalendarService.from_settings(settings)
    output: dict[str, str] = {}
    for horizon in (5, 20):
        try:
            due = calendar.add_open_days(start, horizon, formal=True)
        except ValueError:
            due = calendar.add_open_days(start, horizon, formal=False)
        output[str(horizon)] = due.isoformat()
    return output


def _repository(settings: Settings) -> Round5Repository:
    return Round5Repository(settings.resolve(settings.get("system.database_path")))


__all__ = [
    "POSITION_CSV_COLUMNS",
    "TRADE_CSV_COLUMNS",
    "build_investor_recommendation",
    "confirm_investor_import",
    "create_investor_portfolio",
    "investor_csv_template",
    "investor_recommendation_detail",
    "investor_recommendation_effects",
    "mark_investor_portfolios",
    "preview_investor_csv",
    "record_investor_trade",
    "record_recommendation_adoption",
    "settle_investor_recommendation_outcomes",
]
