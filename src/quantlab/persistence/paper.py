from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from quantlab.backtest import calculate_equity_metrics


class PaperTradingRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    account_type TEXT NOT NULL DEFAULT 'system_shadow',
                    evidence_eligible INTEGER NOT NULL DEFAULT 1,
                    label TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    signal_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reference_price REAL,
                    target_weight REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    filled_date TEXT,
                    fill_price REAL,
                    fees REAL,
                    rejected_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id,signal_date,symbol,side,strategy)
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    order_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fees REAL NOT NULL,
                    gross_value REAL NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS paper_snapshots (
                    account_id TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    equity REAL NOT NULL,
                    drawdown REAL NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(account_id,snapshot_date)
                );
                CREATE TABLE IF NOT EXISTS paper_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_paper_orders_pending
                  ON paper_orders(status,signal_date,account_id);
                CREATE INDEX IF NOT EXISTS idx_paper_trades_account
                  ON paper_trades(account_id,trade_date,id);
            """)
            self._ensure_column(
                db,
                "paper_accounts",
                "account_type",
                "TEXT NOT NULL DEFAULT 'system_shadow'",
            )
            self._ensure_column(
                db,
                "paper_accounts",
                "evidence_eligible",
                "INTEGER NOT NULL DEFAULT 1",
            )

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def ensure_account(
        self,
        account_id: str,
        label: str,
        policy: str,
        initial_capital: float = 100_000.0,
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO paper_accounts(
                    account_id,account_type,evidence_eligible,label,policy,initial_capital
                )
                VALUES(?,'system_shadow',1,?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    account_type='system_shadow',evidence_eligible=1,
                    label=excluded.label,policy=excluded.policy,updated_at=CURRENT_TIMESTAMP
                """,
                (account_id, label, policy, initial_capital),
            )
            row = db.execute(
                "SELECT * FROM paper_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
        return dict(row)

    def accounts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM paper_accounts ORDER BY account_id").fetchall()
        return [
            {
                **dict(row),
                "account_type": "system_shadow",
                "evidence_eligible": True,
            }
            for row in rows
        ]

    def queue_order(
        self,
        *,
        account_id: str,
        symbol: str,
        name: str,
        strategy: str,
        side: str,
        quantity: int,
        signal_date: date,
        reference_price: float,
        target_weight: float,
        payload: dict[str, Any] | None = None,
    ) -> int:
        if side not in {"buy", "sell"} or quantity <= 0:
            raise ValueError("paper order must have a valid side and positive quantity")
        with self.connect() as db:
            existing = db.execute(
                """
                SELECT id,status FROM paper_orders
                WHERE account_id=? AND signal_date=? AND symbol=? AND side=? AND strategy=?
                """,
                (account_id, signal_date.isoformat(), symbol, side, strategy),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = db.execute(
                """
                INSERT INTO paper_orders(
                    account_id,symbol,name,strategy,side,quantity,signal_date,
                    reference_price,target_weight,payload
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    account_id,
                    symbol,
                    name,
                    strategy,
                    side,
                    quantity,
                    signal_date.isoformat(),
                    reference_price,
                    target_weight,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def pending_orders(self, account_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_orders WHERE status='pending'"
        params: list[Any] = []
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        query += " ORDER BY signal_date,id"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._order_row(row) for row in rows]

    def cancel_pending(
        self, account_id: str, symbol: str, signal_date: date, except_side: str | None = None
    ) -> None:
        query = """
            UPDATE paper_orders SET status='cancelled',rejected_reason='superseded target',
                updated_at=CURRENT_TIMESTAMP
            WHERE account_id=? AND symbol=? AND signal_date=? AND status='pending'
        """
        params: list[Any] = [account_id, symbol, signal_date.isoformat()]
        if except_side:
            query += " AND side<>?"
            params.append(except_side)
        with self.connect() as db:
            db.execute(query, params)

    def orders(self, account_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_orders"
        params: list[Any] = []
        if account_id:
            query += " WHERE account_id=?"
            params.append(account_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._order_row(row) for row in rows]

    def fill_order(
        self,
        order_id: int,
        *,
        trade_date: date,
        price: float,
        fees: float,
        gross_value: float,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as db:
            order = db.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()
            if order is None or order["status"] != "pending":
                raise ValueError("paper order is not pending")
            cursor = db.execute(
                """
                INSERT INTO paper_trades(
                    account_id,order_id,symbol,side,quantity,price,fees,
                    gross_value,trade_date,payload
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order["account_id"],
                    order_id,
                    order["symbol"],
                    order["side"],
                    order["quantity"],
                    price,
                    fees,
                    gross_value,
                    trade_date.isoformat(),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            db.execute(
                """
                UPDATE paper_orders SET status='filled',filled_date=?,fill_price=?,fees=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (trade_date.isoformat(), price, fees, order_id),
            )
            return int(cursor.lastrowid)

    def reject_order(self, order_id: int, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE paper_orders SET status='rejected',rejected_reason=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'
                """,
                (reason, order_id),
            )

    def trades(self, account_id: str, limit: int = 100_000) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM paper_trades WHERE account_id=?
                ORDER BY trade_date,id LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def overview(
        self, account_id: str, marks: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        account = next((item for item in self.accounts() if item["account_id"] == account_id), None)
        if account is None:
            raise ValueError(f"paper account not found: {account_id}")
        cash = float(account["initial_capital"])
        realized_pnl = 0.0
        positions: dict[str, dict[str, Any]] = {}
        for trade in self.trades(account_id):
            position = positions.setdefault(
                trade["symbol"],
                {
                    "symbol": trade["symbol"],
                    "quantity": 0,
                    "average_cost": 0.0,
                    "last_trade_price": 0.0,
                },
            )
            quantity = int(trade["quantity"])
            price = float(trade["price"])
            fees = float(trade["fees"])
            if trade["side"] == "buy":
                total = float(trade["gross_value"]) + fees
                old_cost = position["average_cost"] * position["quantity"]
                position["quantity"] += quantity
                position["average_cost"] = (old_cost + total) / position["quantity"]
                cash -= total
            else:
                proceeds = float(trade["gross_value"]) - fees
                realized_pnl += proceeds - position["average_cost"] * quantity
                position["quantity"] -= quantity
                cash += proceeds
            position["last_trade_price"] = price

        active = []
        for position in positions.values():
            if position["quantity"] <= 0:
                continue
            mark = (marks or {}).get(position["symbol"], {})
            last_price = float(mark.get("price") or position["last_trade_price"])
            market_value = position["quantity"] * last_price
            unrealized = (last_price - position["average_cost"]) * position["quantity"]
            active.append(
                {
                    **position,
                    "last_price": last_price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized,
                    "mark_date": mark.get("as_of"),
                    "mark_source": mark.get("source", "last_paper_trade"),
                }
            )
        market_value = sum(item["market_value"] for item in active)
        equity = cash + market_value
        for position in active:
            position["weight"] = position["market_value"] / equity if equity > 0 else 0.0
        return {
            **account,
            "cash": cash,
            "market_value": market_value,
            "equity": equity,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": sum(item["unrealized_pnl"] for item in active),
            "total_return": equity / float(account["initial_capital"]) - 1,
            "positions": active,
            "pending_orders": self.pending_orders(account_id),
        }

    def save_snapshot(self, account_id: str, snapshot_date: date, overview: dict[str, Any]) -> None:
        previous = self.snapshots(account_id, 100_000)
        prior_peak = max([float(item["equity"]) for item in previous] + [float(overview["equity"])])
        drawdown = float(overview["equity"]) / prior_peak - 1 if prior_peak else 0.0
        payload = {
            "positions": overview["positions"],
            "realized_pnl": overview["realized_pnl"],
            "unrealized_pnl": overview["unrealized_pnl"],
            "pending_orders": overview["pending_orders"],
        }
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO paper_snapshots(
                    account_id,snapshot_date,cash,market_value,equity,drawdown,payload
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(account_id,snapshot_date) DO NOTHING
                """,
                (
                    account_id,
                    snapshot_date.isoformat(),
                    overview["cash"],
                    overview["market_value"],
                    overview["equity"],
                    drawdown,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def snapshots(self, account_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM paper_snapshots WHERE account_id=?
                ORDER BY snapshot_date LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def scorecard(self) -> dict[str, Any]:
        rows = []
        curves = {}
        for account in self.accounts():
            snapshots = self.snapshots(account["account_id"], 100_000)
            curve = [
                (date.fromisoformat(item["snapshot_date"]), float(item["equity"]))
                for item in snapshots
            ]
            metrics = calculate_equity_metrics(curve, len(self.trades(account["account_id"])))
            latest = snapshots[-1] if snapshots else None
            rows.append(
                {
                    **account,
                    **metrics,
                    "snapshots": len(snapshots),
                    "latest_equity": float(latest["equity"])
                    if latest
                    else account["initial_capital"],
                    "latest_drawdown": float(latest["drawdown"]) if latest else 0.0,
                    "pending_orders": len(self.pending_orders(account["account_id"])),
                }
            )
            curves[account["account_id"]] = [
                {"date": day.isoformat(), "equity": equity} for day, equity in curve
            ]
        return {"accounts": rows, "curves": curves, "latest_run": self.latest_run()}

    def record_run(self, as_of: date, status: str, payload: dict[str, Any]) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO paper_runs(as_of,status,payload) VALUES(?,?,?)",
                (as_of.isoformat(), status, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def latest_run(self, run_type: str | None = None) -> dict[str, Any] | None:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM paper_runs ORDER BY id DESC").fetchall()
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            if run_type is None or item["payload"].get("run_type") == run_type:
                return item
        return None

    @staticmethod
    def _order_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item
