"""Wallet registry and reconstructed positions.

SQLite, because this is the one part of the system that is genuinely mutable
state: who we watch, and what we currently believe they hold. Everything
immutable lives in the WORM store instead.

Positions carry their provenance. A position read from a `clearinghouseState`
snapshot is `published`; one carried forward from the fill stream is
`derived`, and its liquidation price is an approximation whose error the map
must disclose.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from nat2.core.clock import now_ns
from nat2.features.liqmath import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address       TEXT PRIMARY KEY,
    seed          TEXT NOT NULL,          -- equity | volume | both
    account_value REAL, vlm_week REAL, vlm_day REAL, pnl_month REAL,
    equity_rank   INTEGER, volume_rank INTEGER,
    first_seen    INTEGER, last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
    address     TEXT NOT NULL,
    coin        TEXT NOT NULL,
    szi         REAL NOT NULL,
    mark        REAL NOT NULL,
    max_leverage REAL NOT NULL,
    margin_type TEXT NOT NULL,
    account_value REAL, maint_margin REAL, isolated_margin REAL,
    liquidation_px REAL,
    source      TEXT NOT NULL,            -- published | derived
    t_ingest    INTEGER NOT NULL,
    PRIMARY KEY (address, coin)
);
CREATE INDEX IF NOT EXISTS positions_coin ON positions(coin);
CREATE TABLE IF NOT EXISTS liquidations (
    tid             INTEGER PRIMARY KEY,   -- one row per trade id, whoever saw it
    t_event         INTEGER NOT NULL,
    coin            TEXT NOT NULL,
    liquidated_user TEXT NOT NULL,
    mark_px         REAL NOT NULL,
    method          TEXT NOT NULL,
    px              REAL NOT NULL,
    sz              REAL NOT NULL,
    observer        TEXT NOT NULL,
    source          TEXT NOT NULL,
    t_ingest        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS liquidations_time ON liquidations(t_event);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started INTEGER, finished INTEGER,
    wallets INTEGER, holders INTEGER, positions INTEGER, errors INTEGER
);
"""


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- wallets ---------------------------------------------------------

    def seed_wallets(self, rows, tags: dict[str, str]) -> int:
        by_address = {r.address: r for r in rows}
        equity_rank = {
            r.address: i
            for i, r in enumerate(sorted(rows, key=lambda r: -r.account_value))
        }
        volume_rank = {
            r.address: i for i, r in enumerate(sorted(rows, key=lambda r: -r.vlm_week))
        }
        ts = now_ns()
        payload = [
            (
                address, tag, row.account_value, row.vlm_week, row.vlm_day, row.pnl_month,
                equity_rank[address], volume_rank[address], ts, ts,
            )
            for address, tag in tags.items()
            if (row := by_address.get(address)) is not None
        ]
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                """INSERT INTO wallets VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(address) DO UPDATE SET
                     seed=excluded.seed, account_value=excluded.account_value,
                     vlm_week=excluded.vlm_week, vlm_day=excluded.vlm_day,
                     pnl_month=excluded.pnl_month, equity_rank=excluded.equity_rank,
                     volume_rank=excluded.volume_rank, last_seen=excluded.last_seen""",
                payload,
            )
        return len(payload)

    def addresses(self, seed: str | None = None, limit: int | None = None) -> list[str]:
        query = "SELECT address FROM wallets"
        params: list = []
        if seed:
            query += " WHERE seed IN (?, 'both')"
            params.append(seed)
        query += " ORDER BY account_value DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        with closing(self._connect()) as conn:
            return [r["address"] for r in conn.execute(query, params)]

    def wallet_count(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT seed, COUNT(*) n FROM wallets GROUP BY seed")
            return {r["seed"]: r["n"] for r in rows}

    # --- positions -------------------------------------------------------

    def replace_positions(self, positions: list[tuple[Position, str]]) -> int:
        ts = now_ns()
        payload = [
            (
                p.address, p.coin, p.szi, p.mark, p.max_leverage, p.margin_type,
                p.account_value, p.maint_margin, p.isolated_margin, p.liquidation_px,
                source, ts,
            )
            for p, source in positions
        ]
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM positions")
            conn.executemany(
                "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", payload
            )
        return len(payload)

    def positions(self, coin: str | None = None) -> list[Position]:
        query = "SELECT * FROM positions"
        params: list = []
        if coin:
            query += " WHERE coin = ?"
            params.append(coin)
        with closing(self._connect()) as conn:
            return [
                Position(
                    address=r["address"], coin=r["coin"], szi=r["szi"], mark=r["mark"],
                    max_leverage=r["max_leverage"], margin_type=r["margin_type"],
                    account_value=r["account_value"], maint_margin=r["maint_margin"],
                    isolated_margin=r["isolated_margin"], liquidation_px=r["liquidation_px"],
                )
                for r in conn.execute(query, params)
            ]

    def positions_ts(self) -> int | None:
        """When the current positions were observed.

        One sweep stamps every row identically, so this is the map's epoch --
        and the cutoff that stops a liquidation from being 'predicted' by a
        map built after it happened.
        """
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(t_ingest) t FROM positions").fetchone()
        return row["t"] if row and row["t"] else None

    # --- liquidations ----------------------------------------------------

    def record_liquidations(self, events) -> int:
        """Insert new events. Existing trade ids are kept, never overwritten."""
        ts = now_ns()
        payload = [
            (e.tid, e.t_event, e.coin, e.liquidated_user, e.mark_px, e.method,
             e.px, e.sz, e.observer, e.source, ts)
            for e in events
        ]
        with closing(self._connect()) as conn, conn:
            before = conn.execute("SELECT COUNT(*) n FROM liquidations").fetchone()["n"]
            conn.executemany(
                "INSERT OR IGNORE INTO liquidations VALUES (?,?,?,?,?,?,?,?,?,?,?)", payload
            )
            after = conn.execute("SELECT COUNT(*) n FROM liquidations").fetchone()["n"]
        return after - before

    def liquidations(self, since_ns: int | None = None):
        from nat2.features.liquidations import LiquidationEvent

        query = "SELECT * FROM liquidations"
        params: list = []
        if since_ns is not None:
            query += " WHERE t_event > ?"
            params.append(since_ns)
        query += " ORDER BY t_event"
        with closing(self._connect()) as conn:
            return [
                LiquidationEvent(
                    tid=r["tid"], t_event=r["t_event"], coin=r["coin"],
                    liquidated_user=r["liquidated_user"], mark_px=r["mark_px"],
                    method=r["method"], px=r["px"], sz=r["sz"],
                    observer=r["observer"], source=r["source"],
                )
                for r in conn.execute(query, params)
            ]

    def position_age_ns(self) -> int | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(t_ingest) t FROM positions").fetchone()
        return now_ns() - row["t"] if row and row["t"] else None

    # --- snapshots -------------------------------------------------------

    def record_snapshot(self, started: int, wallets: int, holders: int,
                        positions: int, errors: int) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO snapshots (started, finished, wallets, holders, positions, errors)"
                " VALUES (?,?,?,?,?,?)",
                (started, now_ns(), wallets, holders, positions, errors),
            )
            return cur.lastrowid

    def last_snapshot(self) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
