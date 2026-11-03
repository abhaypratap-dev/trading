"""OpenAIAP settings, recorded flows, secrets, symbol mapping and automation settings, all kept in app_settings.

Several websites: each one (a "site") has its own settings, flows, secrets, mapping and automation, kept under its
own keys (openaiap_<site>_flows ...); the first website is "default" and keeps the original keys (openaiap_flows ...).
Every function takes site=...; for_site(site) gives an object with the same functions bound to that website.
"""
from __future__ import annotations

import functools
import re

from yonexis import db
from yonexis.mt5.config import AUTO_DEFAULTS, LIVE_PHRASE

MODES = ("off", "dry_run", "demo", "live")
DEFAULTS = {
    "mode": "off",             # off | dry_run (decide + log, run nothing) | demo (paper / demo web ID) | live (real money)
    "account": "demo",         # what the recorded web ID is: demo (broker's paper / sandbox login) or real
    "live_confirm": "",        # must equal LIVE_PHRASE for mode=live
    "start_url": "",           # broker web terminal opened by "Start browser"
    "magic": 880011,           # tags YONEXIS's own positions in the tracked list
    "deviation": 0,            # MT5 slippage setting: read by the shared AutoTrader, not used by web flows
    "max_volume": 1000.0,      # max quantity of one order (shares / contracts, e.g. one NIFTY lot = 75)
    "max_positions": 10,       # max open YONEXIS positions
    "max_daily_loss": 0.0,     # web terminals report no equity: kept for the shared guard, not used
    "step_timeout_s": 10,      # each step waits up to this long for its element
    "relogin": True,           # a flow that fails before its final step: refresh + retry, then log in again + retry
    "workers": 1,              # browser tabs placing orders in parallel (1 = one at a time); a symbol keeps its tab
}
ORDER_FLOWS = ("buy", "sell")                       # needed before automation can start (close: optional, see executor)
KNOWN_FLOWS = ("login", "buy", "sell", "close", "close_all", "symbols", "prepare")   # prepare: per worker, its symbol
ACTIONS = ("goto", "reload", "click", "click_item", "pick_symbol", "click_xy", "hover", "fill", "type", "press", "select", "scroll", "wait", "wait_for",
           "wait_value", "expect_text", "extract", "extract_list")
VARS = ("symbol", "query", "qty", "side", "side_lower", "close_side", "price", "sl", "target", "yonexis_symbol")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DEFAULT_SITE = "default"


def _k(name: str, site: str | None = DEFAULT_SITE) -> str:
    return f"openaiap_{name}" if site in (None, "", DEFAULT_SITE) else f"openaiap_{site}_{name}"


def get(site: str = DEFAULT_SITE) -> dict:
    return {**DEFAULTS, **(db.get_setting(_k("config", site)) or {})}


def save(changes: dict, site: str = DEFAULT_SITE) -> dict:
    c = {**get(site), **{k: v for k, v in changes.items() if k in DEFAULTS}}
    db.set_setting(_k("config", site), c)
    return c


def public(site: str = DEFAULT_SITE) -> dict:
    c = get(site)
    return {**c, "live_confirm": c.get("live_confirm") == LIVE_PHRASE, "live_phrase": LIVE_PHRASE}


# ---------------------------------------------------------------------------------------------- flows
def flows(site: str = DEFAULT_SITE) -> dict:
    return db.get_setting(_k("flows", site)) or {}


