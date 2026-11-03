"""MT5 settings (System tab), symbol mapping and automation settings (MT5 tab), all kept in app_settings."""
from __future__ import annotations

import os
import re
import secrets

from yonexis import db

LIVE_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"
MODES = ("off", "dry_run", "demo", "live")
DEFAULTS = {
    "mode": "off",             # off | dry_run (decide + log, send nothing) | demo (demo account only) | live (real account)
    "transport": "file",       # file (Mac, MT5 Common Files folder) | web (EA calls /api/mt5/bridge over HTTPS)
    "live_confirm": "",        # must equal LIVE_PHRASE for mode=live
    "token": "",               # bridge token for the web transport
    "magic": 770011,           # MT5 magic number of every YONEXIS order
    "deviation": 20,           # max slippage in points for market orders
    "max_volume": 10.0,        # max volume of one order
    "max_positions": 20,       # max open YONEXIS positions
    "max_daily_loss": 0.0,     # stop new entries + close all when the day's loss reaches this (account currency), 0 = off
}
AUTO_DEFAULTS = {
    "enabled": False, "watchlist": "", "symbols": [],
    "params": {"max_cycles": 2, "lot_targets": [1.5], "target_mult": 1.5, "target_base": "zone", "spike": True,
               "entry_cutoff": "14:30", "square_off": "15:15", "allow_short": True, "max_zone_pct": 100.0},
}


def get() -> dict:
    c = {**DEFAULTS, **(db.get_setting("mt5_config") or {})}
    if not c.get("token"):
        c["token"] = secrets.token_urlsafe(24)
        db.set_setting("mt5_config", c)
    return c


def save(changes: dict) -> dict:
    c = {**get(), **{k: v for k, v in changes.items() if k in DEFAULTS}}
    db.set_setting("mt5_config", c)
    return c


def common_dir() -> str:
    """Where the api container sees MT5's Common Files folder (docker-compose mounts MT5_COMMON_DIR there)."""
    return os.getenv("MT5_COMMON_MOUNT", "/mt5_common")


MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_ROLL_RE = re.compile(r"^(?P<base>.+?)(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<suffix>-[A-Z]+)?$")
_month_cache: dict = {}


def near_month(symbol: str) -> str | None:
    """Month code (NOV, DEC ...) of the near-month futures contract on NSE, from the Upstox instrument list."""
    import datetime as _dt
    key = (symbol, _dt.date.today())
    if key not in _month_cache:
        try:
            from yonexis.data.connectors.upstox import instruments
            exp = instruments.contract(symbol)["expiry"]
            _month_cache[key] = MONTHS[int(str(exp)[5:7]) - 1]
        except Exception:
            _month_cache[key] = None
    return _month_cache[key]


def mapping() -> dict:
    """Saved mapping with month-coded futures (ANGELONENOV-LOT) rolled to the current near month automatically."""
    out = {}
    for k, v in (db.get_setting("mt5_map") or {}).items():
        v = dict(v)
        if v.get("roll"):
            mon = near_month(k)
            if mon:
                v["mt5"] = f"{v['base']}{mon}{v.get('suffix') or ''}"
        out[k] = v
    return out


def save_mapping(m: dict) -> dict:
    clean = {}
    for k, v in (m or {}).items():
        if not v or not str(v.get("mt5") or "").strip():
            continue
        name = str(v["mt5"]).strip()
        row = {"mt5": name, "volume": float(v.get("volume") or 1.0), "price": "offset" if v.get("price") == "offset" else "same"}
        mm = _ROLL_RE.match(name.upper()) if k.upper().endswith("-FUT") and v.get("roll", True) else None
        if mm:                                   # month-coded contract: follow the near month after each expiry
            row.update(roll=True, base=name[:mm.start("mon")], suffix=name[mm.end("mon"):])
        clean[k.upper()] = row
    db.set_setting("mt5_map", clean)
    return clean


def suggest(symbols: list[str], mt5_names: list[str]) -> dict:
    """Best MT5 symbol for each YONEXIS symbol. Futures: <BASE><near month>[-LOT]; stocks / indices: exact name."""
    names = {n.upper(): n for n in mt5_names}
    out = {}
    for s in symbols:
        s = s.upper()
        if s.endswith("-FUT"):
            base = s[:-4]
            mon = near_month(s)
            cands = [n for u, n in names.items() if (m := _ROLL_RE.match(u)) and m.group("base") == base]
            pick = next((n for n in cands if mon and _ROLL_RE.match(n.upper()).group("mon") == mon), None)
            if not pick and cands:                   # no Upstox month known: the nearest calendar month offered
                order = {mo: i for i, mo in enumerate(MONTHS)}
                import datetime as _dt
                cur = _dt.date.today().month - 1
                pick = min(cands, key=lambda n: (order[_ROLL_RE.match(n.upper()).group("mon")] - cur) % 12)
            if not pick:
                pick = names.get(base + "-FUT") or names.get(base + "-I") or names.get(base)
        else:
            pick = names.get(s) or names.get(s + "-EQ")
        if pick:
            out[s] = pick
    return out


def auto() -> dict:
    a = db.get_setting("mt5_auto") or {}
    return {**AUTO_DEFAULTS, **a, "params": {**AUTO_DEFAULTS["params"], **(a.get("params") or {})}}


def save_auto(a: dict) -> dict:
    cur = auto()
    new = {**cur, **{k: v for k, v in a.items() if k in AUTO_DEFAULTS}}
    new["params"] = {**cur["params"], **(a.get("params") or {})}
    db.set_setting("mt5_auto", new)
    return new


def public() -> dict:
    """Config for the dashboard (the live confirmation is shown as set / not set)."""
    c = get()
    return {**c, "live_confirm": c.get("live_confirm") == LIVE_PHRASE, "live_phrase": LIVE_PHRASE}
