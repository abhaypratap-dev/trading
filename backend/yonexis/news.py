"""News for the watchlist: free RSS headlines + NSE corporate announcements.

Sources
  * Market RSS: Economic Times (markets, stocks), Moneycontrol (market reports, business), Business Standard
    (markets), LiveMint (markets). Headlines are matched to watchlist stocks by symbol / company name.
  * Google News RSS search per stock ("<company> share NSE", India edition) for stock-specific headlines.
  * NSE corporate announcements per stock (results, board meetings, dividends, orders...). NSE blocks plain
    scripts sometimes; we open the NSE home page first for cookies, as a browser does, and skip quietly if refused.

Sentiment: a finance word list (deterministic and instant) scores each headline -1..+1; "results", "order win",
"upgrade", "fraud", "probe" etc. are weighted. Stored in the `news` table (deduplicated by url+symbol).
Uses: the Live tab news panel, a "news today" flag per stock (avoid trading into big news or use it for
Gap & Go), and features for the AI picker once enough history has accumulated.
"""
from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx

from yonexis import db

log = logging.getLogger("yonexis.news")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
      "Accept": "*/*", "Accept-Language": "en-IN,en;q=0.9"}
FEEDS = {
    "Economic Times": ["https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                       "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"],
    "Moneycontrol": ["https://www.moneycontrol.com/rss/marketreports.xml", "https://www.moneycontrol.com/rss/business.xml"],
    "Business Standard": ["https://www.business-standard.com/rss/markets-106.rss"],
    "LiveMint": ["https://www.livemint.com/rss/markets"],
}
POS = {"surge": 2, "surges": 2, "soar": 2, "soars": 2, "jump": 1.5, "jumps": 1.5, "rally": 1.5, "rallies": 1.5, "gain": 1, "gains": 1,
       "rise": 1, "rises": 1, "record": 1, "beats": 2, "beat": 1.5, "upgrade": 2, "upgrades": 2, "outperform": 1.5,
       "buy": 1, "order": 1, "wins": 1.5, "win": 1, "bags": 1.5, "approval": 1.5, "approves": 1, "dividend": 1, "bonus": 1.5,
       "buyback": 1.5, "acquire": 1, "acquires": 1, "expansion": 1, "strong": 1, "profit": 1, "growth": 1, "highs": 1.5,
       "upbeat": 1.5, "robust": 1.5, "boost": 1, "boosts": 1, "positive": 1, "target": 0.5}
NEG = {"plunge": 2, "plunges": 2, "crash": 2.5, "slump": 2, "slumps": 2, "fall": 1, "falls": 1, "drop": 1, "drops": 1, "decline": 1,
       "declines": 1, "loss": 1.5, "losses": 1.5, "downgrade": 2, "downgrades": 2, "sell": 1, "fraud": 3, "probe": 2.5, "raid": 2.5,
       "penalty": 2, "fine": 1, "resigns": 2, "resignation": 2, "default": 2.5, "weak": 1.5, "misses": 2, "miss": 1.5, "cut": 1,
       "cuts": 1, "ban": 2, "lows": 1.5, "warning": 1.5, "pledge": 1.5, "lawsuit": 2, "slowdown": 1.5, "negative": 1, "concern": 1,
       "concerns": 1, "sebi": 0.5, "halt": 2, "strike": 1.5, "fire": 1.5, "recall": 2}


def sentiment(text: str) -> float:
    words = re.findall(r"[a-z]+", text.lower())
    p = sum(POS.get(w, 0) for w in words)
    n = sum(NEG.get(w, 0) for w in words)
    if "not" in words or "no" in words:
        p, n = p * 0.6, n * 0.6
    return round((p - n) / (p + n), 3) if p + n else 0.0


def _aliases(symbols: list[str]) -> dict[str, list[str]]:
    """symbol -> lowercase names to look for in headlines (symbol, company name without Ltd/Limited)."""
    names = {}
    try:
        from yonexis.data.connectors.upstox import instruments
        instruments._load()
        for s in symbols:
            base = s.replace("-FUT", "")
            r = instruments._by_symbol.get(base)
            n = (r or {}).get("n") or ""
            n = re.sub(r"\b(limited|ltd\.?|industries|corporation|company|co\.?|india)\b", " ", n.lower())
            n = re.sub(r"\s+", " ", n).strip()
            names[s] = [x for x in {base.lower(), n} if x and len(x) >= 3]
    except Exception:
        names = {s: [s.replace("-FUT", "").lower()] for s in symbols}
    return names


def _parse_rss(xml_text: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(pub).astimezone().replace(tzinfo=None) if pub else datetime.now()
        except Exception:
            ts = datetime.now()
        if title and link:
            out.append({"title": title, "url": link, "ts": ts})
    return out


def _save(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        try:
            with db.engine.begin() as cx:
                cx.execute(db.news.insert().values(**r))
            n += 1
        except Exception:
            pass               # duplicate url+symbol
    return n


def collect(symbols: list[str], google: bool = True, nse: bool = True) -> dict:
    al = _aliases(symbols)
    saved, errors = 0, []
    with httpx.Client(headers=UA, timeout=15, follow_redirects=True) as c:
        for src, urls in FEEDS.items():
            for u in urls:
                try:
                    items = _parse_rss(c.get(u).text)
                except Exception as e:
                    errors.append(f"{src}: {e}")
                    continue
                rows = []
                for it in items:
                    low = it["title"].lower()
                    hit = [s for s, names in al.items() if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names)]
                    for s in hit or ["MARKET"]:
                        rows.append({"ts": it["ts"], "symbol": s, "source": src, "title": it["title"][:500], "url": it["url"][:1000],
                                     "kind": "headline", "sentiment": sentiment(it["title"])})
                saved += _save(rows)
        if google:
            for s, names in al.items():
                q = quote_plus(f"{max(names, key=len)} share NSE")
                try:
                    items = _parse_rss(c.get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en").text)[:15]
                except Exception as e:
                    errors.append(f"Google News {s}: {e}")
                    continue
                saved += _save([{"ts": it["ts"], "symbol": s, "source": "Google News", "title": it["title"][:500],
                                 "url": it["url"][:1000], "kind": "headline", "sentiment": sentiment(it["title"])} for it in items])
                time.sleep(0.3)
        if nse:
            saved += _nse_announcements(c, [s for s in symbols if not s.endswith("-FUT") or True], errors)
    db.set_setting("news_last", {"at": datetime.now().isoformat(timespec="seconds"), "saved": saved, "errors": errors[:10]})
    return {"saved": saved, "errors": errors[:10]}


def _nse_announcements(c: httpx.Client, symbols: list[str], errors: list) -> int:
    saved = 0
    try:
        c.get("https://www.nseindia.com", headers={**UA, "Accept": "text/html"})
    except Exception as e:
        errors.append(f"NSE: {e}")
        return 0
    for s in sorted({x.replace("-FUT", "") for x in symbols}):
        if s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            continue
        try:
            r = c.get(f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={quote_plus(s)}",
                      headers={**UA, "Accept": "application/json", "Referer": "https://www.nseindia.com/"})
            if r.status_code != 200:
                errors.append(f"NSE {s}: HTTP {r.status_code}")
                if r.status_code in (401, 403):
                    break
                continue
            rows = []
            for a in (r.json() or [])[:20]:
                title = f"{a.get('desc') or ''}: {a.get('attchmntText') or ''}".strip(": ")[:500]
                try:
                    ts = datetime.strptime(a.get("an_dt") or a.get("sort_date") or "", "%d-%b-%Y %H:%M:%S")
                except Exception:
                    ts = datetime.now()
                url = a.get("attchmntFile") or f"https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol={s}#{ts:%Y%m%d%H%M%S}"
                for sym in [x for x in symbols if x.replace("-FUT", "") == s]:
                    rows.append({"ts": ts, "symbol": sym, "source": "NSE", "title": title, "url": url[:1000],
                                 "kind": "announcement", "sentiment": sentiment(title)})
            saved += _save(rows)
            time.sleep(0.4)
        except Exception as e:
            errors.append(f"NSE {s}: {e}")
    return saved


def collect_safe(symbols: list[str]) -> None:
    try:
        collect(symbols)
    except Exception:
        log.exception("news collection failed")


def recent(symbol: str | None = None, hours: int = 72, limit: int = 100) -> list[dict]:
    since = datetime.now() - timedelta(hours=hours)
    w = db.news.c.ts >= since
    if symbol:
        w = w & (db.news.c.symbol == symbol.upper())
    rows = db.fetch(db.news, w, order=db.news.c.ts.desc(), limit=limit)
    return rows


def summary(symbols: list[str], hours: int = 24) -> dict:
    """Per symbol: number of headlines and average sentiment in the last `hours` (for the watchlist flag)."""
    since = datetime.now() - timedelta(hours=hours)
    out = {}
    for r in db.fetch(db.news, db.news.c.ts >= since):
        s = r["symbol"]
        if s not in symbols:
            continue
        o = out.setdefault(s, {"n": 0, "sent": 0.0, "announcements": 0, "latest": None})
        o["n"] += 1
        o["sent"] += r["sentiment"] or 0
        o["announcements"] += r["kind"] == "announcement"
        if o["latest"] is None or r["ts"] > o["latest"]["ts"]:
            o["latest"] = {"ts": r["ts"], "title": r["title"], "source": r["source"]}
    for o in out.values():
        o["sent"] = round(o["sent"] / o["n"], 2) if o["n"] else 0
        if o["latest"]:
            o["latest"]["ts"] = str(o["latest"]["ts"])
    return out