def clean_step(s: dict) -> dict:
    a = str(s.get("action") or "")
    if a not in ACTIONS:
        raise ValueError(f"unknown step action {a!r}")
    out = {"action": a}
    for k in ("selector", "value", "url", "key", "var", "text", "note", "list", "row_tpl"):
        if s.get(k) not in (None, ""):
            out[k] = str(s[k])[:2000]
    if s.get("alt"):
        out["alt"] = [str(x)[:500] for x in s["alt"]][:5]
    for k in ("ms", "dy", "timeout_s", "x", "y"):
        if s.get(k) not in (None, ""):
            out[k] = float(s[k])
    for k in ("optional", "final"):
        if s.get(k):
            out[k] = True
    need = {"goto": "url", "click": "selector", "fill": "selector", "type": "selector", "press": "key",
            "select": "selector", "wait_for": "selector", "expect_text": "text", "extract": "selector",
            "extract_list": "selector", "pick_symbol": "selector", "wait_value": "selector", "hover": "selector", "click_xy": "x", "click_item": "selector"}.get(a)
    if need and out.get(need) is None:
        raise ValueError(f"{a} step needs a {need}")
    if a == "click_item" and not out.get("value"):
        raise ValueError("click_item step needs the row name, e.g. {symbol}")
    if a == "click_xy" and out.get("y") is None:
        raise ValueError("click_xy step needs x and y")
    if a == "pick_symbol":
        out.setdefault("var", "site_name")
    if a in ("extract", "extract_list") and not out.get("var"):
        raise ValueError("extract step needs a variable name")
    return out


def save_flow(name: str, steps: list[dict], start_url: str | None = None, site: str = DEFAULT_SITE) -> dict:
    name = name.strip().lower()
    if not _NAME.match(name):
        raise ValueError("flow name: lower-case letters, digits and _ (e.g. buy, sell, close, login)")
    fl = flows(site)
    fl[name] = {"steps": [clean_step(s) for s in steps][:200], "start_url": start_url or (fl.get(name) or {}).get("start_url") or ""}
    db.set_setting(_k("flows", site), fl)
    return fl[name]


def delete_flow(name: str, site: str = DEFAULT_SITE) -> None:
    fl = flows(site)
    fl.pop(name, None)
    db.set_setting(_k("flows", site), fl)


# ---------------------------------------------------------------------------------------------- secrets
def secrets(site: str = DEFAULT_SITE) -> dict:
    return db.get_setting(_k("secrets", site)) or {}


def secret_names(site: str = DEFAULT_SITE) -> list[str]:
    return sorted(secrets(site))


def save_secrets(changes: dict, site: str = DEFAULT_SITE) -> list[str]:
    """{name: value}; an empty value removes the secret. Values are never sent back to the dashboard."""
    s = secrets(site)
    for k, v in (changes or {}).items():
        k = str(k).strip().lower()
        if not _NAME.match(k):
            raise ValueError(f"secret name {k!r}: lower-case letters, digits and _")
        if v in (None, ""):
            s.pop(k, None)
        else:
            s[k] = str(v)
    db.set_setting(_k("secrets", site), s)
    return sorted(s)


# ---------------------------------------------------------------------------------------------- symbol mapping
def raw_mapping(site: str = DEFAULT_SITE) -> dict:
    """The mapping as saved (no defaults filled in)."""
    return db.get_setting(_k("map", site)) or {}


def mapping(site: str = DEFAULT_SITE) -> dict:
    """YONEXIS symbol -> {"web": the website's name for it ({symbol}), "query": what to type in its search ({query}),
    "volume": quantity per lot}. The "mt5" / "price" keys let yonexis.mt5.auto.AutoTrader read it like the MT5 mapping."""
    from yonexis.openaiap.symbols import query
    return {k: {**v, "query": v.get("query") or query(k), "mt5": v["web"], "price": "same"}
            for k, v in raw_mapping(site).items()}


def save_mapping(m: dict, site: str = DEFAULT_SITE) -> dict:
    clean = {}
    for k, v in (m or {}).items():
        if not v or not str(v.get("web") or "").strip():
            continue
        clean[k.strip().upper()] = {"web": str(v["web"]).strip(), "volume": float(v.get("volume") or 1),
                                    "query": str(v.get("query") or "").strip()}
    db.set_setting(_k("map", site), clean)
    return clean


