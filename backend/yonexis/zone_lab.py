"""YONEXIS AI dashboard: the First Red Candle Zone with the admin's own rules, on 5-minute candles only.

Rules (per stock, per day):
  * Zone = the first RED 5-minute candle of the session (09:15 onwards): zone high / zone low.
  * Entry exactly at the zone levels: price touches the zone high -> BUY at the zone high; touches the zone low ->
    SELL at the zone low. Never at a candle's open/close.
  * Lots: every entry opens `len(lot_targets)` lots; each lot has its own target multiplier (lot 1 x1, lot 2 x2 ...).
    All lots share the stop; the entry is finished when every lot is closed.
  * Target = entry price +/- range x multiplier (BUY above, SELL below). Range (target_base):
        zone  zone candle high - low
        iz    entry candle high - low
    The entry candle is the 5-minute candle in which the entry happened; IZ targets are set when it closes.
  * spike off: entries and stops only when a candle OPENS or CLOSES outside the zone, at that open / close.
  * refined: days with flat no-trade candles or missing candles are left out (per symbol).
  * Stop = the opposite zone edge (a BUY is stopped at the zone low, a SELL at the zone high), also exact.
  * A new entry needs a break out of the zone (BUY: price rises to the zone high from below it; SELL: falls to the
    zone low from above it). A return to the zone from outside is not an entry.
  * Cycles: up to `max_cycles` entry+exit pairs a day. After a stop, the stop level is the other side's entry, so the
    next cycle starts there at once (the relay). After a target, the next cycle starts when price touches a zone edge
    again. When the cycles are used up, the stock is done for the day.
  * Anything still open at the square-off time (default 15:15) is closed at that candle's open (or, when the
    time falls inside a candle, e.g. 15:28, at that candle's close).
  * Only 5-minute candles exist here, so inside one candle the order of prices is unknown. Rules:
      - both zone edges touched in one candle before entering: the edge nearer the candle's open is taken first;
      - in the candle of entry only the stop can be hit (the target is never assumed reached in the same candle);
      - a later candle that reaches both stop and target counts as the stop;
      - a relay entry made inside a candle is stopped in that candle only if the candle closes beyond its stop.
"""
from __future__ import annotations

from datetime import time as dtime

import pandas as pd

from yonexis import db
from yonexis.backtest.costs import CostModel

DEFAULTS = {"max_cycles": 2, "target_mult": 1.5, "entry_cutoff": "14:30", "square_off": "15:15", "allow_short": True,
            "capital_per_trade": 100000.0, "lots_per_trade": 1, "include_costs": True, "min_zone_pct": 0.0, "max_zone_pct": 100.0,
            "target_base": "zone", "spike": True, "refined": False}
TARGET_BASES = {"zone": "Zone", "iz": "IZ"}


