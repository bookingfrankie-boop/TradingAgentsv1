"""HORMOZI Coinbase market monitor + paper-trading engine. No live orders."""
from __future__ import annotations
import argparse, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "hormozi-coinbase-paper-1.0.0"
BASE = "https://api.coinbase.com/api/v3/brokerage/market"
SYMBOLS = ("BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR")
STARTING_EUR, RESERVE_EUR, MAX_POSITION_EUR = 10.715311, 8.50, 2.00
DAILY_DD_LIMIT, MAX_LOSSES = 0.02, 2
STOP_PCT, TARGET_PCT, TAKER_FEE, SLIPPAGE_BPS = 0.04, 0.12, 0.005, 5.0

class MarketDataError(RuntimeError):
    pass

class UnsafeLiveModeError(RuntimeError):
    pass

@dataclass(frozen=True)
class Candle:
    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "Candle":
        try:
            start = int(float(row["start"]))
            vals = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError("Malformed Coinbase candle") from exc
        if start <= 0 or any(not math.isfinite(v) for v in vals):
            raise MarketDataError("Non-finite candle")
        op, hi, lo, close, volume = vals
        if min(op, hi, lo, close) <= 0 or hi < lo or volume < 0:
            raise MarketDataError("Invalid OHLCV candle")
        return cls(start, op, hi, lo, close, volume)

def http_json(url: str, timeout: int = 12) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": f"HORMOZI-Paper/{VERSION}"}
    error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise MarketDataError(f"Coinbase HTTP {response.status}")
                data = json.loads(response.read().decode("utf-8"))
                if not isinstance(data, dict):
                    raise MarketDataError("Unexpected Coinbase response shape")
                return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, MarketDataError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(0.4 * (2 ** attempt))
    raise MarketDataError(f"Public API failed: {error}") from error

