"""One headless Chromium (Playwright) for OpenAIAP, owned by a single worker thread.

Playwright's sync API may only be used from the thread that started it, so every browser action is a job on that
thread's queue. The profile (cookies, local storage) lives in <data>/openaiap/profile: a web login made once in the
tab is still there after a restart. Between jobs the worker refreshes a JPEG of the screen while someone is watching.

Recording: the dashboard sends clicks (x, y on the screenshot), typed text and keys. Each one is performed in the
browser and, while a recording is on, appended as a step with a selector for the element (stable attributes first,
then visible text, then a CSS path) plus alternatives the player tries when the first one is not found.
"""
from __future__ import annotations

import itertools
import json
import logging
import queue
import re
import threading
import time
import zlib
from concurrent.futures import Future

from yonexis.config import settings

log = logging.getLogger("yonexis.openaiap")
W, H = 1280, 800
CDP_PORT = 9333                 # each website's Chromium is also reachable on 9333, 9334 ... (inside the container): worker tabs attach
_PORTS = itertools.count(CDP_PORT)

# The name shown by a list row / result: its text pieces joined with spaces (inline tags often run together, e.g.
# <span>NIFTY 25 NOV FUT</span><small>NSE</small>), trailing exchange / price / change pieces dropped.
CORE_JS = r"""
(el) => {
  const segs = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n && segs.length < 12; n = w.nextNode()) {
    const p = n.parentElement;
    if (p && (p.closest('script,style') || getComputedStyle(p).display === 'none')) continue;
    const t = n.textContent.replace(/\s+/g, ' ').trim();
    if (t) segs.push(t);
  }
  const noise = /^(NSE|BSE|NFO|MCX|BFO|CDS|EQ|INDEX|IDX|[-+]?[\d,]*\.?\d+\s*%?|\(?[-+]?[\d,.]+%?\)?(\s*\(?[-+]?[\d,.]+%?\)?)?)$/i;
  while (segs.length > 1 && noise.test(segs[segs.length - 1])) segs.pop();
  return segs.join(' ');
}
"""