def _t(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def first_red(day5: pd.DataFrame):
    d = day5[day5.index.time >= dtime(9, 15)]
    red = d[d["close"] < d["open"]]
    return (red.index[0], red.iloc[0]) if len(red) else (None, None)


def lot_targets(p: dict) -> list[float]:
    t = [float(x) for x in (p.get("lot_targets") or []) if x not in (None, "")]
    return t or [float(p["target_mult"])]


def target_price(base: str, side: str, m: float, entry: float, hi: float, lo: float, eh=None, el=None):
    """Target of one lot = entry price +/- range x m (BUY up, SELL down).
    base = "zone": range = zone candle high - low.  base = "iz": range = entry candle high - low (the 5-minute candle
    in which the entry happened; known when it closes, so None until then)."""
    if base == "zone":
        r = hi - lo
    elif eh is None:
        return None
    else:
        r = eh - el
    return entry + r * m if side == "BUY" else entry - r * m


def prepare_day(day5: pd.DataFrame) -> dict:
    """The pandas work of one session, done once (the AI backtest replays each day with many configurations)."""
    day5 = day5.sort_index()
    zt, z = first_red(day5)
    if zt is None:
        return {"zt": None}
    after = day5[day5.index > zt]
    return {"zt": zt, "z": {k: float(z[k]) for k in ("open", "high", "low", "close")}, "idx": list(after.index),
            "O": after["open"].to_numpy(dtype=float), "H": after["high"].to_numpy(dtype=float),
            "L": after["low"].to_numpy(dtype=float), "C": after["close"].to_numpy(dtype=float)}


def run_day(sym: str, day5: pd.DataFrame | None, p: dict, lot: int = 1, prep: dict | None = None) -> dict:
    """Replay one stock's session. day5 = that day's 5-minute candles (index = candle start, IST).

    Every entry opens one leg per lot; each leg has its own target multiplier (lot_targets). All open legs share
    the stop (the opposite zone edge). An entry counts as done when all its legs are closed.
    spike=True : entries / stops at the exact zone level the moment a candle touches it (a wick is enough).
    spike=False: entries and stops only when a candle OPENS or CLOSES outside the zone (at that open / close price);
                 targets still fill when touched."""
    p = {**DEFAULTS, **(p or {})}
    out = {"symbol": sym, "trades": [], "zone": None, "note": ""}
    prep = prep or prepare_day(day5)
    zt, z = prep["zt"], prep.get("z")
    if zt is None:
        out["note"] = "no red 5-minute candle this day, so no zone"
        return out
    hi, lo = float(z["high"]), float(z["low"])
    rng = hi - lo
    out["zone"] = {"ts": str(zt), "high": hi, "low": lo, "range": round(rng, 4), "pct": round(rng / float(z["close"]) * 100, 3)}
    if rng <= 0:
        out["note"] = "zone candle has no range"
        return out
    if not (p["min_zone_pct"] <= out["zone"]["pct"] <= p["max_zone_pct"]):
        out["note"] = f"zone is {out['zone']['pct']}% wide, outside your {p['min_zone_pct']}-{p['max_zone_pct']}% limit"
        return out
    mults = lot_targets(p)
    base = {"iz_entry": "iz"}.get(p.get("target_base") or "zone", p.get("target_base") or "zone")
    spike = bool(p.get("spike", True))
    cutoff, sq = _t(p["entry_cutoff"]), _t(p["square_off"])
    fut = sym.upper().endswith("-FUT")
    costs = CostModel().for_futures() if fut else CostModel()
    cycles, legs = 0, []                               # legs = open legs of the current entry
    after = prep["idx"]                                # trading starts with the candle after the zone candle
    bar = pd.Timedelta(minutes=5)
    lean = bool(p.get("_lean"))

    def qty_for(px):                                   # one "lot": futures = exchange lot, stocks = capital per lot
        return int(lot) if fut else max(1, int(p["capital_per_trade"] // px))

    def open_(side, ts, px=None):
        nonlocal cycles, legs
        px = (hi if side == "BUY" else lo) if px is None else px
        cycles += 1
        legs = [{"cycle": cycles, "lot": i + 1, "lots": len(mults), "side": side, "entry_ts": ts, "entry": px, "qty": qty_for(px),
                 "stop": lo if side == "BUY" else hi, "target_mult": m, "target_base": base,
                 "target": target_price(base, side, m, px, hi, lo), "_bar": ts,
                 "range_hi": hi if base == "zone" else None, "range_lo": lo if base == "zone" else None} for i, m in enumerate(mults)]

    def r2(x):
        return None if x is None else round(x, 2)

    last_exit = [None]                                 # candle of the latest exit

    def close_leg(leg, px, ts, why):
        last_exit[0] = ts
        q, d = leg["qty"], (1 if leg["side"] == "BUY" else -1)
        gross = (px - leg["entry"]) * q * d
        bv, sv = (leg["entry"] * q, px * q) if d == 1 else (px * q, leg["entry"] * q)
        c = costs.round_trip(bv, sv) if p["include_costs"] else 0.0
        if lean:                                         # AI backtest: only what the search adds up
            out["trades"].append({"lot": leg["lot"], "net_pnl": gross - c, "exit_reason": why})
            legs.remove(leg)
            return
        t = {k: v for k, v in leg.items() if k != "_bar"}
        out["trades"].append({**t, "symbol": sym, "entry_ts": str(leg["entry_ts"]), "exit_ts": str(ts), "exit": round(px, 2),
                              "exit_reason": why, "gross_pnl": round(gross, 2), "costs": round(c, 2), "net_pnl": round(gross - c, 2),
                              "stop": round(leg["stop"], 2), "target": r2(leg["target"]), "entry": round(leg["entry"], 2)})
        legs.remove(leg)

    def close_all(px, ts, why):
        for leg in list(legs):
            close_leg(leg, px, ts, why)

    def can_enter(ts):
        return cycles < p["max_cycles"] and ts.time() < cutoff

    def stop_and_relay(px, ts):
        side = legs[0]["side"]
        close_all(px, ts, "stop_loss")
        nside = "SELL" if side == "BUY" else "BUY"           # relay: the stop is the other side's entry
        if can_enter(ts) and (nside == "BUY" or p["allow_short"]):
            open_(nside, ts, None if spike else px)

    def hit_targets(ts, h, l_):
        for leg in list(legs):
            t = leg["target"]
            if t is None or leg["_bar"] == ts:               # never in the candle of entry
                continue
            if (leg["side"] == "BUY" and h >= t) or (leg["side"] == "SELL" and l_ <= t):
                close_leg(leg, t, ts, "target")

    def relay_close_check(close_px, ts):
        """A relay entry made inside a candle is only stopped in that same candle if the candle CLOSES beyond its stop."""
        if legs and ((legs[0]["side"] == "BUY" and close_px <= legs[0]["stop"]) or (legs[0]["side"] == "SELL" and close_px >= legs[0]["stop"])):
            close_all(legs[0]["stop"], ts, "stop_loss")

    def bar_spike(ts, o, h, l_, cl, pc):                 # touch rules (a wick is enough)
        if legs:                                         # exits (stop first when a candle reaches both)
            side, stop = legs[0]["side"], legs[0]["stop"]
            if (side == "BUY" and l_ <= stop) or (side == "SELL" and h >= stop):
                stop_and_relay(stop, ts)
                relay_close_check(cl, ts)
                return
            hit_targets(ts, h, l_)
            return
        if not can_enter(ts):
            return
        # a new entry needs a break OUT of the zone: BUY when price rises to the zone high from below it,
        # SELL when it falls to the zone low from above it (a return to the zone from outside is not an entry)
        touch_hi = h >= hi and pc < hi
        touch_lo = l_ <= lo and pc > lo and p["allow_short"]
        if not (touch_hi or touch_lo):
            return
        side = "BUY" if touch_hi and (not touch_lo or abs(o - hi) <= abs(o - lo)) else "SELL"
        open_(side, ts)
        if touch_hi and touch_lo:                        # both edges in the entry candle: the stop was hit too
            stop_and_relay(legs[0]["stop"], ts)
            relay_close_check(cl, ts)

    def bar_close(ts, o, h, l_, cl, pc):                 # spike off: only an open / close outside the zone counts
        done_target = False
        if legs:                                         # 1. at the open
            side = legs[0]["side"]
            if (side == "BUY" and o < lo) or (side == "SELL" and o > hi):
                stop_and_relay(o, ts)
        elif can_enter(ts):
            if o > hi and pc <= hi:
                open_("BUY", ts, o)
            elif o < lo and pc >= lo and p["allow_short"]:
                open_("SELL", ts, o)
        if legs:                                         # 2. inside the candle: targets (touch)
            n = len(legs)
            hit_targets(ts, h, l_)
            done_target = len(legs) < n
        if legs:                                         # 3. at the close
            side = legs[0]["side"]
            if (side == "BUY" and cl < lo) or (side == "SELL" and cl > hi):
                stop_and_relay(cl, ts)
        elif not done_target and can_enter(ts) and last_exit[0] != ts:
            if cl > hi and pc <= hi and o <= hi:
                open_("BUY", ts, cl)
            elif cl < lo and pc >= lo and o >= lo and p["allow_short"]:
                open_("SELL", ts, cl)

    def set_iz(ts, h, l_):                               # entry candle finished: its high/low fix the IZ targets
        for leg in legs:
            if leg["target"] is None and leg["_bar"] == ts:
                leg["iz_range"] = round(h - l_, 4)
                leg["range_hi"], leg["range_lo"] = h, l_
                leg["target"] = target_price(base, leg["side"], leg["target_mult"], leg["entry"], hi, lo, h, l_)

    prev_close, last_ts, squared = float(z["close"]), zt, False
    sq_ts = zt.normalize() + pd.Timedelta(hours=sq.hour, minutes=sq.minute)
    step = bar_spike if spike else bar_close
    O, H, L, C = prep["O"], prep["H"], prep["L"], prep["C"]
    for i, ts in enumerate(after):
        o, h, l_, cl = O[i], H[i], L[i], C[i]
        pc, prev_close, last_ts = prev_close, cl, ts
        if ts >= sq_ts:
            close_all(o, ts, "square_off")
            squared = True
            break
        step(ts, o, h, l_, cl, pc)
        if legs:
            set_iz(ts, h, l_)
        if sq_ts < ts + bar:                             # square-off time falls inside this candle: its close
            close_all(cl, ts, "square_off")
            squared = True
            break
    if legs and not squared and len(after) and (last_ts.time() >= dtime(15, 25) or last_ts.normalize() < pd.Timestamp.now().normalize()):
        close_all(float(C[-1]), last_ts, "square_off")       # session over before the square-off time
    if legs:                                             # data ended early (today, still running)
        last = float(C[-1]) if len(after) else z["close"]
        for leg in legs:
            g = round((last - leg["entry"]) * leg["qty"] * (1 if leg["side"] == "BUY" else -1), 2)
            t = {k: v for k, v in leg.items() if k != "_bar"}
            out["trades"].append({**t, "symbol": sym, "entry_ts": str(leg["entry_ts"]), "exit_ts": None, "exit": None,
                                  "exit_reason": "open", "ltp": last, "gross_pnl": g, "costs": 0.0, "net_pnl": g,
                                  "stop": round(leg["stop"], 2), "target": r2(leg["target"]), "entry": round(leg["entry"], 2)})
    if not out["trades"]:
        out["note"] = (f"price stayed inside the zone ({lo:.2f}-{hi:.2f}) until the {p['entry_cutoff']} cut-off"
                       if len(after) else "the zone candle was the last candle")
    return out


def _day_frames(sym: str, start: str, end: str, conn=None) -> dict[pd.Timestamp, pd.DataFrame]:
    df = db.load_candles(sym, "5m", start, end, real_only=True)
    if conn is not None:
        try:
            today = pd.Timestamp.now().normalize()
            have = set(pd.to_datetime(df.index).normalize()) if not df.empty else set()
            want = [d for d in pd.bdate_range(start, end) if d not in have and d <= today]
            if want:
                got = conn.fetch_history(sym, str(want[0].date()), str(want[-1].date()), "5m")
                if got is not None and not got.empty:
                    got.index = pd.to_datetime(got.index)
                    db.save_candles(sym, got[got.index < today], "5m", "upstox")
                    df = got if df.empty else pd.concat([df, got])
        except Exception:
            pass
    if df is None or df.empty:
        return {}
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    now = pd.Timestamp.now()
    df = df[df.index + pd.Timedelta(minutes=5) <= now]                 # completed candles only
    return {d: g for d, g in df.groupby(df.index.normalize())}


def _report(p: dict, trades: list, notes: list, zones: list, start: str, end: str, symbols: list) -> dict:
    tdf = pd.DataFrame(trades)
    closed = tdf[tdf["exit_reason"] != "open"] if not tdf.empty else tdf

    def summ(g):
        return {"trades": int(len(g)), "entries": int(g.groupby(["symbol", "day", "cycle"]).ngroups) if len(g) else 0,
                "net": round(float(g["net_pnl"].sum()), 2), "gross": round(float(g["gross_pnl"].sum()), 2),
                "costs": round(float(g["costs"].sum()), 2), "wins": int((g["net_pnl"] > 0).sum()),
                "win_rate": round(float((g["net_pnl"] > 0).mean() * 100), 1) if len(g) else None}
    by_day = [{"day": d, **summ(g)} for d, g in closed.groupby("day")] if not closed.empty else []
    by_sym = sorted(([{"symbol": s_, **summ(g)} for s_, g in closed.groupby("symbol")] if not closed.empty else []),
                    key=lambda r: -r["net"])
    by_reason = closed.groupby("exit_reason")["net_pnl"].agg(["count", "sum"]).round(2).reset_index().to_dict("records") if not closed.empty else []
    by_lot = closed.groupby("lot")["net_pnl"].agg(["count", "sum"]).round(2).reset_index().to_dict("records") if not closed.empty else []
    total = summ(closed) if not closed.empty else {"trades": 0, "entries": 0, "net": 0.0, "gross": 0.0, "costs": 0.0, "wins": 0, "win_rate": None}
    if by_day:
        eq = pd.Series([r["net"] for r in by_day]).cumsum()
        total["max_drawdown"] = round(float((eq - eq.cummax()).min()), 2)
        total["green_days"] = sum(1 for r in by_day if r["net"] > 0)
        total["days"] = len(by_day)
    return {"params": p, "start": start, "end": end, "symbols": symbols, "total": total, "by_day": by_day, "by_symbol": by_sym,
            "by_reason": by_reason, "by_lot": by_lot, "trades": trades, "zones": zones, "notes": notes,
            "open": [t for t in trades if t["exit_reason"] == "open"]}


def config_label(c: dict) -> str:
    t = lot_targets(c)
    s = f"{int(c['max_cycles'])} entr{'y' if int(c['max_cycles']) == 1 else 'ies'}/day · {len(t)} lot{'s' if len(t) > 1 else ''} · target{'s' if len(t) > 1 else ''} {' / '.join(f'{x:g}x' for x in t)}"
    s += f" {TARGET_BASES.get(c.get('target_base') or 'zone', c.get('target_base'))}"
    s += "" if c.get("spike", True) else " · no spike"
    if c.get("square_off") and c["square_off"] != "15:15":
        s += f" · exit {c['square_off']}"
    return s


def run(symbols: list[str], start: str, end: str, params: dict, progress=lambda *a: None, conn=None,
        configs: list[dict] | None = None) -> dict:
    """configs: several {max_cycles, lot_targets} combinations compared on the same data (one report each)."""
    from yonexis.data.connectors.upstox import instruments
    base = {**DEFAULTS, **(params or {})}
    cfgs = [{**base, **c} for c in (configs or [{}])]
    per = [{"trades": [], "notes": []} for _ in cfgs]
    zones, skipped = [], []
    for i, sym in enumerate(symbols):
        progress(f"replaying {sym} ({i + 1}/{len(symbols)}) with {len(cfgs)} configuration{'s' if len(cfgs) > 1 else ''}",
                 {"done": i, "total": len(symbols)})
        try:
            lot = instruments.lot_size(sym) if sym.endswith("-FUT") else 1
        except Exception:
            lot = 1
        days = _day_frames(sym, start, end, conn)
        if base.get("refined") and days:
            from yonexis import datacheck
            for d in list(days):
                why = datacheck.day_problem(days[d], "5m")
                if why:
                    skipped.append({"symbol": sym, "day": str(d.date()), "reason": why})
                    days.pop(d)
            if not days:
                for x in per:
                    x["notes"].append({"symbol": sym, "day": None, "note": "every day was removed by refined data"})
                continue
        if not days:
            for x in per:
                x["notes"].append({"symbol": sym, "day": None, "note": "no 5-minute candles stored or from Upstox for this range"})
            continue
        for d, g in days.items():
            for k, c in enumerate(cfgs):
                r = run_day(sym, g, c, lot)
                for t in r["trades"]:
                    t["day"] = str(d.date())
                per[k]["trades"] += r["trades"]
                if not r["trades"]:
                    per[k]["notes"].append({"symbol": sym, "day": str(d.date()), "note": r["note"]})
                if k == 0 and r["zone"]:
                    zones.append({"symbol": sym, "day": str(d.date()), **r["zone"]})
    reports = [_report(c, x["trades"], x["notes"], zones, start, end, symbols) for c, x in zip(cfgs, per)]
    for c, r in zip(cfgs, reports):
        r["label"] = config_label(c)
    best = max(range(len(reports)), key=lambda k: reports[k]["total"]["net"]) if reports else 0
    out = dict(reports[0])
    out["configs"] = [{"label": r["label"], "params": {k: r["params"].get(k) for k in ("max_cycles", "lot_targets", "target_mult", "target_base", "spike", "square_off")},
                       "total": r["total"], "by_reason": r["by_reason"], "by_lot": r["by_lot"]} for r in reports]
    out["best"] = best
    out["reports"] = reports
    out["refined_skipped"] = skipped
    for r in reports:
        r["refined_skipped"] = skipped
    return out
