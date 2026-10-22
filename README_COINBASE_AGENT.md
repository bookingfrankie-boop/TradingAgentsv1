# HORMOZI Coinbase Agent — paper engine

## Status

Scans public Coinbase Advanced Trade market data for BTC-EUR, ETH-EUR, SOL-EUR and XRP-EUR. The engine computes deterministic signals and simulates trades in a paper ledger. It does not place live orders or move funds. Setting COINBASE_LIVE_TRADING_ENABLED=true makes the runner fail closed.

## Free-hosting comparison

| Option | Fit | Limitation |
|---|---|---|
| GitHub Actions on a public repository | Free scheduled checks, tests and paper runs | Scheduled runs may be delayed; not a persistent process or trading SLA |
| Oracle Cloud Free Tier Compute | Can host a persistent VM within Always Free limits when capacity is available | Account verification may request a payment card; capacity and eligibility are not guaranteed |
| Cloudflare Workers Cron | Good for short serverless jobs | Not a conventional always-on Python process; redesign required |
| Replit/Render free runtimes | Demos and dashboards | Free runtimes may sleep; not suitable for dependable live execution |

Official references:
- Coinbase public candles and required start/end: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-candles
- Coinbase Advanced Trade API: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api
- GitHub Actions schedule limits: https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions#onschedule
- Oracle Free Tier: https://www.oracle.com/cloud/free/

## Strategy and guardrails

- Spot-only signal research; no leverage, derivatives, shorting, transfers or live order submission.
- Daily trend: last completed daily close above SMA5, SMA5 above SMA10, and SMA5 rising.
- Hourly confirmation: completed close crosses above SMA6, closes higher, current price confirms recovery, and volume is at least 1.2 times the preceding ten-candle average.
- Entry requires estimated net reward:risk of at least 2:1 after modeled fees and slippage.
- Paper starting cash €10.715311; minimum cash reserve €8.50; maximum simulated position €2; stop 4%; target 12%; stop new entries after 2% daily drawdown or two consecutive paper losses; one position at a time.
- Only completed candles are used. Insufficient, invalid or stale data fail closed. Strategy changes require review and tests.

The paper ledger is a simulation seeded at €10.715311, not live account reconciliation. The workflow runs every 15 minutes, but GitHub Actions is not continuous low-latency hosting; scheduled runs may be delayed and may be disabled after prolonged repository inactivity.

## Local commands

    python -m tradingagents.crypto.coinbase_agent --self-test
    python -m pytest -q tests/test_coinbase_agent.py
    python -m tradingagents.crypto.coinbase_agent --state-path .runtime/coinbase_state.json

## Why live trading remains blocked

The repo is public and scheduled runners are not persistent. The connected Coinbase Agent session cannot be reused as a host API key. Live execution needs a private persistent runtime, dedicated least-privilege Coinbase API credentials (view/trade only; no transfer/withdraw permission), durable state, kill switch, balance reconciliation, order-preview validation and separate end-to-end tests. Never paste API secrets into chat or commit them to this repository.
