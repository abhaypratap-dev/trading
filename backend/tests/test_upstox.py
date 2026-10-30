import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yonexis.data.connectors import upstox as ux  # noqa: E402


class FakeResp:
    def __init__(self, body, status=200):
        self._b, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self._b


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(ux, "tokens", ux.TokenStore(tmp_path / "tok.json"))
    ux.tokens.save("TOKEN")
    inst = ux.Instruments(tmp_path / "inst.json")
    inst._by_symbol = {"RELIANCE": {"s": "RELIANCE", "k": "NSE_EQ|INE002A01018", "n": "RELIANCE INDUSTRIES"}}
    inst._by_key = {"NSE_EQ|INE002A01018": "RELIANCE"}
    inst._loaded_for = __import__("datetime").date.today()
    monkeypatch.setattr(ux, "instruments", inst)
    c = ux.UpstoxConnector()
    c.calls = []

    def fake_get(url, params=None, headers=None):
        c.calls.append(url)
        assert headers["Authorization"] == "Bearer TOKEN"
        if "/intraday/" in url:
            return FakeResp({"status": "success", "data": {"candles": [["2099-01-01T09:15:00+05:30", 1, 2, 0.5, 1.5, 10, 0]]}})
        to_d = url.rstrip("/").split("/")[-2]
        return FakeResp({"status": "success", "data": {"candles": [
            [f"{to_d}T09:20:00+05:30", 101, 103, 100, 102, 2000, 0],
            [f"{to_d}T09:15:00+05:30", 100, 102, 99, 101, 1000, 0]]}})
    monkeypatch.setattr(c.client, "get", fake_get)
    return c


def test_history_chunks_sorted_and_ist(conn):
    df = conn.fetch_history("RELIANCE", "2024-01-01", "2024-03-15", "5m")
    hist_calls = [u for u in conn.calls if "/intraday/" not in u]
    assert len(hist_calls) == 3                                    # 1-15 minute data: one month per call
    assert all("NSE_EQ%7CINE002A01018/minutes/5/" in u for u in hist_calls)
    assert df.index.is_monotonic_increasing
    assert str(df.index[0].time()) == "09:15:00" and df.index.tz is None   # IST wall-clock, naive


def test_token_expires_next_trading_day(tmp_path, monkeypatch):
    t = ux.TokenStore(tmp_path / "t.json")
    t.save("X")
    assert t.token == "X"
    d = t.load()
    d["trading_day"] = "2000-01-01"
    t.path.write_text(__import__("json").dumps(d))
    assert t.token is None


def test_oauth_state_is_single_use():
    t = ux.TokenStore(Path("/nonexistent/x"))
    s = t.new_state()
    assert t.check_state(s) is True
    assert t.check_state(s) is False