# Selectors for the element at a point (or the focused element): stable attributes, then text, then a CSS path.
_DESCRIBE_JS = r"""
(arg) => {
  let el = arg.focused ? document.activeElement : document.elementFromPoint(arg.x, arg.y);
  for (let i = 0; el && el.shadowRoot && i < 10; i++) {          // web components: look inside open shadow roots
    const d = arg.focused ? el.shadowRoot.activeElement : el.shadowRoot.elementFromPoint(arg.x, arg.y);
    if (!d || d === el) break;
    el = d;
  }
  if (!el || el === document.body || el === document.documentElement) return null;
  if (el.tagName === 'IFRAME') return {iframe: true};
  if (el.tagName === 'CANVAS' || el.closest('canvas')) return {canvas: true, tag: 'canvas', selectors: ['canvas'], text: ''};
  const pick = el.closest('button,a,input,select,textarea,label,td,th,li,tr,[role=gridcell],[role=cell],[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=row],[role=checkbox],[onclick],[contenteditable=true]') || el;
  const tag = pick.tagName.toLowerCase();
  const root = pick.getRootNode();
  const q = v => v.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const count = s => { try { return root.querySelectorAll(s).length; } catch (e) { return 0; } };
  const uniq = s => count(s) === 1;
  const out = [];
  for (const a of ['data-testid', 'data-test', 'data-qa', 'data-cy', 'id', 'name', 'aria-label', 'placeholder', 'title']) {
    const v = pick.getAttribute(a);
    if (!v || v.length > 80) continue;
    if (a === 'id' && (/\d{4,}/.test(v) || /^[0-9]/.test(v) || /^(:r|ember|ext-gen|react-)/.test(v))) continue;   // generated ids
    const s = a === 'id' ? '#' + CSS.escape(v) : `${tag}[${a}="${q(v)}"]`;
    if (uniq(s)) out.push(s);
  }
  const first = ((pick.innerText || '').split('\n').map(x => x.trim()).find(Boolean) || '');
  const text = ((pick.innerText || '').trim().replace(/\s+/g, ' '));
  if (text && text.length <= 40 && !['input', 'textarea', 'select'].includes(tag)) out.push(`${tag}:has-text("${q(text)}")`, `text="${q(text)}"`);
  const path = [];
  for (let n = pick; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
    if (n.id && !/\d{4,}/.test(n.id) && uniq('#' + CSS.escape(n.id))) { path.unshift('#' + CSS.escape(n.id)); break; }
    const sib = [...(n.parentElement ? n.parentElement.children : [])].filter(c => c.tagName === n.tagName);
    path.unshift(n.tagName.toLowerCase() + (sib.length > 1 ? `:nth-of-type(${sib.indexOf(n) + 1})` : ''));
  }
  out.push(path.join(' > '));
  // "read all like this": tag + stable classes of the element and its parents, without positions, matching 2-500
  const sig = n => n.tagName.toLowerCase() + [...n.classList].filter(c => c.length < 30 && !/\d{3,}|^(css|sc|jsx|_)[-_]?[a-z0-9]{4,}$|active|selected|hover|focus|^ng-|^is-|valid|pristine|dirty|touched|error|disabled/i.test(c)).slice(0, 3).map(c => '.' + CSS.escape(c)).join('');
  let list = null, chain = [];
  for (let n = pick, i = 0; n && n.nodeType === 1 && n !== document.body && i < 5; n = n.parentElement, i++) {
    chain.unshift(sig(n));
    const c = chain.join(' > '), k = count(c);
    if (k >= 2 && k <= 500 && [...root.querySelectorAll(c)].includes(pick)) { list = c; break; }
    if (k <= 1 && i === 0) continue;
  }
  // a cell of a table row: which column, the names in the row, and "the same column of every row" for read all
  let row = null;
  const cell = el.closest('td,th,[role=gridcell],[role=cell]'), tr = el.closest('tr,[role=row]');
  if (cell && tr && tr.contains(cell)) {
    const cells = [...tr.children], idx = cells.indexOf(cell) + 1;
    if (idx > 0) {
      const names = cells.map(c => (c.innerText || '').split('\n').map(x => x.trim()).find(Boolean) || '').slice(0, 15);
      const rs = sig(tr) + ' > ' + cell.tagName.toLowerCase() + `:nth-child(${idx})`, k = count(rs);
      if (k >= 2 && k <= 3000) list = rs;
      row = {tag: tr.tagName.toLowerCase() === 'tr' ? 'tr' : '[role=row]', idx, names};
    }
  }
  const editable = ['input', 'textarea'].includes(tag) || pick.isContentEditable;
  return {selectors: [...new Set(out)], tag, type: (pick.getAttribute('type') || '').toLowerCase(), text: text.slice(0, 60),
          first: first.slice(0, 80), list, row, core: (__CORE__)(pick).slice(0, 80),
          editable, options: tag === 'select' ? [...pick.options].map(o => ({value: o.value, label: o.label})) : null};
}
"""


_DESCRIBE_JS = _DESCRIBE_JS.replace("__CORE__", CORE_JS.strip())


def norm(t: str) -> str:
    return "".join(str(t or "").split()).upper()


# worker tabs: put the main tab's session storage back before the site's own scripts run (only keys not set yet,
# so a newer login of this tab is never overwritten)
_RESTORE_JS = """(() => { const s = %s;
  try { if (location.origin === s.origin) for (const k in s.items) if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, s.items[k]); }
  catch (e) {} })();"""


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/#?]+)", url or "")
    return m.group(1).lower() if m else ""


def _login_page(url: str) -> bool:
    return bool(re.search(r"sign-?in|log-?in|auth", (url or "").lower()))


class PlaywrightMissing(RuntimeError):
    pass


