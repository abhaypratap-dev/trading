"""OpenAIAP: flow settings, variables, and the zone rules executed through recorded web flows (no real browser)."""
import importlib
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def oa(tmp_path, monkeypatch):
    monkeypatch.setenv("YONEXIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'o.db'}")
    import yonexis.config as cfg
    importlib.reload(cfg)
    import yonexis.db as db
    importlib.reload(db)
    db.init()
    from yonexis.openaiap import config, executor, player
    for m in (config, player, executor):
        importlib.reload(m)
    return config, executor, player


class SyncBrowser:
    """Runs jobs at once on the calling thread; records which flows ran with which variables."""
    busy = error = recording = None

    def submit(self, fn, *args):
        fn(*args)


def _flows(config):
    config.save_flow("buy", [{"action": "fill", "selector": "#q", "value": "{symbol}"},
                             {"action": "fill", "selector": "#qty", "value": "{qty}"},
                             {"action": "click", "selector": "button.buy", "final": True}])
    config.save_flow("sell", [{"action": "click", "selector": "button.sell", "final": True}])
    config.save_flow("close", [{"action": "click", "selector": "button.exit", "final": True}])


def test_clean_step_and_render(oa):
    config, executor, player = oa
    with pytest.raises(ValueError):
        config.clean_step({"action": "click"})                      # needs a selector
    with pytest.raises(ValueError):
        config.clean_step({"action": "hack", "selector": "x"})
    s = config.clean_step({"action": "fill", "selector": "#p", "value": "{secret:password}", "final": 0, "optional": 1})
    assert s == {"action": "fill", "selector": "#p", "value": "{secret:password}", "optional": True}
    assert player.render("{symbol} x {qty}", {"symbol": "NIFTY FUT", "qty": 75}, {}) == "NIFTY FUT x 75"
    assert player.render("{secret:pin}", {}, {"pin": "1234"}) == "1234"
    with pytest.raises(ValueError):
        player.render("{secret:pin}", {}, {})
    with pytest.raises(ValueError):
        player.render("{price}", {"price": None}, {})
    assert player.number("₹ 1,234.50") == 1234.5
    assert config.missing_flows() == ["buy", "sell"]                # close is optional: the opposite order
    _flows(config)
    assert config.missing_flows() == []
    assert config.save_secrets({"pin": "1"}) == ["pin"] and config.save_secrets({"pin": ""}) == []


