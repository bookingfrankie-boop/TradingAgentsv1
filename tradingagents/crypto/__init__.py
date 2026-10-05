"""Low-cost OKX crypto automation primitives.

The crypto layer is deliberately independent from the LLM agents: market data,
risk checks, backtesting and paper trading work without an LLM/API key.
"""

from .engine import CryptoConfig, CryptoEngine, Signal
from .okx import OKXClient, OKXError
from .strategy import backtest_ohlcv, compute_indicators, generate_signal

__all__ = [
    "CryptoConfig",
    "CryptoEngine",
    "OKXClient",
    "OKXError",
    "Signal",
    "backtest_ohlcv",
    "compute_indicators",
    "generate_signal",
]
