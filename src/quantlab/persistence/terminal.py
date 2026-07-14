from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path


class TerminalRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT 'default',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    score REAL NOT NULL,
                    action TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    condition_type TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS portfolio_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    capital REAL NOT NULL,
                    risk_profile TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS manual_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fees REAL NOT NULL DEFAULT 0,
                    trade_date TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS portfolio_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS strategy_validations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    train_days INTEGER NOT NULL,
                    test_days INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS validation_consumptions (
                    consumption_key TEXT PRIMARY KEY,
                    protocol_hash TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    consumed_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS llm_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suite TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS candidate_tournaments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS stock_discovery_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def list_watchlist(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT symbol,name,group_name,notes,created_at,updated_at FROM watchlist "
                "ORDER BY group_name,symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_watchlist(self, symbol: str, name: str, group_name: str, notes: str):
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO watchlist(symbol,name,group_name,notes) VALUES(?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name,group_name=excluded.group_name,notes=excluded.notes,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (symbol, name, group_name, notes),
            )

    def remove_watchlist(self, symbol: str) -> bool:
        with self.connect() as db:
            result = db.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
        return result.rowcount > 0

    def watchlist_groups(self) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT group_name FROM watchlist ORDER BY group_name"
            ).fetchall()
        return [row[0] for row in rows]

    def record_signal(
        self, symbol: str, strategy: str, score: float, action: str, as_of: date, payload: dict
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO signals(symbol,strategy,score,action,as_of,payload)
                VALUES(?,?,?,?,?,?)
                """,
                (symbol, strategy, score, action, as_of.isoformat(), json.dumps(payload)),
            )
            return int(cursor.lastrowid)

    def latest_signals(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id,symbol,strategy,score,action,as_of,payload,created_at
                FROM signals ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            output.append(item)
        return output

    def add_alert(self, symbol: str, condition_type: str, threshold: float) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO alerts(symbol,condition_type,threshold) VALUES(?,?,?)",
                (symbol, condition_type, threshold),
            )
            return int(cursor.lastrowid)

    def list_alerts(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,symbol,condition_type,threshold,active,created_at "
                "FROM alerts ORDER BY id DESC"
            ).fetchall()
        return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def remove_alert(self, alert_id: int) -> bool:
        with self.connect() as db:
            result = db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        return result.rowcount > 0

    def portfolio_settings(self, default_capital: float = 100_000.0) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT capital,risk_profile,updated_at FROM portfolio_settings WHERE id=1"
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO portfolio_settings(id,capital,risk_profile) VALUES(1,?,?)",
                    (default_capital, "balanced"),
                )
                return {
                    "capital": default_capital,
                    "risk_profile": "balanced",
                    "updated_at": None,
                }
        return dict(row)

    def set_capital(self, capital: float):
        settings = self.portfolio_settings(capital)
        with self.connect() as db:
            db.execute(
                "UPDATE portfolio_settings SET capital=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",
                (capital,),
            )
        settings["capital"] = capital
        return settings

    def set_risk_profile(self, profile: str):
        settings = self.portfolio_settings()
        with self.connect() as db:
            db.execute(
                "UPDATE portfolio_settings SET risk_profile=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",
                (profile,),
            )
        settings["risk_profile"] = profile
        return settings

    def record_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        fees: float,
        trade_date: date,
        notes: str = "",
    ) -> int:
        if side == "sell":
            available = self._position_quantities().get(symbol, 0)
            if quantity > available:
                raise ValueError(f"cannot sell {quantity}; recorded position is {available}")
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO manual_trades(symbol,side,quantity,price,fees,trade_date,notes)
                VALUES(?,?,?,?,?,?,?)
                """,
                (symbol, side, quantity, price, fees, trade_date.isoformat(), notes),
            )
            return int(cursor.lastrowid)

    def trades(self, limit: int = 200) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id,symbol,side,quantity,price,fees,trade_date,notes,created_at
                FROM manual_trades ORDER BY trade_date,id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def portfolio_overview(self, default_capital: float = 100_000.0) -> dict:
        settings = self.portfolio_settings(default_capital)
        cash = float(settings["capital"])
        positions: dict[str, dict] = {}
        for trade in self.trades(100_000):
            position = positions.setdefault(
                trade["symbol"],
                {"symbol": trade["symbol"], "quantity": 0, "average_cost": 0.0, "last_price": 0.0},
            )
            if trade["side"] == "buy":
                old_cost = position["average_cost"] * position["quantity"]
                gross = trade["quantity"] * trade["price"]
                position["quantity"] += trade["quantity"]
                position["average_cost"] = (old_cost + gross + trade["fees"]) / position["quantity"]
                cash -= gross + trade["fees"]
            else:
                position["quantity"] -= trade["quantity"]
                cash += trade["quantity"] * trade["price"] - trade["fees"]
            position["last_price"] = trade["price"]
        active = []
        for position in positions.values():
            if position["quantity"] <= 0:
                continue
            position["market_value"] = position["quantity"] * position["last_price"]
            position["unrealized_pnl"] = (
                position["last_price"] - position["average_cost"]
            ) * position["quantity"]
            position["mark_source"] = "last_recorded_trade"
            active.append(position)
        market_value = sum(item["market_value"] for item in active)
        equity = cash + market_value
        for item in active:
            item["weight"] = item["market_value"] / equity if equity > 0 else 0.0
        return {
            **settings,
            "cash": cash,
            "market_value": market_value,
            "equity": equity,
            "positions": active,
            "pricing_warning": "positions use last recorded trade price until live marks are refreshed",
        }

    def _position_quantities(self) -> dict[str, int]:
        quantities: dict[str, int] = {}
        for trade in self.trades(100_000):
            direction = 1 if trade["side"] == "buy" else -1
            quantities[trade["symbol"]] = (
                quantities.get(trade["symbol"], 0) + direction * trade["quantity"]
            )
        return quantities

    def record_scheduler_run(self, task: str, status: str, payload: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO scheduler_runs(task,status,payload) VALUES(?,?,?)",
                (task, status, json.dumps(payload)),
            )

    def scheduler_status(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,task,status,payload,created_at FROM scheduler_runs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def save_portfolio_plan(self, as_of: date, payload: dict) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO portfolio_plans(as_of,payload) VALUES(?,?)",
                (as_of.isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def latest_portfolio_plan(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,as_of,payload,created_at FROM portfolio_plans ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        output = json.loads(row["payload"])
        output["plan_id"] = int(row["id"])
        output["created_at"] = row["created_at"]
        return output

    def portfolio_plans(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,as_of,payload,created_at FROM portfolio_plans ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = json.loads(row["payload"])
            item["plan_id"] = int(row["id"])
            item["created_at"] = row["created_at"]
            output.append(item)
        return output

    def save_strategy_validation(
        self,
        strategy: str,
        start: date,
        end: date,
        train_days: int,
        test_days: int,
        payload: dict,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO strategy_validations(
                    strategy,start_date,end_date,train_days,test_days,payload
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    strategy,
                    start.isoformat(),
                    end.isoformat(),
                    train_days,
                    test_days,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def claim_validation_once(
        self,
        consumption_key: str,
        protocol_hash: str,
        stage: str,
        payload: dict | None = None,
    ) -> bool:
        try:
            with self.connect() as db:
                db.execute(
                    """
                    INSERT INTO validation_consumptions(
                        consumption_key,protocol_hash,stage,payload
                    ) VALUES(?,?,?,?)
                    """,
                    (
                        consumption_key,
                        protocol_hash,
                        stage,
                        json.dumps(payload or {}, ensure_ascii=False),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def validation_consumption(self, consumption_key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT consumption_key,protocol_hash,stage,payload,consumed_at
                FROM validation_consumptions WHERE consumption_key=?
                """,
                (consumption_key,),
            ).fetchone()
        if row is None:
            return None
        output = dict(row)
        output["payload"] = json.loads(output["payload"])
        return output

    def latest_strategy_validation(self, strategy: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT id,strategy,start_date,end_date,train_days,test_days,payload,created_at
                FROM strategy_validations WHERE strategy=? ORDER BY id DESC LIMIT 1
                """,
                (strategy,),
            ).fetchone()
        if row is None:
            return None
        output = json.loads(row["payload"])
        output.update(
            {
                "validation_id": int(row["id"]),
                "strategy": row["strategy"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "train_days": int(row["train_days"]),
                "test_days": int(row["test_days"]),
                "created_at": row["created_at"],
            }
        )
        return output

    def save_llm_evaluation(self, suite: str, payload: dict) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO llm_evaluations(suite,payload) VALUES(?,?)",
                (suite, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def llm_evaluations(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,suite,payload,created_at FROM llm_evaluations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = json.loads(row["payload"])
            item["evaluation_id"] = int(row["id"])
            item["created_at"] = row["created_at"]
            output.append(item)
        return output

    def save_candidate_tournament(self, as_of: date, payload: dict) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO candidate_tournaments(as_of,payload) VALUES(?,?)",
                (as_of.isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def candidate_tournaments(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,as_of,payload,created_at FROM candidate_tournaments "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            payload = json.loads(row["payload"])
            settled_horizons = sorted(
                int(horizon)
                for horizon, result in payload.get("settlement", {}).items()
                if result.get("status") == "settled"
            )
            unscorable_horizons = sorted(
                int(horizon)
                for horizon, result in payload.get("settlement", {}).items()
                if result.get("status") == "not_comparable"
            )
            terminal_horizons = set(settled_horizons + unscorable_horizons)
            output.append(
                {
                    "tournament_id": int(row["id"]),
                    "as_of": row["as_of"],
                    "candidate_count": len(payload.get("candidates", [])),
                    "shortlist_count": len(payload.get("diversified_shortlist", [])),
                    "settled_horizons": settled_horizons,
                    "unscorable_horizons": unscorable_horizons,
                    "settlement_status": (
                        "settled"
                        if {5, 20}.issubset(set(settled_horizons))
                        else "not_comparable"
                        if {5, 20}.issubset(terminal_horizons) and not settled_horizons
                        else "partial"
                        if terminal_horizons
                        else "pending"
                    ),
                    "worst_scenario": payload.get("stress_test", {}).get("worst_scenario"),
                    "created_at": row["created_at"],
                }
            )
        return output

    def candidate_tournament(self, tournament_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,as_of,payload,created_at FROM candidate_tournaments WHERE id=?",
                (tournament_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["tournament_id"] = int(row["id"])
        payload["created_at"] = row["created_at"]
        return payload

    def candidate_tournament_records(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,as_of,payload,created_at FROM candidate_tournaments "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["tournament_id"] = int(row["id"])
            payload["created_at"] = row["created_at"]
            output.append(payload)
        return output

    def update_candidate_tournament(self, tournament_id: int, payload: dict) -> bool:
        stored = dict(payload)
        stored.pop("tournament_id", None)
        stored.pop("created_at", None)
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE candidate_tournaments SET payload=? WHERE id=?",
                (json.dumps(stored, ensure_ascii=False), tournament_id),
            )
            return cursor.rowcount > 0

    def save_stock_discovery(self, run_type: str, as_of: date, payload: dict) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO stock_discovery_runs(run_type,as_of,payload) VALUES(?,?,?)",
                (run_type, as_of.isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def stock_discovery_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,run_type,as_of,payload,created_at FROM stock_discovery_runs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            payload = json.loads(row["payload"])
            output.append(
                {
                    "discovery_id": int(row["id"]),
                    "run_type": row["run_type"],
                    "as_of": row["as_of"],
                    "candidate_count": len(payload.get("candidates", [])),
                    "shortlist_count": len(payload.get("diversified_shortlist", [])),
                    "created_at": row["created_at"],
                }
            )
        return output

    def stock_discovery(self, discovery_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id,run_type,as_of,payload,created_at FROM stock_discovery_runs WHERE id=?",
                (discovery_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["discovery_id"] = int(row["id"])
        payload["run_type"] = row["run_type"]
        payload["created_at"] = row["created_at"]
        return payload
