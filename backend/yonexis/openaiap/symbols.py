"""Symbol matching for OpenAIAP: names read from the broker's website, matched to YONEXIS (Upstox) symbols.

Two ways, chosen by the recorded "symbols" flow:
  * lookup (the flow ends with a click on one result, or a pick_symbol step): for EACH YONEXIS symbol the flow types
    its search text ({query} / {symbol} = NIFTY for NIFTY-FUT) and, of the results shown, clicks the one that is this
    symbol's contract; its name is saved as the mapping's web symbol. Runs on the workers in parallel.
  * list (the flow has an extract_list step): every name the page lists is read, then matched (suggest).

Candles and prices always come from Upstox; the website is only searched for the name it uses for the same contract.
The "symbols" flow is recorded once: type {query} into the site's search box and read every result with an
extract_list step ("read all like this"). For each YONEXIS symbol it runs with {query} = the base name (NIFTY for
NIFTY-FUT); a flow without {query} (a page listing everything) runs once. The names found are kept in app_settings
and scored against the Upstox contract: base name, FUT or not, near-month expiry; options are never chosen.
"""
from __future__ import annotations

import re
import threading
import time

from yonexis import db

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
FULL_MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
               "NOVEMBER", "DECEMBER")
NEUTRAL = {"FUT", "FUTURE", "FUTURES", "FUTIDX", "FUTSTK", "EQ", "NSE", "BSE", "NFO", "INDEX", "LTD", "LIMITED", "IDX",
           "STK", "CASH", "SPOT", "I", "II", "III", *MONTHS, *FULL_MONTHS}


def query(sym: str) -> str:
    """What to type in the website's search box for a YONEXIS symbol: NIFTY-FUT -> NIFTY, RELIANCE-EQ -> RELIANCE."""
    return re.sub(r"-(FUT|EQ|I|BE)$", "", sym.strip().upper())


def search_terms(sym: str, given: str | None = None) -> list[str]:
    """What to type in the website's search, best first: the saved / typed search text, the base name, its first
    word (BAJAJ-AUTO -> BAJAJ), the words joined (BAJAJAUTO), then shorter beginnings (ANGELO, ANGE, ANG) - a site
    that finds nothing for the whole name still finds the contract with a shorter search."""
    q = query(sym)
    toks = [t for t in re.split(r"[\s\-_&./]+", q) if t]
    first = toks[0] if toks else q
    out = [given, q, first, "".join(toks)] + [first[:k] for k in (6, 4, 3) if len(first) > k]
    seen, res = set(), []
    for t in out:
        if t and t.strip() and t.strip().upper() not in seen:
            seen.add(t.strip().upper())
            res.append(t.strip())
    return res


def contract_info(sym: str) -> dict:
    """Near-month expiry of a future from the Upstox instrument list (month code + day); {} for stocks / unknown."""
    if not sym.upper().endswith("-FUT"):
        return {}
    try:
        from yonexis.data.connectors.upstox import instruments
        c = instruments.contract(sym)
        y, m, d = str(c["expiry"])[:10].split("-")
        return {"month": MONTHS[int(m) - 1], "day": int(d), "year": int(y), "trading_symbol": c.get("trading_symbol")}
    except Exception:
        return {}


def score(sym: str, name: str, info: dict | None = None) -> float:
    """How well a website name fits the YONEXIS symbol (< 0: not this instrument)."""
    info = info or {}
    n = name.upper()
    base = query(sym)
    btoks = re.findall(r"[A-Z0-9]+", base)                       # BAJAJ-AUTO -> BAJAJ, AUTO; M&M -> M, M
    base_c = "".join(btoks)
    toks = re.findall(r"[A-Z0-9]+", n)
    compact = re.sub(r"[^A-Z0-9]", "", n)
    lead = bool(btoks) and toks[:len(btoks)] == btoks
    if not toks or not (lead or compact.startswith(base_c) or (len(btoks) == 1 and base_c in toks)):
        return -1
    if not lead and compact.startswith(base_c):                  # NIFTY25NOVFUT: what follows the base must be a date
        rest = compact[len(base_c):]
        if rest and not re.match(r"^(\d|FUT|EQ|" + "|".join(MONTHS) + ")", rest):
            return -1                                            # NIFTYIT, NIFTYNXT50 ... are other instruments
    s = 10.0 if lead or compact.startswith(base_c) else 4.0
    if re.search(r"\d(CE|PE)\b|\b(CE|PE|CALL|PUT|OPT|OPTIDX|OPTSTK)\b", n):
        return -1                                                # options are never the match
    is_fut = bool(re.search(r"FUT", n))
    months_in = {m for m in MONTHS if re.search(rf"(\b|\d){m}(\b|\d|FUT)", n)} | \
                {MONTHS[i] for i, f in enumerate(FULL_MONTHS) if f in toks}
    if sym.upper().endswith("-FUT"):
        s += 10 if is_fut else (-4 if months_in else -12)
        if info.get("month"):
            if info["month"] in months_in:
                s += 8
            elif months_in:
                s -= 6                                           # a later month's contract
    else:
        if is_fut or months_in:
            return -1
        s += 3 + (2 if {"EQ", "NSE"} & set(toks) else 0)
    extra = [t for t in toks[len(btoks) if lead else 0:] if t not in btoks and t not in NEUTRAL and not t.isdigit()
             and not re.match(rf"^\d{{0,2}}({'|'.join(MONTHS)})\d{{0,4}}$", t)]            # 25NOV, 23NOV2026
    s -= 3 * len(extra)                                          # NIFTY BANK, NIFTY NEXT 50 ... are other indices
    return round(s - len(n) * 0.01, 3)


