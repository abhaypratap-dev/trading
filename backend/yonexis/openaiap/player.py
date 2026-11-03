"""Replays a recorded flow in the OpenAIAP browser.

Values AND selectors may hold variables, filled per order: {symbol} {query} {qty} {side} {side_lower} {close_side}
{price} {sl} {target} {yonexis_symbol}, values read earlier in the flow by an extract step, and {secret:name}
(OpenAIAP secrets, never shown). {name|re} is the value escaped for a regular expression, e.g. the selector
text=/^\\s*{symbol|re}\\s*$/i clicks the search result whose text is exactly this order's symbol.
Each step waits up to its timeout for its element; the selector and its alternatives are tried in every frame.
click_xy clicks a fixed point of the 1280x800 screen (canvas / chart terminals that have no elements to find).
  optional: a failure is ignored (e.g. closing a pop-up that is not always there)
  final:    the step that commits the order (the last "Buy" / "Confirm" click). A test run with dry=True stops
            before it, and a failed flow is only retried (after logging in again) when no final step has run.
"""
from __future__ import annotations

import re
import time

from yonexis.openaiap.browser import CORE_JS, Browser, norm

_VAR = re.compile(r"\{(secret:)?([a-z_][a-z0-9_]*)(\|re)?\}")
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def render(text: str, vars: dict, secrets: dict) -> str:
    def sub(m):
        if m.group(1):
            if m.group(2) not in secrets:
                raise ValueError(f"secret {m.group(2)} is not set (OpenAIAP > Settings > Secrets)")
            v = str(secrets[m.group(2)])
        elif m.group(2) not in vars or vars[m.group(2)] is None:
            raise ValueError(f"variable {{{m.group(2)}}} has no value for this order")
        else:
            v = str(vars[m.group(2)])
        return re.escape(v).replace("/", "\\/").replace("\\ ", "\\s*") if m.group(3) else v
    return _VAR.sub(sub, str(text))


