from tradingagents.crypto.strategy import backtest_ohlcv, compute_indicators, generate_signal


def candles(n=120, start=100.0, step=0.2):
    rows = []
    price = start
    for i in range(n):
        price += step
        rows.append(
            {
                "ts": i,
                "open": price - 0.1,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 1000 + i,
            }
        )
    return rows


def test_indicators_and_signal_shape():
    df = compute_indicators(candles())
    signal = generate_signal(df)
    assert "ema20" in df and "ema50" in df and "rsi14" in df and "atr14" in df
    assert signal.action in {"BUY", "HOLD", "SELL"}
    assert 0 <= signal.score <= 1


def test_backtest_is_fee_aware_and_deterministic():
    a = backtest_ohlcv(candles())
    b = backtest_ohlcv(candles())
    assert a == b
    assert a["fees_included"] is True
    assert a["slippage_bps"] == 5.0


def test_insufficient_data_does_not_trade():
    result = backtest_ohlcv(candles(20))
    assert result["status"] == "insufficient_data"
    assert result["trades"] == 0
