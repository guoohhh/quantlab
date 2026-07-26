from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator

from quantlab.domain.models import OrderRequest, Side
from quantlab.domain.trading import AccountStatus, AccountType, MarketQuote, UserOrderStatus
from quantlab.execution import (
    CostModel,
    PortfolioExecutionState,
    TradeConstraints,
    TradeRuleService,
    validate_user_paper_simulation_mode,
)
from quantlab.persistence.migrations import record_component_migration
from quantlab.persistence.notifications import enqueue_outbox, ensure_notification_schema
from quantlab.security import sanitize_for_export
from quantlab.persistence.jobs import JobRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


class UserPaperTradingRepository:
    """Transactional user-controlled simulator isolated from strategy evidence accounts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        from quantlab.market.calendar import TradingCalendarService

        self.calendar_service = TradingCalendarService(JobRepository(self.path))
        record_component_migration(
            self.path,
            component="simulator",
            version=5,
            migration_identity="round4-simulator-test-account-isolation-v1",
        )
        record_component_migration(
            self.path,
            component="simulator",
            version=6,
            migration_identity="research-identity-order-audit-v1",
        )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    account_type TEXT NOT NULL DEFAULT 'user_paper'
                      CHECK(account_type='user_paper'),
                    evidence_eligible INTEGER NOT NULL DEFAULT 0
                      CHECK(evidence_eligible=0),
                    test_only INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    initial_capital REAL NOT NULL CHECK(initial_capital>0),
                    current_cash REAL NOT NULL CHECK(current_cash>=0),
                    frozen_cash REAL NOT NULL DEFAULT 0 CHECK(frozen_cash>=0),
                    benchmark_symbol TEXT NOT NULL DEFAULT 'sh000300',
                    benchmark_start_price REAL,
                    benchmark_start_date TEXT,
                    season INTEGER NOT NULL DEFAULT 1,
                    supersedes_account_id TEXT,
                    idempotency_key TEXT UNIQUE,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    cumulative_fees REAL NOT NULL DEFAULT 0,
                    cumulative_turnover REAL NOT NULL DEFAULT 0,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_paper_orders (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    check_id TEXT,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    requested_quantity INTEGER NOT NULL CHECK(requested_quantity>0),
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    eligible_trade_date TEXT NOT NULL,
                    expires_at TEXT,
                    reference_price REAL NOT NULL,
                    research_run_id TEXT,
                    context_id TEXT,
                    context_version TEXT,
                    context_fingerprint TEXT,
                    reserved_cash REAL NOT NULL DEFAULT 0 CHECK(reserved_cash>=0),
                    reserved_quantity INTEGER NOT NULL DEFAULT 0 CHECK(reserved_quantity>=0),
                    rejection_reason TEXT,
                    user_confirmation TEXT NOT NULL DEFAULT '{}',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES user_paper_accounts(account_id),
                    UNIQUE(account_id,idempotency_key),
                    UNIQUE(check_id)
                );
                CREATE TABLE IF NOT EXISTS user_paper_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    fill_key TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    research_run_id TEXT,
                    context_id TEXT,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity>0),
                    raw_price REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    gross_value REAL NOT NULL,
                    commission REAL NOT NULL,
                    stamp_duty REAL NOT NULL,
                    transfer_fee REAL NOT NULL,
                    slippage REAL NOT NULL,
                    transaction_fees REAL NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES user_paper_orders(order_id),
                    FOREIGN KEY(account_id) REFERENCES user_paper_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS user_paper_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL,
                    industry TEXT,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    frozen_quantity INTEGER NOT NULL DEFAULT 0,
                    reserved_sell_quantity INTEGER NOT NULL DEFAULT 0,
                    frozen_until TEXT,
                    average_cost REAL NOT NULL DEFAULT 0,
                    latest_price REAL NOT NULL DEFAULT 0,
                    latest_price_at TEXT,
                    mark_source TEXT NOT NULL DEFAULT 'unmarked',
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol),
                    FOREIGN KEY(account_id) REFERENCES user_paper_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS user_paper_equity_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    equity REAL NOT NULL,
                    today_pnl REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    total_return REAL NOT NULL,
                    drawdown REAL NOT NULL,
                    maximum_drawdown REAL NOT NULL,
                    cumulative_fees REAL NOT NULL,
                    cumulative_turnover REAL NOT NULL,
                    trade_count INTEGER NOT NULL,
                    benchmark_symbol TEXT,
                    benchmark_price REAL,
                    benchmark_return REAL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES user_paper_accounts(account_id),
                    UNIQUE(account_id,snapshot_date)
                );
                CREATE TABLE IF NOT EXISTS user_trade_decision_links (
                    check_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    order_id TEXT,
                    symbol TEXT NOT NULL,
                    research_run_id TEXT,
                    context_id TEXT,
                    context_version TEXT,
                    context_fingerprint TEXT,
                    reviewer_status TEXT NOT NULL,
                    account_version INTEGER NOT NULL,
                    check_payload TEXT NOT NULL,
                    user_request TEXT NOT NULL,
                    system_suggestion TEXT NOT NULL,
                    final_confirmation TEXT,
                    status TEXT NOT NULL DEFAULT 'checked',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES user_paper_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS user_trade_reviews (
                    review_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    order_id TEXT,
                    symbol TEXT,
                    review_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES user_paper_accounts(account_id)
                );
                CREATE TABLE IF NOT EXISTS user_paper_order_events (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    research_run_id TEXT,
                    context_id TEXT,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES user_paper_orders(order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_user_paper_orders_account_status
                  ON user_paper_orders(account_id,status,requested_at);
                CREATE INDEX IF NOT EXISTS idx_user_paper_fills_account_date
                  ON user_paper_fills(account_id,trade_date,created_at);
                CREATE INDEX IF NOT EXISTS idx_user_paper_snapshots_account_date
                  ON user_paper_equity_snapshots(account_id,snapshot_date);
                """
            )
            ensure_notification_schema(db)
            for table, columns in {
                "user_paper_orders": (
                    ("context_id", "TEXT"),
                    ("context_version", "TEXT"),
                    ("context_fingerprint", "TEXT"),
                    ("reserved_cash", "REAL NOT NULL DEFAULT 0"),
                    ("reserved_quantity", "INTEGER NOT NULL DEFAULT 0"),
                ),
                "user_trade_decision_links": (
                    ("context_id", "TEXT"),
                    ("context_version", "TEXT"),
                    ("context_fingerprint", "TEXT"),
                ),
                "user_paper_accounts": (
                    ("frozen_cash", "REAL NOT NULL DEFAULT 0"),
                    ("test_only", "INTEGER NOT NULL DEFAULT 0"),
                ),
                "user_paper_positions": (
                    ("reserved_sell_quantity", "INTEGER NOT NULL DEFAULT 0"),
                ),
                "user_paper_fills": (
                    ("research_run_id", "TEXT"),
                    ("context_id", "TEXT"),
                ),
                "user_paper_order_events": (
                    ("research_run_id", "TEXT"),
                    ("context_id", "TEXT"),
                ),
            }.items():
                existing = {
                    row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, declaration in columns:
                    if column not in existing:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                        )

    def create_account(
        self,
        *,
        name: str,
        initial_capital: float = 100_000.0,
        benchmark_symbol: str = "sh000300",
        idempotency_key: str | None = None,
        supersedes_account_id: str | None = None,
        season: int = 1,
        test_only: bool = False,
    ) -> dict[str, Any]:
        if initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        resolved_key = idempotency_key or str(uuid.uuid4())
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM user_paper_accounts WHERE idempotency_key=?",
                (resolved_key,),
            ).fetchone()
            if existing is not None:
                return self._account_row(existing)
            account_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """
                INSERT INTO user_paper_accounts(
                    account_id,name,initial_capital,current_cash,benchmark_symbol,
                    season,supersedes_account_id,idempotency_key,test_only,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    account_id,
                    name.strip(),
                    float(initial_capital),
                    float(initial_capital),
                    benchmark_symbol,
                    season,
                    supersedes_account_id,
                    resolved_key,
                    int(test_only),
                    now,
                    now,
                ),
            )
            return self._account_row(
                db.execute(
                    "SELECT * FROM user_paper_accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
            )

    def start_new_season(
        self,
        account_id: str,
        *,
        name: str | None = None,
        initial_capital: float | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM user_paper_accounts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._account_row(existing)
            old = self._require_account_in_tx(db, account_id)
            if old["status"] != AccountStatus.ACTIVE.value:
                raise ValueError("only an active account can start a new season")
            db.execute(
                """
                UPDATE user_paper_accounts
                SET status='closed',version=version+1,updated_at=?
                WHERE account_id=?
                """,
                (_now(), account_id),
            )
            new_id = str(uuid.uuid4())
            capital = float(initial_capital or old["initial_capital"])
            now = _now()
            db.execute(
                """
                INSERT INTO user_paper_accounts(
                    account_id,name,initial_capital,current_cash,benchmark_symbol,
                    season,supersedes_account_id,idempotency_key,test_only,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id,
                    (name or f"{old['name']} 第{int(old['season']) + 1}季").strip(),
                    capital,
                    capital,
                    old["benchmark_symbol"],
                    int(old["season"]) + 1,
                    account_id,
                    idempotency_key,
                    int(old["test_only"]),
                    now,
                    now,
                ),
            )
            return self._account_row(
                db.execute(
                    "SELECT * FROM user_paper_accounts WHERE account_id=?",
                    (new_id,),
                ).fetchone()
            )

    def accounts(self, include_closed: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM user_paper_accounts"
        if not include_closed:
            query += " WHERE status='active'"
        query += " ORDER BY created_at"
        with self.connect() as db:
            return [self._account_row(row) for row in db.execute(query).fetchall()]

    def account(self, account_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM user_paper_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return self._account_row(row) if row else None

    def save_pretrade_check(
        self,
        check: dict[str, Any],
        *,
        user_request: dict[str, Any],
        system_suggestion: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with self.transaction() as db:
            self._require_account_in_tx(db, str(check["account_id"]))
            db.execute(
                """
                INSERT OR IGNORE INTO user_trade_decision_links(
                    check_id,account_id,symbol,research_run_id,context_id,
                    context_version,context_fingerprint,reviewer_status,
                    account_version,check_payload,user_request,system_suggestion,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    check["check_id"],
                    check["account_id"],
                    check["symbol"],
                    check.get("research_run_id"),
                    check.get("context_id"),
                    check.get("context_version"),
                    check.get("context_fingerprint"),
                    check.get("reviewer_status", "unavailable"),
                    int(check["account_version"]),
                    json.dumps(sanitize_for_export(check), ensure_ascii=False),
                    json.dumps(sanitize_for_export(user_request), ensure_ascii=False),
                    json.dumps(sanitize_for_export(system_suggestion), ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.pretrade_check(str(check["check_id"])) or {}

    def pretrade_check(self, check_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM user_trade_decision_links WHERE check_id=?",
                (check_id,),
            ).fetchone()
        return self._decision_row(row) if row else None

    def create_order(
        self,
        *,
        check_id: str,
        quantity: int,
        idempotency_key: str,
        requested_at: datetime,
        eligible_trade_date: date,
        expires_at: datetime | None,
        user_confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        if quantity <= 0:
            raise ValueError("order quantity must be positive")
        with self.transaction() as db:
            decision = db.execute(
                "SELECT * FROM user_trade_decision_links WHERE check_id=?",
                (check_id,),
            ).fetchone()
            if decision is None:
                raise ValueError("pre-trade check not found")
            account = self._require_account_in_tx(db, decision["account_id"])
            existing = db.execute(
                """
                SELECT * FROM user_paper_orders
                WHERE account_id=? AND idempotency_key=?
                """,
                (account["account_id"], idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_order = self._order_row(existing)
                if (
                    existing_order["check_id"] != check_id
                    or int(existing_order["requested_quantity"]) != quantity
                    or existing_order["user_confirmation"] != user_confirmation
                ):
                    raise ValueError(
                        "idempotency key is already bound to a different confirmed order"
                    )
                return existing_order
            check = json.loads(decision["check_payload"])
            expected_confirmation = {
                "confirmed": True,
                "check_id": check_id,
                "account_id": str(decision["account_id"]),
                "symbol": str(check["symbol"]),
                "side": str(check["side"]),
                "quantity": quantity,
            }
            if not isinstance(user_confirmation, dict):
                raise ValueError("explicit user confirmation is required")
            for key, expected_value in expected_confirmation.items():
                if user_confirmation.get(key) != expected_value:
                    raise ValueError(
                        f"user confirmation {key} does not match the pre-trade check"
                    )
            if not str(user_confirmation.get("source") or "").strip():
                raise ValueError("user confirmation source is required")
            checked_quote = MarketQuote.model_validate(check["quote"])
            simulation_mode = validate_user_paper_simulation_mode(
                checked_quote,
                user_confirmation,
                allow_test_quote=bool(account["test_only"]),
            )
            self._release_matured_in_tx(db, account["account_id"], requested_at.date())
            account = self._require_account_in_tx(db, account["account_id"])
            hard_failures = list(check.get("hard_failures", []))
            account_conflict = int(account["version"]) != int(decision["account_version"])
            if account_conflict:
                hard_failures.append("account_state_changed_after_pretrade_check")
            if quantity != int(check["requested_quantity"]):
                hard_failures.append("confirmation_quantity_differs_from_checked_quantity")
            reserved_cash = 0.0
            reserved_quantity = 0
            if not hard_failures and bool(check.get("allowed_to_submit")):
                if check["side"] == Side.BUY.value:
                    reserved_cash = max(
                        0.0,
                        -float(check.get("estimated_total_cash_effect", 0.0)),
                    )
                    available_cash = float(account["current_cash"]) - float(
                        account["frozen_cash"]
                    )
                    if reserved_cash > available_cash + 1e-8:
                        hard_failures.append("insufficient_available_cash_after_reservations")
                        reserved_cash = 0.0
                else:
                    position = self._position_in_tx(
                        db,
                        account["account_id"],
                        check["symbol"],
                    )
                    available_quantity = (
                        int(position["quantity"])
                        - int(position["frozen_quantity"])
                        - int(position["reserved_sell_quantity"])
                        if position is not None
                        else 0
                    )
                    if quantity > available_quantity:
                        hard_failures.append("insufficient_unreserved_sellable_quantity")
                    else:
                        reserved_quantity = quantity
            allowed = bool(check.get("allowed_to_submit")) and not hard_failures
            status = (
                UserOrderStatus.PENDING.value
                if allowed
                else UserOrderStatus.REJECTED.value
            )
            rejection_reason = "; ".join(dict.fromkeys(hard_failures)) or None
            order_id = str(uuid.uuid4())
            now = _now()
            quote = check["quote"]
            if allowed and check["side"] == Side.BUY.value:
                db.execute(
                    """
                    UPDATE user_paper_accounts
                    SET frozen_cash=frozen_cash+?,version=version+1,updated_at=?
                    WHERE account_id=?
                    """,
                    (reserved_cash, now, account["account_id"]),
                )
            elif allowed:
                db.execute(
                    """
                    UPDATE user_paper_positions
                    SET reserved_sell_quantity=reserved_sell_quantity+?,updated_at=?
                    WHERE account_id=? AND symbol=?
                    """,
                    (reserved_quantity, now, account["account_id"], check["symbol"]),
                )
                db.execute(
                    """
                    UPDATE user_paper_accounts
                    SET version=version+1,updated_at=? WHERE account_id=?
                    """,
                    (now, account["account_id"]),
                )
            db.execute(
                """
                INSERT INTO user_paper_orders(
                    order_id,account_id,idempotency_key,check_id,symbol,name,
                    asset_type,side,requested_quantity,status,requested_at,
                    eligible_trade_date,expires_at,reference_price,research_run_id,
                    context_id,context_version,context_fingerprint,reserved_cash,
                    reserved_quantity,rejection_reason,
                    user_confirmation,payload,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order_id,
                    account["account_id"],
                    idempotency_key,
                    check_id,
                    check["symbol"],
                    quote.get("name", ""),
                    quote["asset_type"],
                    check["side"],
                    quantity,
                    status,
                    requested_at.isoformat(),
                    eligible_trade_date.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    float(check["reference_price"]),
                    check.get("research_run_id"),
                    check.get("context_id"),
                    check.get("context_version"),
                    check.get("context_fingerprint"),
                    reserved_cash,
                    reserved_quantity,
                    rejection_reason,
                    json.dumps(sanitize_for_export(user_confirmation), ensure_ascii=False),
                    json.dumps(
                        {
                            "pretrade_check": check,
                            "requires_user_review": check.get("requires_user_review", False),
                            "simulation_mode": simulation_mode,
                            "close_reference_acknowledged": bool(
                                user_confirmation.get("close_reference_acknowledged")
                            ),
                            "quote_submission_context": {
                                "quote_kind": checked_quote.quote_kind,
                                "session_status": checked_quote.session_status,
                                "actionable": checked_quote.actionable,
                                "authoritative": checked_quote.authoritative,
                            },
                        },
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
            self._assert_account_invariants_in_tx(db, account["account_id"])
            db.execute(
                """
                UPDATE user_trade_decision_links
                SET order_id=?,final_confirmation=?,status=?,updated_at=?
                WHERE check_id=?
                """,
                (
                    order_id,
                    json.dumps(sanitize_for_export(user_confirmation), ensure_ascii=False),
                    "confirmed" if allowed else "rejected",
                    now,
                    check_id,
                ),
            )
            self._record_order_event_in_tx(
                db,
                order_id,
                account["account_id"],
                "submitted" if allowed else "rejected",
                rejection_reason,
                {"check_id": check_id},
            )
            event_type = "paper_order_submitted" if allowed else "paper_order_rejected"
            enqueue_outbox(
                db,
                event_type=event_type,
                aggregate_type="user_paper_order",
                aggregate_id=order_id,
                payload={
                    "account_id": account["account_id"],
                    "order_id": order_id,
                    "symbol": check["symbol"],
                    "research_run_id": check.get("research_run_id"),
                    "context_id": check.get("context_id"),
                    "content": rejection_reason or "委托已进入等待成交状态",
                    "data_as_of": quote.get("as_of"),
                },
                dedup_key=f"{event_type}:{order_id}",
            )
            return self._order_row(
                db.execute(
                    "SELECT * FROM user_paper_orders WHERE order_id=?",
                    (order_id,),
                ).fetchone()
            )

    def cancel_order(self, order_id: str, reason: str = "cancelled_by_user") -> dict[str, Any]:
        with self.transaction() as db:
            order = self._require_order_in_tx(db, order_id)
            if order["status"] in {
                UserOrderStatus.CANCELLED.value,
                UserOrderStatus.FILLED.value,
                UserOrderStatus.REJECTED.value,
                UserOrderStatus.EXPIRED.value,
            }:
                return self._order_row(order)
            now = _now()
            self._release_order_reservation_in_tx(db, order)
            db.execute(
                """
                UPDATE user_paper_orders
                SET status='cancelled',rejection_reason=?,updated_at=?
                WHERE order_id=?
                """,
                (reason, now, order_id),
            )
            self._record_order_event_in_tx(
                db,
                order_id,
                order["account_id"],
                "cancelled",
                reason,
                {},
            )
            enqueue_outbox(
                db,
                event_type="paper_order_cancelled",
                aggregate_type="user_paper_order",
                aggregate_id=order_id,
                payload={
                    "account_id": order["account_id"],
                    "order_id": order_id,
                    "symbol": order["symbol"],
                    "research_run_id": order["research_run_id"],
                    "context_id": order["context_id"],
                },
                dedup_key=f"paper_order_cancelled:{order_id}",
            )
            self._assert_account_invariants_in_tx(db, order["account_id"])
            return self._order_row(
                db.execute(
                    "SELECT * FROM user_paper_orders WHERE order_id=?",
                    (order_id,),
                ).fetchone()
            )

    def expire_orders(self, as_of: datetime) -> list[dict[str, Any]]:
        expired: list[dict[str, Any]] = []
        with self.transaction() as db:
            rows = db.execute(
                """
                SELECT * FROM user_paper_orders
                WHERE status IN ('pending','partially_filled')
                  AND expires_at IS NOT NULL AND expires_at<?
                """,
                (as_of.isoformat(),),
            ).fetchall()
            for order in rows:
                self._release_order_reservation_in_tx(db, order)
                db.execute(
                    """
                    UPDATE user_paper_orders
                    SET status='expired',rejection_reason='order_expired',updated_at=?
                    WHERE order_id=?
                    """,
                    (_now(), order["order_id"]),
                )
                self._record_order_event_in_tx(
                    db,
                    order["order_id"],
                    order["account_id"],
                    "expired",
                    "order_expired",
                    {},
                )
                enqueue_outbox(
                    db,
                    event_type="paper_order_expired",
                    aggregate_type="user_paper_order",
                    aggregate_id=order["order_id"],
                    payload={
                        "account_id": order["account_id"],
                        "order_id": order["order_id"],
                        "symbol": order["symbol"],
                        "research_run_id": order["research_run_id"],
                        "context_id": order["context_id"],
                    },
                    dedup_key=f"paper_order_expired:{order['order_id']}",
                )
                expired.append(self._order_row(order) | {"status": "expired"})
                self._assert_account_invariants_in_tx(db, order["account_id"])
        return expired

    def settle_order(
        self,
        *,
        order_id: str,
        quote: MarketQuote,
        cost_model: CostModel,
        constraints: TradeConstraints,
        fill_quantity: int | None = None,
        fill_key: str,
        t_plus_one_release_date: date | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            existing_fill = db.execute(
                "SELECT * FROM user_paper_fills WHERE fill_key=?",
                (fill_key,),
            ).fetchone()
            if existing_fill is not None:
                return {
                    "order": self._order_row(
                        self._require_order_in_tx(db, existing_fill["order_id"])
                    ),
                    "fill": self._fill_row(existing_fill),
                    "idempotent": True,
                }
            order = self._require_order_in_tx(db, order_id)
            if order["status"] not in {
                UserOrderStatus.PENDING.value,
                UserOrderStatus.PARTIALLY_FILLED.value,
            }:
                raise ValueError("order is not eligible for settlement")
            trade_date = quote.as_of
            if trade_date < date.fromisoformat(order["eligible_trade_date"]):
                raise ValueError("order is waiting for its eligible trade date")
            if order["expires_at"] and datetime.now(UTC) > datetime.fromisoformat(
                order["expires_at"]
            ):
                self._release_order_reservation_in_tx(db, order)
                db.execute(
                    """
                    UPDATE user_paper_orders
                    SET status='expired',rejection_reason='order_expired',updated_at=?
                    WHERE order_id=?
                    """,
                    (_now(), order_id),
                )
                self._record_order_event_in_tx(
                    db,
                    order_id,
                    order["account_id"],
                    "expired",
                    "order_expired",
                    {},
                )
                self._assert_account_invariants_in_tx(db, order["account_id"])
                return {
                    "order": self._order_row(
                        db.execute(
                            "SELECT * FROM user_paper_orders WHERE order_id=?",
                            (order_id,),
                        ).fetchone()
                    ),
                    "fill": None,
                    "idempotent": False,
                    "rejected_reason": "order_expired",
                }
            account = self._require_account_in_tx(db, order["account_id"])
            if account["status"] != AccountStatus.ACTIVE.value:
                raise ValueError("account is not active")
            self._release_matured_in_tx(db, account["account_id"], trade_date)
            remaining = int(order["requested_quantity"]) - int(order["filled_quantity"])
            quantity = min(remaining, int(fill_quantity or remaining))
            if quantity <= 0:
                raise ValueError("fill quantity must be positive")
            position = self._position_in_tx(db, account["account_id"], order["symbol"])
            state = self._execution_state_in_tx(
                db,
                account,
                order["symbol"],
                quote.industry,
            )
            state = PortfolioExecutionState(
                cash=state.cash + float(order["reserved_cash"]),
                equity=state.equity,
                market_value=state.market_value,
                symbol_market_value=state.symbol_market_value,
                industry_market_value=state.industry_market_value,
                position_quantity=state.position_quantity,
                sellable_quantity=state.sellable_quantity
                + int(order["reserved_quantity"]),
            )
            request = OrderRequest(
                symbol=order["symbol"],
                side=Side(order["side"]),
                quantity=quantity,
                signal_date=date.fromisoformat(order["eligible_trade_date"]),
                reason="user_paper_order",
            )
            fill = cost_model.fill(request, quote.raw_price, trade_date)
            transaction_fees = fill.commission + fill.stamp_duty + fill.transfer_fee
            rules = TradeRuleService().validate(
                request,
                quote,
                state,
                request_date=trade_date,
                estimated_gross_value=fill.gross_value,
                estimated_transaction_fees=transaction_fees,
                constraints=constraints,
                calendar_service=self.calendar_service,
            )
            if not rules.allowed:
                reason = "; ".join(rules.hard_failures)
                status = (
                    UserOrderStatus.PARTIALLY_FILLED.value
                    if int(order["filled_quantity"]) > 0
                    else UserOrderStatus.REJECTED.value
                )
                db.execute(
                    """
                    UPDATE user_paper_orders
                    SET status=?,rejection_reason=?,updated_at=?
                    WHERE order_id=?
                    """,
                    (status, reason, _now(), order_id),
                )
                if status == UserOrderStatus.REJECTED.value:
                    self._release_order_reservation_in_tx(db, order)
                self._record_order_event_in_tx(
                    db,
                    order_id,
                    account["account_id"],
                    "rejected",
                    reason,
                    {"quote": quote.model_dump(mode="json")},
                )
                event_type = (
                    "paper_cash_insufficient"
                    if "insufficient_cash" in rules.hard_failures
                    else "paper_data_stale"
                    if "market_data_stale" in rules.hard_failures
                    else "paper_order_rejected"
                )
                enqueue_outbox(
                    db,
                    event_type=event_type,
                    aggregate_type="user_paper_order",
                    aggregate_id=order_id,
                    payload={
                        "account_id": account["account_id"],
                        "order_id": order_id,
                        "symbol": order["symbol"],
                        "research_run_id": order["research_run_id"],
                        "context_id": order["context_id"],
                        "content": reason,
                        "data_as_of": quote.as_of.isoformat(),
                    },
                    dedup_key=f"{event_type}:{order_id}:{int(order['filled_quantity'])}",
                )
                self._assert_account_invariants_in_tx(db, account["account_id"])
                return {
                    "order": self._order_row(
                        db.execute(
                            "SELECT * FROM user_paper_orders WHERE order_id=?",
                            (order_id,),
                        ).fetchone()
                    ),
                    "fill": None,
                    "idempotent": False,
                    "rejected_reason": reason,
                }

            fill_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """
                INSERT INTO user_paper_fills(
                    fill_id,order_id,fill_key,account_id,symbol,research_run_id,
                    context_id,side,quantity,raw_price,fill_price,gross_value,
                    commission,stamp_duty,transfer_fee,slippage,transaction_fees,
                    trade_date,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fill_id,
                    order_id,
                    fill_key,
                    account["account_id"],
                    order["symbol"],
                    order["research_run_id"],
                    order["context_id"],
                    order["side"],
                    quantity,
                    quote.raw_price,
                    fill.price,
                    fill.gross_value,
                    fill.commission,
                    fill.stamp_duty,
                    fill.transfer_fee,
                    fill.slippage,
                    transaction_fees,
                    trade_date.isoformat(),
                    json.dumps(
                        sanitize_for_export(
                            {
                                "quote": quote.model_dump(mode="json"),
                                "rule_checks": rules.checks,
                                "rule_warnings": rules.warnings,
                                "research_run_id": order["research_run_id"],
                                "context_id": order["context_id"],
                            }
                        ),
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            if order["side"] == Side.BUY.value:
                cash_delta = -(fill.gross_value + transaction_fees)
                old_quantity = int(position["quantity"]) if position else 0
                old_cost = float(position["average_cost"]) * old_quantity if position else 0.0
                new_quantity = old_quantity + quantity
                new_average = (old_cost + fill.gross_value + transaction_fees) / new_quantity
                old_frozen = int(position["frozen_quantity"]) if position else 0
                frozen_quantity = old_frozen + quantity if quote.t_plus_one else old_frozen
                frozen_until = (
                        (
                            t_plus_one_release_date
                            or self.calendar_service.next_open_day(trade_date)
                        ).isoformat()
                    if quote.t_plus_one
                    else (position["frozen_until"] if position else None)
                )
                db.execute(
                    """
                    INSERT INTO user_paper_positions(
                        account_id,symbol,name,asset_type,industry,quantity,
                        frozen_quantity,frozen_until,average_cost,latest_price,
                        latest_price_at,mark_source,realized_pnl,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,symbol) DO UPDATE SET
                        name=excluded.name,asset_type=excluded.asset_type,
                        industry=excluded.industry,quantity=excluded.quantity,
                        frozen_quantity=excluded.frozen_quantity,
                        frozen_until=excluded.frozen_until,
                        average_cost=excluded.average_cost,
                        latest_price=excluded.latest_price,
                        latest_price_at=excluded.latest_price_at,
                        mark_source=excluded.mark_source,
                        updated_at=excluded.updated_at
                    """,
                    (
                        account["account_id"],
                        order["symbol"],
                        order["name"],
                        order["asset_type"],
                        quote.industry,
                        new_quantity,
                        frozen_quantity,
                        frozen_until,
                        new_average,
                        fill.price,
                        quote.available_at.isoformat() if quote.available_at else None,
                        quote.source,
                        float(position["realized_pnl"]) if position else 0.0,
                        now,
                    ),
                )
                realized_delta = 0.0
            else:
                if position is None:
                    raise ValueError("position disappeared before settlement")
                proceeds = fill.gross_value - transaction_fees
                cash_delta = proceeds
                realized_delta = proceeds - float(position["average_cost"]) * quantity
                new_quantity = int(position["quantity"]) - quantity
                new_frozen = min(int(position["frozen_quantity"]), new_quantity)
                db.execute(
                    """
                    UPDATE user_paper_positions
                    SET quantity=?,frozen_quantity=?,realized_pnl=realized_pnl+?,
                        reserved_sell_quantity=MAX(0,reserved_sell_quantity-?),
                        latest_price=?,latest_price_at=?,mark_source=?,updated_at=?
                    WHERE account_id=? AND symbol=?
                    """,
                    (
                        new_quantity,
                        new_frozen,
                        realized_delta,
                        quantity,
                        fill.price,
                        quote.available_at.isoformat() if quote.available_at else None,
                        quote.source,
                        now,
                        account["account_id"],
                        order["symbol"],
                    ),
                )

            total_cost = transaction_fees + fill.slippage
            new_filled = int(order["filled_quantity"]) + quantity
            new_status = (
                UserOrderStatus.FILLED.value
                if new_filled >= int(order["requested_quantity"])
                else UserOrderStatus.PARTIALLY_FILLED.value
            )
            remaining_before = max(
                1,
                int(order["requested_quantity"]) - int(order["filled_quantity"]),
            )
            remaining_after = max(0, remaining_before - quantity)
            if order["side"] == Side.BUY.value:
                desired_reserved = (
                    float(order["reserved_cash"]) * remaining_after / remaining_before
                )
                other_frozen = max(
                    0.0,
                    float(account["frozen_cash"]) - float(order["reserved_cash"]),
                )
                resulting_cash = float(account["current_cash"]) + cash_delta
                remaining_reserved_cash = min(
                    desired_reserved,
                    max(0.0, resulting_cash - other_frozen),
                )
                released_cash = float(order["reserved_cash"]) - remaining_reserved_cash
            else:
                remaining_reserved_cash = 0.0
                released_cash = 0.0
            remaining_reserved_quantity = (
                max(0, int(order["reserved_quantity"]) - quantity)
                if order["side"] == Side.SELL.value
                else 0
            )
            if new_status == UserOrderStatus.FILLED.value:
                released_cash += remaining_reserved_cash
                remaining_reserved_cash = 0.0
                if remaining_reserved_quantity and order["side"] == Side.SELL.value:
                    db.execute(
                        """
                        UPDATE user_paper_positions
                        SET reserved_sell_quantity=MAX(0,reserved_sell_quantity-?),updated_at=?
                        WHERE account_id=? AND symbol=?
                        """,
                        (
                            remaining_reserved_quantity,
                            now,
                            account["account_id"],
                            order["symbol"],
                        ),
                    )
                    remaining_reserved_quantity = 0
            db.execute(
                """
                UPDATE user_paper_accounts
                SET current_cash=current_cash+?,realized_pnl=realized_pnl+?,
                    frozen_cash=MAX(0,frozen_cash-?),
                    cumulative_fees=cumulative_fees+?,
                    cumulative_turnover=cumulative_turnover+?,
                    trade_count=trade_count+1,version=version+1,updated_at=?
                WHERE account_id=?
                """,
                (
                    cash_delta,
                    realized_delta,
                    released_cash,
                    total_cost,
                    fill.gross_value,
                    now,
                    account["account_id"],
                ),
            )
            db.execute(
                """
                UPDATE user_paper_orders
                SET filled_quantity=?,status=?,reserved_cash=?,reserved_quantity=?,
                    rejection_reason=NULL,updated_at=?
                WHERE order_id=?
                """,
                (
                    new_filled,
                    new_status,
                    remaining_reserved_cash,
                    remaining_reserved_quantity,
                    now,
                    order_id,
                ),
            )
            self._assert_account_invariants_in_tx(db, account["account_id"])
            event_suffix = "filled" if new_status == "filled" else "partially_filled"
            self._record_order_event_in_tx(
                db,
                order_id,
                account["account_id"],
                event_suffix,
                None,
                {"fill_id": fill_id, "quantity": quantity},
            )
            event_type = (
                "paper_order_filled"
                if new_status == UserOrderStatus.FILLED.value
                else "paper_order_partially_filled"
            )
            enqueue_outbox(
                db,
                event_type=event_type,
                aggregate_type="user_paper_order",
                aggregate_id=order_id,
                payload={
                    "account_id": account["account_id"],
                    "order_id": order_id,
                    "symbol": order["symbol"],
                    "research_run_id": order["research_run_id"],
                    "context_id": order["context_id"],
                    "data_as_of": quote.as_of.isoformat(),
                    "action_type": "view_order",
                    "action_payload": {"order_id": order_id},
                },
                dedup_key=f"{event_type}:{order_id}:{new_filled}",
            )
            return {
                "order": self._order_row(
                    db.execute(
                        "SELECT * FROM user_paper_orders WHERE order_id=?",
                        (order_id,),
                    ).fetchone()
                ),
                "fill": self._fill_row(
                    db.execute(
                        "SELECT * FROM user_paper_fills WHERE fill_id=?",
                        (fill_id,),
                    ).fetchone()
                ),
                "idempotent": False,
            }

    def mark_to_market(
        self,
        *,
        account_id: str,
        snapshot_date: date,
        marks: list[MarketQuote],
        benchmark_quote: MarketQuote | None = None,
        constraints: TradeConstraints | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            existing = db.execute(
                """
                SELECT * FROM user_paper_equity_snapshots
                WHERE account_id=? AND snapshot_date=?
                """,
                (account_id, snapshot_date.isoformat()),
            ).fetchone()
            account = self._require_account_in_tx(db, account_id)
            self._release_matured_in_tx(db, account_id, snapshot_date)
            warnings: list[str] = []
            for quote in marks:
                position = self._position_in_tx(db, account_id, quote.symbol)
                if position is None or int(position["quantity"]) <= 0:
                    continue
                if quote.as_of > snapshot_date:
                    warnings.append(f"{quote.symbol}:future_mark_ignored")
                    continue
                if quote.data_quality.value in {"missing", "stale"}:
                    warnings.append(f"{quote.symbol}:stale_or_missing_mark")
                    continue
                db.execute(
                    """
                    UPDATE user_paper_positions
                    SET latest_price=?,latest_price_at=?,mark_source=?,industry=COALESCE(?,industry),
                        updated_at=?
                    WHERE account_id=? AND symbol=?
                    """,
                    (
                        quote.raw_price,
                        quote.available_at.isoformat() if quote.available_at else None,
                        quote.source,
                        quote.industry,
                        _now(),
                        account_id,
                        quote.symbol,
                    ),
                )
            if benchmark_quote is not None:
                if account["benchmark_start_price"] is None:
                    db.execute(
                        """
                        UPDATE user_paper_accounts
                        SET benchmark_start_price=?,benchmark_start_date=?,updated_at=?
                        WHERE account_id=?
                        """,
                        (
                            benchmark_quote.raw_price,
                            benchmark_quote.as_of.isoformat(),
                            _now(),
                            account_id,
                        ),
                    )
            account = self._require_account_in_tx(db, account_id)
            overview = self._overview_in_tx(db, account)
            self._enqueue_position_risk_in_tx(
                db,
                account_id=account_id,
                snapshot_date=snapshot_date,
                overview=overview,
                constraints=constraints or TradeConstraints(),
            )
            previous = db.execute(
                """
                SELECT equity FROM user_paper_equity_snapshots
                WHERE account_id=? AND snapshot_date<?
                ORDER BY snapshot_date DESC LIMIT 1
                """,
                (account_id, snapshot_date.isoformat()),
            ).fetchone()
            today_pnl = (
                overview["equity"] - float(previous["equity"])
                if previous
                else overview["equity"] - float(account["initial_capital"])
            )
            peak_row = db.execute(
                """SELECT MAX(equity) AS peak FROM user_paper_equity_snapshots
                   WHERE account_id=? AND snapshot_date<?""",
                (account_id, snapshot_date.isoformat()),
            ).fetchone()
            peak = max(
                float(account["initial_capital"]),
                float(peak_row["peak"] or 0),
                overview["equity"],
            )
            drawdown = overview["equity"] / peak - 1 if peak else 0.0
            prior_min = db.execute(
                """
                SELECT MIN(drawdown) AS minimum_drawdown
                FROM user_paper_equity_snapshots
                WHERE account_id=? AND snapshot_date<?
                """,
                (account_id, snapshot_date.isoformat()),
            ).fetchone()
            maximum_drawdown = min(float(prior_min["minimum_drawdown"] or 0), drawdown)
            benchmark_return = None
            benchmark_price = benchmark_quote.raw_price if benchmark_quote else None
            if benchmark_price and account["benchmark_start_price"]:
                benchmark_return = benchmark_price / float(account["benchmark_start_price"]) - 1
            snapshot_id = existing["snapshot_id"] if existing else str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO user_paper_equity_snapshots(
                    snapshot_id,account_id,snapshot_date,cash,market_value,equity,
                    today_pnl,realized_pnl,unrealized_pnl,total_return,drawdown,
                    maximum_drawdown,cumulative_fees,cumulative_turnover,trade_count,
                    benchmark_symbol,benchmark_price,benchmark_return,payload,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,snapshot_date) DO UPDATE SET
                    cash=excluded.cash,market_value=excluded.market_value,
                    equity=excluded.equity,today_pnl=excluded.today_pnl,
                    realized_pnl=excluded.realized_pnl,
                    unrealized_pnl=excluded.unrealized_pnl,
                    total_return=excluded.total_return,drawdown=excluded.drawdown,
                    maximum_drawdown=excluded.maximum_drawdown,
                    cumulative_fees=excluded.cumulative_fees,
                    cumulative_turnover=excluded.cumulative_turnover,
                    trade_count=excluded.trade_count,
                    benchmark_symbol=excluded.benchmark_symbol,
                    benchmark_price=excluded.benchmark_price,
                    benchmark_return=excluded.benchmark_return,
                    payload=excluded.payload
                """,
                (
                    snapshot_id,
                    account_id,
                    snapshot_date.isoformat(),
                    overview["cash"],
                    overview["market_value"],
                    overview["equity"],
                    today_pnl,
                    overview["realized_pnl"],
                    overview["unrealized_pnl"],
                    overview["total_return"],
                    drawdown,
                    maximum_drawdown,
                    overview["cumulative_fees"],
                    overview["cumulative_turnover"],
                    overview["trade_count"],
                    account["benchmark_symbol"],
                    benchmark_price,
                    benchmark_return,
                    json.dumps(
                        {
                            "positions": overview["positions"],
                            "warnings": warnings,
                            "marks": [
                                quote.model_dump(mode="json") for quote in marks
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    _now(),
                ),
            )
            enqueue_outbox(
                db,
                event_type="paper_mark_completed",
                aggregate_type="user_paper_account",
                aggregate_id=account_id,
                payload={
                    "account_id": account_id,
                    "data_as_of": snapshot_date.isoformat(),
                    "content": f"账户总资产 {overview['equity']:.2f}",
                },
                dedup_key=f"paper_mark_completed:{account_id}:{snapshot_date.isoformat()}",
            )
            return self._snapshot_row(
                db.execute(
                    """
                    SELECT * FROM user_paper_equity_snapshots
                    WHERE account_id=? AND snapshot_date=?
                    """,
                    (account_id, snapshot_date.isoformat()),
                ).fetchone()
            )

    def _enqueue_position_risk_in_tx(
        self,
        db: sqlite3.Connection,
        *,
        account_id: str,
        snapshot_date: date,
        overview: dict[str, Any],
        constraints: TradeConstraints,
    ) -> None:
        equity = float(overview["equity"])
        if equity <= 0:
            return
        messages: list[str] = []
        critical = False
        total_exposure = float(overview["market_value"]) / equity
        if total_exposure >= constraints.maximum_total_exposure * 0.9:
            messages.append(
                f"总仓位{total_exposure:.1%}，上限{constraints.maximum_total_exposure:.1%}"
            )
            critical = critical or total_exposure > constraints.maximum_total_exposure
        industries: dict[str, float] = {}
        for position in overview["positions"]:
            weight = float(position["weight"])
            if weight >= constraints.maximum_single_weight * 0.9:
                messages.append(
                    f"{position['symbol']}仓位{weight:.1%}，"
                    f"上限{constraints.maximum_single_weight:.1%}"
                )
                critical = critical or weight > constraints.maximum_single_weight
            industry = str(position.get("industry") or "未分类")
            industries[industry] = industries.get(industry, 0.0) + float(
                position["market_value"]
            )
        for industry, market_value in industries.items():
            weight = market_value / equity
            if weight >= constraints.maximum_industry_weight * 0.9:
                messages.append(
                    f"{industry}行业仓位{weight:.1%}，"
                    f"上限{constraints.maximum_industry_weight:.1%}"
                )
                critical = critical or weight > constraints.maximum_industry_weight
        if not messages:
            return
        enqueue_outbox(
            db,
            event_type="paper_position_risk",
            aggregate_type="user_paper_account",
            aggregate_id=account_id,
            payload={
                "account_id": account_id,
                "severity": "critical" if critical else "warning",
                "content": "；".join(messages),
                "data_as_of": snapshot_date.isoformat(),
                "action_type": "view_positions",
                "action_payload": {"account_id": account_id},
            },
            dedup_key=f"paper_position_risk:{account_id}:{snapshot_date.isoformat()}",
        )

    def overview(self, account_id: str) -> dict[str, Any]:
        with self.connect() as db:
            account = self._require_account_in_tx(db, account_id)
            return self._overview_in_tx(db, account)

    def positions(self, account_id: str, include_closed: bool = False) -> list[dict[str, Any]]:
        if not include_closed:
            return self.overview(account_id)["positions"]
        query = "SELECT * FROM user_paper_positions WHERE account_id=?"
        params: list[Any] = [account_id]
        query += " ORDER BY symbol"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def orders(
        self,
        account_id: str,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM user_paper_orders WHERE account_id=?"
        params: list[Any] = [account_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY requested_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            return [self._order_row(row) for row in db.execute(query, params).fetchall()]

    def order(self, order_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM user_paper_orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
        return self._order_row(row) if row else None

    def order_by_idempotency(
        self,
        account_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM user_paper_orders
                   WHERE account_id=? AND idempotency_key=?""",
                (account_id, idempotency_key),
            ).fetchone()
        return self._order_row(row) if row else None

    def fills(self, account_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM user_paper_fills
                WHERE account_id=? ORDER BY trade_date,created_at LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return [self._fill_row(row) for row in rows]

    def order_events(self, order_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM user_paper_order_events
                   WHERE order_id=? ORDER BY created_at,event_id LIMIT ?""",
                (order_id, max(1, int(limit))),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def snapshots(self, account_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM user_paper_equity_snapshots
                WHERE account_id=? ORDER BY snapshot_date LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return [self._snapshot_row(row) for row in rows]

    def performance(self, account_id: str) -> dict[str, Any]:
        overview = self.overview(account_id)
        curve = self.snapshots(account_id, 100_000)
        latest = curve[-1] if curve else None
        return {
            "account_id": account_id,
            "account_type": AccountType.USER_PAPER.value,
            "evidence_eligible": False,
            "equity": overview["equity"],
            "total_return": overview["total_return"],
            "realized_pnl": overview["realized_pnl"],
            "unrealized_pnl": overview["unrealized_pnl"],
            "maximum_drawdown": latest["maximum_drawdown"] if latest else 0.0,
            "benchmark_symbol": overview["benchmark_symbol"],
            "benchmark_return": latest["benchmark_return"] if latest else None,
            "cumulative_fees": overview["cumulative_fees"],
            "turnover": overview["turnover"],
            "trade_count": overview["trade_count"],
            "curve": [
                {
                    "date": item["snapshot_date"],
                    "equity": item["equity"],
                    "benchmark_return": item["benchmark_return"],
                }
                for item in curve
            ],
        }

    def record_review(
        self,
        account_id: str,
        *,
        order_id: str | None,
        symbol: str | None,
        review_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        review_id = str(uuid.uuid4())
        with self.transaction() as db:
            self._require_account_in_tx(db, account_id)
            db.execute(
                """
                INSERT INTO user_trade_reviews(
                    review_id,account_id,order_id,symbol,review_type,payload,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    account_id,
                    order_id,
                    symbol,
                    review_type,
                    json.dumps(sanitize_for_export(payload), ensure_ascii=False),
                    _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM user_trade_reviews WHERE review_id=?",
                (review_id,),
            ).fetchone()
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def reviews(self, account_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM user_trade_reviews
                WHERE account_id=? ORDER BY created_at DESC LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def _overview_in_tx(
        self,
        db: sqlite3.Connection,
        account: sqlite3.Row,
    ) -> dict[str, Any]:
        rows = db.execute(
            """
            SELECT * FROM user_paper_positions
            WHERE account_id=? AND quantity>0 ORDER BY symbol
            """,
            (account["account_id"],),
        ).fetchall()
        latest_snapshot = db.execute(
            """
            SELECT equity,today_pnl,payload FROM user_paper_equity_snapshots
            WHERE account_id=? ORDER BY snapshot_date DESC LIMIT 1
            """,
            (account["account_id"],),
        ).fetchone()
        snapshot_positions: dict[str, dict[str, Any]] = {}
        if latest_snapshot is not None:
            try:
                snapshot_payload = json.loads(latest_snapshot["payload"])
                snapshot_positions = {
                    str(item["symbol"]): item
                    for item in snapshot_payload.get("positions", [])
                    if isinstance(item, dict) and item.get("symbol")
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot_positions = {}
        positions: list[dict[str, Any]] = []
        market_value = 0.0
        unrealized = 0.0
        for row in rows:
            item = dict(row)
            value = int(row["quantity"]) * float(row["latest_price"])
            item["sellable_quantity"] = max(
                0,
                int(row["quantity"])
                - int(row["frozen_quantity"])
                - int(row["reserved_sell_quantity"]),
            )
            item["market_value"] = value
            item["unrealized_pnl"] = (
                float(row["latest_price"]) - float(row["average_cost"])
            ) * int(row["quantity"])
            cost_basis = float(row["average_cost"]) * int(row["quantity"])
            item["return"] = item["unrealized_pnl"] / cost_basis if cost_basis > 0 else 0.0
            prior = snapshot_positions.get(str(row["symbol"]))
            if prior is None:
                item["today_pnl"] = item["unrealized_pnl"]
            elif (
                int(prior.get("quantity", 0)) == int(row["quantity"])
                and abs(float(prior.get("latest_price", 0)) - float(row["latest_price"])) < 1e-9
            ):
                item["today_pnl"] = float(prior.get("today_pnl", 0.0))
            else:
                item["today_pnl"] = (
                    float(row["latest_price"]) - float(prior.get("latest_price", 0.0))
                ) * int(row["quantity"])
            market_value += value
            unrealized += item["unrealized_pnl"]
            positions.append(item)
        cash = float(account["current_cash"])
        frozen_cash = float(account["frozen_cash"])
        available_cash = cash - frozen_cash
        equity = cash + market_value
        for item in positions:
            item["weight"] = item["market_value"] / equity if equity > 0 else 0.0
        turnover = (
            float(account["cumulative_turnover"]) / float(account["initial_capital"])
            if float(account["initial_capital"]) > 0
            else 0.0
        )
        return {
            **self._account_row(account),
            "cash": cash,
            "frozen_cash": frozen_cash,
            "available_cash": available_cash,
            "market_value": market_value,
            "equity": equity,
            "today_pnl": (
                float(latest_snapshot["today_pnl"])
                if latest_snapshot is not None
                and abs(equity - float(latest_snapshot["equity"])) < 1e-6
                else equity - float(latest_snapshot["equity"])
                if latest_snapshot is not None
                else equity - float(account["initial_capital"])
            ),
            "realized_pnl": float(account["realized_pnl"]),
            "unrealized_pnl": unrealized,
            "total_return": equity / float(account["initial_capital"]) - 1,
            "cumulative_fees": float(account["cumulative_fees"]),
            "cumulative_turnover": float(account["cumulative_turnover"]),
            "turnover": turnover,
            "trade_count": int(account["trade_count"]),
            "positions": positions,
            "evidence_eligible": False,
            "training_eligible": False,
        }

    def _execution_state_in_tx(
        self,
        db: sqlite3.Connection,
        account: sqlite3.Row,
        symbol: str,
        industry: str | None,
    ) -> PortfolioExecutionState:
        overview = self._overview_in_tx(db, account)
        position = next(
            (item for item in overview["positions"] if item["symbol"] == symbol),
            None,
        )
        industry_value = sum(
            float(item["market_value"])
            for item in overview["positions"]
            if industry and item.get("industry") == industry
        )
        return PortfolioExecutionState(
            cash=overview["available_cash"],
            equity=max(overview["equity"], 0.01),
            market_value=overview["market_value"],
            symbol_market_value=float(position["market_value"]) if position else 0.0,
            industry_market_value=industry_value,
            position_quantity=int(position["quantity"]) if position else 0,
            sellable_quantity=int(position["sellable_quantity"]) if position else 0,
        )

    def _release_matured_in_tx(
        self,
        db: sqlite3.Connection,
        account_id: str,
        as_of: date,
    ) -> None:
        rows = db.execute(
            """
            SELECT symbol,frozen_quantity,frozen_until FROM user_paper_positions
            WHERE account_id=? AND frozen_quantity>0 AND frozen_until IS NOT NULL
            """,
            (account_id,),
        ).fetchall()
        for row in rows:
            if date.fromisoformat(row["frozen_until"]) <= as_of:
                db.execute(
                    """
                    UPDATE user_paper_positions
                    SET frozen_quantity=0,frozen_until=NULL,updated_at=?
                    WHERE account_id=? AND symbol=?
                    """,
                    (_now(), account_id, row["symbol"]),
                )
                enqueue_outbox(
                    db,
                    event_type="paper_t1_released",
                    aggregate_type="user_paper_position",
                    aggregate_id=f"{account_id}:{row['symbol']}",
                    payload={
                        "account_id": account_id,
                        "symbol": row["symbol"],
                        "data_as_of": as_of.isoformat(),
                    },
                    dedup_key=f"paper_t1_released:{account_id}:{row['symbol']}:{as_of.isoformat()}",
                )

    def _release_order_reservation_in_tx(
        self,
        db: sqlite3.Connection,
        order: sqlite3.Row,
    ) -> None:
        reserved_cash = max(0.0, float(order["reserved_cash"]))
        reserved_quantity = max(0, int(order["reserved_quantity"]))
        now = _now()
        if reserved_cash:
            db.execute(
                """
                UPDATE user_paper_accounts
                SET frozen_cash=MAX(0,frozen_cash-?),version=version+1,updated_at=?
                WHERE account_id=?
                """,
                (reserved_cash, now, order["account_id"]),
            )
        if reserved_quantity:
            db.execute(
                """
                UPDATE user_paper_positions
                SET reserved_sell_quantity=MAX(0,reserved_sell_quantity-?),updated_at=?
                WHERE account_id=? AND symbol=?
                """,
                (
                    reserved_quantity,
                    now,
                    order["account_id"],
                    order["symbol"],
                ),
            )
            db.execute(
                """
                UPDATE user_paper_accounts
                SET version=version+1,updated_at=? WHERE account_id=?
                """,
                (now, order["account_id"]),
            )
        if reserved_cash or reserved_quantity:
            db.execute(
                """
                UPDATE user_paper_orders
                SET reserved_cash=0,reserved_quantity=0,updated_at=? WHERE order_id=?
                """,
                (now, order["order_id"]),
            )

    def _assert_account_invariants_in_tx(
        self,
        db: sqlite3.Connection,
        account_id: str,
    ) -> None:
        account = self._require_account_in_tx(db, account_id)
        cash = float(account["current_cash"])
        frozen_cash = float(account["frozen_cash"])
        if cash < -1e-8 or frozen_cash < -1e-8 or frozen_cash > cash + 1e-8:
            raise RuntimeError("paper account cash reservation invariant violated")
        positions = db.execute(
            """
            SELECT symbol,quantity,frozen_quantity,reserved_sell_quantity
            FROM user_paper_positions WHERE account_id=?
            """,
            (account_id,),
        ).fetchall()
        for position in positions:
            quantity = int(position["quantity"])
            frozen = int(position["frozen_quantity"])
            reserved = int(position["reserved_sell_quantity"])
            if (
                quantity < 0
                or frozen < 0
                or reserved < 0
                or frozen + reserved > quantity
            ):
                raise RuntimeError(
                    f"paper position reservation invariant violated: {position['symbol']}"
                )
        market_value = sum(
            int(row["quantity"]) * max(0.0, float(row["latest_price"]))
            for row in db.execute(
                """
                SELECT quantity,latest_price FROM user_paper_positions
                WHERE account_id=? AND quantity>0
                """,
                (account_id,),
            ).fetchall()
        )
        if cash + market_value < -1e-8:
            raise RuntimeError("paper account total asset invariant violated")

    def _position_in_tx(
        self,
        db: sqlite3.Connection,
        account_id: str,
        symbol: str,
    ) -> sqlite3.Row | None:
        return db.execute(
            """
            SELECT * FROM user_paper_positions WHERE account_id=? AND symbol=?
            """,
            (account_id, symbol),
        ).fetchone()

    def _require_account_in_tx(
        self,
        db: sqlite3.Connection,
        account_id: str,
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM user_paper_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise ValueError("user paper account not found")
        return row

    def _require_order_in_tx(
        self,
        db: sqlite3.Connection,
        order_id: str,
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM user_paper_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise ValueError("user paper order not found")
        return row

    def _record_order_event_in_tx(
        self,
        db: sqlite3.Connection,
        order_id: str,
        account_id: str,
        event_type: str,
        detail: str | None,
        payload: dict[str, Any],
    ) -> None:
        identity = db.execute(
            "SELECT research_run_id,context_id FROM user_paper_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        research_run_id = identity["research_run_id"] if identity else None
        context_id = identity["context_id"] if identity else None
        audited_payload = {
            **payload,
            "research_run_id": research_run_id,
            "context_id": context_id,
        }
        db.execute(
            """
            INSERT INTO user_paper_order_events(
                event_id,order_id,account_id,research_run_id,context_id,
                event_type,detail,payload,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                order_id,
                account_id,
                research_run_id,
                context_id,
                event_type,
                detail,
                json.dumps(sanitize_for_export(audited_payload), ensure_ascii=False),
                _now(),
            ),
        )

    @staticmethod
    def _account_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence_eligible"] = False
        item["training_eligible"] = False
        item["test_only"] = bool(item.get("test_only", False))
        return item

    @staticmethod
    def _order_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["user_confirmation"] = json.loads(item["user_confirmation"])
        item["payload"] = json.loads(item["payload"])
        item["remaining_quantity"] = max(
            0,
            int(item["requested_quantity"]) - int(item["filled_quantity"]),
        )
        return item

    @staticmethod
    def _fill_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for field in (
            "check_payload",
            "user_request",
            "system_suggestion",
            "final_confirmation",
        ):
            if item.get(field):
                item[field] = json.loads(item[field])
        return item


__all__ = ["UserPaperTradingRepository"]