class Browser:
    """main=None: owns Chromium (the tab shown in the dashboard, the recorder). main=<Browser>: a worker - its own
    thread and tab in the same Chromium (same cookies), its session storage copied from the main tab, so all
    tabs share one login and orders for different symbols run at the same time."""

    def __init__(self, name: str = "main", main: "Browser | None" = None, site: str = "default"):
        self.name, self.main = name, main
        self.site = main.site if main is not None else site
        self.port = main.port if main is not None else next(_PORTS)
        self._prev: dict = {}                   # pop-up page id -> the page it was opened from
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pw = self._ctx = None
        self.page = None
        self.jpeg: bytes | None = None
        self.jpeg_at = 0.0
        self.watch_at = 0.0                     # last time the dashboard asked for the screen
        self.busy: str | None = None            # what the worker is doing (a flow name while one runs)
        self.recording: dict | None = None      # {"flow": name, "steps": [...]}
        self.samples: dict[str, str] = {}       # sample text typed "as" a variable -> variable (NIFTY -> query)
        self.error: str | None = None
        self.staged: dict | None = None         # an order prepared up to its final step (executor.stage)
        self.prepared_for: str | None = None    # the symbol the prepare flow last put on this tab
        self.last_url = self.last_title = ""    # the tab's address / title, refreshed by its own thread
        self._title_at = 0.0

    # ------------------------------------------------------------- worker
    def submit(self, fn, *args) -> Future:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name=f"openaiap-{self.name}", daemon=True)
            self._thread.start()
        fut: Future = Future()
        self._q.put((fn, args, fut))
        return fut

    def do(self, fn, *args, timeout: float = 60):
        return self.submit(fn, *args).result(timeout)

    def _loop(self) -> None:
        while True:
            try:
                fn, args, fut = self._q.get(timeout=0.5)
            except queue.Empty:
                if self.page is not None and time.time() - self.watch_at < 5:
                    self._snap()
                self._remember(title=time.time() - self._title_at > 2)
                continue
            if fn is None:
                break
            try:
                fut.set_result(fn(*args))
            except Exception as e:              # the caller gets the error; the worker keeps running
                fut.set_exception(e)
            self._snap()
            self._remember(title=True)

    def _remember(self, title: bool = False) -> None:
        """Worker thread: keep the tab's address / title where other threads can read them (Playwright objects may
        only be used on this thread). Kept fresh between jobs too: a site changes its address after a login click."""
        p = self.page
        try:
            if p is None or p.is_closed():
                self.last_url, self.last_title = "", ""
                return
            self.last_url = p.url or ""
            if title:
                self.last_title, self._title_at = p.title(), time.time()
        except Exception:
            pass

    # ------------------------------------------------------------- browser (worker thread only)
    def _page(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise PlaywrightMissing("Playwright is not installed in this image - rebuild it (docker compose up -d --build)") from e
        if self.main is not None:
            return self._worker_page(sync_playwright)
        if self._ctx is None:
            prof = settings.DATA_DIR / "openaiap" / ("profile" if self.site == "default" else f"profile-{self.site}")
            prof.mkdir(parents=True, exist_ok=True)
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(prof), headless=True, viewport={"width": W, "height": H}, locale="en-IN", timezone_id="Asia/Kolkata",
                args=["--no-sandbox", "--disable-dev-shm-usage", f"--remote-debugging-port={self.port}",
                      "--remote-debugging-address=127.0.0.1"])
            self._ctx.on("page", self._new_page)            # brokers open order windows as pop-ups: follow them
            pages = [p for p in self._ctx.pages if not p.is_closed()]
            self.page = pages[0] if pages else self._ctx.new_page()
        else:
            self.page = self._ctx.new_page()
        return self.page

    def _worker_page(self, sync_playwright):
        """A tab of the main Chromium with the main tab's login (cookies are shared; session storage is copied)."""
        snap = self.main.do(self.main._session_snapshot)
        if self._pw is None or self._ctx is None or not self._ctx.browser.is_connected():
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}").contexts[0]
            self._ctx.on("page", self._new_page)
        pg = self._ctx.new_page()
        pg.set_viewport_size({"width": W, "height": H})
        pg.add_init_script(_RESTORE_JS % json.dumps(snap))
        self.page = pg
        if snap.get("url", "").startswith("http"):
            pg.goto(snap["url"], wait_until="domcontentloaded", timeout=30000)
            self._settle(pg)
        return pg

    def _session_snapshot(self) -> dict:
        """Main thread: where the main tab is and its session storage (the login of sites that keep it there)."""
        p = self._page()
        try:
            return {"url": p.url, **p.evaluate("({origin: location.origin, items: Object.fromEntries(Object.entries(sessionStorage))})")}
        except Exception:
            return {"url": p.url, "origin": "", "items": {}}

    def resync(self) -> None:
        """Worker thread: open this tab again from the main tab's page and login (the tab may be blank, logged out
        or on a login page; the main tab may have logged in again since)."""
        if self.main is None:
            return
        old, self.page = self.page, None
        try:
            if old is not None and not old.is_closed():
                old.close()
        except Exception:
            pass
        self.staged = self.prepared_for = None
        self._page()                                      # new tab: the main tab's current session storage + page

    def like_main(self) -> tuple[bool, str]:
        """Does this worker show the same website as the main tab (not blank, not a login page while the main tab is
        past it)? Worker thread."""
        if self.main is None:
            return True, ""
        m = self.main.last_url or ""
        try:
            u = self._page().url or ""
        except Exception:
            return False, "tab not open"
        if not u.startswith("http"):
            return False, "blank tab"
        if _origin(u) != _origin(m):
            return False, f"on {_origin(u)}, main tab on {_origin(m) or 'nothing'}"
        if _login_page(u) and not _login_page(m):
            return False, "on the login page"
        return True, ""

    def logged_in(self, wait_s: float = 6) -> tuple[bool, str]:
        """Any thread: is the main tab past the site's login page? Asks the tab itself, waiting a few seconds while a
        login is still finishing (the address changes only after the site has answered)."""
        end = time.time() + wait_s
        while True:
            try:
                u = self.do(lambda: (self._page().url or ""), timeout=15)
            except Exception as e:
                return False, f"browser not answering: {e}"
            self.last_url = u
            if u.startswith("http") and not _login_page(u):
                return True, u
            if time.time() > end:
                return False, u or "blank tab"
            time.sleep(0.5)

    def ensure_like_main(self) -> None:
        """Worker thread: re-open the tab from the main tab when it does not show the main tab's website."""
        ok, _ = self.like_main()
        if not ok:
            self.resync()

    def _new_page(self, p) -> None:
        """Follow a pop-up only when this tab opened it (other tabs of the same Chromium are other workers)."""
        try:
            opener = p.opener()
        except Exception:
            opener = None
        if opener is None or opener is not self.page:
            return
        self._prev[id(p)] = self.page
        self.page = p

        def closed(_):
            prev = self._prev.pop(id(p), None)
            if self.page is p:
                self.page = prev if prev is not None and not prev.is_closed() else None
        p.on("close", closed)

    def _snap(self) -> None:
        try:
            if self.page is not None and not self.page.is_closed():
                self.jpeg = self.page.screenshot(type="jpeg", quality=60, timeout=5000)
                self.jpeg_at = time.time()
        except Exception:
            pass

    def _close(self) -> None:
        if self.main is not None:                       # a worker: close only its tab (the context is shared)
            try:
                self.page and not self.page.is_closed() and self.page.close()
            except Exception:
                pass
            closers = [self._pw.stop] if self._pw else []
        else:
            closers = [self._ctx.close] if self._ctx else []
            closers += [self._pw.stop] if self._pw else []
        for c in closers:
            try:
                c()
            except Exception:
                pass
        self._ctx = self._pw = self.page = None
        self.jpeg = None

    def _settle(self, p) -> None:
        try:
            p.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        p.wait_for_timeout(300)

    # ------------------------------------------------------------- recording helpers (worker thread)
    def _templated(self, d: dict) -> dict | None:
        """A clicked list row (search result) whose name is the sample of {symbol} / {query} is recorded as click_item:
        at replay the row whose name is THIS order's symbol is clicked, not the one clicked while recording."""
        names = {norm(d.get("core")), norm(d.get("first")), norm(d.get("text"))} - {""}
        var = next((v for smp, v in self.samples.items() if norm(smp) in names), None)
        if not var and d.get("row"):                  # a cell (Bid / Ask ...) in the row of the sample symbol
            rn = {norm(x) for x in d["row"]["names"]} - {""}
            var = next((v for smp, v in self.samples.items() if norm(smp) in rn), None)
            if var:
                return {"action": "click", "selector": self._row_tpl(d, var)}
        if not var:
            return None
        if d.get("list"):
            return {"action": "click_item", "selector": d["list"], "value": f"{{{var}}}"}
        return {"action": "click", "selector": f"text=/^\\s*{{{var}|re}}\\s*$/i", "alt": [f"text={{{var}}}"]}

    @staticmethod
    def _row_tpl(d: dict, var: str = "symbol") -> str | None:
        r = d.get("row")
        return f'{r["tag"]}:has(:text-is("{{{var}}}")) > :nth-child({r["idx"]})' if r else None

    def _record(self, step: dict) -> dict:
        if self.recording is not None:
            self.recording["steps"].append(step)
        return step

    def _describe(self, p, x: float | None = None, y: float | None = None, focused: bool = False) -> tuple[dict | None, object]:
        """What is at (x, y) / focused, descending into iframes. Returns (description, frame)."""
        frame = p.main_frame
        for _ in range(4):
            d = frame.evaluate(_DESCRIBE_JS, {"x": x or 0, "y": y or 0, "focused": focused})
            if not d or not d.get("iframe"):
                break
            h = frame.evaluate_handle("(a) => a.focused ? document.activeElement : document.elementFromPoint(a.x, a.y)",
                                      {"x": x or 0, "y": y or 0, "focused": focused}).as_element()
            box, inner = h.bounding_box(), h.content_frame()
            if inner is None or box is None:
                break
            frame = inner
            if not focused:
                x, y = x - box["x"], y - box["y"]
        if d:
            d["selectors"] = self._unique(frame, d["selectors"])
        return d, frame

    @staticmethod
    def _unique(frame, sels: list[str]) -> list[str]:
        """Selectors that match exactly one element first (text= ones can only be checked by Playwright)."""
        good, rest = [], []
        for s in sels:
            try:
                (good if frame.locator(s).count() == 1 else rest).append(s)
            except Exception:
                pass
        return good + rest

    # ------------------------------------------------------------- public actions (any thread)
    def start(self, url: str = "") -> dict:
        def job():
            p = self._page()
            if url:
                p.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._settle(p)
            return self.info()
        return self.do(job)

    def stop(self) -> None:
        self.do(self._close)

    def info(self) -> dict:
        """Any thread: what the tab shows (from the values its own thread keeps up to date)."""
        p = self.page
        return {"open": p is not None, "url": self.last_url, "title": self.last_title, "busy": self.busy, "error": self.error,
                "recording": {"flow": self.recording["flow"], "steps": len(self.recording["steps"])} if self.recording else None,
                "size": [W, H]}

    def screen(self) -> bytes | None:
        self.watch_at = time.time()
        return self.jpeg

    def act(self, a: dict) -> dict:
        """One action from the dashboard: performed in the browser and recorded (when recording)."""
        kind = a.get("type")
        if kind == "add":                                 # a step picked with "pick element" while recording
            from yonexis.openaiap import config
            return {"step": self._record(config.clean_step(a.get("step") or {}))}

        def job():
            p = self._page()
            if kind == "goto":
                url = str(a["url"])
                url = url if "://" in url else "https://" + url
                p.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._settle(p)
                return {"step": self._record({"action": "goto", "url": url})}
            if kind == "click":
                x, y = float(a["x"]), float(a["y"])
                d, frame = self._describe(p, x, y)
                if d and d.get("tag") == "select":     # native drop-downs do not draw in a headless screenshot
                    return {"select": d}
                p.mouse.click(x, y)
                self._settle(p)
                if a.get("exact") or (d and d.get("canvas")):   # chart / canvas terminals: the spot itself
                    return {"step": self._record({"action": "click_xy", "x": round(x), "y": round(y),
                                                  "note": "canvas" if d and d.get("canvas") else "exact spot"})}
                if not d:
                    return {"step": None, "note": "nothing clickable there"}
                step = self._templated(d) or {"action": "click", "selector": d["selectors"][0], "alt": d["selectors"][1:4],
                                              **({"list": d["list"]} if d.get("list") else {}),
                                              **({"row_tpl": self._row_tpl(d)} if d.get("row") else {})}
                return {"step": self._record({**step, "note": d.get("core") or d.get("text") or d.get("tag")}), "element": d}
            if kind == "hover":
                x, y = float(a["x"]), float(a["y"])
                d, _ = self._describe(p, x, y)
                p.mouse.move(x, y)
                p.wait_for_timeout(400)
                if not d or d.get("canvas"):
                    return {"step": None, "note": "nothing to hover there"}
                return {"step": self._record({"action": "hover", "selector": d["selectors"][0], "alt": d["selectors"][1:4],
                                              "note": d.get("text") or d.get("tag")})}
            if kind == "select":
                d, frame = self._describe(p, float(a["x"]), float(a["y"]))
                frame.locator(d["selectors"][0]).first.select_option(str(a["value"]))
                self._settle(p)
                return {"step": self._record({"action": "select", "selector": d["selectors"][0], "alt": d["selectors"][1:4],
                                              "value": a.get("as") or str(a["value"])})}
            if kind == "fill":
                d, frame = self._describe(p, focused=True)
                if not d or not d.get("editable"):
                    raise ValueError("click into a text field in the browser first")
                text = str(a.get("text") or "")
                try:
                    frame.locator(d["selectors"][0]).first.fill(text, timeout=5000)
                except Exception:                         # selector not usable: replace the focused field's text
                    p.keyboard.press("ControlOrMeta+A")
                    p.keyboard.type(text) if text else p.keyboard.press("Backspace")
                value = a.get("as") or text                   # "{qty}" / "{secret:password}" / the literal text
                if a.get("as") and text and not str(a["as"]).startswith("{secret:"):
                    self.samples[text] = str(a["as"]).strip("{}")
                if d.get("type") == "password" and not a.get("as"):
                    from yonexis.openaiap import config
                    config.save_secrets({"password": text}, site=self.site)
                    value = "{secret:password}"
                step = {"action": "fill", "selector": d["selectors"][0], "alt": d["selectors"][1:4],
                        "value": value, "note": d.get("type") or d.get("tag")}
                rec = (self.recording or {}).get("steps") or []
                if a.get("update") and rec and rec[-1]["action"] == "fill" and rec[-1]["selector"] == step["selector"]:
                    rec[-1] = step                        # live typing: one fill step per field, kept up to date
                    return {"step": step}
                return {"step": self._record(step)}
            if kind == "press":
                p.keyboard.press(str(a["key"]))
                self._settle(p)
                return {"step": self._record({"action": "press", "key": str(a["key"])})}
            if kind == "scroll":
                dy = float(a.get("dy") or 400)
                p.mouse.wheel(0, dy)
                p.wait_for_timeout(250)
                return {"step": self._record({"action": "scroll", "dy": dy})}
            if kind == "reload":
                p.reload(wait_until="domcontentloaded", timeout=30000)
                self._settle(p)
                return {"step": self._record({"action": "reload"}) if self.recording is not None else None}
            if kind == "back":
                p.go_back(wait_until="domcontentloaded", timeout=15000)
                return {"step": None}
            if kind == "inspect":                         # what is under the pointer (for wait_for / extract steps)
                d, _ = self._describe(p, float(a["x"]), float(a["y"]))
                return {"element": d}
            raise ValueError(f"unknown action {kind}")

        if self.busy:
            raise RuntimeError(f"the browser is running the {self.busy} flow - wait for it to finish")
        self.staged = self.prepared_for = None           # the dashboard changes the screen: nothing stays prepared
        return self.do(job)

    def record_start(self, flow: str, samples: dict | None = None) -> None:
        """samples: test values of the variables ({"symbol": "NIFTY 25 NOV FUT"}): clicking that text records {symbol}."""
        self.recording = {"flow": flow, "steps": []}
        self.samples = {str(v): k for k, v in (samples or {}).items() if v not in (None, "") and k in ("symbol", "query")}

    def record_stop(self) -> dict | None:
        r, self.recording = self.recording, None
        return r