def test_web_trader_spike_entry_stop_relay_and_square_off(oa, monkeypatch):
    config, executor, player = oa
    _flows(config)
    runs = []

    def fake_run(b, name, steps, vars, secrets, dry=False, step_timeout_s=10, max_s=90):
        runs.append((name, dict(vars)))
        return {"flow": name, "ok": True, "steps": [], "vars": {}, "final_ran": True, "dry": dry, "ms": 5}
    monkeypatch.setattr(player, "run", fake_run)
    config.save({"mode": "demo", "account": "demo"})
    config.save_mapping({"X-FUT": {"web": "XFUT NOV", "volume": 75}})
    config.save_auto({"enabled": True, "symbols": ["X-FUT"],
                      "params": {"max_cycles": 2, "lot_targets": [1], "target_base": "zone", "spike": True,
                                 "entry_cutoff": "14:30", "square_off": "15:15", "allow_short": True}})

    class Eng:
        px, bar = None, None
        quote = lambda self, s, strict=False: {"ltp": self.px}
        forming = lambda self, s: self.bar
        candles = lambda self, s: pd.DataFrame()

    eng, ex = Eng(), executor.WebExecutor(b=SyncBrowser())
    clock = [datetime(2026, 11, 2, 9, 15, 30)]
    t = executor.WebTrader(engine_getter=lambda: eng, bridge_getter=lambda: ex, clock=lambda: clock[0],
                           configs=lambda: (config.auto(), config.get(), config.mapping()), state_key="openaiap_auto_state",
                           log_fn=executor.log_event, venue="OpenAIAP")

    def tick(hhmmss, px, bar):
        clock[0] = datetime(2026, 11, 2, *map(int, hhmmss.split(":")))
        eng.px = px
        eng.bar = {"ts": pd.Timestamp(f"2026-11-02 {bar[0]}"), "open": bar[1], "high": bar[2], "low": bar[3], "close": bar[4]}
        t.step()

    tick("09:16:00", 99.8, ("09:15", 100, 101, 99, 99.8))
    tick("09:20:01", 100.0, ("09:20", 99.6, 100.0, 99.6, 100.0))     # zone 99-101 known
    tick("09:21:00", 101.2, ("09:20", 99.6, 101.2, 99.6, 101.2))     # crosses the high -> buy flow
    assert [r[0] for r in runs] == ["buy"], [e["text"] for e in t.events]
    assert runs[0][1]["symbol"] == "XFUT NOV" and runs[0][1]["qty"] == 75 and runs[0][1]["side"] == "BUY"
    assert runs[0][1]["sl"] == 99 and runs[0][1]["yonexis_symbol"] == "X-FUT"
    tick("09:21:01", 101.3, ("09:20", 99.6, 101.3, 99.6, 101.3))     # result collected
    assert t.syms["X-FUT"]["legs"][0]["status"] == "open"
    assert ex.positions()[0]["type"] == "buy" and ex.positions()[0]["volume"] == 75
    # no SL sits at the broker: the trader closes at the stop and relays the SELL
    tick("09:30:00", 98.9, ("09:30", 99.5, 99.5, 98.9, 98.9))
    assert [r[0] for r in runs] == ["buy", "close", "sell"]
    assert runs[1][1]["side"] == "BUY" and runs[1][1]["close_side"] == "SELL" and runs[1][1]["qty"] == 75
    tick("09:30:01", 98.9, ("09:30", 99.5, 99.5, 98.9, 98.9))
    assert ex.positions()[0]["type"] == "sell"
    tick("15:15:01", 98.0, ("15:15", 98, 98, 98, 98))                # square-off
    assert [r[0] for r in runs][-1] == "close"
    tick("15:15:02", 98.0, ("15:15", 98, 98, 98, 98))
    assert ex.positions() == []


def test_failed_flow_relogs_in_only_before_final(oa, monkeypatch):
    config, executor, player = oa
    _flows(config)
    config.save_flow("login", [{"action": "fill", "selector": "#pw", "value": "{secret:password}"}])
    calls = []

    def fake_run(b, name, steps, vars, secrets, dry=False, step_timeout_s=10, max_s=90):
        calls.append(name)
        first = calls.count(name) == 1
        final = name == "sell"                                      # sell fails AFTER its final click
        ok = name == "login" or not first
        return {"flow": name, "ok": ok, "error": None if ok else "step 1: element not found", "steps": [], "vars": {},
                "final_ran": final, "dry": dry, "ms": 1}
    monkeypatch.setattr(player, "run", fake_run)
    ex = executor.WebExecutor(b=SyncBrowser())
    r = ex.take(ex.send("buy", symbol="XFUT NOV", volume=75, magic=1))
    assert r["ok"] and calls == ["buy", "login", "buy"] and r["relogin"]["ok"]
    r = ex.take(ex.send("sell", symbol="XFUT NOV", volume=75, magic=1))
    assert not r["ok"] and calls[3:] == ["sell"]                    # never retried: the order may be in


