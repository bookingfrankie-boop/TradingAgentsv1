from __future__ import annotations

from dataclasses import dataclass
from math import inf

import pandas as pd


@dataclass(frozen=True)
class Signal:
    action: str
    score: float
    reason: str
    price: float


def compute_indicators(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if df.empty:
        return df

    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Missing candle columns: {sorted(missing)}")

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    delta = close.diff()

    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    loss_safe = loss.where(loss != 0, 1e-12)
    rs = gain / loss_safe
    df["rsi14"] = (100 - (100 / (1 + rs))).astype(float)
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["volume_ma20"] = df["volume"].rolling(20).mean()
    return df


def generate_signal(df: pd.DataFrame) -> Signal:
    if df.empty:
        return Signal("HOLD", 0.0, "No market data", 0.0)

    row = df.iloc[-1]
    price = float(row["close"])
    if any(pd.isna(row.get(k)) for k in ("ema20", "ema50", "rsi14", "atr14")):
        return Signal("HOLD", 0.0, "Insufficient warm-up data", price)

    bullish = row["ema20"] > row["ema50"]
    bearish = row["ema20"] < row["ema50"]
    rsi = float(row["rsi14"])

    if bullish and 50 <= rsi <= 70:
        score = min(1.0, 0.6 + (rsi - 50) / 100)
        return Signal(
            "BUY",
            score,
            "EMA20 above EMA50; RSI confirms positive momentum",
            price,
        )

    if bearish or rsi < 45:
        score = min(1.0, 0.6 + abs(rsi - 45) / 100)
        return Signal("SELL", score, "Trend/momentum turned defensive", price)

    return Signal("HOLD", 0.4, "Trend and momentum are not aligned", price)


def _max_drawdown(equity: list[float]) -> float:
    peak = -inf
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd


def backtest_ohlcv(
    candles: list[dict],
    initial_cash: float = 21.88,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    position_pct: float = 0.70,
) -> dict:
    df = compute_indicators(candles)
    if len(df) < 60:
        return {
            "status": "insufficient_data",
            "trades": 0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }

    cash = float(initial_cash)
    asset = 0.0
    trades = []
    equity_curve = []

    for idx in range(50, len(df)):
        window = df.iloc[: idx + 1]
        sig = generate_signal(window)
        price = float(df.iloc[idx]["close"])
        buy_px = price * (1 + slippage_bps / 10000)
        sell_px = price * (1 - slippage_bps / 10000)

        if sig.action == "BUY" and asset == 0 and cash > 0:
            allocation = cash * max(0.0, min(position_pct, 1.0))
            fee = allocation * fee_rate
            spend = allocation - fee
            asset = spend / buy_px
            cash -= allocation
            trades.append({"side": "buy", "price": buy_px, "fee": fee, "qty": asset})

        elif sig.action == "SELL" and asset > 0:
            gross = asset * sell_px
            fee = gross * fee_rate
            cash += gross - fee
            trades.append({"side": "sell", "price": sell_px, "fee": fee, "qty": asset})
            asset = 0.0

        equity_curve.append(cash + asset * price)

    if not equity_curve:
        return {
            "status": "no_trades",
            "trades": 0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }

    final_equity = equity_curve[-1]
    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0

    for i in range(1, len(trades), 2):
        entry = trades[i - 1]
        exit_ = trades[i]
        qty = entry["qty"]
        pnl = (exit_["price"] - entry["price"]) * qty - entry["fee"] - exit_["fee"]
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            losses += 1
            gross_loss += -pnl

    profit_factor = gross_profit / gross_loss if gross_loss else (inf if gross_profit else 0.0)
    return {
        "status": "ok",
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "return_pct": (final_equity / initial_cash - 1) * 100,
        "max_drawdown_pct": _max_drawdown(equity_curve) * 100,
        "trades": len(trades) // 2,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / (wins + losses) * 100) if (wins + losses) else 0.0,
        "profit_factor": profit_factor,
        "fees_included": True,
        "slippage_bps": slippage_bps,
    }
