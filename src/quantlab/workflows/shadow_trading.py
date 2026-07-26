from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, date, datetime
from typing import Any

from quantlab.config import Settings
from quantlab.domain import OrderRequest, Side
from quantlab.domain.data_governance import DataTrustLevel, trust_at_least
from quantlab.execution import CostModel
from quantlab.market import ResearchBarService, TradingCalendarService
from quantlab.persistence.round5 import Round5Repository


def create_shadow_orders_for_registration(
    settings: Settings,
    registration_id: str,
) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    samples = [
        item
        for item in repository.registration_samples(registration_id)
        if item["status"] == "registered" and int(item["horizon_days"]) == 5
    ]
    if not samples:
        return {"registration_id": registration_id, "orders_created": 0, "events": 0}
    experiment = repository.primary_experiment()
    if experiment is None or experiment["cohort_id"] != samples[0]["cohort_id"]:
        raise ValueError("shadow orders require the current primary experiment")
    accounts = {item["variant"]: item for item in repository.ensure_shadow_accounts(experiment)}
    minimum_trust = DataTrustLevel(experiment["minimum_trust_level"])
    calendar = TradingCalendarService.from_settings(settings)
    created = events = 0
    signal_date = date.fromisoformat(samples[0]["trade_date"])
    eligible_date = calendar.next_open_day(
        signal_date,
        formal=True,
        minimum_trust=minimum_trust,
    )
    expires_at = calendar.add_open_days(
        eligible_date,
        int(experiment["matching_rules"].get("order_expiry_open_sessions", 5)),
        formal=True,
        minimum_trust=minimum_trust,
    )
    selected_symbols = {item["symbol"] for item in samples}
    with repository.transaction() as db:
        for account in accounts.values():
            if selected_symbols:
                placeholders = ",".join("?" for _ in selected_symbols)
                reentered = db.execute(
                    f"""SELECT order_id,symbol FROM shadow_orders
                        WHERE account_id=? AND status='pending' AND side='sell'
                          AND reason='symbol left the frozen daily candidate set'
                          AND symbol IN ({placeholders})""",
                    (account["account_id"], *sorted(selected_symbols)),
                ).fetchall()
                for order in reentered:
                    db.execute(
                        """UPDATE shadow_orders SET status='cancelled',
                           reason='symbol re-entered the frozen daily candidate set',updated_at=?
                           WHERE order_id=? AND status='pending'""",
                        (datetime.now(UTC).isoformat(), order["order_id"]),
                    )
                    _event(
                        db,
                        account["account_id"],
                        "candidate_exit_cancelled",
                        order["symbol"],
                        signal_date,
                        "pending exit cancelled because the symbol re-entered the candidate set",
                    )
                    events += 1
            stale_positions = db.execute(
                """SELECT * FROM shadow_positions
                   WHERE account_id=? AND quantity>0 ORDER BY symbol""",
                (account["account_id"],),
            ).fetchall()
            for position in stale_positions:
                if position["symbol"] in selected_symbols:
                    continue
                pending_sell = int(
                    db.execute(
                        """SELECT COALESCE(SUM(requested_quantity-filled_quantity),0)
                           FROM shadow_orders
                           WHERE account_id=? AND symbol=? AND side='sell'
                             AND status='pending'""",
                        (account["account_id"], position["symbol"]),
                    ).fetchone()[0]
                    or 0
                )
                quantity = max(0, int(position["quantity"]) - pending_sell)
                if quantity <= 0:
                    continue
                result = db.execute(
                    """INSERT OR IGNORE INTO shadow_orders(
                        order_id,account_id,sample_key,symbol,side,requested_quantity,
                        target_weight,signal_date,eligible_trade_date,expires_at,status,
                        reference_close,reserved_cash,reason,payload,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,0,?,?,?,'pending',?,0,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        account["account_id"],
                        f"{registration_id}:candidate-exit",
                        position["symbol"],
                        "sell",
                        quantity,
                        signal_date.isoformat(),
                        eligible_date.isoformat(),
                        expires_at.isoformat(),
                        float(position["latest_price"]),
                        "symbol left the frozen daily candidate set",
                        json.dumps(
                            {
                                "variant": account["variant"],
                                "minimum_trust_level": minimum_trust.value,
                                "signal_at": "T_close",
                                "execute_at": "T_plus_1_open",
                                "target_weight": 0.0,
                                "exit_reason": "not_in_current_candidate_set",
                            }
                        ),
                        datetime.now(UTC).isoformat(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if result.rowcount:
                    db.execute(
                        """UPDATE shadow_accounts SET actual_trigger_count=actual_trigger_count+1,
                           updated_at=? WHERE account_id=?""",
                        (datetime.now(UTC).isoformat(), account["account_id"]),
                    )
                    created += 1
        for sample in samples:
            predictions = db.execute(
                """SELECT variant,target_weight,action,start_price,actually_triggered
                   FROM forward_ablation_predictions
                   WHERE cohort_id=? AND sample_key=? AND horizon_days=5
                     AND registration_origin='automatic_primary'""",
                (sample["cohort_id"], sample["sample_key"]),
            ).fetchall()
            for prediction in predictions:
                account = accounts[prediction["variant"]]
                account_state = db.execute(
                    "SELECT cash,frozen_cash FROM shadow_accounts WHERE account_id=?",
                    (account["account_id"],),
                ).fetchone()
                position = db.execute(
                    "SELECT * FROM shadow_positions WHERE account_id=? AND symbol=?",
                    (account["account_id"], sample["symbol"]),
                ).fetchone()
                current_quantity = int(position["quantity"]) if position else 0
                equity = _account_equity(
                    db,
                    account["account_id"],
                    float(account_state["cash"]),
                )
                lot = _trade_lot(settings, sample["symbol"])
                reference = float(prediction["start_price"])
                desired_quantity = int(
                    math.floor(
                        equity * float(prediction["target_weight"]) / reference / lot
                    )
                    * lot
                )
                difference = desired_quantity - current_quantity
                if difference == 0:
                    _event(
                        db,
                        account["account_id"],
                        "no_rebalance_required",
                        sample["symbol"],
                        signal_date,
                        "target quantity already matches the independent account",
                    )
                    events += 1
                    continue
                side = "buy" if difference > 0 else "sell"
                quantity = abs(difference)
                if side == "sell":
                    pending_sell = int(
                        db.execute(
                            """SELECT COALESCE(SUM(requested_quantity-filled_quantity),0)
                               FROM shadow_orders
                               WHERE account_id=? AND symbol=? AND side='sell'
                                 AND status='pending'""",
                            (account["account_id"], sample["symbol"]),
                        ).fetchone()[0]
                        or 0
                    )
                    quantity = min(quantity, max(0, current_quantity - pending_sell))
                reserved_cash = reference * quantity * 1.02 if side == "buy" else 0.0
                available_cash = float(account_state["cash"]) - float(
                    account_state["frozen_cash"]
                )
                if side == "buy" and reserved_cash > available_cash:
                    quantity = int(math.floor(available_cash / (reference * 1.02) / lot) * lot)
                    reserved_cash = reference * quantity * 1.02
                if quantity <= 0:
                    _event(
                        db,
                        account["account_id"],
                        "order_rejected",
                        sample["symbol"],
                        signal_date,
                        "independent shadow account has insufficient cash or position",
                    )
                    events += 1
                    continue
                order_id = str(uuid.uuid4())
                result = db.execute(
                    """INSERT OR IGNORE INTO shadow_orders(
                        order_id,account_id,sample_key,symbol,side,requested_quantity,
                        target_weight,signal_date,eligible_trade_date,expires_at,status,reference_close,
                        reserved_cash,reason,payload,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)""",
                    (
                        order_id,
                        account["account_id"],
                        sample["sample_key"],
                        sample["symbol"],
                        side,
                        quantity,
                        float(prediction["target_weight"]),
                        signal_date.isoformat(),
                        eligible_date.isoformat(),
                        expires_at.isoformat(),
                        reference,
                        reserved_cash,
                        f"{prediction['variant']}:{prediction['action']}",
                        json.dumps(
                            {
                                "variant": prediction["variant"],
                                "minimum_trust_level": minimum_trust.value,
                                "signal_at": "T_close",
                                "execute_at": "T_plus_1_open",
                            }
                        ),
                        datetime.now(UTC).isoformat(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if result.rowcount:
                    db.execute(
                        """UPDATE shadow_accounts SET frozen_cash=frozen_cash+?,
                           actual_trigger_count=actual_trigger_count+1,updated_at=?
                           WHERE account_id=?""",
                        (reserved_cash, datetime.now(UTC).isoformat(), account["account_id"]),
                    )
                    created += 1
    return {"registration_id": registration_id, "orders_created": created, "events": events}


def execute_pending_shadow_orders(
    settings: Settings,
    *,
    as_of: date,
    bar_service: ResearchBarService | None = None,
) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    service = bar_service or ResearchBarService.from_settings(settings)
    filled = partial = pending = rejected = expired = 0
    with repository.transaction() as db:
        orders = db.execute(
            """SELECT o.*,a.variant,a.initial_capital,a.cash,a.frozen_cash
               FROM shadow_orders o JOIN shadow_accounts a ON a.account_id=o.account_id
               WHERE o.status='pending' AND o.eligible_trade_date<=?
               ORDER BY o.eligible_trade_date,o.created_at""",
            (as_of.isoformat(),),
        ).fetchall()
        for order in orders:
            if order["expires_at"] and as_of > date.fromisoformat(order["expires_at"]):
                db.execute(
                    """UPDATE shadow_orders SET status='expired',reason=?,reserved_cash=0,
                       updated_at=? WHERE order_id=?""",
                    (
                        "trusted opening price was unavailable before expiry",
                        datetime.now(UTC).isoformat(),
                        order["order_id"],
                    ),
                )
                db.execute(
                    """UPDATE shadow_accounts
                       SET frozen_cash=MAX(0,frozen_cash-?),updated_at=? WHERE account_id=?""",
                    (
                        order["reserved_cash"],
                        datetime.now(UTC).isoformat(),
                        order["account_id"],
                    ),
                )
                _event(
                    db,
                    order["account_id"],
                    "order_expired",
                    order["symbol"],
                    as_of,
                    "trusted opening price was unavailable before the frozen expiry",
                )
                expired += 1
                continue
            minimum_trust = json.loads(order["payload"]).get(
                "minimum_trust_level", DataTrustLevel.SERVER_OBSERVED.value
            )
            trade_date = date.fromisoformat(order["eligible_trade_date"])
            try:
                observation = service.get(
                    order["symbol"],
                    as_of=trade_date,
                    minimum_trust=minimum_trust,
                    exact=True,
                )
            except Exception as exc:
                _event(
                    db,
                    order["account_id"],
                    "missing_open_price",
                    order["symbol"],
                    as_of,
                    f"order remains pending: {type(exc).__name__}",
                )
                pending += 1
                continue
            if not trust_at_least(observation.trust_level, minimum_trust):
                pending += 1
                continue
            lot = _trade_lot(settings, order["symbol"])
            quantity = int(order["requested_quantity"])
            position = db.execute(
                "SELECT * FROM shadow_positions WHERE account_id=? AND symbol=?",
                (order["account_id"], order["symbol"]),
            ).fetchone()
            if order["side"] == "buy":
                account_state = db.execute(
                    "SELECT cash,frozen_cash FROM shadow_accounts WHERE account_id=?",
                    (order["account_id"],),
                ).fetchone()
                other_reservations = max(
                    0.0,
                    float(account_state["frozen_cash"]) - float(order["reserved_cash"]),
                )
                spendable_cash = max(0.0, float(account_state["cash"]) - other_reservations)
                affordable = int(
                    math.floor(spendable_cash / (observation.bar.open * 1.02) / lot)
                    * lot
                )
                quantity = min(quantity, affordable)
            else:
                quantity = min(quantity, int(position["quantity"]) if position else 0)
            if quantity <= 0:
                db.execute(
                    """UPDATE shadow_orders SET status='rejected',reason=?,reserved_cash=0,
                       updated_at=? WHERE order_id=?""",
                    ("insufficient_cash_or_position", datetime.now(UTC).isoformat(), order["order_id"]),
                )
                db.execute(
                    "UPDATE shadow_accounts SET frozen_cash=MAX(0,frozen_cash-?),updated_at=? WHERE account_id=?",
                    (order["reserved_cash"], datetime.now(UTC).isoformat(), order["account_id"]),
                )
                rejected += 1
                continue
            model = _cost_model(settings, order["symbol"])
            fill = model.fill(
                OrderRequest(
                    symbol=order["symbol"],
                    side=Side(order["side"]),
                    quantity=quantity,
                    signal_date=date.fromisoformat(order["signal_date"]),
                ),
                observation.bar.open,
                trade_date,
            )
            cash_fees = fill.commission + fill.stamp_duty + fill.transfer_fee
            total_cost = fill.total_cost
            realized_delta = 0.0
            if order["side"] == "buy":
                previous_quantity = int(position["quantity"]) if position else 0
                previous_cost = float(position["average_cost"]) if position else 0.0
                new_quantity = previous_quantity + quantity
                average_cost = (
                    previous_cost * previous_quantity + fill.gross_value + cash_fees
                ) / new_quantity
                db.execute(
                    """INSERT INTO shadow_positions(
                        account_id,symbol,quantity,average_cost,latest_price,latest_price_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,symbol) DO UPDATE SET
                      quantity=excluded.quantity,average_cost=excluded.average_cost,
                      latest_price=excluded.latest_price,latest_price_at=excluded.latest_price_at,
                      updated_at=excluded.updated_at""",
                    (
                        order["account_id"],
                        order["symbol"],
                        new_quantity,
                        average_cost,
                        observation.bar.open,
                        trade_date.isoformat(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                cash_delta = -(fill.gross_value + cash_fees)
            else:
                average_cost = float(position["average_cost"])
                new_quantity = int(position["quantity"]) - quantity
                realized_delta = (fill.price - average_cost) * quantity - cash_fees
                db.execute(
                    """UPDATE shadow_positions SET quantity=?,latest_price=?,latest_price_at=?,
                       realized_pnl=realized_pnl+?,updated_at=? WHERE account_id=? AND symbol=?""",
                    (
                        new_quantity,
                        observation.bar.open,
                        trade_date.isoformat(),
                        realized_delta,
                        datetime.now(UTC).isoformat(),
                        order["account_id"],
                        order["symbol"],
                    ),
                )
                cash_delta = fill.gross_value - cash_fees
            status = "filled" if quantity == int(order["requested_quantity"]) else "partially_filled"
            db.execute(
                """UPDATE shadow_orders SET filled_quantity=?,status=?,reserved_cash=0,
                   updated_at=? WHERE order_id=?""",
                (quantity, status, datetime.now(UTC).isoformat(), order["order_id"]),
            )
            db.execute(
                """UPDATE shadow_accounts SET cash=cash+?,frozen_cash=MAX(0,frozen_cash-?),
                   realized_pnl=realized_pnl+?,cumulative_cost=cumulative_cost+?,
                   cumulative_turnover=cumulative_turnover+?,updated_at=? WHERE account_id=?""",
                (
                    cash_delta,
                    order["reserved_cash"],
                    realized_delta,
                    total_cost,
                    fill.gross_value,
                    datetime.now(UTC).isoformat(),
                    order["account_id"],
                ),
            )
            db.execute(
                """INSERT INTO shadow_fills(
                    fill_id,order_id,account_id,symbol,side,quantity,raw_open,fill_price,
                    gross_value,transaction_cost,trade_date,source,trust_level,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    order["order_id"],
                    order["account_id"],
                    order["symbol"],
                    order["side"],
                    quantity,
                    observation.bar.open,
                    fill.price,
                    fill.gross_value,
                    total_cost,
                    trade_date.isoformat(),
                    observation.source,
                    observation.trust_level.value,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if status == "filled":
                filled += 1
            else:
                partial += 1
    return {
        "as_of": as_of.isoformat(),
        "filled": filled,
        "partially_filled": partial,
        "pending": pending,
        "rejected": rejected,
        "expired": expired,
    }


def mark_shadow_accounts(
    settings: Settings,
    *,
    as_of: date,
    bar_service: ResearchBarService | None = None,
) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    service = bar_service or ResearchBarService.from_settings(settings)
    experiment = repository.primary_experiment()
    if experiment is None:
        return {"accounts_marked": 0, "accounts": []}
    minimum_trust = DataTrustLevel(experiment["minimum_trust_level"])
    output: list[dict[str, Any]] = []
    with repository.transaction() as db:
        accounts = db.execute(
            "SELECT * FROM shadow_accounts WHERE cohort_id=? ORDER BY variant",
            (experiment["cohort_id"],),
        ).fetchall()
        for account in accounts:
            positions = db.execute(
                "SELECT * FROM shadow_positions WHERE account_id=? AND quantity>0",
                (account["account_id"],),
            ).fetchall()
            stale: list[str] = []
            market_value = unrealized = 0.0
            for position in positions:
                price = float(position["latest_price"])
                try:
                    observation = service.get(
                        position["symbol"],
                        as_of=as_of,
                        minimum_trust=minimum_trust,
                        exact=True,
                    )
                    price = observation.bar.close
                    db.execute(
                        """UPDATE shadow_positions SET latest_price=?,latest_price_at=?,updated_at=?
                           WHERE account_id=? AND symbol=?""",
                        (
                            price,
                            as_of.isoformat(),
                            datetime.now(UTC).isoformat(),
                            account["account_id"],
                            position["symbol"],
                        ),
                    )
                except Exception:
                    stale.append(position["symbol"])
                    _event(
                        db,
                        account["account_id"],
                        "mark_price_unavailable",
                        position["symbol"],
                        as_of,
                        "last known price retained and marked stale",
                    )
                market_value += int(position["quantity"]) * price
                unrealized += int(position["quantity"]) * (price - float(position["average_cost"]))
            equity = float(account["cash"]) + market_value
            previous = db.execute(
                """SELECT * FROM shadow_nav
                   WHERE account_id=? AND nav_date<? ORDER BY nav_date DESC LIMIT 1""",
                (account["account_id"], as_of.isoformat()),
            ).fetchone()
            daily_pnl = equity - (float(previous["equity"]) if previous else float(account["initial_capital"]))
            peak = max(
                [float(account["initial_capital"])]
                + [
                    float(row[0])
                    for row in db.execute(
                        "SELECT equity FROM shadow_nav WHERE account_id=? AND nav_date<?",
                        (account["account_id"], as_of.isoformat()),
                    ).fetchall()
                ]
                + [equity]
            )
            drawdown = equity / peak - 1.0 if peak else 0.0
            previous_max = float(previous["maximum_drawdown"]) if previous else 0.0
            maximum_drawdown = min(previous_max, drawdown)
            target_weights = {
                row["symbol"]: float(row["target_weight"])
                for row in db.execute(
                    """SELECT symbol,target_weight FROM shadow_orders
                       WHERE account_id=? ORDER BY signal_date,created_at""",
                    (account["account_id"],),
                ).fetchall()
            }
            actual_weights = {
                row["symbol"]: int(row["quantity"]) * float(row["latest_price"]) / max(equity, 0.01)
                for row in db.execute(
                    "SELECT * FROM shadow_positions WHERE account_id=? AND quantity>0",
                    (account["account_id"],),
                ).fetchall()
            }
            drift = sum(
                abs(actual_weights.get(symbol, 0.0) - target_weights.get(symbol, 0.0))
                for symbol in set(actual_weights) | set(target_weights)
            )
            db.execute(
                """INSERT INTO shadow_nav(
                    nav_id,account_id,nav_date,cash,market_value,equity,daily_pnl,
                    realized_pnl,unrealized_pnl,cumulative_cost,cumulative_turnover,
                    drawdown,maximum_drawdown,position_drift,data_status,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,nav_date) DO UPDATE SET
                  cash=excluded.cash,market_value=excluded.market_value,equity=excluded.equity,
                  daily_pnl=excluded.daily_pnl,realized_pnl=excluded.realized_pnl,
                  unrealized_pnl=excluded.unrealized_pnl,cumulative_cost=excluded.cumulative_cost,
                  cumulative_turnover=excluded.cumulative_turnover,drawdown=excluded.drawdown,
                  maximum_drawdown=excluded.maximum_drawdown,
                  position_drift=excluded.position_drift,data_status=excluded.data_status,
                  payload=excluded.payload""",
                (
                    str(uuid.uuid4()),
                    account["account_id"],
                    as_of.isoformat(),
                    account["cash"],
                    market_value,
                    equity,
                    daily_pnl,
                    account["realized_pnl"],
                    unrealized,
                    account["cumulative_cost"],
                    account["cumulative_turnover"],
                    drawdown,
                    maximum_drawdown,
                    drift,
                    "stale" if stale else "available",
                    json.dumps({"stale_symbols": stale}),
                    datetime.now(UTC).isoformat(),
                ),
            )
            output.append(
                {
                    "account_id": account["account_id"],
                    "variant": account["variant"],
                    "equity": equity,
                    "drawdown": drawdown,
                    "maximum_drawdown": maximum_drawdown,
                    "stale_symbols": stale,
                }
            )
    return {"accounts_marked": len(output), "accounts": output}


def shadow_scorecard(settings: Settings) -> dict[str, Any]:
    repository = Round5Repository(settings.resolve(settings.get("system.database_path")))
    experiment = repository.primary_experiment()
    if experiment is None:
        return {"experiment": None, "variants": {}}
    variants: dict[str, Any] = {}
    for account in repository.shadow_accounts(experiment["cohort_id"]):
        overview = repository.shadow_overview(account["account_id"])
        nav = overview["nav"]
        latest = nav[-1] if nav else None
        orders = overview["orders"]
        order_statuses: dict[str, int] = {}
        requested_quantity = 0
        filled_quantity = 0
        for order in orders:
            status = str(order["status"])
            order_statuses[status] = order_statuses.get(status, 0) + 1
            requested_quantity += int(order["requested_quantity"])
            filled_quantity += int(order["filled_quantity"])
        stale_nav_days = sum(item.get("data_status") != "available" for item in nav)
        variants[account["variant"]] = {
            "account_id": account["account_id"],
            "initial_capital": account["initial_capital"],
            "cash": account["cash"],
            "equity": latest["equity"] if latest else account["initial_capital"],
            "return_pct": (
                (float(latest["equity"]) / float(account["initial_capital"]) - 1.0) * 100.0
                if latest
                else 0.0
            ),
            "maximum_drawdown": latest["maximum_drawdown"] if latest else 0.0,
            "turnover": float(account["cumulative_turnover"]) / float(account["initial_capital"]),
            "transaction_cost": account["cumulative_cost"],
            "actual_trigger_count": account["actual_trigger_count"],
            "orders": len(overview["orders"]),
            "fills": len(overview["fills"]),
            "positions": len([item for item in overview["positions"] if item["quantity"] > 0]),
            "data_status": latest["data_status"] if latest else "not_started",
            "execution": {
                "order_status_counts": order_statuses,
                "requested_quantity": requested_quantity,
                "filled_quantity": filled_quantity,
                "fill_rate": (
                    filled_quantity / requested_quantity if requested_quantity else None
                ),
                "pending_orders": order_statuses.get("pending", 0),
                "partially_filled_orders": order_statuses.get("partially_filled", 0),
                "rejected_orders": order_statuses.get("rejected", 0),
                "expired_orders": order_statuses.get("expired", 0),
                "cancelled_orders": order_statuses.get("cancelled", 0),
                "nav_days": len(nav),
                "stale_nav_days": stale_nav_days,
            },
            "evidence_scope": "executable_simulated_account_nav",
        }
    return {
        "experiment": experiment,
        "variants": variants,
        "claim_boundary": (
            "Metrics come from seven isolated cash/order/fill/position/NAV ledgers. They are "
            "the only executable simulated P&L metrics in the forward scorecards, but remain "
            "simulation evidence rather than broker-confirmed performance."
        ),
    }


def _account_equity(db: Any, account_id: str, cash: float) -> float:
    market_value = db.execute(
        "SELECT COALESCE(SUM(quantity*latest_price),0) FROM shadow_positions WHERE account_id=?",
        (account_id,),
    ).fetchone()[0]
    return cash + float(market_value or 0.0)


def _trade_lot(settings: Settings, symbol: str) -> int:
    key = "etf" if symbol in set(settings.get("strategies.etf_rotation.universe", [])) else "stock"
    return int(settings.get(f"costs.{key}.trade_lot", 100))


def _cost_model(settings: Settings, symbol: str) -> CostModel:
    key = "etf" if symbol in set(settings.get("strategies.etf_rotation.universe", [])) else "stock"
    defaults = {
        "commission_rate": 0.00025 if key == "stock" else 0.0001,
        "minimum_commission": 5.0,
        "stamp_duty_rate": 0.0005 if key == "stock" else 0.0,
        "transfer_fee_rate": 0.00001 if key == "stock" else 0.0,
        "slippage_bps": 10.0 if key == "stock" else 5.0,
        "stop_slippage_bps": 25.0 if key == "stock" else 15.0,
    }
    values = {**defaults, **dict(settings.get(f"costs.{key}", {}))}
    values.pop("trade_lot", None)
    return CostModel.from_dict(values)


def _event(
    db: Any,
    account_id: str,
    event_type: str,
    symbol: str | None,
    event_date: date,
    detail: str,
) -> None:
    db.execute(
        """INSERT INTO shadow_events(
            event_id,account_id,event_type,symbol,event_date,detail,created_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()),
            account_id,
            event_type,
            symbol,
            event_date.isoformat(),
            detail,
            datetime.now(UTC).isoformat(),
        ),
    )


__all__ = [
    "create_shadow_orders_for_registration",
    "execute_pending_shadow_orders",
    "mark_shadow_accounts",
    "shadow_scorecard",
]