# ---------------------------------------------------------------------------------------------- automations
# A website can run several automations, each with its own symbols (or a named watchlist) and strategy settings.
# The first one ("main") keeps the original key (openaiap_auto); the others are openaiap_auto_<id>.
MAIN_AUTO = "main"
AUTO_FIELDS = {**AUTO_DEFAULTS, "name": ""}


def _auto_key(aid: str | None) -> str:
    return "auto" if aid in (None, "", MAIN_AUTO) else f"auto_{aid}"


def automation_ids(site: str = DEFAULT_SITE) -> list[str]:
    return [MAIN_AUTO] + [r["id"] for r in (db.get_setting(_k("autos", site)) or []) if r.get("id") != MAIN_AUTO]


def _watchlist(name: str) -> list[str] | None:
    lists = db.get_setting("named_watchlists") or {}
    return sorted({str(x).strip().upper() for x in lists[name] if str(x).strip()}) if name in lists else None


def auto(site: str = DEFAULT_SITE, aid: str = MAIN_AUTO) -> dict:
    """One automation. With a watchlist, its symbols are that list as it is now (edit the list, the automation follows)."""
    a = db.get_setting(_k(_auto_key(aid), site)) or {}
    out = {**AUTO_FIELDS, **a, "params": {**AUTO_DEFAULTS["params"], **(a.get("params") or {})}}
    if out.get("watchlist"):
        wl = _watchlist(out["watchlist"])
        if wl is not None:
            out["symbols"] = wl
    n = automation_ids(site).index(aid) + 1 if aid in automation_ids(site) else 1
    return {**out, "id": aid or MAIN_AUTO, "name": out.get("name") or f"Automation {n}"}


def save_auto(a: dict, site: str = DEFAULT_SITE, aid: str = MAIN_AUTO) -> dict:
    cur = db.get_setting(_k(_auto_key(aid), site)) or {}
    new = {**{k: v for k, v in AUTO_FIELDS.items()}, **cur, **{k: v for k, v in a.items() if k in AUTO_FIELDS}}
    new["params"] = {**AUTO_DEFAULTS["params"], **(cur.get("params") or {}), **(a.get("params") or {})}
    db.set_setting(_k(_auto_key(aid), site), new)
    return auto(site, aid)


def automations(site: str = DEFAULT_SITE) -> list[dict]:
    return [auto(site, i) for i in automation_ids(site)]


def add_automation(name: str, copy_from: str | None = None, site: str = DEFAULT_SITE) -> dict:
    name = str(name or "").strip()[:40]
    if not name:
        raise ValueError("give the automation a name")
    ids = automation_ids(site)
    n = 2
    while f"a{n}" in ids:
        n += 1
    aid = f"a{n}"
    db.set_setting(_k("autos", site), [{"id": i} for i in ids[1:]] + [{"id": aid}])
    src = auto(site, copy_from) if copy_from in ids else {}
    return save_auto({"name": name, "params": src.get("params") or {}, "symbols": [], "watchlist": "", "enabled": False}, site, aid)


def remove_automation(aid: str, site: str = DEFAULT_SITE) -> None:
    if aid == MAIN_AUTO:
        raise ValueError("the first automation cannot be removed (switch it off instead)")
    db.set_setting(_k("autos", site), [{"id": i} for i in automation_ids(site)[1:] if i != aid])
    db.set_setting(_k(_auto_key(aid), site), None)
    db.set_setting(_k(state_key_name(aid), site), None)


def state_key_name(aid: str) -> str:
    """Where an automation's day (zones, lots) is kept: auto_state (main), auto_state_<id> (others)."""
    return "auto_state" if aid in (None, "", MAIN_AUTO) else f"auto_state_{aid}"


def all_symbols(site: str = DEFAULT_SITE) -> list[str]:
    """Every symbol of every automation of the website (workers are assigned over these)."""
    return sorted({s for a in automations(site) for s in a["symbols"]})


