# Low-cost OKX Crypto Automation

This add-on is intentionally independent of the TradingAgents LLM runtime. Public market data, deterministic signals, backtesting, risk sizing, and paper trading work without an LLM API key.

## Commands

```bash
python -m cli.crypto status
python -m cli.crypto scan
python -m cli.crypto backtest SOL-USDC
python -m cli.crypto paper
```

`paper` never submits an OKX order. Exchange order submission is locked unless `OKX_TRADING_ENABLED=true` is explicitly set. For OKX Demo Trading, use a demo API key and `OKX_ENV=demo`; OKX requires the `x-simulated-trading: 1` header for simulated requests.

## Region and secrets

The default host is `https://eea.okx.com` for EEA accounts. Override with `OKX_BASE_URL` only when your account region requires it. Keep `OKX_API_KEY`, `OKX_API_SECRET`, and `OKX_API_PASSPHRASE` in `.env` or a secret store; never commit them.

## Strategy

The first deterministic strategy is a deliberately simple baseline: EMA20/EMA50 trend alignment plus RSI14 confirmation, with fees and 5 bps slippage included in the backtest. It is a research baseline, not a profit guarantee.

## Automation

`.github/workflows/crypto-smoke.yml` runs unit tests and public OKX smoke checks on pushes/PRs and every 15 minutes. It does not submit trades and does not require exchange secrets.