def number(text: str) -> float | None:
    m = _NUM.search(str(text or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _frames(page):
    return [page.main_frame] + [f for f in page.frames if f is not page.main_frame]


def _find(page, step: dict, timeout_s: float, vars: dict | None = None, secrets: dict | None = None):
    """First visible match of the selector or one of its alternatives, in any frame, within the timeout.
    Alternatives that need a variable this order does not have are skipped."""
    sels = []
    for s in [step["selector"]] + list(step.get("alt") or []):
        try:
            sels.append(render(s, vars or {}, secrets or {}))
        except ValueError:
            if not sels and s == step["selector"]:
                raise
    sels = list(dict.fromkeys(sels))
    end = time.time() + timeout_s
    while True:
        for fr in _frames(page):
            for s in sels:
                try:
                    loc = fr.locator(s)
                    if loc.count() and loc.first.is_visible():
                        return loc.first
                except Exception:
                    continue
        if time.time() > end:
            raise TimeoutError(f"element not found: {sels[0]}")
        page.wait_for_timeout(200)


def _rows(page, sel: str, visible: bool = False) -> list[tuple]:
    """(frame, index, name) of every element matching sel, in every frame (visible=True: shown ones only)."""
    out = []
    js = f"els => els.map(e => ({'!e.getClientRects().length ? null : ' if visible else ''}({CORE_JS.strip()})(e)))"
    for fr in _frames(page):
        try:
            out += [(fr, i, n) for i, n in enumerate(fr.locator(sel).evaluate_all(js)) if n is not None]
        except Exception:
            continue
    return out


def _pick_symbol(page, sel: str, yx_symbol: str, timeout_s: float) -> str:
    """Symbol lookup: of the visible results like sel, click the one that is the YONEXIS symbol's contract
    (yonexis.openaiap.symbols.score: base name, futures by the Upstox near month, never options). Returns its name."""
    from yonexis.openaiap import symbols
    info, end = symbols.contract_info(yx_symbol), time.time() + timeout_s
    while True:
        rows = _rows(page, sel, visible=True)
        ranked = sorted(((symbols.score(yx_symbol, n, info), k, fr, i, n) for k, (fr, i, n) in enumerate(rows) if n), reverse=True)
        if ranked and ranked[0][0] > 0:
            _, _, fr, i, n = ranked[0]
            fr.locator(sel).nth(i).click(timeout=timeout_s * 1000)
            return n
        if time.time() > end:
            seen = ", ".join(n for _, _, n in rows[:6])
            raise TimeoutError(f"no result matches {yx_symbol} (seen: {seen or 'none'})")
        page.wait_for_timeout(250)


_SHOWS_JS = """(want) => [...document.querySelectorAll('td,th,span,div,li,a,p,b,strong,label,option,button')].some(e =>
  e.getClientRects().length && e.children.length <= 3 && (e.innerText || '').replace(/\\s+/g, '').toUpperCase() === want)"""


def shows(page, text: str, timeout_s: float = 3) -> bool:
    """Is an element whose whole text is `text` visible (any frame)? Used to check a prepare flow worked."""
    want, end = norm(text), time.time() + timeout_s
    while True:
        for fr in _frames(page):
            try:
                if fr.evaluate(_SHOWS_JS, want):
                    return True
            except Exception:
                continue
        if time.time() > end:
            return False
        page.wait_for_timeout(200)


def _click_item(page, sel: str, name: str, timeout_s: float) -> None:
    want, end = norm(name), time.time() + timeout_s
    while True:
        rows = _rows(page, sel)
        hit = next(((fr, i) for fr, i, n in rows if norm(n) == want), None)
        if hit:
            hit[0].locator(sel).nth(hit[1]).click(timeout=timeout_s * 1000)
            return
        if time.time() > end:
            seen = ", ".join(n for _, _, n in rows[:6])
            raise TimeoutError(f"no row named {name!r} in {sel} (seen: {seen or 'none'})")
        page.wait_for_timeout(200)


def run(b: Browser, name: str, steps: list[dict], vars: dict, secrets: dict, dry: bool = False,
        step_timeout_s: float = 10, max_s: float = 90) -> dict:
    """Runs on the browser worker thread (submit it with b.submit). Never raises: the result says what failed."""
    t0 = time.time()
    vars = dict(vars)
    out = {"flow": name, "ok": True, "steps": [], "vars": {}, "final_ran": False, "dry": dry}
    b.busy = name
    try:
        page = b._page()
        for i, st in enumerate(steps):
            a = st["action"]
            rec = {"i": i + 1, "action": a, "ok": True}
            if st.get("final") and dry:
                rec.update(ok=None, note="dry run: final step not done")
                out["steps"].append(rec)
                break
            if time.time() - t0 > max_s:
                raise TimeoutError(f"flow took longer than {max_s:.0f} s")
            s0 = time.time()
            to = float(st.get("timeout_s") or step_timeout_s)
            try:
                page = b._page()                       # an earlier click may have opened a pop-up window
                if a == "goto":
                    page.goto(render(st["url"], vars, secrets), wait_until="domcontentloaded", timeout=to * 3000)
                elif a == "reload":                      # fresh page: the site's live data feed starts again
                    page.reload(wait_until="domcontentloaded", timeout=to * 3000)
                    page.wait_for_timeout(float(st.get("ms") or 2000))
                elif a == "click":
                    _find(page, st, to, vars, secrets).click(timeout=to * 1000)
                elif a == "fill":
                    _find(page, st, to, vars, secrets).fill(render(st.get("value", ""), vars, secrets), timeout=to * 1000)
                elif a == "type":                      # key by key, for fields that ignore fill
                    loc = _find(page, st, to, vars, secrets)
                    loc.fill("", timeout=to * 1000)
                    loc.press_sequentially(render(st.get("value", ""), vars, secrets), delay=40)
                elif a == "click_xy":
                    page.mouse.click(float(st["x"]), float(st["y"]))
                elif a == "click_item":                  # the list row whose name is this order's {symbol}
                    _click_item(page, render(st["selector"], vars, secrets), render(st["value"], vars, secrets), to)
                elif a == "pick_symbol":                 # symbol lookup: the result that is this symbol's contract
                    vars[st.get("var") or "site_name"] = out["vars"][st.get("var") or "site_name"] = \
                        _pick_symbol(page, render(st["selector"], vars, secrets), vars.get("yonexis_symbol") or "", to)
                elif a == "hover":
                    _find(page, st, to, vars, secrets).hover(timeout=to * 1000)
                elif a == "press":
                    page.keyboard.press(render(st["key"], vars, secrets))
                elif a == "select":
                    loc, v = _find(page, st, to, vars, secrets), render(st.get("value", ""), vars, secrets)
                    try:
                        loc.select_option(v, timeout=to * 1000)
                    except Exception:
                        loc.select_option(label=v, timeout=to * 1000)
                elif a == "scroll":
                    page.mouse.wheel(0, float(st.get("dy") or 400))
                elif a == "wait":
                    page.wait_for_timeout(float(st.get("ms") or 1000))
                elif a == "wait_value":                  # e.g. the order price: stop before Place Order if it is empty / 0
                    loc, end = _find(page, st, to, vars, secrets), time.time() + to
                    while True:
                        v = loc.evaluate("e => ('value' in e && e.tagName !== 'BUTTON') ? e.value : e.innerText")
                        if number(v) not in (None, 0.0) or (str(v).strip() and number(v) is None):
                            vars[st.get("var") or "value"] = out["vars"][st.get("var") or "value"] = str(v).strip()
                            break
                        if time.time() > end:
                            raise TimeoutError(f"{st['selector']} has no value ({str(v).strip() or 'empty'}) - is the site's price feed live?")
                        page.wait_for_timeout(200)
                elif a == "wait_for":
                    _find(page, st, to, vars, secrets)
                elif a == "expect_text":
                    page.get_by_text(render(st["text"], vars, secrets)).first.wait_for(state="visible", timeout=to * 1000)
                elif a == "extract":
                    txt = _find(page, st, to, vars, secrets).inner_text(timeout=to * 1000).strip()
                    vars[st["var"]] = out["vars"][st["var"]] = number(txt) if st["var"] in ("price", "fill_price") else txt
                elif a == "extract_list":                # every element like the picked one (search results ...)
                    sel = render(st["selector"], vars, secrets)
                    _find(page, st, to, vars, secrets)
                    page.wait_for_timeout(float(st.get("ms") or 500))     # let the list finish filling
                    items = [n for _, _, n in _rows(page, sel) if n]
                    vars[st["var"]] = out["vars"][st["var"]] = items[:500]
                if a in ("click", "click_item", "pick_symbol", "click_xy", "press", "select", "goto", "reload"):
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=3000)
                    except Exception:
                        pass
                if st.get("final"):
                    out["final_ran"] = True
            except Exception as e:
                msg = str(e).split("\n")[0][:300]
                rec.update(ok=False, error=msg)
                if not st.get("optional"):
                    out["steps"].append({**rec, "ms": round((time.time() - s0) * 1000)})
                    out.update(ok=False, error=f"step {i + 1} ({a}): {msg}")
                    break
            rec["ms"] = round((time.time() - s0) * 1000)
            out["steps"].append(rec)
    except Exception as e:                             # browser could not start
        out.update(ok=False, error=str(e)[:300])
    finally:
        b.busy = None
        b.error = None if out["ok"] else out.get("error")
    out["ms"] = round((time.time() - t0) * 1000)
    return out