def worker_symbols(site: str = DEFAULT_SITE) -> list[str]:
    """The symbols workers are assigned over: those of the running automations (all of them when none runs, so the
    tabs are ready for tests and lookups)."""
    on = sorted({s for a in automations(site) if a["enabled"] for s in a["symbols"]})
    return on or all_symbols(site)


def symbol_owner(site: str = DEFAULT_SITE) -> dict[str, str]:
    """symbol -> the enabled automation trading it on this website (one per symbol: the website nets positions)."""
    out = {}
    for a in automations(site):
        if a["enabled"]:
            for s in a["symbols"]:
                out.setdefault(s, a["id"])
    return out


def missing_flows(site: str = DEFAULT_SITE) -> list[str]:
    fl = flows(site)
    return [f for f in ORDER_FLOWS if not (fl.get(f) or {}).get("steps")]


# ---------------------------------------------------------------------------------------------- several websites
_SITE_FNS = ("get", "save", "public", "flows", "save_flow", "delete_flow", "secrets", "secret_names", "save_secrets",
             "raw_mapping", "mapping", "save_mapping", "auto", "save_auto", "missing_flows", "automation_ids",
             "automations", "add_automation", "remove_automation", "all_symbols", "worker_symbols", "symbol_owner")
_SITE_ID = re.compile(r"[^a-z0-9]+")
SITE_KEYS = ("config", "flows", "secrets", "map", "auto", "auto_state", "positions", "symbols")


class Site:
    """The settings functions bound to one website: Site("zerodha").flows() == flows(site="zerodha")."""

    def __init__(self, site: str | None = DEFAULT_SITE):
        self.id = site or DEFAULT_SITE

    def __getattr__(self, n):
        if n in _SITE_FNS:
            return functools.partial(globals()[n], site=self.id)
        return globals()[n]                              # constants and clean_step are the same for every site

    def key(self, name: str) -> str:
        return _k(name, self.id)


def for_site(site: str | None = DEFAULT_SITE) -> Site:
    return Site(site)


def sites() -> list[dict]:
    """Every website: [{"id", "name"}], the default one first."""
    rows = [r for r in (db.get_setting("openaiap_sites") or []) if r.get("id") != DEFAULT_SITE]
    name = next((r.get("name") for r in (db.get_setting("openaiap_sites") or []) if r.get("id") == DEFAULT_SITE), None)
    return [{"id": DEFAULT_SITE, "name": name or "Website 1"}] + rows


def site_ids() -> set[str]:
    return {r["id"] for r in sites()}


def add_site(name: str) -> dict:
    name = str(name or "").strip()[:40]
    if not name:
        raise ValueError("give the website a name")
    base = _SITE_ID.sub("_", name.lower()).strip("_")[:20] or "site"
    if not base[0].isalpha():
        base = "s_" + base
    sid, n = base, 2
    while sid in site_ids():
        sid, n = f"{base}_{n}", n + 1
    rows = sites()
    rows.append({"id": sid, "name": name})
    db.set_setting("openaiap_sites", rows)
    return {"id": sid, "name": name}


def rename_site(sid: str, name: str) -> list[dict]:
    name = str(name or "").strip()[:40]
    if not name:
        raise ValueError("give the website a name")
    rows = [{**r, "name": name} if r["id"] == sid else r for r in sites()]
    db.set_setting("openaiap_sites", rows)
    return rows


def remove_site(sid: str) -> list[dict]:
    """Forget a website and everything recorded for it (not the default one)."""
    if sid == DEFAULT_SITE:
        raise ValueError("the first website cannot be removed")
    rows = [r for r in sites() if r["id"] != sid]
    db.set_setting("openaiap_sites", rows)
    for aid in automation_ids(sid)[1:]:
        db.set_setting(_k(_auto_key(aid), sid), None)
        db.set_setting(_k(state_key_name(aid), sid), None)
    for k in SITE_KEYS + ("autos",):
        db.set_setting(_k(k, sid), None)
    return rows