def suggest(symbols: list[str], found: dict, site: str = "default") -> dict:
    """{yonexis symbol: {"web": best name, "score": s, "candidates": [...top 8]}} from the names found per query."""
    allnames = sorted({n for v in found.values() for n in v})
    out = {}
    lk = lookups(site)
    for sym in symbols:
        sym = sym.upper()
        if (lk.get(sym) or {}).get("web"):                # looked up on the website: that name
            out[sym] = {"web": lk[sym]["web"], "score": None, "query": query(sym), "expiry": contract_info(sym),
                        "candidates": [lk[sym]["web"]], "lookup": True}
            continue
        names = found.get(query(sym)) or allnames
        info = contract_info(sym)
        ranked = sorted(((score(sym, n, info), n) for n in set(names)), reverse=True)
        ranked = [(s, n) for s, n in ranked if s > 0]
        out[sym] = {"web": ranked[0][1] if ranked else None, "score": ranked[0][0] if ranked else None,
                    "query": query(sym), "expiry": info, "candidates": [n for _, n in ranked[:8]]}
    return out


# ---------------------------------------------------------------------------------------------- fetching
JOBS: dict[str, dict] = {}


def job(site: str = "default") -> dict:
    """Progress of the last fetch of one website."""
    return JOBS.setdefault(site, {"running": False})


def _store(site: str) -> dict:
    from yonexis.openaiap import config
    return db.get_setting(config.for_site(site).key("symbols")) or {}


def _save_store(site: str, v: dict) -> None:
    from yonexis.openaiap import config
    db.set_setting(config.for_site(site).key("symbols"), v)


def found(site: str = "default") -> dict:
    return _store(site).get("found") or {}


def lookup_steps(steps: list[dict]) -> list[dict] | None:
    """The flow as a lookup: its last click on a result becomes pick_symbol over every result like it.
    None when the flow is a list read (extract_list) or has no such click."""
    if any(s["action"] == "extract_list" for s in steps):
        return None
    if any(s["action"] == "pick_symbol" for s in steps):
        return [{"timeout_s": 4, **s} if s["action"] == "pick_symbol" else s for s in steps]   # results show fast
    for k in range(len(steps) - 1, -1, -1):
        st = steps[k]
        if st["action"] in ("click", "click_item"):
            sel = None
            if st.get("row_tpl"):                        # tr:has(:text-is("{symbol}")) > :nth-child(1) -> tr > :nth-child(1)
                sel = re.sub(r'^(\S+?):has\(:text-is\("\{\w+\}"\)\)', r"\1", st["row_tpl"])
            elif st["action"] == "click_item" or st.get("list"):
                sel = st["selector"] if st["action"] == "click_item" else st["list"]
            if sel:
                return steps[:k] + [{"action": "pick_symbol", "selector": sel, "var": "site_name", "timeout_s": 4}] + steps[k + 1:]
    return None


def lookups(site: str = "default") -> dict:
    """YONEXIS symbol -> {"web": name found on the website, "at": time} | {"error": ...}."""
    return _store(site).get("lookups") or {}


def fetch(symbols: list[str], site: str = "default") -> dict:
    """Start reading the website's names for these symbols in the background (progress in job(site))."""
    from yonexis.openaiap import config
    fl = (config.flows(site=site).get("symbols") or {}).get("steps")
    if not fl:
        raise RuntimeError("record the symbols flow first: search the symbol, then click its result (or pick one result and 'read all like this')")
    if job(site).get("running"):
        raise RuntimeError("already fetching symbols")
    lk = lookup_steps(fl)
    if lk is not None:
        return _fetch_lookup(symbols, lk, site)
    return _fetch_list(symbols, fl, site)