def test_symbol_matching_picks_near_month_future_and_cash(oa):
    from yonexis.openaiap import symbols
    nov = {"month": "NOV", "day": 25, "year": 2026}
    names = ["NIFTY 25 NOV FUT", "NIFTY 30 DEC FUT", "NIFTY BANK", "BANKNIFTY NOV FUT", "NIFTY 25NOV 24000 CE",
             "NIFTY 50", "NIFTY IT", "NIFTY26NOVFUT", "NIFTYNXT50 NOV FUT"]
    ranked = sorted(names, key=lambda n: symbols.score("NIFTY-FUT", n, nov), reverse=True)
    assert ranked[0] in ("NIFTY 25 NOV FUT", "NIFTY26NOVFUT")
    assert symbols.score("NIFTY-FUT", "NIFTY 25NOV 24000 CE", nov) < 0
    assert symbols.score("NIFTY-FUT", "BANKNIFTY NOV FUT", nov) < 0
    assert symbols.score("NIFTY-FUT", "NIFTYNXT50 NOV FUT", nov) < 0
    assert symbols.score("NIFTY-FUT", "NIFTY 30 DEC FUT", nov) < symbols.score("NIFTY-FUT", "NIFTY 25 NOV FUT", nov)
    cash = ["RELIANCE", "RELIANCE NOV FUT", "RELIANCE-EQ NSE", "RELIANCE 1400 CE", "RELINFRA"]
    best = max(cash, key=lambda n: symbols.score("RELIANCE", n))
    assert best == "RELIANCE-EQ NSE" and symbols.score("RELIANCE", "RELINFRA") < 0
    assert symbols.score("MARUTI", "MARUTI") > 0 and symbols.query("NIFTY-FUT") == "NIFTY"
    s = symbols.suggest(["RELIANCE"], {"RELIANCE": cash})
    assert s["RELIANCE"]["web"] == "RELIANCE-EQ NSE" and "RELIANCE 1400 CE" not in s["RELIANCE"]["candidates"]


def test_selector_variables_and_new_steps(oa):
    config, executor, player = oa
    assert player.render("text=/^\\s*{symbol|re}\\s*$/i", {"symbol": "M&M 25 NOV FUT (1.5)"}, {}) == \
        "text=/^\\s*M\\&M\\s*25\\s*NOV\\s*FUT\\s*\\(1\\.5\\)\\s*$/i"
    assert config.clean_step({"action": "click_xy", "x": 0, "y": 12})["x"] == 0
    with pytest.raises(ValueError):
        config.clean_step({"action": "extract_list", "selector": "li"})          # needs a variable
    assert config.save_mapping({"x-fut": {"web": "X NOV FUT", "volume": 1}})["X-FUT"]["query"] == ""
    assert config.mapping()["X-FUT"]["query"] == "X"


def test_close_without_close_flow_places_the_opposite_order_and_dry_books_nothing(oa, monkeypatch):
    config, executor, player = oa
    config.save_flow("buy", [{"action": "click", "selector": "#ask", "final": True}])
    config.save_flow("sell", [{"action": "click", "selector": "#bid", "final": True}])
    runs = []

    def fake_run(b, name, steps, vars, secrets, dry=False, step_timeout_s=10, max_s=90):
        runs.append((name, dry))
        return {"flow": name, "ok": True, "steps": [], "vars": {}, "final_ran": not dry, "dry": dry, "ms": 1}
    monkeypatch.setattr(player, "run", fake_run)
    ex = executor.WebExecutor(b=SyncBrowser())
    assert ex.take(ex.send("buy", symbol="ABB 23NOV2026", volume=1, magic=1, dry=True))["ok"]
    assert ex.positions() == [] and runs == [("buy", True)]
    ex.take(ex.send("buy", symbol="ABB 23NOV2026", volume=1, magic=1))
    assert ex.positions()[0]["type"] == "buy"
    r = ex.take(ex.send("close", symbol="ABB 23NOV2026", volume=1, magic=1))
    assert r["ok"] and runs[-1] == ("sell", False) and ex.positions() == []