def test_candle_builder_from_ticks():
    b = ux.CandleBuilder()
    base = pd.Timestamp("2026-10-30 09:15:00") - ux.IST_OFFSET    # IST 09:15 as UTC epoch
    ms = lambda sec: int((base + pd.Timedelta(seconds=sec)).value // 1_000_000)
    b.on_tick("X", 100, ms(1), vtt=1000)
    b.on_tick("X", 105, ms(30), vtt=1500)
    b.on_tick("X", 99, ms(70), vtt=1800)      # next minute
    b.on_tick("X", 101, ms(310), vtt=2000)    # 09:20 -> second 5m candle
    one = b.candles("X", 1, closed_only=False)
    assert list(one.iloc[0][["open", "high", "low", "close"]]) == [100, 105, 100, 105]
    assert one.iloc[0]["volume"] == 500 and one.iloc[1]["volume"] == 300
    five = b.candles("X", 5, closed_only=False)
    assert list(five.index.strftime("%H:%M")) == ["09:15", "09:20"]
    assert five.iloc[0]["high"] == 105 and five.iloc[0]["low"] == 99 and five.iloc[0]["close"] == 99
    b.on_tick("X", 50, ms(-3600))             # pre-open tick: quote only, no candle
    assert b.last["X"]["ltp"] == 50 and len(b.candles("X", 1, closed_only=False)) == 3


def test_feed_decodes_real_protobuf(conn):
    from yonexis.data.connectors.upstox_proto import MarketDataFeedV3_pb2 as pb
    msg = pb.FeedResponse()
    msg.type = pb.Type.Value("live_feed")
    f = msg.feeds["NSE_EQ|INE002A01018"]
    ltpc = f.fullFeed.marketFF.ltpc
    ts_ms = int((pd.Timestamp("2026-10-30 10:00:05") - ux.IST_OFFSET).value // 1_000_000)
    ltpc.ltp, ltpc.ltt, ltpc.cp = 2690.75, ts_ms, 2689.15
    f.fullFeed.marketFF.vtt = 123456
    feed = ux.UpstoxFeed(conn, ["RELIANCE"])
    feed._keymap = {"NSE_EQ|INE002A01018": "RELIANCE"}
    from google.protobuf import json_format
    feed._handle(json_format.MessageToDict(pb.FeedResponse.FromString(msg.SerializeToString())))
    q = feed.quote("RELIANCE")
    assert q["ltp"] == 2690.75 and q["cp"] == 2689.15 and q["ts"].endswith("10:00:05")
    assert feed.ticks == 1


def test_order_manager_exits_at_target_and_cancels_stop():
    from yonexis.broker import UpstoxBroker
    b = UpstoxBroker.__new__(UpstoxBroker)
    placed, cancelled = [], []

    class U:
        def place_order(self, symbol, side, qty, order_type="MARKET", price=0.0, trigger_price=0.0, tag=""):
            placed.append((side, order_type, trigger_price, price))
            return [f"O{len(placed)}"]

        def cancel_order(self, oid):
            cancelled.append(oid)

        def orders(self):
            return []
    b.u, b.open, b._last_book = U(), {}, 1e18
    b.place("RELIANCE", "BUY", 10, 100.0, 98.0, 104.0, "t")
    assert placed == [("BUY", "MARKET", 0.0, 0.0), ("SELL", "SL-M", 98.0, 0.0)]   # no resting target order
    assert b.manage({"RELIANCE": {"ltp": 103.9}}, square_off=False) == []
    acts = b.manage({"RELIANCE": {"ltp": 104.1}}, square_off=False)
    assert acts[0]["exit"] == "target" and cancelled == ["O2"] and placed[-1][:2] == ("SELL", "MARKET")
    assert "RELIANCE" not in b.open


def test_order_manager_square_off():
    from yonexis.broker import UpstoxBroker
    b = UpstoxBroker.__new__(UpstoxBroker)

    class U:
        def place_order(self, *a, **k):
            return ["X"]

        def cancel_order(self, oid):
            pass

        def orders(self):
            return []
    b.u, b.open, b._last_book = U(), {}, 1e18
    b.place("SBIN", "SELL", 5, 800.0, 805.0, 790.0, "t")
    assert b.manage({}, square_off=True)[0]["exit"] == "square_off" and not b.open


def test_exchange_and_official_candles_override_tick_candles(monkeypatch):
    import pandas as pd
    from yonexis.data.connectors import upstox as ux
    b = ux.CandleBuilder()
    day = pd.Timestamp.now().normalize()
    m = day + pd.Timedelta(hours=10)
    ms = lambda t: int((t - ux.IST_OFFSET).value // 1_000_000)
    monkeypatch.setattr(ux.pd.Timestamp, "utcnow", staticmethod(lambda: (m + pd.Timedelta(minutes=3) - ux.IST_OFFSET).tz_localize("UTC")))
    for px in (100.0, 100.4, 100.2):                     # snapshots miss the real high (100.9) and low (99.8)
        b.on_tick("X", px, ms(m + pd.Timedelta(seconds=10)))
    assert b.bars["X"][m][1:3] == [100.4, 100.0]
    b.on_ohlc("X", ms(m), 100.0, 100.9, 99.8, 100.2, 5000)          # exchange I1 for the completed minute wins
    assert b.bars["X"][m] == [100.0, 100.9, 99.8, 100.2, 5000.0] and b.corrections == 1
    rest = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.7], "close": [100.3], "volume": [5100.0]}, index=[m])
    b.merge_rest("X", rest)                                          # official REST candle wins over everything
    b.on_ohlc("X", ms(m), 1, 2, 0.5, 1, 1)
    b.on_tick("X", 120.0, ms(m + pd.Timedelta(seconds=20)))
    assert b.bars["X"][m] == [100.0, 101.0, 99.7, 100.3, 5100.0] and b.confirmed_upto("X") == m


def test_orderflow_aggressor_volume_imbalance_and_minutes():
    import pandas as pd
    from yonexis.data.orderflow import OrderFlow
    f = OrderFlow()
    t0 = pd.Timestamp("2026-11-02 10:00:05")
    depth = [{"bidP": 100.0, "bidQ": 900, "askP": 100.1, "askQ": 100}] + [{"bidP": 99.9, "bidQ": 100, "askP": 100.2, "askQ": 100}]
    f.on_full("X", t0, 100.0, 1000, depth, 5000, 3000)
    f.on_full("X", t0 + pd.Timedelta(seconds=5), 100.1, 1500, depth, 5000, 3000)      # printed at the ask: buy 500
    f.on_full("X", t0 + pd.Timedelta(seconds=9), 100.0, 1700, depth, 5000, 3000)      # printed at the bid: sell 200
    f.on_full("X", t0 + pd.Timedelta(minutes=1), 100.0, 1700, depth, 5000, 3000)      # next minute closes the first
    rows = f.drain()
    assert len(rows) == 1 and rows[0]["buy_vol"] == 500 and rows[0]["sell_vol"] == 200 and rows[0]["delta"] == 300
    assert abs(rows[0]["imbalance"] - (1000 - 200) / 1200) < 1e-9
    snap = f.snapshot("X")
    assert snap["summary"]["tbq_tsq"] == round(5000 / 3000, 2) and snap["minutes"][0]["cum_delta"] == 300


def test_news_rss_parse_and_sentiment():
    from yonexis import news
    xml = """<rss><channel><item><title>Reliance shares surge after strong Q2 profit beats estimates</title>
    <link>https://x/1</link><pubDate>Mon, 02 Nov 2026 09:30:00 +0530</pubDate></item>
    <item><title>SEBI probe: Adani stock plunges on fraud allegations</title><link>https://x/2</link></item></channel></rss>"""
    items = news._parse_rss(xml)
    assert len(items) == 2 and items[0]["ts"].hour in (9, 4)
    assert news.sentiment(items[0]["title"]) > 0.5 and news.sentiment(items[1]["title"]) < -0.5
