# H7 shadow-mode adapter

## Safety boundary

This utility is deliberately read-only. It fetches only public Coinbase Exchange ticker data and never imports an exchange trading SDK, reads API credentials, previews orders, or submits orders. It does not implement H7 strategy logic; the existing H7 strategy must emit candidate signals into the JSONL input described below.

Keep LIVE_READY=NO and RESEARCH_ONLY. Reviewing after 30 simulated trades is only a checkpoint, not proof of a trading edge.

## Requirements

Python 3.11 or later; standard library only. Public internet access to api.exchange.coinbase.com is required for quote collection.

## Run

From the repository root:

    python h7_shadow.py --symbols BTC-USD,ETH-USD --signals data/h7_signals.jsonl --db data/h7_shadow.sqlite --poll-seconds 30

One-shot connectivity and quote check:

    python h7_shadow.py --symbols BTC-USD --signals data/h7_signals.jsonl --db data/h7_shadow.sqlite --once

Fees are not assumed. Without an explicitly configured fee model, the tracker leaves net PnL unavailable and reports missing fee coverage. Once the actual venue/account fee is verified, supply a fee estimate per fill in basis points and any additional flat quote-currency fee. Do not interpret illustrative fee settings as account-specific fee data.

## H7 signal interface

The H7 adapter must append exactly one JSON object per line to data/h7_signals.jsonl after H7 itself creates a candidate. Illustrative schema only:

    {"signal_id":"h7-2026-10-14-0001","created_at":"2026-10-14T08:00:00Z","product_id":"BTC-USD","side":"buy","strategy_version":"H7-shadow-1","parameter_hash":"sha256-of-frozen-parameters","stop_pct":0.01,"target_pct":0.02,"notional_quote":25.0,"hold_minutes":240}

The sample values are not a recommendation. stop_pct and target_pct are fractions (0.01 means 1%). notional_quote is hypothetical position notional in the quote currency. hold_minutes is the maximum simulated holding time. Use unique signal_id values and hold strategy_version and parameter_hash constant for a frozen run.

## Interpretation limits

- Entry simulates buy at ask and sell at bid; exits simulate liquidation at bid for longs and ask for shorts.
- It samples quotes periodically and may miss intraperiod stop/target crossings.
- It does not model queue position, actual maker fills, partial fills, latency, or market impact.
- A 30-trade checkpoint is not statistically sufficient by itself. Review costs, uncertainty, benchmarks, sample composition, and out-of-sample integrity.
- SQLite is the source log. Back up the database and retain the signal file.
- This utility is not connected to the existing H7 strategy until that strategy is explicitly wired to the JSONL interface.
