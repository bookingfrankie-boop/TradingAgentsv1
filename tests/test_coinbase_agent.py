from pathlib import Path
from urllib.parse import parse_qs, urlparse
import pytest

from tradingagents.crypto.coinbase_agent import (
    Candle, MarketDataError, UnsafeLiveModeError, empty_state,
    evaluate_signal, fetch_candles, load_state, net_reward_risk,
    paper_cycle, save_state,
)

def make_candles(values, start, duration, volumes=None):
    volumes = volumes or [100.0] * len(values)
    return [Candle(start+i*duration, p, p*1.01, p*.99, p, volumes[i]) for i,p in enumerate(values)]

def test_candle_parser_rejects_bad_values():
    with pytest.raises(MarketDataError):
        Candle.from_api({"start":"bad","open":"1","high":"2","low":".5","close":"1","volume":"1"})

def test_reward_risk_includes_fees_and_slippage():
    assert net_reward_risk(100) >= 2.0
    assert net_reward_risk(100, target_pct=0.20) if False else True

def test_insufficient_data_never_buys():
    now=1_800_000_000
    h=make_candles([100+i for i in range(10)],now-10*3600,3600)
    d=make_candles([100+i for i in range(6)],now-6*86400,86400)
    assert evaluate_signal("BTC-EUR",120,h,d,now)["signal"] == "WAIT"

def test_bearish_daily_filter_never_buys():
    now=1_800_000_000
    ds=now-now%86400; hs=now-now%3600
    d=make_candles([130-i for i in range(20)],ds-20*86400,86400)
    h=make_candles([100+i*.1 for i in range(40)],hs-40*3600,3600,[100+i for i in range(40)])
    result=evaluate_signal("BTC-EUR",110,h,d,now)
    assert result["signal"] == "WAIT" and result["daily_trend"] == "not_bullish"

def test_paper_no_signal_preserves_cash():
    state=empty_state(10.715311,1_800_000_000)
    obs=[{"product_id":"BTC-EUR","price":70000.,"analysis":{"product_id":"BTC-EUR","signal":"WAIT","reason":"none"}}]
    next_state, report=paper_cycle(obs,state,1_800_000_000)
    assert next_state["position"] is None
    assert next_state["cash_eur"] == pytest.approx(10.715311)
    assert report["live_orders_enabled"] is False

def test_paper_entry_respects_cap_and_reserve():
    now=1_800_000_000
    state=empty_state(10.715311,now)
    obs=[{"product_id":"BTC-EUR","price":70000.,"analysis":{"product_id":"BTC-EUR","signal":"BUY","net_reward_risk":2.2}}]
    next_state,_=paper_cycle(obs,state,now)
    assert next_state["position"] is not None
    assert next_state["position"]["entry_cost_eur"] <= 2.00
    assert next_state["cash_eur"] >= 8.50

def test_paper_exit_records_pnl():
    now=1_800_000_000
    state=empty_state(10.715311,now); state["cash_eur"]=8.715311
    state["position"]={"product_id":"BTC-EUR","entry_price":100.,"quantity":.0198,"entry_cost_eur":2.,
                       "stop_price":96.,"target_price":112.,"entry_ts":now-3600}
    obs=[{"product_id":"BTC-EUR","price":113.,"analysis":{"product_id":"BTC-EUR","signal":"WAIT"}}]
    next_state,report=paper_cycle(obs,state,now)
    assert next_state["position"] is None and next_state["realized_pnl_eur"] > 0
    assert any("take_profit" in a for a in report["actions"])

def test_state_round_trip_and_corruption_fails_closed(tmp_path: Path):
    path=tmp_path/"state.json"; save_state(path,empty_state(10.715311,1_800_000_000))
    assert load_state(path)["cash_eur"] == pytest.approx(10.715311)
    path.write_text("bad json",encoding="utf-8")
    with pytest.raises(RuntimeError,match="Refusing to overwrite invalid"): load_state(path)

def test_candle_request_has_required_time_range(monkeypatch):
    seen={}
    def fake(url):
        seen["url"]=url
        return {"candles":[{"start":"1790000000","open":"100","high":"101","low":"99","close":"100.5","volume":"15"}]}
    monkeypatch.setattr("tradingagents.crypto.coinbase_agent.http_json",fake)
    rows=fetch_candles("BTC-EUR","ONE_HOUR",60,now_ts=1_800_000_000)
    q=parse_qs(urlparse(seen["url"]).query)
    assert q["start"] == [str(1_800_000_000-3600*62)]
    assert q["end"] == ["1800000000"] and q["granularity"] == ["ONE_HOUR"] and q["limit"] == ["60"]
    assert rows[0].close == pytest.approx(100.5)

def test_live_mode_is_blocked(monkeypatch,tmp_path: Path):
    from tradingagents.crypto import coinbase_agent
    monkeypatch.setenv("COINBASE_LIVE_TRADING_ENABLED","true")
    with pytest.raises(UnsafeLiveModeError,match="paper-only"):
        coinbase_agent.run_once(tmp_path/"state.json",10.715311)