class Pool:
    """Workers for the automation: each automation symbol always goes to the same worker (its orders stay in order),
    different symbols run in parallel. Worker 0 is the main tab; settings "workers" = how many tabs (1 = main only)."""

    def __init__(self, main: Browser):
        self.main = main
        self.site = main.site
        self.workers: dict[int, Browser] = {}

    def size(self) -> int:
        from yonexis.openaiap import config
        return max(1, min(20, int(config.get(site=self.site).get("workers") or 1)))

    def get(self, i: int) -> Browser:
        if i <= 0:
            return self.main
        if i not in self.workers:
            self.workers[i] = Browser(f"w{i}", self.main)
        return self.workers[i]

    def index(self, web_symbol: str) -> int:
        from yonexis.openaiap import config
        n = self.size()
        if n <= 1:
            return 0
        mp = config.mapping(site=self.site)
        order = sorted({mp[s]["web"] for s in config.worker_symbols(site=self.site) if s in mp})
        if web_symbol in order:
            return order.index(web_symbol) % n
        return zlib.crc32(str(web_symbol).encode()) % n

    def for_symbol(self, web_symbol: str) -> Browser:
        return self.get(self.index(web_symbol))

    def assignment(self) -> dict[int, list[str]]:
        """worker index -> the web symbols (of the automation symbols) it trades."""
        from yonexis.openaiap import config
        mp = config.mapping(site=self.site)
        out: dict[int, list[str]] = {}
        for s in config.worker_symbols(site=self.site):
            if s in mp:
                out.setdefault(self.index(mp[s]["web"]), []).append(mp[s]["web"])
        return out

    def symbols_of(self, b: Browser) -> list[str]:
        i = 0 if b is self.main else next((k for k, w in self.workers.items() if w is b), -1)
        return self.assignment().get(i, [])

    def needed(self) -> list[Browser]:
        return [self.get(i) for i in sorted(self.assignment())]

    def all(self) -> list[Browser]:
        return [self.main] + [self.workers[i] for i in sorted(self.workers)]

    def info(self) -> list[dict]:
        out = []
        for b in self.all():
            p = b.page
            st = b.staged
            out.append({"name": b.name, "open": p is not None and not p.is_closed(), "busy": b.busy, "error": b.error,
                        "symbols": [], "staged": f"{st['name']} {st['symbol']} ({time.time() - st['at']:.0f}s ago)" if st else None})
        return out

    def prune(self) -> int:
        """Close the worker tabs no running automation needs any more (e.g. a stopped automation's symbols)."""
        need = set(self.assignment())
        gone = [i for i in list(self.workers) if i not in need or i >= self.size()]
        for i in gone:
            w = self.workers.pop(i)
            w.submit(w._close)
        return len(gone)

    def resync_all(self) -> int:
        """The main tab logged in (again): every worker tab takes that login now (in parallel, each on its thread)."""
        for w in self.workers.values():
            w.submit(w.resync)
        return len(self.workers)

    def stop_workers(self) -> None:
        for i, w in list(self.workers.items()):
            try:
                w.do(w._close, timeout=20)
            except Exception:
                pass
            self.workers.pop(i, None)


_SITES: dict[str, tuple[Browser, Pool]] = {}


def get(site: str = "default") -> tuple[Browser, Pool]:
    """The browser (main tab) and worker pool of one website - each website has its own Chromium and saved login."""
    site = site or "default"
    if site not in _SITES:
        b = Browser(site=site)
        _SITES[site] = (b, Pool(b))
    return _SITES[site]


def drop(site: str) -> None:
    """A website was removed: close its browser."""
    b, p = _SITES.pop(site, (None, None))
    if b is not None:
        try:
            p.stop_workers()
            b.stop()
        except Exception:
            pass


browser, pool = get("default")
