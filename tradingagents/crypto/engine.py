from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .okx import OKXClient, OKXError
from .strategy import backtest_ohlcv, compute_indicators, generate_signal


@dataclass(frozen=True)
class CryptoConfig:
    symbols: tuple[str, ...] = ("SOL-USDC", "XRP-USDC", "ETH-USDC", "BTC-USDC")
    bar: str = "15m"
    candles: int = 200
    starting_usdc: float = 21.88
    max_position_pct: float = 0.70
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    state_dir: Path = Path.home() / ".tradingagents" / "crypto"


class CryptoEngine:
    """Deterministic crypto scanner/backtester/paper trader; no LLM is required."""

    def __init__(self, client: OKXClient | None = None, config: CryptoConfig | None = None) -> None:
        self.client = client or OKXClient()
        self.config = config or CryptoConfig(
            symbols=tuple(
                s.strip().upper()
                for s in os.getenv(
                    "OKX_SYMBOLS", "SOL-USDC,XRP-USDC,ETH-USDC,BTC-USDC"
                ).split(",")
                if s.strip()
            ),
            bar=os.getenv("OKX_BAR", "15m"),
            candles=int(os.getenv("OKX_CANDLES", "200")),
            starting_usdc=float(os.getenv("PAPER_STARTING_USDC", "21.88")),
            max_position_pct=float(os.getenv("PAPER_MAX_POSITION_PCT", "0.70")),
            fee_rate=float(os.getenv("PAPER_FEE_RATE", "0.001")),
            slippage_bps=float(os.getenv("PAPER_SLIPPAGE_BPS", "5")),
        )
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.config.state_dir / "paper_state.json"

    def scan(self) -> list[dict]:
        instruments = {item.inst_id: item for item in self.client.get_instruments("USDC")}
        output = []

        for symbol in self.config.symbols:
            instrument = instruments.get(symbol)
            if not instrument or instrument.state != "live":
                continue

            candles = self.client.get_candles(symbol, self.config.bar, self.config.candles)
            frame = compute_indicators(candles)
            signal = generate_signal(frame)
            ticker = self.client.get_ticker(symbol)

            output.append(
                {
                    "symbol": symbol,
                    "last": float(ticker.get("last", signal.price)),
                    "bid": float(ticker.get("bidPx", 0) or 0),
                    "ask": float(ticker.get("askPx", 0) or 0),
                    "signal": signal.action,
                    "score": signal.score,
                    "reason": signal.reason,
                    "min_size": str(instrument.min_size),
                    "lot_size": str(instrument.lot_size),
                    "tick_size": str(instrument.tick_size),
                }
            )
        return output

    def backtest(self, symbol: str) -> dict:
        candles = self.client.get_candles(
            symbol.upper(), self.config.bar, min(self.config.candles, 300)
        )
        return {
            "symbol": symbol.upper(),
            **backtest_ohlcv(
                candles,
                initial_cash=self.config.starting_usdc,
                fee_rate=self.config.fee_rate,
                slippage_bps=self.config.slippage_bps,
                position_pct=self.config.max_position_pct,
            ),
        }

    def _load_state(self) -> dict:
        if not self.state_file.exists():
            return {"cash_usdc": self.config.starting_usdc, "positions": {}, "trades": []}
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _save_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_file)

    def paper_step(self) -> list[dict]:
        state = self._load_state()
        results = []

        for row in self.scan():
            symbol = row["symbol"]
            position = state["positions"].get(symbol)
            action = row["signal"]

            if action == "BUY" and not state["positions"] and state["cash_usdc"] > 0:
                allocation = state["cash_usdc"] * self.config.max_position_pct
                if allocation <= 0:
                    continue
                state["cash_usdc"] -= allocation
                state["positions"][symbol] = {
                    "quote_value": allocation,
                    "entry": row["last"],
                }
                state["trades"].append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "price": row["last"],
                        "quote": allocation,
                        "ts": int(time.time()),
                    }
                )
                results.append({**row, "paper_action": "BUY", "quote": allocation})

            elif action == "SELL" and position is not None:
                exit_value = position["quote_value"] * (row["last"] / position["entry"])
                exit_value *= 1 - self.config.fee_rate
                state["cash_usdc"] += exit_value
                state["trades"].append(
                    {
                        "symbol": symbol,
                        "side": "sell",
                        "price": row["last"],
                        "quote": exit_value,
                        "ts": int(time.time()),
                    }
                )
                del state["positions"][symbol]
                results.append({**row, "paper_action": "SELL", "quote": exit_value})
            else:
                results.append({**row, "paper_action": "HOLD"})

        self._save_state(state)
        return results

    def private_status(self) -> dict:
        return {
            "config": self.client.get_account_config(),
            "balance": self.client.get_account_balance(),
        }