def test_trader_prepares_next_order_and_fires_only_the_final_step(oa, monkeypatch):
    config, executor, player = oa
    config.save_flow("buy", [{"action": "click", "selector": "#ask"}, {"action": "fill", "selector": "#lot", "value": "{qty}"},
                             {"action": "click", "selector": "#place", "final": True}])
    config.save_flow("sell", [{"action": "click", "selector": "#bid"}, {"action": "fill", "selector": "#lot", "value": "{qty}"},
                              {"action": "click", "selector": "#place", "final": True}])
    runs = []

    def fake_run(b, name, steps, vars, secrets, dry=False, step_timeout_s=10, max_s=90):
        runs.append((name, dry, [s["selector"] for s in steps], vars.get("qty")))
        return {"flow": name, "ok": True, "steps": [], "vars": {}, "final_ran": not dry, "dry": dry, "ms": 1}
    monkeypatch.setattr(player, "run", fake_run)
    config.save({"mode": "demo"})
    config.save_mapping({"X-FUT": {"web": "X NOV", "volume": 1}})
    config.save_auto({"enabled": True, "symbols": ["X-FUT"], "params": {"max_cycles": 1, "lot_targets": [1, 2], "spike": True,
                                                                         "entry_cutoff": "14:30", "square_off": "15:15"}})

    class W(SyncBrowser):
        name, staged = "w1", None

    w = W()

    class Pool:
        def for_symbol(self, s): return w
        def symbols_of(self, b): return ["X NOV"]
        def all(self): return [w]
    ex = executor.WebExecutor(b=w, pool=Pool())

    class Eng:
        px, bar = None, None
        quote = lambda self, s, strict=False: {"ltp": self.px}
        forming = lambda self, s: self.bar
        candles = lambda self, s: pd.DataFrame()
    eng, clock = Eng(), [datetime(2026, 11, 2, 9, 15, 30)]
    t = executor.WebTrader(engine_getter=lambda: eng, bridge_getter=lambda: ex, clock=lambda: clock[0],
                           configs=lambda: (config.auto(), config.get(), config.mapping()), state_key="oa_t", log_fn=executor.log_event)

    def tick(hhmmss, px, bar):
        clock[0] = datetime(2026, 11, 2, *map(int, hhmmss.split(":")))
        eng.px, eng.bar = px, {"ts": pd.Timestamp(f"2026-11-02 {bar[0]}"), "open": bar[1], "high": bar[2], "low": bar[3], "close": bar[4]}
        t.step()
    tick("09:16:00", 99.8, ("09:15", 100, 101, 99, 99.8))
    tick("09:20:01", 100.6, ("09:20", 99.6, 100.6, 99.6, 100.6))     # zone 99-101, price in the upper half
    tick("09:20:02", 100.6, ("09:20", 99.6, 100.6, 99.6, 100.6))
    assert runs[-1] == ("stage buy", True, ["#ask", "#lot", "#place"], 2) and w.staged["name"] == "buy"
    tick("09:21:00", 101.2, ("09:20", 99.6, 101.2, 99.6, 101.2))     # break: only the final click
    assert runs[-1] == ("buy", False, ["#place"], 2)
    import time as _t
    t.syms["X-FUT"]["staged_at"] = _t.time() - 10
    tick("09:21:01", 101.3, ("09:20", 99.6, 101.3, 99.6, 101.3))     # filled -> the exit of one lot is prepared
    tick("09:21:02", 101.3, ("09:20", 99.6, 101.3, 99.6, 101.3))
    assert runs[-1] == ("stage sell", True, ["#bid", "#lot", "#place"], 1)
    tick("09:30:00", 98.9, ("09:30", 99.5, 99.5, 98.9, 98.9))         # stop: both lots, quantity refilled
    assert runs[-1] == ("sell", False, ["#lot", "#place"], 2)


def test_recorded_search_and_click_becomes_a_lookup(oa):
    from yonexis.openaiap import symbols
    flow = [{"action": "click", "selector": "#txtFocusScript"},
            {"action": "fill", "selector": "#txtFocusScript", "value": "{symbol}", "optional": True},
            {"action": "click", "selector": 'td:has-text("ANGELONE 23NOV2026")', "row_tpl": 'tr:has(:text-is("{symbol}")) > :nth-child(1)'}]
    lk = symbols.lookup_steps(flow)
    assert lk[:2] == flow[:2] and lk[2] == {"action": "pick_symbol", "selector": "tr > :nth-child(1)", "var": "site_name", "timeout_s": 4}
    assert symbols.lookup_steps(flow[:2] + [{"action": "extract_list", "selector": "li", "var": "names"}]) is None
    nov = {"month": "NOV", "day": 23, "year": 2026}
    seen = ["NSE", "ANGELONE 23NOV2026", "ANGELONE 28DEC2026", "ANGELONE 23NOV2026 300 CE"]
    assert max(seen, key=lambda n: symbols.score("ANGELONE-FUT", n, nov)) == "ANGELONE 23NOV2026"


