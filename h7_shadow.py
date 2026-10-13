#!/usr/bin/env python3
"""Read-only H7 shadow tracker. This module never submits exchange orders.

Input: append-only JSONL candidate signals from the actual H7 strategy adapter.
Market data: Coinbase Exchange public ticker endpoint only; no authentication.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("h7_shadow")
PRODUCT_RE = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def positive_number(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return number


class CoinbasePublicFeed:
    """Public ticker reader. No authentication and no order endpoints."""
    base_url = "https://api.exchange.coinbase.com"

    def quote(self, product_id: str) -> dict[str, Any]:
        if not PRODUCT_RE.fullmatch(product_id):
            raise ValueError("product_id must look like BTC-USD")
        url = f"{self.base_url}/products/{quote(product_id, safe='-')}/ticker"
        request = Request(url, headers={"User-Agent": "H7-shadow-research/1.0"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        bid = positive_number(payload.get("bid"), "bid")
        ask = positive_number(payload.get("ask"), "ask")
        last = positive_number(payload.get("price"), "price")
        if ask < bid:
            raise ValueError(f"invalid crossed quote for {product_id}")
        observed = utc_now()
        return {
            "product_id": product_id,
            "market_time": payload.get("time") or iso(observed),
            "observed_at": iso(observed),
            "bid": bid, "ask": ask, "last": last,
            "spread": ask - bid,
            "spread_bps": ((ask - bid) / ((ask + bid) / 2.0)) * 10000.0,
            "source": "coinbase_exchange_public_ticker",
        }


class ShadowStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL, market_time TEXT NOT NULL,
            observed_at TEXT NOT NULL, bid REAL NOT NULL, ask REAL NOT NULL,
            last REAL NOT NULL, spread REAL NOT NULL, spread_bps REAL NOT NULL,
            source TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_quotes_product_time
            ON quotes(product_id, observed_at);
        CREATE TABLE IF NOT EXISTS trades (
            signal_id TEXT PRIMARY KEY,
            created_at TEXT, received_at TEXT NOT NULL,
            product_id TEXT, side TEXT, strategy_version TEXT, parameter_hash TEXT,
            status TEXT NOT NULL, reason TEXT,
            entry_time TEXT, entry_bid REAL, entry_ask REAL, entry_price REAL,
            stop_price REAL, target_price REAL, notional_quote REAL, quantity REAL,
            risk_quote REAL, hold_minutes REAL, fee_bps REAL, flat_fee_quote REAL,
            exit_time TEXT, exit_bid REAL, exit_ask REAL, exit_price REAL,
            exit_reason TEXT, pnl_gross_quote REAL, fees_quote REAL,
            pnl_net_quote REAL, r_multiple_gross REAL, r_multiple_net REAL
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()

    def seen(self, signal_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM trades WHERE signal_id=?", (signal_id,)
        ).fetchone() is not None

    def quote(self, q: dict[str, Any]) -> None:
        self.db.execute("""
            INSERT INTO quotes(product_id, market_time, observed_at, bid, ask, last,
                               spread, spread_bps, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (q["product_id"], q["market_time"], q["observed_at"], q["bid"],
              q["ask"], q["last"], q["spread"], q["spread_bps"], q["source"]))
        self.db.commit()

    def open_products(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT product_id FROM trades WHERE status='OPEN'"
        ).fetchall()
        return [r["product_id"] for r in rows if r["product_id"]]

    def open_trades(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()

    def freeze_parameters(self, version: str, parameter_hash: str) -> str | None:
        version_row = self.db.execute(
            "SELECT value FROM metadata WHERE key='strategy_version'"
        ).fetchone()
        hash_row = self.db.execute(
            "SELECT value FROM metadata WHERE key='parameter_hash'"
        ).fetchone()
        if version_row is None and hash_row is None:
            self.db.execute("INSERT INTO metadata(key,value) VALUES('strategy_version',?)",
                            (version,))
            self.db.execute("INSERT INTO metadata(key,value) VALUES('parameter_hash',?)",
                            (parameter_hash,))
            self.db.commit()
            return None
        if version_row is None or hash_row is None:
            return "parameter lock is incomplete; manual review required"
        if version_row["value"] != version or hash_row["value"] != parameter_hash:
            return "strategy version or parameter hash changed; frozen run rejected"
        return None

    def reject(self, signal: dict[str, Any], reason: str) -> None:
        sid = str(signal.get("signal_id", "")).strip()
        if not sid or self.seen(sid):
            return
        self.db.execute("""
            INSERT INTO trades(signal_id, created_at, received_at, product_id, side,
                strategy_version, parameter_hash, status, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'REJECTED', ?)
        """, (sid, signal.get("created_at"), iso(), signal.get("product_id"),
              signal.get("side"), signal.get("strategy_version"),
              signal.get("parameter_hash"), reason))
        self.db.commit()

    def open_trade(self, signal: dict[str, Any], q: dict[str, Any],
                   fee_bps: float | None, flat_fee_quote: float) -> None:
        side = signal["side"]
        entry = q["ask"] if side == "buy" else q["bid"]
        notional = float(signal["notional_quote"])
        quantity = notional / entry
        stop_pct = float(signal["stop_pct"])
        target_pct = float(signal["target_pct"])
        stop = entry * (1 - stop_pct) if side == "buy" else entry * (1 + stop_pct)
        target = entry * (1 + target_pct) if side == "buy" else entry * (1 - target_pct)
        risk_quote = notional * stop_pct
        self.db.execute("""
            INSERT INTO trades(
              signal_id, created_at, received_at, product_id, side, strategy_version,
              parameter_hash, status, entry_time, entry_bid, entry_ask, entry_price,
              stop_price, target_price, notional_quote, quantity, risk_quote,
              hold_minutes, fee_bps, flat_fee_quote)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (signal["signal_id"], signal["created_at"], iso(), signal["product_id"],
              side, signal["strategy_version"], signal["parameter_hash"],
              q["observed_at"], q["bid"], q["ask"], entry, stop, target, notional,
              quantity, risk_quote, float(signal["hold_minutes"]), fee_bps,
              flat_fee_quote))
        self.db.commit()

    def monitor(self, q: dict[str, Any]) -> None:
        for trade in self.open_trades():
            if trade["product_id"] != q["product_id"]:
                continue
            side = trade["side"]
            exit_price = q["bid"] if side == "buy" else q["ask"]
            exit_reason = None
            if side == "buy":
                if exit_price <= trade["stop_price"]:
                    exit_reason = "STOP_OBSERVED"
                elif exit_price >= trade["target_price"]:
                    exit_reason = "TARGET_OBSERVED"
            else:
                if exit_price >= trade["stop_price"]:
                    exit_reason = "STOP_OBSERVED"
                elif exit_price <= trade["target_price"]:
                    exit_reason = "TARGET_OBSERVED"
            try:
                elapsed = (parse_time(q["observed_at"]) -
                           parse_time(trade["entry_time"])).total_seconds() / 60
            except ValueError:
                elapsed = 0
            if exit_reason is None and elapsed >= float(trade["hold_minutes"]):
                exit_reason = "TIME_LIMIT"
            if exit_reason is None:
                continue
            gross = ((exit_price - trade["entry_price"]) if side == "buy"
                     else (trade["entry_price"] - exit_price)) * trade["quantity"]
            fees = net = r_net = None
            if trade["fee_bps"] is not None:
                entry_fee = trade["notional_quote"] * trade["fee_bps"] / 10000.0
                exit_fee = exit_price * trade["quantity"] * trade["fee_bps"] / 10000.0
                fees = entry_fee + exit_fee + trade["flat_fee_quote"] * 2.0
                net = gross - fees
                r_net = net / trade["risk_quote"] if trade["risk_quote"] else None
            r_gross = gross / trade["risk_quote"] if trade["risk_quote"] else None
            self.db.execute("""
                UPDATE trades SET status='CLOSED', exit_time=?, exit_bid=?, exit_ask=?,
                  exit_price=?, exit_reason=?, pnl_gross_quote=?, fees_quote=?,
                  pnl_net_quote=?, r_multiple_gross=?, r_multiple_net=?
                WHERE signal_id=?
            """, (q["observed_at"], q["bid"], q["ask"], exit_price, exit_reason,
                  gross, fees, net, r_gross, r_net, trade["signal_id"]))
        self.db.commit()

    def summary(self) -> dict[str, Any]:
        counts = {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) AS n FROM trades GROUP BY status").fetchall()}
        closed = self.db.execute("""
            SELECT COUNT(*) AS n, SUM(pnl_gross_quote) AS gross,
                   SUM(pnl_net_quote) AS net, SUM(r_multiple_gross) AS sum_r
            FROM trades WHERE status='CLOSED'
        """).fetchone()
        missing = self.db.execute("""
            SELECT COUNT(*) AS n FROM trades
            WHERE status='CLOSED' AND pnl_net_quote IS NULL
        """).fetchone()["n"]
        return {"counts": counts, "closed": dict(closed), "fee_model_missing": missing}

    def close(self) -> None:
        self.db.close()


def read_new_signals(path: str, store: ShadowStore) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()
        return []
    out = []
    with file_path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("each JSONL line must be an object")
                sid = str(item.get("signal_id", "")).strip()
                if not sid:
                    LOG.error("line %s ignored: signal_id is required", line_no)
                    continue
                if not store.seen(sid) and all(x.get("signal_id") != sid for x in out):
                    out.append(item)
            except (json.JSONDecodeError, ValueError) as exc:
                LOG.error("signals line %s ignored: %s", line_no, exc)
    return out


def validate_signal(s: dict[str, Any], max_age_seconds: int) -> str | None:
    required = ("signal_id", "created_at", "product_id", "side", "strategy_version",
                "parameter_hash", "stop_pct", "target_pct", "notional_quote", "hold_minutes")
    for key in required:
        if s.get(key) is None or str(s.get(key)).strip() == "":
            return f"missing required field: {key}"
    if s["side"] not in ("buy", "sell"):
        return "side must be buy or sell"
    if not PRODUCT_RE.fullmatch(str(s["product_id"])):
        return "product_id must look like BTC-USD"
    try:
        age = (utc_now() - parse_time(str(s["created_at"]))).total_seconds()
        if age < -30:
            return "signal timestamp is in the future"
        if age > max_age_seconds:
            return "signal is stale"
        stop_pct = positive_number(s["stop_pct"], "stop_pct")
        target_pct = positive_number(s["target_pct"], "target_pct")
        positive_number(s["notional_quote"], "notional_quote")
        hold = positive_number(s["hold_minutes"], "hold_minutes")
        if stop_pct >= 1 or target_pct >= 1:
            return "stop_pct and target_pct must be fractions between 0 and 1"
        if hold > 10080:
            return "hold_minutes cannot exceed seven days"
    except (TypeError, ValueError, OverflowError) as exc:
        return str(exc)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="H7 shadow tracker: public quotes and simulated positions; never sends orders.")
    parser.add_argument("--signals", default="data/h7_signals.jsonl",
                        help="append-only JSONL produced by the H7 strategy adapter")
    parser.add_argument("--db", default="data/h7_shadow.sqlite", help="SQLite output database")
    parser.add_argument("--symbols", default="BTC-USD",
                        help="comma-separated Coinbase product IDs, e.g. BTC-USD,ETH-USD")
    parser.add_argument("--poll-seconds", type=float, default=30,
                        help="quote polling interval, minimum 5 seconds")
    parser.add_argument("--max-signal-age-seconds", type=int, default=300)
    parser.add_argument("--fee-bps", type=float, default=None,
                        help="estimated fee in basis points per fill; omit = net PnL unavailable")
    parser.add_argument("--flat-fee-quote", type=float, default=0.0,
                        help="extra quote-currency fee per fill")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args()
    if args.poll_seconds < 5:
        parser.error("--poll-seconds must be at least 5")
    if args.fee_bps is not None and args.fee_bps < 0:
        parser.error("--fee-bps cannot be negative")
    if args.flat_fee_quote < 0:
        parser.error("--flat-fee-quote cannot be negative")
    watchlist = {s.strip() for s in args.symbols.split(",") if s.strip()}
    if not watchlist or any(not PRODUCT_RE.fullmatch(s) for s in watchlist):
        parser.error("each --symbols value must look like BTC-USD")
    store = ShadowStore(args.db)
    feed = CoinbasePublicFeed()
    LOG.info("SHADOW ONLY | LIVE_TRADING=DISABLED | signals=%s | db=%s",
             args.signals, args.db)
    try:
        while True:
            signals = read_new_signals(args.signals, store)
            candidates = {str(s.get("product_id")) for s in signals
                          if PRODUCT_RE.fullmatch(str(s.get("product_id", "")))}
            products = watchlist | candidates | set(store.open_products())
            quotes: dict[str, dict[str, Any]] = {}
            for product in sorted(products):
                try:
                    q = feed.quote(product)
                    store.quote(q)
                    quotes[product] = q
                    LOG.info("%s bid=%.8f ask=%.8f spread=%.2f bps",
                             product, q["bid"], q["ask"], q["spread_bps"])
                except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                    LOG.error("quote unavailable for %s: %s", product, exc)
            for signal in signals:
                sid = str(signal.get("signal_id", "")).strip()
                if not sid or store.seen(sid):
                    continue
                reason = validate_signal(signal, args.max_signal_age_seconds)
                if reason:
                    store.reject(signal, reason)
                    LOG.warning("signal %s rejected: %s", sid, reason)
                    continue
                lock_error = store.freeze_parameters(
                    str(signal["strategy_version"]), str(signal["parameter_hash"]))
                if lock_error:
                    store.reject(signal, lock_error)
                    LOG.warning("signal %s rejected: %s", sid, lock_error)
                    continue
                q = quotes.get(str(signal["product_id"]))
                if q is None:
                    LOG.warning("signal %s pending: no current quote; will retry", sid)
                    continue
                store.open_trade(signal, q, args.fee_bps, args.flat_fee_quote)
                LOG.info("signal %s recorded as SHADOW OPEN at %.8f; no order sent",
                         sid, q["ask"] if signal["side"] == "buy" else q["bid"])
            for q in quotes.values():
                store.monitor(q)
            LOG.info("SUMMARY %s", json.dumps(store.summary(), sort_keys=True))
            if args.once:
                break
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        LOG.info("Stopped; shadow database retained.")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