def _fetch_lookup(syms: list[str], steps: list[dict], site: str = "default") -> dict:
    from yonexis.openaiap import browser as browser_mod, config as config_mod, player
    config, pool, JOB = config_mod.for_site(site), browser_mod.get(site)[1], job(site)
    syms = sorted({s.upper() for s in syms})
    ok, where = pool.main.logged_in()
    if not ok:
        raise RuntimeError(f"the main tab is not logged in (it shows {where}) - run the login flow first "
                           "(Steps: login -> Test run); the worker tabs then take that login")
    JOB.clear()
    JOB.update(running=True, mode="lookup", total=len(syms), done=0, errors=[], started=time.time(), found={})
    sec, tmo = config.secrets(), float(config.get()["step_timeout_s"])
    n = max(1, min(pool.size(), len(syms)))
    lock = threading.Lock()

    def one(w, sym):
        given = (config.raw_mapping().get(sym) or {}).get("query") or None   # a search text typed in the mapping
        tried, resynced = [], False
        if w.main is not None:
            w.ensure_like_main()                          # a blank / logged-out worker tab takes the main tab's login
        for term in search_terms(sym, given):            # whole name first, then shorter searches
            r = player.run(w, "symbols", steps, {"query": term, "symbol": term, "yonexis_symbol": sym}, sec, False, tmo)
            if not r["ok"] and not resynced and w.main is not None and (r["steps"] or [{}])[0].get("ok") is False:
                resynced = True                           # even the first step failed: the tab is not the website
                w.resync()
                r = player.run(w, "symbols", steps, {"query": term, "symbol": term, "yonexis_symbol": sym}, sec, False, tmo)
            tried.append(term)
            if r["ok"] and r["vars"].get("site_name"):
                break
            if not r["ok"] and (r["steps"] or [{}])[0].get("ok") is False:
                break                                     # the search box itself is missing: shorter searches cannot help
        w.prepared_for = None                            # its market watch now shows the search, not a prepared symbol
        name = r["vars"].get("site_name")
        if not name:
            r["error"] = f"{r.get('error') or 'not found'} (searched: {', '.join(tried)})"
        with lock:
            cur = _store(site)
            lk = cur.get("lookups") or {}
            lk[sym] = {"web": name, "at": time.time()} if r["ok"] and name else {"error": r.get("error") or "not found", "at": time.time()}
            cur["lookups"] = lk
            _save_store(site, cur)
            if r["ok"] and name:
                m = {k: {"web": v["web"], "volume": v["volume"], "query": v.get("query") or ""} for k, v in config.mapping().items()}
                m[sym] = {**(m.get(sym) or {"volume": 1, "query": ""}), "web": name, "query": tried[-1]}   # the search that found it
                config.save_mapping(m)
                JOB["found"][sym] = name
            else:
                JOB["errors"].append(f"{sym}: {r.get('error') or 'not found'}")
            JOB["done"] += 1

    def worker_loop(i, part):
        w = pool.get(i)
        for sym in part:
            try:
                w.submit(one, w, sym).result(180)
            except Exception as e:
                with lock:
                    JOB["errors"].append(f"{sym}: {str(e)[:200]}")
                    JOB["done"] += 1

    def work():
        ts = [threading.Thread(target=worker_loop, args=(i, syms[i::n]), daemon=True) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        JOB["running"] = False
    threading.Thread(target=work, name="openaiap-lookup", daemon=True).start()
    return JOB


def _fetch_list(symbols: list[str], fl: list[dict], site: str = "default") -> dict:
    from yonexis.openaiap import browser as browser_mod, config as config_mod, player
    config, browser, JOB = config_mod.for_site(site), browser_mod.get(site)[0], job(site)
    uses_query = any("{query" in (str(s.get("value", "")) + str(s.get("selector", ""))) for s in fl)
    queries = sorted({query(s) for s in symbols}) if uses_query else ["*"]
    JOB.clear()
    JOB.update(running=True, mode="list", total=len(queries), done=0, errors=[], started=time.time(), found={})

    def work():
        got = dict(found(site))
        sec, tmo = config.secrets(), float(config.get()["step_timeout_s"])
        try:
            for q in queries:
                r = browser.submit(player.run, browser, "symbols", fl, {"query": None if q == "*" else q}, sec, False, tmo).result(180)
                names = sorted({str(x).strip()[:80] for v in r["vars"].values() if isinstance(v, list) for x in v} - {""})
                if not r["ok"]:
                    JOB["errors"].append(f"{q}: {r.get('error')}")
                if q == "*":
                    got = {"*": names}
                else:
                    got[q] = names
                JOB["done"] += 1
                _save_store(site, {**_store(site), "found": got, "at": time.time()})
        except Exception as e:
            JOB["errors"].append(str(e)[:300])
        finally:
            JOB["running"] = False
    threading.Thread(target=work, name="openaiap-symbols", daemon=True).start()
    return JOB