def test_several_automations_per_website_watchlist_and_own_close(oa):
    config, executor, player = oa
    import yonexis.db as db
    db.set_setting("named_watchlists", {"Banks": ["sbin-fut", "HDFCBANK-FUT"]})
    a2 = config.add_automation("Banks list", copy_from="main")
    assert a2["id"] == "a2" and config.automation_ids() == ["main", "a2"]
    config.save_auto({"watchlist": "Banks", "enabled": True}, aid="a2")
    assert config.auto(aid="a2")["symbols"] == ["HDFCBANK-FUT", "SBIN-FUT"]          # follows the list
    db.set_setting("named_watchlists", {"Banks": ["SBIN-FUT", "HDFCBANK-FUT", "AXISBANK-FUT"]})
    assert len(config.auto(aid="a2")["symbols"]) == 3
    config.save_auto({"symbols": ["NIFTY-FUT", "SBIN-FUT"], "enabled": True})
    assert config.symbol_owner()["SBIN-FUT"] == "main" and config.symbol_owner()["AXISBANK-FUT"] == "a2"
    assert config.all_symbols() == ["AXISBANK-FUT", "HDFCBANK-FUT", "NIFTY-FUT", "SBIN-FUT"]
    assert config.auto(aid="a2")["name"] == "Banks list" and config.auto()["name"] == "Automation 1"
    # each automation closes only its own lots
    sent = []

    class Ex:
        state = {"account": {"margin_mode": "netting"}, "positions": []}
        def send(self, op, **f): sent.append((op, f["symbol"])); return "c"
        def health(self): return {"connected": True, "message": "ok"}
    ex = Ex()
    t = executor.WebTrader(bridge_getter=lambda: ex, configs=lambda: (config.auto(aid="a2"), {**config.get(), "mode": "demo"}, config.mapping()),
                           state_key="oa_s2", log_fn=executor.log_event)
    t.syms = {"SBIN-FUT": {"legs": [{"id": "1", "status": "open", "mt5": "SBIN NOV", "volume": 1, "side": "BUY", "stop": 1, "ref": 2,
                                      "off": 0, "target": None, "lot": 1}], "done": None},
              "AXISBANK-FUT": {"legs": [], "done": None}}
    t.close_all(why="test")
    assert sent == [("close", "SBIN NOV")] and t.syms["SBIN-FUT"]["legs"][0]["status"] == "closed"
    config.remove_automation("a2")
    assert config.automation_ids() == ["main"]


def test_running_automation_reopens_missing_worker_tabs(oa):
    config, executor, player = oa

    class W:
        def __init__(s, name, page, prepared=None):
            s.name, s.page, s.prepared_for, s.busy, s.staged, s.jobs = name, page, prepared, None, None, []
        def submit(s, fn, *a): s.jobs.append(a[-1])

    main, w1, w2 = W("main", object(), "A"), W("w1", None), W("w2", object(), "C")
    main.last_url = "https://site/#/signin"

    class Pool:
        def __init__(s): s.main = main
        def needed(s): return [main, w1, w2]
        def symbols_of(s, w): return {"main": ["A"], "w1": ["B"], "w2": ["C"]}[w.name]
        def all(s): return [main, w1, w2]
    config.save_flow("prepare", [{"action": "fill", "selector": "#q", "value": "{symbol}"}])
    ex = executor.WebExecutor(b=main, pool=Pool())
    assert ex.ensure_warm() == 0                                     # main tab not logged in yet: nothing opened
    main.last_url, ex._warm_at = "https://site/#/marketWatch", 0
    assert ex.ensure_warm() == 1 and w1.jobs == ["B"] and not main.jobs and not w2.jobs   # only the missing tab
    assert ex.ensure_warm() == 0                                     # at most every 30 s