def fetch_product(product_id: str) -> dict[str, Any]:
    url = f"{BASE}/products/{urllib.parse.quote(product_id, safe='-')}"
    product = http_json(url)
    try:
        price = float(product["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError(f"Missing price for {product_id}") from exc
    if product.get("product_id") not in (None, product_id) or not math.isfinite(price) or price <= 0:
        raise MarketDataError(f"Invalid product data for {product_id}")
    product["price"] = price
    return product

def fetch_candles(product_id: str, granularity: str, limit: int, now_ts: int | None = None) -> list[Candle]:
    if granularity not in {"ONE_HOUR", "ONE_DAY"}:
        raise ValueError("Only ONE_HOUR and ONE_DAY are supported")
    if not 1 <= int(limit) <= 350:
        raise ValueError("limit must be 1..350")
    duration = 3600 if granularity == "ONE_HOUR" else 86400
    end = int(time.time()) if now_ts is None else int(now_ts)
    query = urllib.parse.urlencode({
        "start": str(end - duration * (int(limit) + 2)), "end": str(end),
        "granularity": granularity, "limit": int(limit),
    })
    url = f"{BASE}/products/{urllib.parse.quote(product_id, safe='-')}/candles?{query}"
    rows = http_json(url).get("candles")
    if not isinstance(rows, list):
        raise MarketDataError(f"Missing candles for {product_id}")
    candles = [Candle.from_api(row) for row in rows if isinstance(row, dict)]
    return sorted(candles, key=lambda c: c.start)

def sma(values: list[float], n: int) -> float:
    if n < 1 or len(values) < n:
        raise ValueError(f"Need {n} values for SMA")
    return sum(values[-n:]) / n

def net_reward_risk(price: float) -> float:
    slip = SLIPPAGE_BPS / 10000
    cost = price * (1 + slip) * (1 + TAKER_FEE)
    stop = price * (1 - STOP_PCT) * (1 - slip) * (1 - TAKER_FEE)
    target = price * (1 + TARGET_PCT) * (1 - slip) * (1 - TAKER_FEE)
    loss, reward = cost - stop, target - cost
    return reward / loss if loss > 0 and reward > 0 else 0.0

def evaluate_signal(product_id: str, price: float, hourly: list[Candle], daily: list[Candle], now_ts: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now_ts is None else int(now_ts)
    h = [c for c in hourly if c.start + 3600 <= now]
    d = [c for c in daily if c.start + 86400 <= now]
    result = {"product_id": product_id, "price": round(price, 8), "signal": "WAIT"}
    if len(h) < 26 or len(d) < 11:
        return {**result, "reason": "Insufficient completed candles", "daily_trend": "unknown"}
    if now - h[-1].start - 3600 > 7200 or now - d[-1].start - 86400 > 172800:
        return {**result, "reason": "Stale candle data", "daily_trend": "unknown"}
    dc = [c.close for c in d]
    d5, d10, prior_d5 = sma(dc, 5), sma(dc, 10), sma(dc[:-1], 5)
    daily_ok = dc[-1] > d5 and d5 > d10 and d5 > prior_d5
    hc = [c.close for c in h]
    h6, prior_h6 = sma(hc, 6), sma(hc[:-1], 6)
    vol_base = sum(c.volume for c in h[-11:-1]) / 10
    vol_ok = vol_base > 0 and h[-1].volume >= vol_base * 1.20
    recovery = hc[-2] <= prior_h6 and hc[-1] > h6 and hc[-1] > hc[-2] and price >= hc[-1]
    rr = net_reward_risk(price)
    ok = daily_ok and recovery and vol_ok and rr >= 2.0
    reasons = []
    if not daily_ok: reasons.append("daily trend is not bullish")
    if not recovery: reasons.append("hourly recovery is unconfirmed")
    if not vol_ok: reasons.append("volume confirmation is absent")
    if rr < 2.0: reasons.append("net reward:risk below 2:1")
    if ok: reasons.append("all entry filters confirmed")
    return {**result, "signal": "BUY" if ok else "WAIT", "reason": "; ".join(reasons),
            "daily_trend": "bullish" if daily_ok else "not_bullish",
            "daily_sma5": round(d5, 8), "daily_sma10": round(d10, 8),
            "hourly_sma6": round(h6, 8), "volume_confirmed": vol_ok,
            "net_reward_risk": round(rr, 3), "last_closed_hour_ts": h[-1].start,
            "last_closed_day_ts": d[-1].start}

def empty_state(starting_eur: float = STARTING_EUR, now_ts: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now_ts is None else int(now_ts)
    return {"schema_version": 1, "strategy_version": VERSION, "mode": "paper",
            "day_utc": datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
            "day_start_equity_eur": round(starting_eur, 8), "cash_eur": round(starting_eur, 8),
            "position": None, "realized_pnl_eur": 0.0, "consecutive_losses": 0,
            "halted_day_utc": None, "trade_count": 0, "history": []}

def load_state(path: Path, starting_eur: float = STARTING_EUR) -> dict[str, Any]:
    if not path.exists():
        return empty_state(starting_eur)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("schema_version") != 1 or state.get("mode") != "paper":
            raise ValueError("unsupported schema/mode")
        for key in ("cash_eur", "day_start_equity_eur", "realized_pnl_eur", "history"):
            if key not in state: raise ValueError(f"missing {key}")
        return state
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Refusing to overwrite invalid paper state: {exc}") from exc

def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)

def account_equity(state: dict[str, Any], prices: dict[str, float]) -> float:
    value = float(state["cash_eur"])
    pos = state.get("position")
    if pos: value += float(pos["quantity"]) * float(prices.get(pos["product_id"], pos["entry_price"]))
    return value

def record(state: dict[str, Any], event: dict[str, Any]) -> None:
    state.setdefault("history", []).append(event)
    state["history"] = state["history"][-250:]

def paper_cycle(observations: list[dict[str, Any]], state: dict[str, Any], now_ts: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    now = int(time.time()) if now_ts is None else int(now_ts)
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    prices = {o["product_id"]: float(o["price"]) for o in observations}
    if not prices or any(not math.isfinite(v) or v <= 0 for v in prices.values()):
        raise MarketDataError("No valid prices")
    if state.get("day_utc") != day:
        state["day_utc"], state["day_start_equity_eur"] = day, round(account_equity(state, prices), 8)
        state["halted_day_utc"], state["consecutive_losses"] = None, 0
    actions, closed = [], False
    pos = state.get("position")
    if pos and pos["product_id"] in prices:
        price = prices[pos["product_id"]]
        reason = "stop_loss" if price <= pos["stop_price"] else ("take_profit" if price >= pos["target_price"] else None)
        if reason:
            proceeds = pos["quantity"] * price * (1 - SLIPPAGE_BPS / 10000) * (1 - TAKER_FEE)
            pnl = proceeds - pos["entry_cost_eur"]
            state["cash_eur"] = round(state["cash_eur"] + proceeds, 8)
            state["realized_pnl_eur"] = round(state["realized_pnl_eur"] + pnl, 8)
            state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1 if pnl < 0 else 0
            state["position"], closed = None, True
            actions.append(f"Closed {pos['product_id']} ({reason}); net paper P/L €{pnl:.4f}")
            record(state, {"ts": now, "action": "PAPER_SELL", "product_id": pos["product_id"], "price": price, "pnl_eur": round(pnl, 8), "reason": reason})
    dd = max(0.0, (state["day_start_equity_eur"] - account_equity(state, prices)) / state["day_start_equity_eur"])
    if dd >= DAILY_DD_LIMIT or state.get("consecutive_losses", 0) >= MAX_LOSSES:
        state["halted_day_utc"] = day
    candidates = [x["analysis"] for x in observations if x.get("analysis", {}).get("signal") == "BUY"]
    if not state.get("position") and not closed and state.get("halted_day_utc") != day and candidates:
        candidate = sorted(candidates, key=lambda x: (-float(x.get("net_reward_risk", 0)), x["product_id"]))[0]
        price = prices[candidate["product_id"]]
        spend = min(MAX_POSITION_EUR, float(state["cash_eur"]) - RESERVE_EUR)
        if spend >= 1.01 and net_reward_risk(price) >= 2.0:
            qty = spend / (price * (1 + SLIPPAGE_BPS / 10000) * (1 + TAKER_FEE))
            pos = {"product_id": candidate["product_id"], "entry_price": price, "quantity": qty,
                   "entry_cost_eur": round(spend, 8), "stop_price": price * (1 - STOP_PCT),
                   "target_price": price * (1 + TARGET_PCT), "entry_ts": now}
            state["cash_eur"] = round(float(state["cash_eur"]) - spend, 8)
            state["position"], state["trade_count"] = pos, state.get("trade_count", 0) + 1
            actions.append(f"Opened simulated {candidate['product_id']} position for €{spend:.2f}")
            record(state, {"ts": now, "action": "PAPER_BUY", **pos})
        else: actions.append("No entry: cash reserve or after-cost risk check failed")
    if not actions: actions.append("No trade: no confirmed entry signal")
    eq = account_equity(state, prices)
    dd = max(0.0, (state["day_start_equity_eur"] - eq) / state["day_start_equity_eur"])
    if dd >= DAILY_DD_LIMIT or state.get("consecutive_losses", 0) >= MAX_LOSSES:
        state["halted_day_utc"] = day
    report = {"timestamp_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
              "strategy_version": VERSION, "mode": "paper", "live_orders_enabled": False,
              "actions": actions, "signals": [x["analysis"] for x in observations],
              "cash_eur": round(float(state["cash_eur"]), 8), "equity_eur": round(eq, 8),
              "realized_pnl_eur": round(float(state["realized_pnl_eur"]), 8),
              "daily_drawdown_pct": round(dd * 100, 4),
              "consecutive_losses": state.get("consecutive_losses", 0),
              "halted_today": state.get("halted_day_utc") == day, "position": state.get("position")}
    state["updated_at_utc"] = report["timestamp_utc"]
    return state, report

def run_once(state_path: Path, starting_eur: float = STARTING_EUR) -> dict[str, Any]:
    if os.getenv("COINBASE_LIVE_TRADING_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        raise UnsafeLiveModeError("This runner is paper-only; live trading is blocked by design.")
    now, state, observations = int(time.time()), load_state(state_path, starting_eur), []
    for product_id in SYMBOLS:
        product = fetch_product(product_id)
        hourly = fetch_candles(product_id, "ONE_HOUR", 60, now)
        daily = fetch_candles(product_id, "ONE_DAY", 30, now)
        analysis = evaluate_signal(product_id, product["price"], hourly, daily, now)
        observations.append({"product_id": product_id, "price": product["price"],
                             "change_24h_pct": float(product.get("price_percentage_change_24h", 0) or 0),
                             "analysis": analysis})
    state, report = paper_cycle(observations, state, now)
    save_state(state_path, state)
    report_path = state_path.with_name("coinbase_report.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["# HORMOZI Coinbase Paper Engine", "", f"- Time UTC: {report['timestamp_utc']}",
                 "- Mode: PAPER ONLY — real orders disabled", f"- Equity: €{report['equity_eur']:.4f}",
                 f"- Cash: €{report['cash_eur']:.4f}", f"- Paper P/L: €{report['realized_pnl_eur']:.4f}",
                 f"- Daily drawdown: {report['daily_drawdown_pct']:.3f}%", f"- Halted today: {report['halted_today']}", "",
                 "| Product | Price EUR | 24h | Signal | Reason |", "|---|---:|---:|---|---|"]
        for row in observations:
            s = row["analysis"]
            lines.append(f"| {row['product_id']} | {row['price']:.8f} | {row['change_24h_pct']:+.2f}% | {s['signal']} | {str(s.get('reason','')).replace('|','/')} |")
        lines += ["", "## Cycle actions", ""] + [f"- {a}" for a in report["actions"]]
        with open(summary, "a", encoding="utf-8") as stream: stream.write("\n".join(lines) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return report

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HORMOZI Coinbase paper-only monitor")
    parser.add_argument("--state-path", default=".runtime/coinbase_state.json")
    parser.add_argument("--starting-cash-eur", type=float, default=float(os.getenv("COINBASE_PAPER_STARTING_EUR", STARTING_EUR)))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            assert net_reward_risk(100.0) >= 2.0
            print(json.dumps({"status": "ok", "mode": "paper", "net_reward_risk": round(net_reward_risk(100), 3)}))
            return 0
        run_once(Path(args.state_path), args.starting_cash_eur)
        return 0
    except (MarketDataError, RuntimeError, UnsafeLiveModeError, AssertionError) as exc:
        print(f"HORMOZI ERROR: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
