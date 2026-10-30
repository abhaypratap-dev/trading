// YONEXIS dashboard (vanilla JS, no build step)
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let TOKEN = localStorage.getItem("yx_token") || "";
if (!TOKEN) { TOKEN = prompt("Admin token (ADMIN_TOKEN from .env)") || ""; localStorage.setItem("yx_token", TOKEN); }
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

async function api(path, opts = {}) {
  const o = { ...opts, headers: { "X-Admin-Token": TOKEN, ...(opts.headers || {}) } };
  if (opts.json !== undefined) { o.body = JSON.stringify(opts.json); o.headers["Content-Type"] = "application/json"; o.method = o.method || "POST"; }
  const r = await fetch(path, o);
  if (r.status === 401) { localStorage.removeItem("yx_token"); alert("Wrong admin token"); location.reload(); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof body === "string" ? body : JSON.stringify(body.detail ?? body, null, 1));
  return body;
}
const MINUS = "−";
const fmt = (n, d = 2) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const inr = (n, d = 0) => n == null || isNaN(n) ? "—" : (n < 0 ? MINUS + "₹" : "₹") + fmt(Math.abs(n), d);
// signed money for P&L / MTM: always shows + or − (zero shows no sign)
const inrS = (n, d = 0) => n == null || isNaN(n) ? "—" : (n > 0 ? "+₹" : n < 0 ? MINUS + "₹" : "₹") + fmt(Math.abs(n), d);
const pctS = n => n == null || isNaN(n) ? "" : (n > 0 ? "+" : n < 0 ? MINUS : "") + fmt(Math.abs(n)) + "%";
const cls = n => n > 0 ? "green" : n < 0 ? "red" : "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const syms = id => $(id).value.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
const pj = (id) => { const v = $(id).value.trim(); return v ? JSON.parse(v) : {}; };

// ---------------------------------------------------------------- time: one IST formatter for every timestamp
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const IST_DTF = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
function istParts(d) {
  const p = {}; IST_DTF.formatToParts(d).forEach(x => p[x.type] = x.value);
  return { y: +p.year, m: +p.month, d: +p.day, hh: p.hour, mm: p.minute, ss: p.second, ymd: `${p.year}-${p.month}-${p.day}` };
}
const istToday = () => istParts(new Date()).ymd;
// fmtTs: "30 Oct 10:00", or "10:00" when the date is today (IST). Naive strings are market/IST time unless {utc:true}.
function fmtTs(ts, o = {}) {
  if (ts == null || ts === "") return "—";
  let p;
  if (typeof ts === "number") p = istParts(new Date(ts < 1e12 ? ts * 1000 : ts));
  else {
    const s = String(ts).trim();
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/);
    if (!m) return s;
    const hasTz = /(Z|[+-]\d{2}:?\d{2})$/.test(s) && m[4] != null;
    if ((o.utc || hasTz) && m[4] != null) p = istParts(new Date(hasTz ? s.replace(" ", "T") : s.replace(" ", "T").slice(0, 19) + "Z"));
    else p = { y: +m[1], m: +m[2], d: +m[3], hh: m[4], mm: m[5], ss: m[6] || "00", ymd: `${m[1]}-${m[2]}-${m[3]}` };
  }
  const t = p.hh != null ? `${p.hh}:${p.mm}${o.sec ? ":" + p.ss : ""}` : "";
  const today = istToday();
  if (t && p.ymd === today && !o.date) return t;
  const yr = p.y !== +today.slice(0, 4) ? " " + p.y : "";
  return `${p.d} ${MONTHS[p.m - 1]}${yr}${t ? " " + t : ""}`;
}
const tsDay = ts => String(ts || "").slice(0, 10);
function dayLabel(ymd) {
  if (!ymd) return "";
  if (ymd === istToday()) return "Today";
  const d = new Date(ymd + "T12:00:00Z");
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: d.getUTCFullYear() !== new Date().getFullYear() ? "numeric" : undefined, timeZone: "UTC" });
}

// ---------------------------------------------------------------- plain-language reasons
const RELAY_TIP = "Relay: price crossed the whole zone, so this trade was closed at the opposite zone edge and the opposite side was opened there.";
const REASONS = { target: "Target hit", target_gap: "Target (gap)", stop_loss: "Stop hit", sl: "Stop hit", sl_gap: "Stop hit", reversed: "Reversed at zone edge",
  square_off: "15:15 square-off", manual: "Closed manually", eod: "End of day", void: "Void", taken: "Traded (paper)", closed: "Closed", open: "Open" };
const PREFIXES = [["blocked", "Blocked"], ["not traded", "Not traded"], ["void", "Void"], ["skipped", "Skipped"], ["no trade", "No trade"], ["taken", "Traded"], ["waiting", "Waiting"]];
function reasonText(raw) {
  const s = String(raw ?? "").trim(); if (!s) return "";
  if (REASONS[s]) return REASONS[s];
  const w = s.match(/^would:\s*(.*)$/); if (w) return "Would have: " + reasonText(w[1]).toLowerCase();
  for (const [p, label] of PREFIXES) {
    if (s.toLowerCase().startsWith(p)) {
      const rest = s.slice(p.length).replace(/^\s*[:\-–]?\s*/, "").trim();
      return rest ? `${label} – ${rest.replaceAll("_", " ")}` : label;
    }
  }
  return s.replaceAll("_", " ");
}
function reasonTone(raw) {
  const s = String(raw ?? "").toLowerCase().replace(/^would:\s*/, "");
  if (s.startsWith("target")) return "green";
  if (s === "stop_loss" || s === "sl" || s === "sl_gap") return "red";
  if (s === "reversed") return "cyan";
  if (s.startsWith("blocked") || s.startsWith("skipped")) return "amber";
  if (s.startsWith("taken") || s === "open") return "cyan";
  return "t3";
}
function reasonHTML(raw, tone = true) {
  if (raw == null || raw === "") return "";
  const tip = String(raw) + (String(raw).replace(/^would:\s*/, "") === "reversed" ? " — " + RELAY_TIP : "");
  return `<span class="why ${tone ? reasonTone(raw) : ""}" title="${esc(tip)}">${esc(reasonText(raw))}</span>`;
}

// ---------------------------------------------------------------- tabs + navigation
const TAB_ORDER = ["live", "wallet", "backtest", "models", "builder", "import", "feedback", "data", "risk"];
const MORE_TABS = ["models", "builder", "import", "feedback", "data"];
let curTab = "live";
function showTab(name) {
  if (!TAB_ORDER.includes(name)) return;
  const changed = name !== curTab; curTab = name;
  $$("#tabs [data-tab]").forEach(x => { const on = x.dataset.tab === name; x.classList.toggle("on", on); on ? x.setAttribute("aria-current", "page") : x.removeAttribute("aria-current"); });
  $$(".tab").forEach(t => t.classList.toggle("on", t.id === "tab-" + name));
  $$("#bnav [data-go]").forEach(b => b.classList.toggle("on", b.dataset.go === name));
  $("#bnav-more").classList.toggle("on", MORE_TABS.includes(name));
  $$("#more-sheet [data-go]").forEach(b => b.classList.toggle("on", b.dataset.go === name));
  closeOverlays();
  const loaders = { wallet: () => { loadWallet(); typeof loadScope === "function" && loadScope(); }, models: loadModelsTab, data: () => { loadInventory(); loadUpstox(); }, feedback: loadFeedback, risk: loadRisk, backtest: () => { loadStrategySelects(); loadLab(); }, import: loadStrategySelects };
  loaders[name]?.();
  if (changed) window.scrollTo(0, 0);
  setTimeout(markScrolls, 50);
}
$$("#tabs [data-tab]").forEach(b => b.onclick = () => showTab(b.dataset.tab));
document.addEventListener("click", e => {
  const g = e.target.closest("[data-go]"); if (!g) return;
  e.preventDefault(); showTab(g.dataset.go);
});
$("#bnav-more").onclick = () => { const s = $("#more-sheet"); s.hidden = !s.hidden; $("#bnav-more").setAttribute("aria-expanded", String(!s.hidden)); if (!s.hidden) s.querySelector("[data-go]")?.focus(); };
$("#more-sheet").onclick = e => { if (e.target.id === "more-sheet" || e.target.closest("[data-close]")) closeOverlays(); };

function closeOverlays() {
  $("#more-sheet").hidden = true; $("#bnav-more").setAttribute("aria-expanded", "false");
  $("#kbd-help").hidden = true;
  if (typeof closeTradeChart === "function") closeTradeChart();
  if (typeof indPop === "function") indPop(false);
  if (!$("#ev-drawer").hidden) { $("#ev-drawer").hidden = true; $("#ev-bell").setAttribute("aria-expanded", "false"); }
}
$("#kbd-open").onclick = () => { $("#kbd-help").hidden = false; $("#kbd-help .x").focus(); };
$("#kbd-help").onclick = e => { if (e.target.id === "kbd-help" || e.target.closest("[data-close]")) closeOverlays(); };

// ---------------------------------------------------------------- header status chips
window._health = null;
function renderHeader() {
  const h = window._health, f = window._feed, w = window._wallet;
  const lock = $("#hc-lock");
  const locked = !h || h.real_orders === "LOCKED";
  lock.classList.toggle("unlocked", !locked);
  lock.querySelector("b").textContent = locked ? "PAPER" : "REAL ORDERS ON";
  lock.querySelector(".lock-x").textContent = locked ? " · real orders LOCKED" : "";
  if (w) {
    $("#hc-eq").textContent = inr(w.equity);
    const day = (w.today_realized || 0) + (w.open_mtm || 0);
    const p = $("#hc-pnl"); p.textContent = inrS(day); p.className = cls(day);
  }
  if (h) {
    const fdot = !f ? (h.connector === "simulated" ? "a" : "") : f.status === "connected" ? "g" : f.status === "waiting_for_login" ? "r" : "a";
    $("#hc-feed-dot").className = "dot " + fdot;
    $("#hc-feed-t").textContent = `${h.connector}${f ? " · " + f.status.replaceAll("_", " ") : ""}`;
    $("#hc-feed").title = `Feed: ${f ? f.status + (f.error ? ": " + f.error : "") : h.connector} — open Data`;
    $("#hc-ai-dot").className = "dot " + (h.llm.ok ? "g" : "r");
    $("#hc-ai-t").textContent = `AI ${h.llm.ok ? "ready" : "offline"}`;
    $("#hc-ai").title = `AI model ${h.llm.model || ""} — open System`;
  }
}
async function health() {
  try { window._health = await api("/api/health"); renderHeader(); if (curTab === "risk") renderSysStatus(); }
  catch (e) { $("#hc-feed-dot").className = "dot r"; $("#hc-feed-t").textContent = "API offline"; }
}
function tickClock() { const p = istParts(new Date()); $("#hc-clock").textContent = `${p.hh}:${p.mm}:${p.ss} IST`; }
tickClock(); setInterval(tickClock, 1000);
async function pollWallet() {
  if (curTab === "wallet") return;   // the wallet tab refreshes itself (and the header) every 5 s
  try { window._wallet = await api("/api/wallet"); renderHeader(); renderLivePositions(); } catch (e) {}
}

// ---------------------------------------------------------------- toasts + today's events
const EVENTS = []; let evUnread = 0;
function pushEvent(html, kind) {
  EVENTS.unshift({ t: new Date(), html, kind }); if (EVENTS.length > 300) EVENTS.pop();
  if ($("#ev-drawer").hidden) evUnread++; renderEvents();
}
function renderEvents() {
  const b = $("#ev-count"); b.hidden = !evUnread; b.textContent = evUnread > 99 ? "99+" : evUnread;
  $("#ev-bell").setAttribute("aria-label", `Today's events${evUnread ? ", " + evUnread + " unread" : ""}`);
  if ($("#ev-drawer").hidden) return;
  $("#ev-list").innerHTML = EVENTS.map(e => { const p = istParts(e.t); return `<li class="${esc(e.kind)}"><span class="t">${p.hh}:${p.mm}:${p.ss}</span><span class="b">${e.html}</span></li>`; }).join("")
    || `<li class="empty">Nothing yet. Fills, exits, saved settings and job results land here as they happen, so a missed notification is never lost.</li>`;
}
$("#ev-bell").onclick = () => {
  const d = $("#ev-drawer"); const open = d.hidden;
  closeOverlays();
  if (open) { d.hidden = false; evUnread = 0; renderEvents(); $("#ev-bell").setAttribute("aria-expanded", "true"); d.querySelector(".x").focus(); }
};
$("#ev-drawer").onclick = e => { if (e.target.closest("[data-close]")) { closeOverlays(); $("#ev-bell").focus(); } };
$("#ev-clear").onclick = () => { EVENTS.length = 0; renderEvents(); };
function toast(html, kind = "", ms = 4500) {
  if (matchMedia("(max-width: 599px)").matches) ms = Math.min(ms, 3000);
  pushEvent(html, kind);
  const box = $("#toasts");
  const t = document.createElement("div"); t.className = "toast " + kind;
  t.innerHTML = `<div class="toast-b">${html}</div><button type="button" class="x" aria-label="Dismiss">×</button>`;
  box.prepend(t);
  const max = matchMedia("(max-width: 599px)").matches ? 1 : 3;     // phone: one compact toast, rest go to the bell
  while (box.children.length > max) box.lastElementChild.remove();
  let left = ms, start = 0, timer = null;
  const run = () => { start = Date.now(); clearTimeout(timer); timer = setTimeout(() => t.remove(), left); };
  const pause = () => { clearTimeout(timer); left = Math.max(1200, left - (Date.now() - start)); };
  t.addEventListener("mouseenter", pause); t.addEventListener("mouseleave", run);
  t.addEventListener("focusin", pause); t.addEventListener("focusout", run);
  t.querySelector(".x").onclick = () => t.remove();
  run();
}
window.addEventListener("error", e => { try { fetch("/api/client-log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: String(e.message), where: `${e.filename}:${e.lineno}` }) }); } catch (_) {} });
window.addEventListener("unhandledrejection", e => { try { fetch("/api/client-log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: String(e.reason && e.reason.message || e.reason), where: "promise" }) }); } catch (_) {} });
// Coming back from the Upstox login page restores a frozen copy of this page; reload it so the live socket reconnects.
window.addEventListener("pageshow", e => { if (e.persisted) location.reload(); });

// ---------------------------------------------------------------- horizontal-scroll hint for tables
function markScroll(el) {
  const can = el.scrollWidth > el.clientWidth + 2;
  el.classList.toggle("can-x", can);
  el.classList.toggle("at-end", can && el.scrollLeft + el.clientWidth >= el.scrollWidth - 2);
}
function markScrolls() { $$(".tab.on .tscroll").forEach(markScroll); }
document.addEventListener("scroll", e => { if (e.target.classList && e.target.classList.contains("tscroll")) markScroll(e.target); }, true);
window.addEventListener("resize", () => { clearTimeout(window._msT); window._msT = setTimeout(markScrolls, 150); });
setInterval(markScrolls, 2500);

// ---------------------------------------------------------------- live
let selected = null, chart, series, zoneLines = [], statusRows = {};
const C = { surface: "#0d1424", text: "#8d9bb8", grid: "#131d33", border: "#1e2a44", buy: "#22c55e", sell: "#ef4444", info: "#38bdf8" };
function initChart() {
  chart = LightweightCharts.createChart($("#chart"), {
    layout: { background: { color: C.surface }, textColor: C.text },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: C.border }, rightPriceScale: { borderColor: C.border },
    autoSize: true,
  });
  series = chart.addCandlestickSeries({ upColor: C.buy, downColor: C.sell, borderVisible: false, wickUpColor: C.buy, wickDownColor: C.sell });
  // OHLC legend: follows the mouse; a click pins that candle (click again or Esc to unpin); otherwise the last candle
  const lg = document.createElement("div"); lg.id = "ohlc"; lg.className = "ohlc"; $("#chart").style.position = "relative"; $("#chart").appendChild(lg);
  chart.subscribeCrosshairMove(p => { if (!ohlcPin) showOhlc(p && p.time ? p.seriesData.get(series) : null); });
  chart.subscribeClick(p => {
    const d = p && p.time ? p.seriesData.get(series) : null;
    ohlcPin = d && !(ohlcPin && ohlcPin.time === d.time) ? d : null;
    showOhlc(ohlcPin || d);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && ohlcPin) { ohlcPin = null; showOhlc(null); } });
}
let ohlcPin = null, ohlcLast = null, volByTime = new Map();
function showOhlc(d) {
  const el = $("#ohlc"); if (!el) return;
  d = d && d.open != null ? d : ohlcLast;
  if (!d) { el.innerHTML = ""; return; }
  const t = new Date(d.time * 1000), hh = String(t.getUTCHours()).padStart(2, "0"), mm = String(t.getUTCMinutes()).padStart(2, "0");
  const day = t.toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "UTC" });
  const ch = d.close - d.open, pct = d.open ? ch / d.open * 100 : 0, c = ch >= 0 ? "green" : "red", v = volByTime.get(d.time);
  el.innerHTML = `<span class="muted">${day} ${hh}:${mm}${ohlcPin ? " · pinned" : ""}</span>
    <span>O <b>${fmt(d.open)}</b></span><span>H <b class="green">${fmt(d.high)}</b></span><span>L <b class="red">${fmt(d.low)}</b></span>
    <span>C <b class="${c}">${fmt(d.close)}</b></span><span class="${c}">${ch >= 0 ? "+" : "−"}${fmt(Math.abs(ch))} (${ch >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)</span>
    <span class="muted">range ${fmt(d.high - d.low)}</span>${v != null ? `<span class="muted">vol ${Number(v).toLocaleString("en-IN")}</span>` : ""}`;
}
const toUnix = ts => Math.floor(new Date(ts.replace(" ", "T") + "Z").getTime() / 1000);

let histMode = null;   // null = live, else {start, end, interval}
let curBars = [];      // candle times on the main chart (unix s), shared with the oscillator pane
let SI = null;         // /api/strategy-indicators (indicators per strategy, descriptions, families)
let NEWS_SUM = {};     // /api/news/summary by symbol
const TC_SRC = {};     // trade rows per table, for the trade chart modal
let IND = indLoad();   // chart indicator choice (persisted)
function paint(rows, zone, sigs) {
  const data = rows.map(r => ({ time: toUnix(r.ts), open: r.open, high: r.high, low: r.low, close: r.close }));
  series.setData(data); curBars = data.map(d => d.time);
  volByTime = new Map(rows.map(r => [toUnix(r.ts), r.volume]));
  ohlcLast = data.at(-1) || null; ohlcPin = null; showOhlc(null);
  zoneLines.forEach(l => series.removePriceLine(l)); zoneLines = [];
  if (zone && zone.zone_high) {
    zoneLines.push(series.createPriceLine({ price: zone.zone_high, color: C.sell, lineStyle: 2, lineWidth: 1, title: "zone high" }));
    zoneLines.push(series.createPriceLine({ price: zone.zone_low, color: C.sell, lineStyle: 2, lineWidth: 1, title: "zone low" }));
  }
  const bars = rows.map(r => toUnix(r.ts));
  const snap = t => { let lo = 0, hi = bars.length - 1, best = bars[0]; while (lo <= hi) { const m = (lo + hi) >> 1; if (bars[m] <= t) { best = bars[m]; lo = m + 1; } else hi = m - 1; } return best; };
  series.setMarkers((sigs || []).filter(() => bars.length).map(s => ({ time: snap(toUnix(String(s.ts).replace("T", " ").slice(0, 19))), position: s.side === "BUY" ? "belowBar" : "aboveBar",
    color: s.side === "BUY" ? C.buy : C.sell, shape: s.side === "BUY" ? "arrowUp" : "arrowDown", text: s.side })).sort((a, b) => a.time - b.time));
  chart.timeScale().fitContent();
}
async function drawChart(sym) {
  if (!sym) return;
  if (histMode) return drawHistory(sym);
  const rows = await api(`/api/candles?symbol=${sym}&live_view=true`);
  const day = (rows.at(-1)?.ts || "").slice(0, 10);
  const pool = [...(window._signals || []), ...(window._todaySig || [])];
  const seen = new Set();
  paint(rows, statusRows[sym], pool.filter(s => s.symbol === sym && s.ts.slice(0, 10) === day && !seen.has(s.id) && seen.add(s.id)));
  $("#ch-src").textContent = day ? `live · session ${fmtTs(day, { date: true })}` : "";
  drawIndicators(sym, null);
}
async function drawHistory(sym) {
  const { start, end, interval } = histMode;
  $("#ch-src").textContent = "loading...";
  try {
    const r = await api(`/api/chart?symbol=${sym}&start=${start}&end=${end}&interval=${interval}`);
    paint(r.candles, r.zone, r.signals);
    drawIndicators(sym, { start, end, interval });
    $("#ch-src").textContent = `${sym} ${fmtTs(start, { date: true })}${end !== start ? " – " + fmtTs(end, { date: true }) : ""} · ${interval} · ${r.candles.length} candles · ${r.source}`;
  } catch (e) { series.setData([]); curBars = []; clearIndicators(); $("#ch-src").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
}
const isoDay = d => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
function prevWeekday(d) { const x = new Date(d); do { x.setDate(x.getDate() - 1); } while (x.getDay() === 0 || x.getDay() === 6); return x; }
$("#ch-end").value = isoDay(new Date()); $("#ch-start").value = isoDay(prevWeekday(new Date()));
$("#ch-go").onclick = () => { histMode = { start: $("#ch-start").value, end: $("#ch-end").value || $("#ch-start").value, interval: $("#ch-int").value }; $("#ch-live").classList.remove("on"); drawChart(selected); };
$("#ch-live").onclick = () => { histMode = null; $("#ch-live").classList.add("on"); drawChart(selected); };
$("#ch-live").classList.add("on");
$$(".chart-bar [data-days]").forEach(b => b.onclick = () => {
  const n = +b.dataset.days, today = new Date();
  if (n === 1) { const d = isoDay(prevWeekday(today)); $("#ch-start").value = d; $("#ch-end").value = d; }
  else { const s = new Date(today); s.setDate(s.getDate() - n); $("#ch-start").value = isoDay(s); $("#ch-end").value = isoDay(today); if ($("#ch-int").value === "1m") $("#ch-int").value = n > 5 ? "15m" : "5m"; }
  syncTf();
  $("#ch-go").click();
});
// timeframe chips mirror the #ch-int select (same values; applies to history ranges)
function syncTf() { const v = $("#ch-int").value; $$("#tf-chips button").forEach(b => { const on = b.dataset.v === v; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); }); }
$("#tf-chips").innerHTML = [...$("#ch-int").options].map(o => `<button type="button" data-v="${esc(o.value)}" title="${esc(o.value)} candles for history ranges (Show / Prev day / 5D / 1M)">${esc(o.value)}</button>`).join("");
$$("#tf-chips button").forEach(b => b.onclick = () => { $("#ch-int").value = b.dataset.v; $("#ch-int").onchange(); });
$("#ch-int").onchange = () => {
  syncTf();
  if (histMode) { histMode.interval = $("#ch-int").value; drawChart(selected); }
  else $("#ch-src").textContent = `${$("#ch-int").value} applies to history ranges — pick Show, Prev day, 5D or 1M`;
};
syncTf();
$("#ch-range-t").onclick = () => { const o = $("#chart-bar").classList.toggle("range-open"); $("#ch-range-t").setAttribute("aria-expanded", String(o)); };

let lastPx = {};
function lastCallFor(sym) { return (window._todaySig || []).find(s => s.symbol === sym); }
function renderWatch(rows, simDay) {
  statusRows = rows;
  const q = ($("#w-search").value || "").trim().toUpperCase(), sort = $("#w-sort").value;
  const seg = $("#w-seg") ? $("#w-seg").value : "";
  let list = Object.entries(rows).filter(([s]) => (!q || s.includes(q)) && (!seg || (seg === "fut") === s.endsWith("-FUT")));
  if (sort === "chg") list.sort((a, b) => (b[1].change_pct ?? -1e9) - (a[1].change_pct ?? -1e9));
  else if (sort === "name") list.sort((a, b) => a[0].localeCompare(b[0]));
  else if (sort === "sig") list.sort((a, b) => (b[1].signal ? 1 : 0) - (a[1].signal ? 1 : 0));
  const tb = $("#watch tbody"); tb.innerHTML = "";
  list.forEach(([sym, r]) => {
    const stc = r.status === "ABOVE" ? "green" : r.status === "BELOW" ? "red" : "amber";
    const arrow = r.status === "ABOVE" ? "▲ " : r.status === "BELOW" ? "▼ " : "";
    const lc = lastCallFor(sym);
    const why = lc && /^(blocked|not traded|void)/i.test(String(lc.status)) ? ` <span class="info-i ${/^blocked/i.test(lc.status) ? "" : "mut"}" title="Last call today (${esc(fmtTs(lc.ts))} ${esc(lc.side)}): ${esc(reasonText(lc.status))}" aria-label="${esc(reasonText(lc.status))}">ⓘ</span>` : "";
    const sig = r.not_in_test ? `<span class="t3 xs" title="not selected in Paper Wallet > Today's live test: watched, no calls">off</span>`
      : r.signal ? `<span class="pill ${r.signal.toLowerCase()}">${r.signal}</span>${why}` : `<span class="t3">–</span>${why}`;
    const tr = document.createElement("tr");
    tr.dataset.sym = sym;
    if (sym === selected) tr.className = "sel";
    const prev = lastPx[sym]; lastPx[sym] = r.price;
    const flash = prev != null && r.price !== prev ? (r.price > prev ? "flash-up" : "flash-dn") : "";
    const ct = r.contract;
    const fut = sym.endsWith("-FUT") ? ` <span class="pill review xs" title="${esc(ct ? ct.trading_symbol + " · lot " + ct.lot_size + " · expiry " + ct.expiry : "near-month future")}">FUT${ct ? " " + ct.lot_size : ""}</span>` : "";
    tr.innerHTML = `<td><b class="${sym === selected ? "amber" : ""}">${sym.replace("-FUT", "")}</b>${fut}${newsDot(sym)}</td><td class="r ${flash}">${fmt(r.price)}</td>
      <td class="r ${cls(r.change_pct)}">${r.change_pct == null ? "" : pctS(r.change_pct)}</td>
      <td class="${stc} xs">${arrow}${(r.status || "").replace("WAITING", "waiting")}</td><td>${sig}</td>
      <td><button type="button" class="rm" title="Remove ${esc(sym)} from watchlist" aria-label="Remove ${esc(sym)}">×</button></td>`;
    tr.querySelector(".rm").onclick = ev => { ev.stopPropagation(); removeSymbol(sym); };
    tr.onclick = () => selectSymbol(sym);
    tb.appendChild(tr);
  });
  if (!list.length) tb.innerHTML = `<tr><td colspan="6" class="empty">${Object.keys(rows).length ? "No symbol matches this filter." : "Watchlist is empty — add a stock above (e.g. RELIANCE)."}</td></tr>`;
  if (!selected && Object.keys(rows).length) { selected = Object.keys(rows)[0]; drawChart(selected); }
  renderCards(simDay);
}
function selectSymbol(sym) { const ch = sym !== selected; selected = sym; renderWatch(statusRows, window._simDay); renderCards(window._simDay); drawChart(sym); if (ch) dockSymbolChanged(); }
$("#w-search").oninput = () => renderWatch(statusRows, window._simDay);

// ---- add / remove watchlist symbols
let suggTimer = null, suggIdx = -1;
async function loadWatchNote() {
  try {
    const w = await api("/api/watchlist");
    window._watch = w.symbols;
    $("#wl-note").innerHTML = `${w.symbols.length}/${w.max} symbols · calls come from: ${w.strategies_making_calls.length ? w.strategies_making_calls.map(x => esc(x.replaceAll("_", " "))).join(", ") : '<span class="amber">no strategy in paper status</span>'}`;
    renderTodayStrip();
  } catch (e) {}
}
async function addSymbol(sym) {
  $("#wl-sugg").hidden = true; $("#wl-add").value = "";
  toast(`Adding <b>${esc(sym)}</b>… loading its history`, "", 3000);
  try {
    await api("/api/watchlist", { json: { symbol: sym } });
    toast(`<b>${esc(sym)}</b> added — it appears in the list within a few seconds`, "buy");
    selected = sym; loadWatchNote();
  } catch (e) { toast(esc(e.message), "err"); }
}
async function removeSymbol(sym) {
  if (!confirm(`Remove ${sym} from the watchlist? No new calls will be made on it.`)) return;
  try { await api(`/api/watchlist/${encodeURIComponent(sym)}`, { method: "DELETE" }); delete statusRows[sym]; if (selected === sym) selected = null; renderWatch(statusRows, window._simDay); loadWatchNote(); }
  catch (e) { toast(esc(e.message), "err"); }
}
$("#wl-add").oninput = () => {
  clearTimeout(suggTimer);
  const q = $("#wl-add").value.trim();
  if (q.length < 2) { $("#wl-sugg").hidden = true; return; }
  suggTimer = setTimeout(async () => {
    try {
      const rows = await api(`/api/upstox/search?q=${encodeURIComponent(q)}`);
      const have = new Set(window._watch || Object.keys(statusRows));
      suggIdx = -1;
      $("#wl-sugg").innerHTML = rows.length ? rows.map(r => `<div data-s="${esc(r.symbol)}"><b>${esc(r.symbol)}</b><small>${esc(r.name || "")}</small>${have.has(r.symbol) ? '<small class="green">added</small>' : ""}</div>`).join("")
        : `<div class="t3">No NSE equity matches “${esc(q)}”. Press Enter to try “${esc(q.toUpperCase())}” anyway.</div>`;
      $("#wl-sugg").hidden = false;
      $$("#wl-sugg [data-s]").forEach(d => d.onmousedown = () => addSymbol(d.dataset.s));
    } catch (e) { $("#wl-sugg").innerHTML = `<div class="red">${esc(e.message)}</div>`; $("#wl-sugg").hidden = false; }
  }, 200);
};
$("#wl-add").onkeydown = e => {
  const items = $$("#wl-sugg [data-s]");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault(); suggIdx = Math.max(0, Math.min(items.length - 1, suggIdx + (e.key === "ArrowDown" ? 1 : -1)));
    items.forEach((d, i) => d.classList.toggle("hi", i === suggIdx));
  } else if (e.key === "Enter") { const pick = items[suggIdx] || items[0]; addSymbol(pick ? pick.dataset.s : $("#wl-add").value.trim().toUpperCase()); }
  else if (e.key === "Escape") $("#wl-sugg").hidden = true;
};
$("#wl-add").onblur = () => setTimeout(() => $("#wl-sugg").hidden = true, 150);
$("#w-sort").onchange = () => renderWatch(statusRows, window._simDay);

function renderCards(simDay) {
  const r = statusRows[selected]; if (!r) return;
  $("#c-sym").textContent = selected;
  const fm = r.forming ? ` · forming ${fmtTs(r.forming.ts)}` : "";
  $("#c-day").textContent = simDay ? `simulated session ${fmtTs(simDay, { date: true })}` : `${fmtTs(r.tick_ts || r.ts)}${fm}`;
  $("#c-zone").textContent = r.zone_size ? `${fmt(r.zone_size)} (${fmt(r.zone_pct)}%)${r.zone_provisional ? " · forming" : ""}` : "waiting for first red candle";
  $("#c-price").textContent = fmt(r.price);
  $("#c-chg").innerHTML = r.change_pct == null ? "" : `<span class="${cls(r.change_pct)}">${pctS(r.change_pct)}</span>`;
  const sc = r.status === "ABOVE" ? "green" : r.status === "BELOW" ? "red" : "amber";
  $("#c-status").innerHTML = `<span class="${sc}">${r.status === "ABOVE" || r.status === "BELOW" ? r.status + " ZONE" : (r.status || "—")}</span>`;
  const lc = lastCallFor(selected);
  $("#c-signal").innerHTML = r.signal ? `<span class="${r.signal === "BUY" ? "green" : "red"}">${r.signal}</span>${lc ? ` <span class="xs">${reasonHTML(lc.status)}</span>` : ""}` : "—";
}

// ---- bottom dock (positions / calls / order flow / news)
const DOCKS = ["pos", "calls", "flow", "news"];
let dockTab = DOCKS.includes(store.get("yx_dock")) ? store.get("yx_dock") : "pos";
function setDock(t) {
  dockTab = t; store.set("yx_dock", t);
  $$(".dock-tabs [data-dock]").forEach(b => { const on = b.dataset.dock === t; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; });
  DOCKS.forEach(d => $("#dock-" + d).hidden = t !== d);
  if (t !== "pos") $("#dock-note").textContent = ""; else renderLivePositions();
  if (t === "flow") loadOrderflow();
  if (t === "news") loadNews();
  setTimeout(markScrolls, 30);
}
$$(".dock-tabs [data-dock]").forEach(b => b.onclick = () => setDock(b.dataset.dock));
$(".dock-tabs").onkeydown = e => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { const i = DOCKS.indexOf(dockTab); setDock(DOCKS[(i + (e.key === "ArrowRight" ? 1 : DOCKS.length - 1)) % DOCKS.length]); $(`.dock-tabs [data-dock="${dockTab}"]`).focus(); } };

async function closePosition(id, sym) {
  if (!confirm(`Close the paper position ${sym ? "in " + sym + " " : ""}at the last price now?`)) return;
  try { await api(`/api/wallet/close/${id}`, { method: "POST" }); toast(`Closed paper position <b>${esc(sym || "#" + id)}</b>`, ""); loadWallet(); pollWallet(); }
  catch (e) { alert(e.message); }
}
function posCards(rows, src) {
  return rows.map((t, i) => `<div class="pcard"><div class="pcard-h"><b>${esc(t.symbol)}</b>${src ? tcBtn(src + ":" + i, "") : ""}<span class="pill ${t.side.toLowerCase()}">${t.side}</span><span class="t3 xs">${t.qty} · ${esc(fmtTs(t.entry_ts))}</span>
      <span class="pcard-m ${cls(t.mtm)}">${inrS(t.mtm)}</span></div>
    <div class="pcard-g"><div><span>Entry</span><b>${fmt(t.entry_price)}</b></div><div><span>LTP</span><b>${fmt(t.ltp)}</b></div><div><span>SL</span><b>${fmt(t.stop_loss)}</b></div><div><span>Target</span><b>${fmt(t.target)}</b></div></div>
    <button type="button" class="sm" data-close="${t.id}" data-sym="${esc(t.symbol)}">Close now</button></div>`).join("");
}
function wireClose(root) { $$("[data-close]", root).forEach(b => b.onclick = () => closePosition(b.dataset.close, b.dataset.sym)); }
function renderLivePositions() {
  const w = window._wallet; if (!w) return;
  const rows = w.open_positions || [];
  $("#dock-npos").textContent = rows.length;
  const mtm = rows.reduce((a, t) => a + (t.mtm || 0), 0);
  $("#dock-note").innerHTML = dockTab === "pos" && rows.length ? `open MTM <b class="${cls(mtm)}">${inrS(mtm)}</b>` : "";
  $("#lv-pos tbody").innerHTML = rows.map(t => `<tr><td><b>${esc(t.symbol)}</b></td><td><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td><td class="r">${t.qty}</td>
    <td class="r">${fmt(t.entry_price)}</td><td class="r">${fmt(t.ltp)}</td><td class="r">${fmt(t.stop_loss)}</td><td class="r">${fmt(t.target)}</td>
    <td class="r ${cls(t.mtm)}"><b>${inrS(t.mtm)}</b></td><td class="t3">${esc(fmtTs(t.entry_ts))}</td><td><button type="button" class="sm" data-close="${t.id}" data-sym="${esc(t.symbol)}">Close</button></td></tr>`).join("")
    || `<tr><td colspan="10" class="empty">No open paper positions. When a strategy in paper status makes a call that passes the risk checks, it is filled here at the live price.</td></tr>`;
  $("#lv-pos-cards").innerHTML = posCards(rows) || `<p class="t3 xs">No open paper positions. Calls that pass the risk checks are filled here at the live price.</p>`;
  wireClose($("#dock-pos"));
}

// ---- today so far (computed from today's calls)
let cfPred = null;   // extra filter set from the today strip: {label, fn}
async function loadTodaySignals() {
  try { const t = istToday(); window._todaySig = await api(`/api/signals?since=${t}&until=${t}&limit=2000`); renderTodayStrip(); }
  catch (e) {}
}
function bucketNT(st) { const r = String(st).replace(/^not traded:\s*/i, ""); return /min old/.test(r) ? "too old" : r; }
function renderTodayStrip() {
  const sig = window._todaySig || [];
  const st = s => String(s.status || "");
  const traded = sig.filter(s => /^(taken|closed)/.test(st(s)));
  const wins = traded.filter(s => s.outcome_pnl > 0).length, losses = traded.filter(s => s.outcome_pnl != null && s.outcome_pnl <= 0).length;
  const group = (pref, key) => { const g = {}; sig.filter(s => st(s).toLowerCase().startsWith(pref)).forEach(s => { const k = key(st(s)); g[k] = (g[k] || 0) + 1; }); return g; };
  const blocked = group("blocked", x => x.replace(/^blocked:\s*/i, ""));
  const nt = group("not traded", bucketNT);
  const voids = sig.filter(s => st(s).startsWith("void")).length;
  const rows = Object.values(statusRows || {});
  const off = rows.filter(r => r.not_in_test).length;
  const nWatch = (window._watch || Object.keys(statusRows || {})).length;
  const btn = (key, label, n, tone, title) => `<button type="button" class="tcount ${tone}" data-k="${esc(key)}" title="${esc(title || "Show these calls")}">${esc(label)} <b>${n}</b></button>`;
  let h = `<span class="ts-l">Today so far</span>`;
  h += btn("all", "Calls", sig.length, "mute", "Show all of today's calls");
  h += btn("traded", "Traded", traded.length, "buy", `${wins} profit · ${losses} loss · ${traded.length - wins - losses} still open`);
  Object.entries(blocked).sort((a, b) => b[1] - a[1]).forEach(([k, n]) => h += btn("b:" + k, "Blocked – " + k, n, "warn"));
  Object.entries(nt).sort((a, b) => b[1] - a[1]).forEach(([k, n]) => h += btn("n:" + k, "Not traded – " + k, n, "mute"));
  if (voids) h += btn("void", "Void", voids, "mute");
  h += `<span class="tcount static" title="Symbols on the watchlist">Watchlist <b>${nWatch}</b></span>`;
  if (off) h += `<span class="tcount static" title="Watched but not selected in Paper Wallet > Today's live test: no calls are made on them">Off (not in test) <b>${off}</b></span>`;
  if (!sig.length) h += `<span class="t3 xs">No calls yet today — they appear when a strategy in paper status triggers during market hours (09:15–15:30 IST).</span>`;
  $("#today-strip").innerHTML = h;
  $$("#today-strip [data-k]").forEach(b => b.onclick = () => {
    const k = b.dataset.k;
    cfPred = k === "all" ? null
      : k === "traded" ? { label: "Traded", fn: s => /^(taken|closed)/.test(String(s.status)) }
      : k === "void" ? { label: "Void", fn: s => String(s.status).startsWith("void") }
      : k.startsWith("b:") ? { label: "Blocked – " + k.slice(2), fn: s => String(s.status).replace(/^blocked:\s*/i, "") === k.slice(2) && /^blocked/i.test(s.status) }
      : { label: "Not traded – " + k.slice(2), fn: s => /^not traded/i.test(s.status) && bucketNT(s.status) === k.slice(2) };
    $("#cf-range").value = "today"; $("#cf-from").hidden = $("#cf-to").hidden = true; $("#cf-status").value = ""; $("#cf-out").value = "";
    setDock("calls"); loadCalls();
  });
}

let callsFilter = "all";
$$("#calls-filter button").forEach(b => b.onclick = () => { callsFilter = b.dataset.f; $$("#calls-filter button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", String(x === b)); }); loadCalls(); });
function callRange() {
  const r = $("#cf-range").value, d = new Date(), day = x => isoDay(x);
  if (r === "today") return [day(d), day(d)];
  if (r === "yday") { const y = prevWeekday(d); return [day(y), day(y)]; }
  if (r === "custom") return [$("#cf-from").value || null, $("#cf-to").value || null];
  if (r === "all") return [null, null];
  const s0 = new Date(d); s0.setDate(s0.getDate() - (+r)); return [day(s0), day(d)];
}
["cf-range", "cf-from", "cf-to", "cf-side", "cf-status", "cf-out", "cf-strat"].forEach(id => $("#" + id).onchange = () => {
  const c = $("#cf-range").value === "custom"; $("#cf-from").hidden = $("#cf-to").hidden = !c; loadCalls();
});
let cfT = null; $("#cf-sym").oninput = () => { clearTimeout(cfT); cfT = setTimeout(loadCalls, 250); };
$("#cf-clear").onclick = () => { cfPred = null; ["cf-sym"].forEach(i => $("#" + i).value = ""); ["cf-side", "cf-status", "cf-out", "cf-strat"].forEach(i => $("#" + i).value = ""); $("#cf-range").value = "today"; $("#cf-from").hidden = $("#cf-to").hidden = true; loadCalls(); };
$("#cf-toggle").onclick = () => { const o = $("#cf").classList.toggle("open"); $("#cf-toggle").setAttribute("aria-expanded", String(o)); };
async function loadCalls() {
  const [from, to] = callRange();
  const q = new URLSearchParams({ limit: 2000 });
  if (from) q.set("since", from); if (to) q.set("until", to);
  if ($("#cf-side").value) q.set("side", $("#cf-side").value);
  if ($("#cf-strat").value) q.set("strategy", $("#cf-strat").value);
  const rows = await api(`/api/signals?${q}`);
  if (!from && !to) window._signals = rows; else if (from === isoDay(new Date())) window._signals = rows;
  const strats = [...new Set(rows.map(r => r.strategy))]; const sel = $("#cf-strat"), cur = sel.value;
  if (strats.some(x => ![...sel.options].some(o => o.value === x))) sel.innerHTML = `<option value="">All strategies</option>` + strats.map(x => `<option>${esc(x)}</option>`).join(""), sel.value = cur;
  const sym = $("#cf-sym").value.trim().toUpperCase(), st = $("#cf-status").value, out = $("#cf-out").value;
  const shown = rows.filter(s => (callsFilter === "all" || (callsFilter === "review" ? !s.feedback : !!s.feedback))
    && (!sym || s.symbol.includes(sym)) && (!st || String(s.status).startsWith(st))
    && (!out || (out === "open" ? s.outcome_pnl == null : out === "win" ? s.outcome_pnl > 0 : s.outcome_pnl != null && s.outcome_pnl <= 0))
    && (!cfPred || cfPred.fn(s)));
  const rc = $("#cf-reason");
  rc.hidden = !cfPred;
  if (cfPred) { rc.innerHTML = `${esc(cfPred.label)} <button type="button" aria-label="Remove filter ${esc(cfPred.label)}">×</button>`; rc.querySelector("button").onclick = () => { cfPred = null; loadCalls(); }; }
  $("#cf-count").textContent = `${shown.length} of ${rows.length} calls`;
  $("#dock-ncalls").textContent = shown.length;
  const tb = $("#calls tbody"); tb.innerHTML = "";
  if (!shown.length) tb.innerHTML = `<tr><td colspan="10" class="empty">${rows.length ? "No calls match these filters. <button type=\"button\" class=\"sm\" id=\"cf-clear2\">Clear filters</button>" : "No calls in this period. Calls appear when a strategy in paper status triggers during market hours (09:15–15:30 IST); set one to paper on the Strategies tab."}</td></tr>`;
  $("#cf-clear2") && ($("#cf-clear2").onclick = () => $("#cf-clear").click());
  shown.forEach(s => {
    const sc = s.score == null ? `<span class="t3 xs">no model</span>` : `<b class="${s.score >= .5 ? "green" : "red"}">${fmt(s.score * 100, 0)}%</b>`;
    const out = s.outcome_pnl == null ? `<span class="t3">open</span>` : `<span class="num ${cls(s.outcome_pnl)}">${inrS(s.outcome_pnl)}</span> ${reasonHTML(s.outcome, false)}`;
    const fb = s.feedback ? `<span class="t3 xs">${esc(s.feedback.action)}${s.feedback.remark ? ": " + esc(s.feedback.remark).slice(0, 40) : ""}</span>` :
      `<button type="button" class="sm ib ok" data-a="agree" title="Agree" aria-label="Agree">✓</button> <button type="button" class="sm ib no" data-a="reject" title="Reject" aria-label="Reject">✗</button> <button type="button" class="sm ib" data-a="modify" title="Modify SL / target" aria-label="Modify">✎</button> <button type="button" class="sm ib" data-a="skip" title="Skip">Skip</button>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="mono">${esc(fmtTs(s.ts))}</td><td><b>${esc(s.symbol)}</b></td><td><span class="pill ${s.side.toLowerCase()}">${s.side}</span></td>
      <td class="r">${fmt(s.price)}</td><td class="r">${fmt(s.stop_loss)}</td><td class="r">${fmt(s.target)}</td><td class="r">${sc}</td><td>${reasonHTML(s.status)}</td><td>${out}</td><td>${fb}</td>`;
    $$("button", tr).forEach(b => b.onclick = () => feedbackDialog(s, b.dataset.a));
    tb.appendChild(tr);
  });
}

function feedbackDialog(s, action) {
  const d = $("#dlg");
  $("#dlg-body").innerHTML = `<div class="panel-h"><h2>${esc(action.toUpperCase())} · ${s.side} ${esc(s.symbol)} @ ${fmt(s.price)}</h2></div>
    <p class="t3">${esc(s.reason)}</p>
    ${action === "modify" ? `<div class="form"><label>New SL <input id="fb-sl" type="number" step="0.05" value="${s.stop_loss}"></label><label>New target <input id="fb-tg" type="number" step="0.05" value="${s.target ?? ""}"></label></div>` : ""}
    <label class="t3">Remark (why?) — this becomes training data<textarea id="fb-remark" rows="3" placeholder="e.g. results tomorrow, resistance at 2700, volume fake"></textarea></label>`;
  d.showModal();
  d.onclose = async () => {
    if (d.returnValue !== "ok") return;
    const body = { action, remark: $("#fb-remark").value };
    if (action === "modify") { body.new_sl = parseFloat($("#fb-sl").value); body.new_target = parseFloat($("#fb-tg").value); }
    try { await api(`/api/signals/${s.id}/feedback`, { json: body }); loadCalls(); } catch (e) { alert(e.message); }
  };
}

function updateForming(r) {
  if (histMode || !r || !r.forming) return;
  const f = r.forming;
  try {
    const bar = { time: toUnix(String(f.ts)), open: f.open, high: f.high, low: Math.min(f.low, f.close), close: f.close };
    series.update(bar); if (f.volume != null) volByTime.set(bar.time, f.volume);
    if (!ohlcLast || bar.time >= ohlcLast.time) { ohlcLast = bar; if (!ohlcPin) showOhlc(null); }
  } catch (e) {}
}
let _hdrT = 0;
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live?token=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "status") {
      const prev = statusRows[selected] || {};
      window._simDay = m.sim_day;
      if (m.feed) { window._feed = m.feed; liveBanner(m.feed, m.rows); if (Date.now() - _hdrT > 2000) { _hdrT = Date.now(); renderHeader(); } }
      renderWatch(m.rows, m.sim_day);
      const r = m.rows[selected] || {};
      $("#clock").textContent = (r.tick_ts || r.ts || "").slice(11, 19);
      const newSession = (r.forming && !prev.forming) || String(r.last_closed || "").slice(0, 10) !== String(prev.last_closed || "").slice(0, 10);
      if (selected && !histMode && (r.last_closed !== prev.last_closed || newSession)) drawChart(selected).then(() => updateForming(r));
      else updateForming(r);
    } else if (m.type === "signal") {
      toast(`<b>${m.side} ${esc(m.symbol)}</b> @ ${fmt(m.price)}<div class="t3 xs">${esc(m.reason || "")}${m.order ? " · paper filled " + m.order.qty : ""}</div>`, m.side.toLowerCase());
      loadCalls(); loadTodaySignals(); if (curTab === "wallet") loadWallet(); else pollWallet();
    } else if (m.type === "paper_exit") {
      toast(`Paper exit <b>${esc(m.symbol)}</b> · ${reasonHTML(m.exit, false)}<div class="num ${cls(m.net_pnl)}">${inrS(m.net_pnl)}</div>`, m.net_pnl >= 0 ? "buy" : "sell");
      loadCalls(); loadTodaySignals(); if (curTab === "wallet") loadWallet(); else pollWallet();
    }
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}

// ---------------------------------------------------------------- keyboard
const typing = el => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    const open = !$("#kbd-help").hidden || !$("#ev-drawer").hidden || !$("#more-sheet").hidden || !$("#tc").hidden || !$("#ind-pop").hidden;
    if (open) { closeOverlays(); e.preventDefault(); return; }
    if (typing(e.target) && e.target.id === "w-search") e.target.blur();
    return;
  }
  if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-9]$/.test(e.code)) {
    e.preventDefault(); showTab(TAB_ORDER[+e.code.slice(5) - 1]); return;
  }
  if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey || $("#dlg").open) return;
  if (e.key === "?") { e.preventDefault(); $("#kbd-help").hidden ? ($("#kbd-help").hidden = false, $("#kbd-help .x").focus()) : closeOverlays(); return; }
  if (e.key === "/") { e.preventDefault(); showTab("live"); $("#w-search").focus(); $("#w-search").select(); return; }
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && curTab === "live") {
    const rows = $$("#watch tbody tr[data-sym]"); if (!rows.length) return;
    e.preventDefault();
    let i = rows.findIndex(r => r.dataset.sym === selected);
    i = i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)));
    selectSymbol(rows[i].dataset.sym);
    $(`#watch tbody tr[data-sym="${CSS.escape(rows[i].dataset.sym)}"]`)?.scrollIntoView({ block: "nearest" });
  }
});
// arrow keys inside the watchlist filter also move the selection
$("#w-search").addEventListener("keydown", e => {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const rows = $$("#watch tbody tr[data-sym]"); if (!rows.length) return;
  e.preventDefault();
  let i = rows.findIndex(r => r.dataset.sym === selected);
  i = i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)));
  selectSymbol(rows[i].dataset.sym);
});

// ---------------------------------------------------------------- backtest
async function loadStrategySelects() {
  const rows = await api("/api/strategies");
  const latest = {}; rows.forEach(r => latest[r.name] = r);
  const opts = Object.values(latest).map(r => `<option value="${esc(r.name)}">${esc(r.name)} (v${r.version}, ${esc(r.status)})</option>`).join("");
  ["#bt-strategy", "#par-strategy", "#tm-strategy"].forEach(id => { const v = $(id).value; $(id).innerHTML = opts; if (v) $(id).value = v; });
}

function metricsKV(m) {
  if (!m || !m.trades) return `<p class="t3">No trades.</p>`;
  const items = [["Trades", m.trades], ["Net P&L (after costs)", `<span class="${cls(m.net_pnl)}">${inrS(m.net_pnl)}</span>`],
    ["Costs paid", `${inr(m.total_costs)}`], ["Win rate", fmt(m.win_rate) + "%"], ["Avg win / loss", `${inrS(m.avg_win)} / ${inrS(m.avg_loss)}`],
    ["Profit factor", fmt(m.profit_factor, 2)], ["Sharpe", fmt(m.sharpe, 2)], ["Max drawdown", `${inr(m.max_drawdown)} (${fmt(m.max_drawdown_pct)}%)`],
    ["Trades / month", fmt(m.trades_per_month, 1)], ["Period", `${fmtTs(tsDay(m.start), { date: true })} → ${fmtTs(tsDay(m.end), { date: true })}`]];
  return `<div class="kv">${items.map(([k, v]) => `<div>${k}</div><div class="num">${v}</div>`).join("")}</div>`;
}

function equityChart(el, daily) {
  if (!daily || !daily.length) return;
  const c = LightweightCharts.createChart(el, { height: 220, autoSize: true, layout: { background: { color: C.surface }, textColor: C.text },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } } });
  c.addAreaSeries({ lineColor: C.info, topColor: "rgba(56,189,248,.25)", bottomColor: "rgba(56,189,248,0)" })
    .setData(daily.map(d => ({ time: d.date.slice(0, 10), value: d.equity })));
  c.timeScale().fitContent();
}

function evaluationHTML(ev) {
  const v = (ev.verdict || "").toLowerCase();
  const checks = Object.entries(ev.checks || {}).map(([k, ok]) => `<span class="${ok ? "ok" : "no"}">${ok ? "✓" : "✗"} ${k.replaceAll("_", " ")}</span>`).join("");
  const mc = ev.monte_carlo || {}, wf = ev.walk_forward || {}, st = ev.stress || {};
  const regimes = Object.entries(ev.regimes || {}).map(([r, x]) => `<tr><td>${esc(r)}</td><td class="r">${x.trades}</td><td class="r ${cls(x.net_pnl)}">${inrS(x.net_pnl)}</td><td class="r">${fmt(x.win_rate)}%</td></tr>`).join("");
  const stressRows = ["costs_x2", "slippage_x2", "slippage_x4"].map(k => st[k] ? `<tr><td>${k.replace("_", " ")}</td><td class="r ${cls(st[k].net_pnl)}">${inrS(st[k].net_pnl)}</td></tr>` : "").join("")
    + Object.entries(st.historical_windows || {}).filter(([k]) => k !== "note").map(([k, x]) => `<tr><td>${esc(k)}</td><td class="r ${cls(x.net_pnl)}">${inrS(x.net_pnl)}</td></tr>`).join("");
  return `<div class="panel"><div class="panel-h"><h2>Verdict <span class="pill ${v}">${esc(ev.verdict)}</span> · ${esc(ev.score)} checks passed</h2><span class="t3 xs">deflated Sharpe ${fmt(ev.deflated_sharpe_prob, 3)} · ${ev.n_trials} variants tried</span></div><div class="checks">${checks}</div></div>
  <div class="grid2">
    <div class="panel"><div class="panel-h"><h2>Development period</h2></div>${metricsKV(ev.development)}</div>
    <div class="panel"><div class="panel-h"><h2>Locked holdout</h2></div><p class="panel-sub">Never seen in tuning.</p>${metricsKV(ev.holdout)}</div>
    <div class="panel"><div class="panel-h"><h2>Monte Carlo</h2><span class="t3 xs">${mc.n_sims || 0} runs</span></div><div class="kv">
      <div>Net P&L 5th / 50th / 95th pct</div><div class="num">${inrS(mc.net_pnl_p5)} / ${inrS(mc.net_pnl_p50)} / ${inrS(mc.net_pnl_p95)}</div>
      <div>Probability of loss</div><div>${fmt(mc.prob_loss)}%</div><div>95% worst drawdown</div><div>${inr(mc.max_dd_p95_worst)}</div></div></div>
    <div class="panel"><div class="panel-h"><h2>Walk-forward (out of sample)</h2></div>${wf.oos_metrics ? metricsKV(wf.oos_metrics) + `<p class="t3">${wf.pct_profitable_folds}% of ${wf.folds.length} folds profitable</p>` : `<p class="t3">${esc(wf.note || "No parameter grid given.")}</p>`}</div>
    <div class="panel"><div class="panel-h"><h2>Stress tests (net P&L)</h2></div><div class="tscroll"><table class="tbl"><tbody>${stressRows}</tbody></table></div></div>
    <div class="panel"><div class="panel-h"><h2>By market regime</h2></div><div class="tscroll"><table class="tbl"><thead><tr><th>Regime</th><th class="r">Trades</th><th class="r">Net P&L</th><th class="r">Win %</th></tr></thead><tbody>${regimes}</tbody></table></div>
      <p class="t3">Parameter sensitivity: ${ev.sensitivity?.fragile ? '<b class="red">fragile</b>' : '<span class="green">stable</span>'} (worst ±10% move: ${inrS(ev.sensitivity?.worst_net_pnl)})</p></div>
  </div>
  ${ev.report ? `<div class="panel"><div class="panel-h"><h2>Report agent</h2></div><div class="prose">${esc(ev.report)}</div></div>` : ""}
  ${ev.critic ? `<div class="panel"><div class="panel-h"><h2>Critic agent (red team)</h2></div><div class="prose">${esc(ev.critic)}</div></div>` : ""}`;
}

async function pollJob(jid, onLog) {
  while (true) {
    const j = await api(`/api/jobs/${jid}`);
    onLog?.(j);
    if (j.status !== "running") return j;
    await new Promise(r => setTimeout(r, 1500));
  }
}

// quick / full mode
function btMode() { return ($("input[name=bt-mode]:checked") || {}).value || "quick"; }
function applyBtMode() {
  const full = btMode() === "full";
  $$("#tab-backtest .full-only").forEach(el => el.hidden = !full);
  $("#bt-quick").hidden = full; $("#bt-full").hidden = !full;
  $("#bt-mode-help").textContent = full
    ? "Full evaluation: development + locked hold-out, walk-forward, Monte Carlo, stress tests and agent reports. Takes minutes; the live log below shows progress."
    : "Quick: one pass over the period with the strategy's settings — results in seconds.";
  store.set("yx_btmode", full ? "full" : "quick");
}
$$("input[name=bt-mode]").forEach(r => r.onchange = applyBtMode);
if (store.get("yx_btmode") === "full") $("input[name=bt-mode][value=full]").checked = true;
applyBtMode();

function btBody() {
  return { strategy: $("#bt-strategy").value, symbols: syms("#bt-symbols"), interval: $("#bt-interval").value, start: $("#bt-start").value,
    end: $("#bt-end").value, source: $("#bt-source").value, params: pj("#bt-params"),
    param_space: $("#bt-space").value.trim() ? JSON.parse($("#bt-space").value) : null,
    baseline_net_pnl: $("#bt-baseline").value ? parseFloat($("#bt-baseline").value) : null };
}
$("#bt-quick").onclick = async () => {
  $("#bt-out").innerHTML = `<div class="panel t3"><span class="spin"></span> Running…</div>`;
  try {
    const r = await api("/api/backtests/quick", { json: btBody() });
    $("#bt-out").innerHTML = `<div class="grid2"><div class="panel"><div class="panel-h"><h2>Results</h2></div>${metricsKV(r.metrics)}</div><div class="panel"><div class="panel-h"><h2>Equity</h2></div><div id="eq"></div></div></div>
      <div class="panel"><div class="panel-h"><h2>Trades</h2><span class="t3 xs">first 200</span></div><div class="tscroll tall"><table class="tbl"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th class="r">Price</th><th>Exit</th><th class="r">Price</th><th>Why</th><th class="r">Net P&L</th></tr></thead><tbody>
      ${r.trades.slice(0, 200).map(t => `<tr><td>${esc(t.symbol)}</td><td><span class="pill ${String(t.side).toLowerCase()}">${t.side}</span></td><td class="mono">${esc(fmtTs(t.entry_ts))}</td><td class="r">${fmt(t.entry)}</td><td class="mono">${esc(String(t.exit_ts).slice(11, 16))}</td><td class="r">${fmt(t.exit)}</td><td>${reasonHTML(t.exit_reason)}</td><td class="r ${cls(t.net_pnl)}">${inrS(t.net_pnl)}</td></tr>`).join("")}</tbody></table></div></div>`;
    equityChart($("#eq"), r.daily);
  } catch (e) { $("#bt-out").innerHTML = `<pre class="log red">${esc(e.message)}</pre>`; }
};
function btLogShell() {
  $("#bt-out").innerHTML = `<div class="panel"><div class="panel-h"><h2>Full evaluation</h2><span class="t3 xs" id="bt-meta"></span></div>
    <pre class="log" id="bt-log" style="max-height:320px;overflow:auto">starting…</pre></div><div id="bt-res"></div>`;
}
async function followBt(jid) {
  btLogShell();
  const j = await pollJob(jid, j => jobLog("#bt-log", "#bt-meta", j));
  jobLog("#bt-log", "#bt-meta", j);
  if (j.status === "error") { $("#bt-res").innerHTML = `<pre class="log red">${esc(j.error)}</pre>`; return; }
  $("#bt-res").innerHTML = evaluationHTML(j.result);
  toast(`Full evaluation finished: <b>${esc(j.result.verdict || "")}</b>`, String(j.result.verdict).toLowerCase() === "pass" ? "buy" : "");
}
$("#bt-full").onclick = async () => {
  try { const { job_id } = await api("/api/backtests/full", { json: btBody() }); followBt(job_id); }
  catch (e) { $("#bt-out").innerHTML = `<pre class="log red">${esc(e.message)}</pre>`; }
};
api("/api/jobs/latest/full_backtest").then(j => { if (j.status === "running") { $("input[name=bt-mode][value=full]").checked = true; applyBtMode(); followBt(j.id); } }).catch(() => {});

// ---------------------------------------------------------------- AI builder
$("#ai-spec").onclick = async () => {
  $("#ai-spec-status").innerHTML = `<span class="spin"></span> Spec agent is reading your idea…`;
  try {
    const card = await api("/api/agents/spec", { json: { prompt: $("#ai-prompt").value } });
    $("#ai-card").value = JSON.stringify(card, null, 2);
    $("#ai-questions").innerHTML = (card.questions || []).map(q => `<div class="q">❓ ${esc(q)}</div>`).join("") +
      ((card.questions || []).length ? `<p class="t3">Answer these by editing the card below (or add the answer to your idea and create the card again).</p>` : "");
    $("#ai-card-panel").hidden = false; $("#ai-spec-status").textContent = "";
  } catch (e) { $("#ai-spec-status").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};
$("#ai-build").onclick = async () => {
  let spec; try { spec = JSON.parse($("#ai-card").value); } catch { return alert("Rule card is not valid JSON"); }
  $("#ai-progress").hidden = false; $("#ai-log").textContent = ""; $("#ai-out").innerHTML = "";
  try {
    const { job_id } = await api("/api/agents/build", { json: { spec, symbols: syms("#ai-symbols"), start: $("#ai-start").value, end: $("#ai-end").value,
      source: $("#ai-source").value, baseline_net_pnl: $("#ai-baseline").value ? parseFloat($("#ai-baseline").value) : null } });
    const j = await pollJob(job_id, j => { $("#ai-log").textContent = j.log.map(l => `[${l.t}s] ${l.stage} ${JSON.stringify(Object.fromEntries(Object.entries(l).filter(([k]) => !["t", "stage"].includes(k))))}`).join("\n"); });
    if (j.status === "error") throw new Error(j.error + "\n" + (j.trace || ""));
    const r = j.result;
    if (!r.ok) { $("#ai-out").innerHTML = `<div class="panel"><div class="panel-h"><h2 class="red">Stopped at: ${esc(r.stage)}</h2></div><pre class="log">${esc(JSON.stringify(r.problems || r.error, null, 2))}</pre><pre class="log">${esc(r.code)}</pre></div>`; return; }
    $("#ai-out").innerHTML = `<div class="panel"><div class="panel-h"><h2>Saved as ${esc(r.saved?.name)} v${r.saved?.version} (candidate)</h2><button type="button" class="sm" data-go="models">Open Strategies</button></div><p class="panel-sub">Approve it on the Strategies tab.</p></div>` +
      evaluationHTML({ ...r.evaluation, critic: r.critic, report: r.report }) +
      `<div class="panel"><div class="panel-h"><h2>Generated code</h2></div><pre class="log">${esc(r.code)}</pre></div>`;
    toast(`AI Builder saved <b>${esc(r.saved?.name)} v${r.saved?.version}</b>`, "buy");
  } catch (e) { $("#ai-out").innerHTML = `<pre class="log red">${esc(e.message)}</pre>`; }
};

// ---------------------------------------------------------------- import & parity
$("#imp-convert").onclick = async () => {
  $("#imp-status").innerHTML = `<span class="spin"></span> Converter agent working…`; $("#imp-out").innerHTML = "";
  try {
    const { job_id } = await api("/api/import/convert", { json: { source: $("#imp-src").value, language: $("#imp-lang").value, notes: $("#imp-notes").value, name: $("#imp-name").value || null } });
    const j = await pollJob(job_id, j => $("#imp-status").innerHTML = `<span class="spin"></span> stage: ${esc(j.stage)}`);
    if (j.status === "error") throw new Error(j.error);
    const r = j.result;
    $("#imp-status").textContent = r.saved ? `Saved as ${r.saved.name} v${r.saved.version}. Now run a parity check below.` : "Not saved: review found problems.";
    $("#imp-out").innerHTML = `<pre class="log">${esc(JSON.stringify({ approved: r.review.approved, issues: r.review.issues, static: r.review.static_check }, null, 2))}</pre><pre class="log">${esc(r.code)}</pre>`;
    loadStrategySelects();
  } catch (e) { $("#imp-status").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};
$("#par-run").onclick = async () => {
  const f = $("#par-file").files[0]; if (!f) return alert("Choose the admin's trades file");
  const fd = new FormData(); fd.append("file", f); fd.append("strategy", $("#par-strategy").value); fd.append("interval", $("#par-interval").value); fd.append("params", $("#par-params").value || "{}");
  $("#par-out").innerHTML = `<p class="t3"><span class="spin"></span> Comparing trade by trade…</p>`;
  try {
    const r = await api("/api/import/parity", { method: "POST", body: fd });
    $("#par-out").innerHTML = `<div class="panel-h"><h2>Verdict: <span class="pill ${r.verdict === "PARITY" ? "pass" : r.verdict === "CLOSE" ? "review" : "fail"}">${esc(r.verdict)}</span> ${fmt(r.match_rate_pct)}% matched</h2></div>
      <div class="kv"><div>Admin trades</div><div>${r.admin_trades}</div><div>Our trades</div><div>${r.our_trades}</div><div>Matched</div><div>${r.matched}</div><div>Shifted</div><div>${r.shifted}</div><div>Missing</div><div>${r.missing}</div><div>Extra</div><div>${r.extra}</div>
      ${r.admin_net_pnl != null ? `<div>Admin net P&L</div><div>${inrS(r.admin_net_pnl)}</div><div>Our net P&L</div><div>${inrS(r.our_net_pnl)}</div>` : ""}</div>
      <p>${r.likely_causes.map(esc).join("<br>")}</p><pre class="log">${esc(JSON.stringify(r.examples, null, 1))}</pre>`;
  } catch (e) { $("#par-out").innerHTML = `<pre class="log red">${esc(e.message)}</pre>`; }
};

// ---------------------------------------------------------------- data
const today = new Date(); $("#df-end").value = today.toISOString().slice(0, 10);
$("#df-start").value = new Date(today - 55 * 864e5).toISOString().slice(0, 10);
$("#df-go").onclick = async () => {
  $("#df-out").textContent = "Fetching…";
  try { const r = await api("/api/data/fetch", { json: { symbols: syms("#df-symbols"), source: $("#df-source").value, interval: $("#df-interval").value, start: $("#df-start").value, end: $("#df-end").value } });
    $("#df-out").textContent = JSON.stringify(r, null, 2); loadInventory(); } catch (e) { $("#df-out").textContent = e.message; }
};
$("#up-preview").onclick = async () => {
  const f = $("#up-file").files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
  try { const r = await api("/api/data/upload/preview", { method: "POST", body: fd }); $("#up-out").textContent = JSON.stringify(r, null, 2);
    $("#up-kind").value = r.kind_guess === "trades" ? "trades" : "candles"; } catch (e) { $("#up-out").textContent = e.message; }
};
$("#up-go").onclick = async () => {
  const f = $("#up-file").files[0]; if (!f) return; const fd = new FormData();
  fd.append("file", f); fd.append("kind", $("#up-kind").value); fd.append("symbol", $("#up-symbol").value); fd.append("interval", $("#up-interval").value); fd.append("mapping", $("#up-mapping").value || "{}");
  $("#up-out").textContent = "Validating…";
  try { const r = await api("/api/data/upload", { method: "POST", body: fd }); $("#up-out").textContent = JSON.stringify(r, null, 2); loadInventory(); } catch (e) { $("#up-out").textContent = e.message; }
};
async function loadInventory() {
  const r = await api("/api/data/inventory");
  $("#inv tbody").innerHTML = r.candles.map(c => `<tr><td><b>${esc(c.symbol)}</b></td><td>${esc(c.interval)}</td><td class="r">${Number(c.n).toLocaleString("en-IN")}</td><td class="mono">${esc(fmtTs(c.first))}</td><td class="mono">${esc(fmtTs(c.last))}</td><td class="t3">${esc(c.source)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="empty"><div class="empty-act"><span>No stored candles yet. Real backtests and the AI score model need history.</span><button type="button" class="sm primary" id="inv-go">Download history for the watchlist</button></div></td></tr>`;
  $("#inv-go") && ($("#inv-go").onclick = () => { $("#dh-panel").scrollIntoView({ behavior: "smooth", block: "start" }); $("#dh-go").focus({ preventScroll: true }); });
}

// ---------------------------------------------------------------- models & strategies
const STATUSES = ["candidate", "approved", "paper", "live_small", "live", "retired"];
async function loadPerformance() {
  try {
    const p = await api("/api/performance");
    const vb = v => v === "ready_for_next_stage" ? '<span class="pill pass">READY</span>' : v === "underperforming" ? '<span class="pill fail">UNDER</span>' : '<span class="pill review">COLLECTING</span>';
    $("#perf tbody").innerHTML = p.strategies.map(x => { const a = x.paper || {}, b = x.backtest || {}; return `<tr><td><b>${esc(x.strategy)}</b> v${x.version}</td><td>${esc(x.status)}</td>
      <td class="r">${a.trades || 0}</td><td class="r">${a.sessions || 0}/${p.min_sessions}</td><td class="r ${cls(a.net_pnl)}">${inrS(a.net_pnl)}</td>
      <td class="r">${a.win_rate ?? "—"} / ${b.win_rate ?? "—"}</td><td class="r">${a.avg_ret_pct ?? "—"} / ${b.avg_ret_pct ?? "—"}</td><td>${vb(x.verdict)}</td>
      <td class="t3 xs wrap" style="max-width:360px">${esc(x.note || (x.expected_avg_ret_band ? "expected avg return " + x.expected_avg_ret_band.join(" to ") + "%" : "needs " + (x.sessions_left ?? "") + " more sessions"))}</td></tr>`; }).join("")
      || `<tr><td colspan="9" class="empty">No strategy in paper status yet — set one to <b>paper</b> below to start collecting live paper results.</td></tr>`;
  } catch (e) {}
}
async function loadModelsTab() {
  loadPerformance();
  await loadStrategySelects();
  const st = await api("/api/strategies");
  $("#strats tbody").innerHTML = st.map(r => `<tr><td><b>${esc(r.name)}</b></td><td class="r">${r.version}</td><td class="t3">${esc(r.origin)}</td><td class="t3 mono">${esc(fmtTs(r.created_at, { utc: true }))}</td>
    <td><select class="sm" data-n="${esc(r.name)}" data-v="${r.version}" aria-label="Status of ${esc(r.name)} v${r.version}">${STATUSES.map(s => `<option ${s === r.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
    <td><button type="button" class="sm" data-params="${esc(r.name)}|${r.version}|${esc(r.status)}">settings</button> <button type="button" class="sm" data-code="${r.id}">code</button></td></tr>`).join("");
  $$("#strats select").forEach(s => s.onchange = async () => { try { await api(`/api/strategies/${s.dataset.n}/${s.dataset.v}/status`, { json: { status: s.value } }); toast(`${esc(s.dataset.n)} v${s.dataset.v} → <b>${esc(s.value)}</b>`, ""); } catch (e) { alert(e.message); loadModelsTab(); } });
  $$("#strats [data-params]").forEach(b => b.onclick = async () => {
    const [n, v, st] = b.dataset.params.split("|");
    const p = await api(`/api/strategies/${n}/${v}/params`);
    const merged = { ...p.defaults, ...p.saved };
    $("#dlg-body").innerHTML = `<div class="panel-h"><h2>${esc(n)} v${v} settings</h2></div><div class="form" id="sp-form">` +
      Object.entries(merged).map(([k, val]) => `<label>${esc(k)}${typeof val === "boolean" ? `<select data-k="${k}" data-t="b"><option ${val ? "selected" : ""}>true</option><option ${!val ? "selected" : ""}>false</option></select>`
        : `<input data-k="${k}" data-t="${typeof val === "number" ? "n" : "s"}" value="${esc(val)}">`}</label>`).join("") +
      `</div><p class="t3 xs">Applies from the next candle. entry_mode: touch = buy/sell the moment the zone edge is hit; close = wait for a candle close beyond it. max_trades_per_day limits calls per stock per day.</p>`;
    const d = $("#dlg"); d.showModal();
    d.onclose = async () => {
      if (d.returnValue !== "ok") return;
      const params = {}; $$("#sp-form [data-k]").forEach(i => { const t = i.dataset.t; params[i.dataset.k] = t === "n" ? parseFloat(i.value) : t === "b" ? i.value === "true" : i.value; });
      try { await api(`/api/strategies/${n}/${v}/status`, { json: { status: st, params } }); toast("Strategy settings saved", "buy"); loadModelsTab(); } catch (e) { toast(esc(e.message), "err"); }
    };
  });
  $$("#strats [data-code]").forEach(b => b.onclick = () => { const r = st.find(x => x.id == b.dataset.code); $("#dlg-body").innerHTML = `<pre class="log" style="max-height:70vh;min-width:min(60vw,900px)">${esc(r.code)}</pre>`; $("#dlg").showModal(); });
  const ms = await api("/api/models");
  $("#models tbody").innerHTML = ms.map(m => `<tr><td>${esc(m.kind)}</td><td class="mono">${esc(m.version)}</td><td>${esc(m.strategy || "all")}</td><td class="r">${fmt(m.metrics.oos_auc, 3)}</td>
    <td class="r">${fmt((m.metrics.hit_rate_top40pct || 0) * 100, 1)}% / ${fmt((m.metrics.hit_rate_all || 0) * 100, 1)}%</td><td>${m.status === "champion" ? '<span class="pill pass">champion</span>' : esc(m.status)}</td>
    <td>${m.status !== "champion" ? `<button type="button" class="sm" data-p="${m.id}">promote</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="7" class="empty">No models yet. Train a meta-label model above (needs stored history — see the Data tab), or run Phase 4 setup on the System tab.</td></tr>`;
  $$("#models [data-p]").forEach(b => b.onclick = async () => { await api(`/api/models/${b.dataset.p}/promote`, { method: "POST" }); loadModelsTab(); });
  const bts = await api("/api/backtests");
  $("#bts tbody").innerHTML = bts.map(b => { const s = b.summary || {}; return `<tr><td class="r">${b.id}</td><td>${esc(b.kind)}</td><td>${esc(b.strategy)} v${b.strategy_version}</td><td>${s.verdict ? `<span class="pill ${s.verdict.toLowerCase()}">${esc(s.verdict)}</span>` : ""}</td>
    <td class="r">${s.trades ?? ""}</td><td class="r ${cls(s.net_pnl)}">${s.net_pnl == null ? "—" : inrS(s.net_pnl)}</td><td class="r">${fmt(s.win_rate)}</td><td class="r">${fmt(s.profit_factor)}</td><td class="r">${fmt(s.sharpe)}</td>
    <td>${b.kind === "full" ? `<button type="button" class="sm" data-b="${b.id}">open</button>` : ""}</td></tr>`; }).join("")
    || `<tr><td colspan="10" class="empty">No backtests yet. <button type="button" class="sm" data-go="backtest">Run one</button></td></tr>`;
  $$("#bts [data-b]").forEach(x => x.onclick = async () => { const b = await api(`/api/backtests/${x.dataset.b}`); showTab("backtest"); $("#bt-out").innerHTML = evaluationHTML(b.result); });
}
$("#tm-go").onclick = async () => {
  $("#tm-status").innerHTML = `<span class="spin"></span> Training…`;
  try { const { job_id } = await api("/api/models/train-meta", { json: { strategy: $("#tm-strategy").value, symbols: syms("#tm-symbols"), start: $("#tm-start").value, end: $("#tm-end").value, source: $("#tm-source").value } });
    const j = await pollJob(job_id, j => $("#tm-status").innerHTML = `<span class="spin"></span> stage: ${esc(j.stage)}`);
    $("#tm-status").textContent = j.status === "done" ? `Trained ${j.result.version}: OOS AUC ${fmt(j.result.metrics.oos_auc, 3)}` : j.error; loadModelsTab();
  } catch (e) { $("#tm-status").textContent = e.message; }
};
$("#ti-go").onclick = async () => { try { const r = await api("/api/models/train-imitation", { method: "POST" }); $("#tm-status").textContent = `Imitation model: OOS AUC ${fmt(r.metrics.oos_auc, 3)}`; loadModelsTab(); } catch (e) { $("#tm-status").textContent = e.message; } };

// ---------------------------------------------------------------- KPI tiles
const kpi = (k, v, c = "", s = "", title = "") => `<div class="card"${title ? ` title="${esc(title)}"` : ""}><div class="k">${k}</div><div class="v ${c}">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;

// ---------------------------------------------------------------- feedback
async function loadFeedback() {
  const s = await api("/api/feedback/scoreboard");
  $("#fb-cards").innerHTML = kpi("Calls with outcome", s.calls_with_outcome) + kpi("Calls you reviewed", s.calls_with_feedback) +
    kpi("AI alone P&L", inrS(s.ai_alone_pnl), cls(s.ai_alone_pnl)) + kpi("Your filter P&L", inrS(s.admin_filtered_pnl), cls(s.admin_filtered_pnl)) +
    kpi("Combined P&L", inrS(s.combined_pnl), cls(s.combined_pnl)) + kpi("Your decision accuracy", s.admin_decision_accuracy_pct == null ? "—" : s.admin_decision_accuracy_pct + "%");
  $("#fb-tags tbody").innerHTML = Object.entries(s.by_tag).map(([t, d]) => `<tr><td>${esc(t)}</td><td class="r">${d.count}</td><td class="r">${d.rejected}</td><td class="r">${d.admin_right_pct}%</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty"><div class="empty-act"><span>No remarks yet. Review calls on the Live tab (✓ / ✗ / ✎) and add a short remark to see patterns here.</span><button type="button" class="sm" data-go="live">Go to Live calls</button></div></td></tr>`;
}
$("#fb-suggest").onclick = async () => {
  $("#fb-sugg").innerHTML = `<p class="t3"><span class="spin"></span> Thinking…</p>`;
  try { const r = await api("/api/feedback/suggestions", { method: "POST" });
    $("#fb-sugg").innerHTML = r.suggestions.map(s => `<div class="panel"><b>${esc(s.title)}</b><p class="t3">${esc(s.evidence)}</p><button type="button" class="sm" data-p="${esc(s.prompt_for_builder)}">Send to AI Builder</button></div>`).join("") || `<p class="t3">Not enough reviewed calls yet.</p>`;
    $$("#fb-sugg [data-p]").forEach(b => b.onclick = () => { $("#ai-prompt").value = b.dataset.p; showTab("builder"); });
  } catch (e) { $("#fb-sugg").innerHTML = `<pre class="log red">${esc(e.message)}</pre>`; }
};

// ---------------------------------------------------------------- paper wallet
let _walletBusy = false, _walletAgain = false;
async function loadWallet() {
  if (_walletBusy) { _walletAgain = true; return; }      // collapse bursts of refreshes into one
  _walletBusy = true;
  try { await _loadWallet(); } finally { _walletBusy = false; if (_walletAgain) { _walletAgain = false; setTimeout(loadWallet, 300); } }
}
function openPosRows(rows) {
  return rows.map((t, i) => `<tr><td class="t3">${t.id}</td><td class="mono">${esc(fmtTs(t.entry_ts))}</td><td>${tcBtn("open:" + i, t.symbol)}</td>
      <td><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td><td class="r">${t.qty}</td><td class="r">${fmt(t.entry_price)}</td><td class="r">${fmt(t.ltp)}</td>
      <td class="r">${fmt(t.stop_loss)}</td><td class="r">${fmt(t.target)}</td><td class="r ${cls(t.mtm)}"><b>${inrS(t.mtm)}</b></td><td class="t3">${esc((t.strategy || "").replaceAll("_", " "))}</td>
      <td><button type="button" class="sm" data-close="${t.id}" data-sym="${esc(t.symbol)}">Close now</button></td></tr>`).join("");
}
async function _loadWallet() {
  try {
    const w = await api("/api/wallet");
    window._wallet = w; renderHeader(); renderLivePositions();
    const tr = await api("/api/wallet/trades");
    const closed = tr.filter(t => t.status === "closed" && t.exit_reason !== "void").sort((a, b) => String(b.exit_ts).localeCompare(String(a.exit_ts)) || b.id - a.id);
    const tday = istToday();
    const todayClosed = closed.filter(t => tsDay(t.exit_ts) === tday);
    const tWins = todayClosed.filter(t => t.net_pnl > 0).length;
    const winToday = todayClosed.length ? Math.round(tWins / todayClosed.length * 1000) / 10 : null;
    const day = (w.today_realized || 0) + (w.open_mtm || 0);
    $("#w-cards").innerHTML =
      `<div class="kpi-grp"><div class="kpi-gl">Today</div><div class="kpi-row">` +
        kpi("Day P&L", inrS(day), cls(day), "realised + open MTM") +
        kpi("Today realised", inrS(w.today_realized), cls(w.today_realized), `${w.today_trades ?? 0} trades`) +
        kpi("Open MTM", inrS(w.open_mtm), cls(w.open_mtm), `${(w.open_positions || []).length} open`) +
        (winToday != null ? kpi("Win rate today", winToday + "%", "", `${tWins} of ${todayClosed.length}`) : kpi("Win rate", w.win_rate == null ? "—" : w.win_rate + "%", "", `all ${w.closed_trades ?? ""} closed`)) +
      `</div></div><div class="kpi-grp"><div class="kpi-gl">Account</div><div class="kpi-row">` +
        kpi("Equity", inr(w.equity), "", `started ${inr(w.starting_balance)}`) +
        kpi("Cash balance", inr(w.balance), "", `margin blocked ${inr(w.blocked_margin)}`) +
        kpi("Available", inr(w.available), "", "for new trades") +
        kpi("Total P&L", inrS(w.total_pnl), cls(w.total_pnl), `costs paid ${inr(w.total_costs)}`) +
      `</div></div>`;
    $("#w-lock").innerHTML = w.real_orders === "LOCKED"
      ? `<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2h.5a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zm1.5 0h4V5a2 2 0 0 0-4 0z" fill="currentColor"/></svg><b>Paper trading only — real orders are LOCKED.</b> <span class="t3">Calls fill against live Upstox prices; nothing is sent to Upstox.</span>`
      : `<b class="red">WARNING: real orders are ENABLED in .env</b>`;
    $("#w-note").textContent = `slippage ${w.slippage_bps} bps · leverage ${w.leverage}x · futures margin ${w.fut_margin_pct}%`;
    const op = w.open_positions || [];
    $("#w-open tbody").innerHTML = openPosRows(op) || `<tr><td colspan="12" class="empty">No open paper positions. Calls that pass the risk checks are filled here at the live price.</td></tr>`;
    TC_SRC.open = op.map(t => ({ symbol: t.symbol, day: tsDay(t.entry_ts), strategy: t.strategy, side: t.side, entry_ts: t.entry_ts, entry: t.entry_price, exit_ts: null, exit: null, exit_reason: "open", stop_loss: t.stop_loss, target: t.target, pnl: t.mtm }));
    $("#w-open-cards").innerHTML = posCards(op, "open") || `<p class="t3 xs">No open paper positions.</p>`;
    wireClose($("#tab-wallet"));
    // closed trades: newest first, grouped by day, with totals
    let lastDay = null, html = "";
    TC_SRC.closed = closed.map(t => ({ symbol: t.symbol, day: tsDay(t.entry_ts), strategy: t.strategy, side: t.side, entry_ts: t.entry_ts, entry: t.entry_price, exit_ts: t.exit_ts, exit: t.exit_price, exit_reason: t.exit_reason, stop_loss: t.init_stop ?? t.stop_loss, target: t.target, pnl: t.net_pnl }));
    closed.forEach((t, ci) => {
      const d = tsDay(t.exit_ts);
      if (d !== lastDay) { lastDay = d; const g = closed.filter(x => tsDay(x.exit_ts) === d); const net = g.reduce((a, x) => a + (x.net_pnl || 0), 0);
        html += `<tr class="day-h"><td colspan="12">${esc(dayLabel(d))} · ${g.length} trades · net <span class="${cls(net)}">${inrS(net)}</span></td></tr>`; }
      html += `<tr><td class="t3">${t.id}</td><td class="mono">${esc(fmtTs(t.entry_ts))}</td><td class="mono">${esc(fmtTs(t.exit_ts))}</td>
      <td>${tcBtn("closed:" + ci, t.symbol)}</td><td><span class="pill ${t.side.toLowerCase()}">${t.side}</span></td><td class="r">${t.qty}</td><td class="r">${fmt(t.entry_price)}</td><td class="r">${fmt(t.exit_price)}</td><td>${reasonHTML(t.exit_reason)}</td>
      <td class="r ${cls(t.gross_pnl)}">${inrS(t.gross_pnl)}</td><td class="r t3">${inr(t.costs)}</td><td class="r ${cls(t.net_pnl)}"><b>${inrS(t.net_pnl)}</b></td></tr>`;
    });
    $("#w-closed tbody").innerHTML = html || `<tr><td colspan="12" class="empty">No closed trades yet.</td></tr>`;
    const tot = list => { const g = list.reduce((a, x) => a + (x.gross_pnl || 0), 0), c = list.reduce((a, x) => a + (x.costs || 0), 0), n = list.reduce((a, x) => a + (x.net_pnl || 0), 0);
      return `gross <b class="${cls(g)}">${inrS(g)}</b> costs <b>${inr(c)}</b> net <b class="${cls(n)}">${inrS(n)}</b>`; };
    const latest = closed.length ? tsDay(closed[0].exit_ts) : null;
    const latestList = closed.filter(x => tsDay(x.exit_ts) === latest);
    $("#w-closed-tot").innerHTML = closed.length
      ? `<span class="grp">${esc(dayLabel(latest))} (${latestList.length}) · ${tot(latestList)}</span><span class="grp">All ${closed.length} · ${tot(closed)}</span>` : "";
    const lg = await api("/api/wallet/ledger");
    lg.sort((a, b) => String(b.ts).localeCompare(String(a.ts)) || b.id - a.id);
    $("#w-ledger tbody").innerHTML = lg.map(l => `<tr><td class="mono">${esc(fmtTs(l.ts))}</td><td>${esc(l.kind)}</td><td class="r ${cls(l.amount)}">${inrS(l.amount, 2)}</td><td class="r">${inr(l.balance_after, 2)}</td><td class="t3">${esc(String(l.note || "").replace(/\b(stop_loss|target_gap|target|sl_gap|square_off|reversed|manual|eod)\b/g, m => reasonText(m).toLowerCase()))}</td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">No ledger entries yet.</td></tr>`;
  } catch (e) { $("#w-cards").innerHTML = `<pre class="log red">${esc(e.message)}</pre>`; }
}
let _lastRebuild = null;
function resultTone(x) { const r = String(x.result || ""); return x.entry == null ? (r.startsWith("skipped") ? "amber" : "t3") : reasonTone(r); }
function renderToday(j) {
  if (!j || !j.status) return;
  if (j.status === "running") { $("#td-msg").innerHTML = `<span class="spin"></span> ${esc(j.stage)}`; return; }
  $("#td-bar").hidden = true; $("#td-go").disabled = false;
  if (j.status !== "done") { $("#td-msg").innerHTML = `<span class="red">${esc(j.error || "failed")}</span>`; return; }
  const r = j.result;
  _lastRebuild = r.at; tsSumline();
  $("#td-out").hidden = false;
  const hm = t => !t ? null : /^\d{2}:\d{2}$/.test(t) ? `${r.date} ${t}:00` : String(t);
  TC_SRC.rebuild = r.rows.map(x => ({ symbol: x.symbol, day: r.date, strategy: x.strategy || (r.strategy === "AI pick" ? "ai_pick" : r.strategy === "pair trading" ? "pair_trading" : TS.logic), side: x.side, entry_ts: hm(x.entry_time), entry: x.entry, exit_ts: hm(x.exit_time), exit: x.exit,
    exit_reason: x.result, stop_loss: x.stop_loss, target: x.target, pnl: x.net_pnl ?? x.mtm, chartable: !!x.entry_time }));
  $("#td-out tbody").innerHTML = r.rows.map((x, i) => `<tr><td><b>${esc(x.symbol)}</b>${x.strategy ? `<div class="t3 xs">${esc(x.strategy.replaceAll("_", " "))}</div>` : ""}</td><td>${x.side ? `<span class="pill ${x.side.toLowerCase()}">${x.side}</span>` : ""}</td>
    <td class="mono">${esc(x.entry_time || "")}</td><td class="r">${x.entry != null ? fmt(x.entry) : ""}</td>
    <td class="wrap"><span class="why ${resultTone(x)}" title="${esc(x.result)}${x.result === "reversed" ? " — " + esc(RELAY_TIP) : ""}">${esc(x.result === "open" ? "Open" : reasonText(x.result))}</span></td><td class="mono">${esc(x.exit_time || "")}</td>
    <td class="r">${x.exit != null ? fmt(x.exit) : x.ltp != null ? fmt(x.ltp) : ""}</td>
    <td class="r ${cls(x.net_pnl ?? x.mtm)}">${x.net_pnl != null ? inrS(x.net_pnl) : x.mtm != null ? inrS(x.mtm) + " MTM" : ""}</td><td>${x.entry_time ? tcBtn("rebuild:" + i, "") : ""}</td></tr>`).join("");
  renderTodayExtra(r);
  $("#td-msg").innerHTML = `<b>${esc(r.is_today === false ? new Date(r.date + "T00:00:00Z").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "Today")}</b> · ${r.strategy ? esc(r.strategy.replaceAll("_", " ")) + " · " : ""}${r.zone_candles ? "zone: " + esc(r.zone_candles) + " · entries: 1-minute · " : ""}${r.stocks} instruments · ${r.trades} trades · ${r.open} still open · realised <b class="${cls(r.realised)}">${inrS(r.realised)}</b> · open MTM <b class="${cls(r.open_mtm)}">${inrS(r.open_mtm)}</b> <span class="xs t3">(built ${esc(fmtTs(r.at))})</span>`;
}
async function followToday(jid) {
  $("#td-bar").hidden = false; $("#td-go").disabled = true;
  const j = await pollJob(jid, renderToday);
  renderToday(j); loadWallet(); loadCalls?.(); loadTodaySignals();
  if (j.status === "done") toast(`Today rebuilt: ${j.result.trades} trades, ${j.result.open} open`, "buy");
}
// ---- test scope: instruments + one logic + version
const TS = { stocks: [], futures: [], allFut: [], strategies: {}, sel: new Set(), logic: null };
function lotInfo(sym) { const f = TS.allFut.find(x => x.symbol === sym); return f ? `${f.trading_symbol} · lot ${f.lot_size} · expiry ${f.expiry}` : "near-month future"; }
const LOGIC_NAMES = { first_red_candle_zone: "Zone (first red candle)", hari_master_block: "Hari Master Block", reversal_rsi: "RSI reversal", momentum: "Momentum",
  breakout_sr: "Breakout (S/R)", scalping_vwap: "VWAP scalping", pivot_points: "Pivot / CPR", orb: "Opening range breakout", gap_and_go: "Gap and Go",
  flag_pattern: "Bull / bear flag", ma_crossover: "EMA crossover", pullback: "Trend pullback", vwap_reversion: "VWAP mean reversion", supertrend_trend: "Supertrend",
  pair_trading: "Pair trading", ai_pick: "AI pick (best per stock)" };
const lname = n => LOGIC_NAMES[n] || String(n || "—").replaceAll("_", " ");
function tsSumline() {
  const n = TS.sel.size, ver = $("#ts-version").value;
  $("#ts-sumline").textContent = `${n} instruments · ${lname(TS.logic)}${ver ? " v" + ver : ""} · last rebuilt ${_lastRebuild ? fmtTs(_lastRebuild) : "—"}`;
}
function tsVisible() {
  const q = ($("#ts-filter").value || "").trim().toUpperCase();
  const futs = $("#ts-allfut").checked ? [...new Set([...TS.futures, ...TS.allFut.map(f => f.symbol)])] : TS.futures;
  return [...($("#ts-seg-eq").checked ? TS.stocks : []), ...($("#ts-seg-fut").checked ? futs : [])].filter(x => !q || x.replace("-FUT", "").includes(q.replace("-FUT", "")));
}
function tsRender() {
  $("#ts-n-eq").textContent = `(${TS.stocks.filter(x => TS.sel.has(x)).length}/${TS.stocks.length})`;
  const nf = [...TS.sel].filter(x => x.endsWith("-FUT")).length;
  $("#ts-n-fut").textContent = `(${nf}/${$("#ts-allfut").checked ? TS.allFut.length || TS.futures.length : TS.futures.length})`;
  $("#ts-list").innerHTML = tsVisible().map(x => `<label class="${TS.sel.has(x) ? "on" : ""}"><input type="checkbox" data-s="${esc(x)}" ${TS.sel.has(x) ? "checked" : ""}>${esc(x.replace("-FUT", ""))}${x.endsWith("-FUT") ? ` <span class="pill review xs" title="${esc(lotInfo(x))}">FUT</span>${TS.futures.includes(x) ? "" : ' <span class="t3 xs" title="not in the watchlist yet: added on rebuild">+</span>'}` : ""}</label>`).join("") || `<span class="t3 xs">nothing to show</span>`;
  $$("#ts-list [data-s]").forEach(i => i.onchange = () => { i.checked ? TS.sel.add(i.dataset.s) : TS.sel.delete(i.dataset.s); tsRender(); });
  const names = Object.keys(TS.strategies);
  if (!TS.logic || !names.includes(TS.logic)) TS.logic = names.includes("first_red_candle_zone") ? "first_red_candle_zone" : names[0];
  $("#ts-logic").innerHTML = logicGroups(names).map(([g, list]) => `<div class="lp-g"><div class="lp-h">${esc(g)}</div><div class="lp-c">${list.map(n =>
    `<button type="button" role="radio" class="lp-chip${n === TS.logic ? " on" : ""}" aria-checked="${n === TS.logic}" data-l="${esc(n)}" title="${esc(stratDesc(n))}">${esc(lname(n))}</button>`).join("")}</div></div>`).join("");
  $$("#ts-logic [data-l]").forEach(b => b.onclick = () => { TS.logic = b.dataset.l; TS.version = null; tsRender(); if (IND.auto && !IND.stratSet) { IND.strat = TS.logic; indSave(); indRenderPop(); drawIndicators(selected, histMode); } });
  const vs = (TS.strategies[TS.logic] || []).slice().reverse();
  $("#ts-version").innerHTML = vs.map(v => `<option value="${v.version}" ${String(v.version) === String(TS.version ?? vs[0]?.version) ? "selected" : ""}>v${v.version} · ${esc(v.status)}${v === vs[0] ? " (latest)" : ""}</option>`).join("");
  $("#ts-desc").innerHTML = `<b>${esc(lname(TS.logic))}</b> · ${esc(stratDesc(TS.logic) || "")}`;
  const nEq = [...TS.sel].filter(x => !x.endsWith("-FUT")).length, nFu = [...TS.sel].filter(x => x.endsWith("-FUT")).length;
  $("#ts-summary").textContent = `${nEq + nFu} selected (${nEq} stocks, ${nFu} futures) · ${lname(TS.logic)} v${$("#ts-version").value}`;
  tsSumline();
}
async function loadScope() {
  try {
    const r = await api("/api/wallet/test-scope");
    Object.assign(TS, { stocks: r.stocks, futures: r.futures, allFut: r.all_futures || [], strategies: r.strategies });
    const all = [...r.stocks, ...r.futures, ...(r.all_futures || []).map(f => f.symbol)];
    if (r.scope) { TS.sel = new Set((r.scope.symbols || all).filter(x => all.includes(x))); TS.logic = r.scope.strategy; TS.version = r.scope.version;
      const sel = [...TS.sel]; $("#ts-seg-eq").checked = sel.some(x => !x.endsWith("-FUT")); $("#ts-seg-fut").checked = sel.some(x => x.endsWith("-FUT"));
      if (sel.some(x => x.endsWith("-FUT") && !r.futures.includes(x))) $("#ts-allfut").checked = true; }
    else if (!TS.sel.size) TS.sel = new Set([...r.stocks, ...r.futures]);
    tsRender();
    if (IND.auto && !IND.stratSet && TS.logic && IND.strat !== TS.logic) { IND.strat = TS.logic; indRenderPop(); drawIndicators(selected, histMode); }
  } catch (e) { $("#ts-list").innerHTML = `<span class="red xs">${esc(e.message)}</span>`; }
}
// unticking Stocks / Futures removes that whole group from the selection (what you see is what gets rebuilt)
$("#ts-seg-eq").onchange = () => { if (!$("#ts-seg-eq").checked) [...TS.sel].filter(x => !x.endsWith("-FUT")).forEach(x => TS.sel.delete(x)); tsRender(); };
$("#ts-seg-fut").onchange = () => { if (!$("#ts-seg-fut").checked) [...TS.sel].filter(x => x.endsWith("-FUT")).forEach(x => TS.sel.delete(x)); tsRender(); };
$("#ts-allfut").onchange = tsRender;
async function loadMargin() {
  try { const m = await api("/api/wallet/margin"); $("#w-lev").value = m.equity_leverage; $("#w-futm").value = m.fut_margin_pct; } catch (e) {}
}
$("#w-margin").onclick = async () => {
  try { const m = await api("/api/wallet/margin", { method: "PUT", json: { equity_leverage: parseFloat($("#w-lev").value), fut_margin_pct: parseFloat($("#w-futm").value) } });
    $("#w-margin-msg").innerHTML = `<span class="green">saved: stocks ${m.equity_leverage}x, futures ${m.fut_margin_pct}% - rebuild to apply to today</span>`; loadWallet(); }
  catch (e) { $("#w-margin-msg").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};
loadMargin();
$("#ts-filter").oninput = tsRender;
$("#ts-version").onchange = () => { TS.version = $("#ts-version").value; tsRender(); };
$("#ts-all").onclick = () => { tsVisible().forEach(x => TS.sel.add(x)); tsRender(); };
$("#ts-none").onclick = () => { TS.sel.clear(); tsRender(); };
loadScope();
// ---- which day to rebuild
function selDay() { return $("#ts-date").value || istToday(); }
function tsDayLabel() { const d = selDay(); return d === istToday() ? "today" : new Date(d + "T00:00:00Z").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }); }
function tsDateChanged() {
  const d = selDay(), wd = new Date(d + "T00:00:00Z").getUTCDay(), past = d !== istToday();
  $("#td-go").textContent = `Rebuild trades for ${tsDayLabel()}`;
  $("#ts-date-note").innerHTML = wd === 0 || wd === 6 ? '<span class="amber">weekend - no market that day</span>'
    : d > istToday() ? '<span class="amber">future date</span>'
    : past ? "past day: replays the whole session 09:15–15:30; your live trading selection is not changed" : "";
}
$("#ts-date").max = istToday(); $("#ts-date").value = istToday();
$("#ts-date").onchange = tsDateChanged;
$("#ts-today").onclick = () => { $("#ts-date").value = istToday(); tsDateChanged(); };
$("#ts-prev").onclick = () => { const d = new Date(selDay() + "T00:00:00Z"); do { d.setUTCDate(d.getUTCDate() - 1); } while ([0, 6].includes(d.getUTCDay())); $("#ts-date").value = d.toISOString().slice(0, 10); tsDateChanged(); };
tsDateChanged();
$("#td-go").onclick = async () => {
  const symbols = [...TS.sel].filter(x => x.endsWith("-FUT") ? $("#ts-seg-fut").checked : $("#ts-seg-eq").checked);
  if (!symbols.length) { $("#td-msg").innerHTML = '<span class="red">select at least one stock or future</span>'; return; }
  const logic = LOGIC_NAMES[TS.logic] || TS.logic, ver = parseInt($("#ts-version").value);
  const ns = symbols.filter(x => !x.endsWith("-FUT")).length, nf = symbols.length - ns;
  const day = selDay(), isToday = day === istToday(), lbl = tsDayLabel();
  if (!confirm(`Rebuild ${lbl} with ${logic} v${ver} on ${symbols.length} instruments (${ns} stocks, ${nf} futures)?\n\n${symbols.map(x => x.replace("-FUT", " FUT")).join(", ")}\n\nZone from 5-minute candles, entries/exits on 1-minute candles.\nAll other paper trades and calls of ${lbl} are removed${isToday ? ", and the live engine trades only this selection from now on." : ". Other days are not touched."}`)) return;
  try { const { job_id } = await api("/api/wallet/rebuild-today", { json: { symbols, strategy: TS.logic, version: ver, date: day } }); followToday(job_id); }
  catch (e) { $("#td-msg").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};
$("#w-clear").onclick = async () => {
  const calls = $("#w-clear-calls").checked, amt = parseFloat($("#w-amt").value);
  if (!confirm(`Delete ALL paper trades${calls ? ", calls and feedback" : ""} and the ledger, and restart the demo wallet at Rs ${amt.toLocaleString("en-IN")}? This cannot be undone.`)) return;
  try { const r = await api("/api/wallet/clear-history", { json: { amount: amt, include_calls: calls } });
    toast(`Cleared ${r.removed_trades} trades${calls ? " and " + r.removed_calls + " calls" : ""}`, "buy"); loadWallet(); loadCalls?.(); loadTodaySignals(); $("#td-out").hidden = true; $("#td-msg").textContent = ""; }
  catch (e) { toast(esc(e.message), "sell"); }
};
api("/api/jobs/latest/today_rebuild").then(j => { if (j.status === "running") followToday(j.id); else renderToday(j); }).catch(() => {});
$("#w-deposit").onclick = async () => { try { await api("/api/wallet/deposit", { json: { amount: parseFloat($("#w-amt").value) } }); toast(`Added ${inr(parseFloat($("#w-amt").value))} demo money`, "buy"); loadWallet(); } catch (e) { alert(e.message); } };
$("#w-reset").onclick = async () => { if (!confirm("Reset the demo wallet? Trade history is kept.")) return; try { await api("/api/wallet/reset", { json: { amount: parseFloat($("#w-amt").value) } }); toast("Demo wallet reset", ""); loadWallet(); } catch (e) { alert(e.message); } };
setInterval(() => { if (curTab === "wallet") loadWallet(); }, 5000);

// ---------------------------------------------------------------- system: sub-tabs
let sysPane = store.get("yx_sys") || "health";
function setSys(p) {
  if (!["health", "setup", "risk", "activity"].includes(p)) p = "health";
  sysPane = p; store.set("yx_sys", p);
  $$("#sys-seg [data-sys]").forEach(b => { const on = b.dataset.sys === p; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on)); });
  $$("#tab-risk .sys-pane").forEach(x => x.hidden = x.dataset.pane !== p);
  setTimeout(markScrolls, 30);
}
$$("#sys-seg [data-sys]").forEach(b => b.onclick = () => setSys(b.dataset.sys));
setSys(sysPane);

// ---------------------------------------------------------------- system check
const QUICK_CHECKS = 9;
const scBadge = st => `<span class="pill ${st === "pass" ? "pass" : st === "warn" ? "review" : "fail"}">${esc(String(st).toUpperCase())}</span>`;
function scRows(results, running) {
  return (results || []).map(x => `<tr><td>${esc(x.check)}</td><td>${scBadge(x.status)}</td><td class="r">${x.seconds}</td>
    <td class="t3 wrap" style="white-space:pre-wrap;max-width:760px">${esc(typeof x.detail === "string" ? x.detail : JSON.stringify(x.detail))}</td></tr>`).join("") +
    (running ? `<tr><td>${esc(running)}</td><td><span class="spin"></span> running</td><td></td><td class="t3 wrap">${running.startsWith("AI builder") ? "the local model is writing and testing a strategy — this step takes several minutes" : ""}</td></tr>` : "");
}
function renderCheck(r) {
  if (!r || !r.results) return;
  $("#sc-when").textContent = `last run ${fmtTs(r.at)} · ${r.summary.pass} pass · ${r.summary.warn} warn · ${r.summary.fail} fail`;
  $("#sc-out tbody").innerHTML = scRows(r.results);
  $("#sc-bar").hidden = true;
  window._lastCheck = r; if (curTab === "risk") renderSysStatus();
}
let scTimer = null;
async function followCheck(jobId, deep) {
  $("#sc-quick").disabled = $("#sc-deep").disabled = true;
  $("#sc-bar").hidden = false;
  let total = QUICK_CHECKS + (deep ? 1 : 0);
  const t0 = Date.now();
  clearInterval(scTimer);
  scTimer = setInterval(() => { $("#sc-elapsed").textContent = `${Math.round((Date.now() - t0) / 1000)}s`; }, 1000);
  try {
    const j = await pollJob(jobId, j => {
      total = (j.log && j.log[0] && j.log[0].total) || total;
      const done = (j.partial || []).length;
      $("#sc-bar > div").style.width = `${Math.min(100, Math.round(done / total * 100))}%`;
      $("#sc-stage").innerHTML = `<span class="spin"></span> ${done}/${total} · ${esc(j.stage)}`;
      $("#sc-out tbody").innerHTML = scRows(j.partial, j.stage);
    });
    if (j.status === "done") { renderCheck(j.result); $("#sc-stage").textContent = "finished"; toast(`System check finished: ${j.result.summary.pass} pass, ${j.result.summary.warn} warn, ${j.result.summary.fail} fail`, j.result.summary.fail ? "err" : "buy"); }
    else { $("#sc-stage").innerHTML = `<span class="red">${esc(j.error || "failed")}</span>`; }
  } catch (e) { $("#sc-stage").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
  clearInterval(scTimer);
  $("#sc-quick").disabled = $("#sc-deep").disabled = false;
}
async function runCheck(deep) {
  $("#sc-out tbody").innerHTML = scRows([], "starting");
  $("#sc-stage").innerHTML = `<span class="spin"></span> starting`;
  try { const { job_id } = await api(`/api/system/check?deep=${deep}`, { method: "POST" }); followCheck(job_id, deep); }
  catch (e) { $("#sc-stage").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
}
$("#sc-quick").onclick = () => runCheck(false);
$("#sc-deep").onclick = () => runCheck(true);
async function resumeCheck() {     // a check started earlier (or in another tab) keeps showing its progress
  try { const j = await api("/api/jobs/latest/system_check"); if (j && j.status === "running") followCheck(j.id, false); } catch (e) {}
}

// ---------------------------------------------------------------- system: setup + risk
async function loadPhases() {
  try {
    const ph = await api("/api/phases");
    $("#ph-out tbody").innerHTML = ph.map(p => `<tr><td><b>${esc(p.phase)}</b></td><td>${p.done ? '<span class="pill pass">DONE</span>' : '<span class="pill review">OPEN</span>'}</td>
      <td class="t3 wrap">${esc(p.gate)}</td><td class="wrap">${esc(p.status)}</td></tr>`).join("");
  } catch (e) {}
}
async function runIntegrity(repair) {
  $("#ig-msg").innerHTML = '<span class="spin"></span> checking every call and trade…';
  try {
    const r = await api(`/api/integrity?repair=${repair}`, { method: "POST" });
    const c = r.calls, t = r.trades, w = r.wallet || {};
    $("#ig-sum").innerHTML = [["Calls checked", c.checked], ["Verified against real candles", c.verified], ["Simulated (hidden)", c.simulated],
      ["Wrong price (hidden)", c.invalid], ["No candles to verify", c.unverifiable], ["Paper trades checked", t.checked],
      ["Paper trades voided", `${t.voided}${t.voided ? " (P&L reversed " + inrS(t.reversed_pnl) + ")" : ""}`],
      ["Wallet balance", w.stored_balance != null ? `${inr(w.stored_balance)} ${w.balanced ? '<span class="green">matches ledger</span>' : '<span class="red">ledger says ' + inr(w.ledger_balance) + "</span>"}` : "—"],
      ["Real candles stored", (r.candles.real_candles || 0).toLocaleString("en-IN") + (r.candles.inconsistent_ohlc ? ` · <span class="red">${r.candles.inconsistent_ohlc} bad</span>` : "")]]
      .map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join("");
    $("#ig-out").hidden = !r.issues.length;
    $("#ig-out tbody").innerHTML = r.issues.map(i => `<tr><td>${esc(i.kind)}</td><td>${esc(i.symbol || "")}</td><td class="mono">${esc(fmtTs(i.ts))}</td><td class="t3 wrap">${esc(i.problem || JSON.stringify(i))}</td></tr>`).join("");
    $("#ig-msg").innerHTML = repair ? '<span class="green">repaired</span>' : (r.issues.length ? `<span class="amber">${r.issues.length} problems found - click Verify and repair</span>` : '<span class="green">all good</span>');
    if (repair) { loadCalls(); health(); }
  } catch (e) { $("#ig-msg").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
}
$("#ig-check").onclick = () => runIntegrity(false);
$("#ig-fix").onclick = () => runIntegrity(true);
function jobLog(boxSel, metaSel, j) {
  const box = $(boxSel); if (!box || !j || !j.log) return;
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  const mmss = t => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
  box.hidden = false;
  box.textContent = j.log.map(l => `${mmss(l.t)}  ${l.stage}${l.total ? `  (${l.done}/${l.total})` : ""}`).join("\n") || "waiting for the first update…";
  if (stick) box.scrollTop = box.scrollHeight;
  const el = j.status === "running" ? (Date.now() / 1000 - j.started) : (j.elapsed || 0);
  const last = j.log.at(-1);
  if (metaSel && $(metaSel)) { $(metaSel).hidden = false;
    $(metaSel).innerHTML = `${j.status === "running" ? '<span class="spin"></span> running' : esc(j.status)} · elapsed ${mmss(el)}${j.status === "running" && last ? ` · last update ${mmss(Math.max(0, el - last.t))} ago` : ""}`; }
}
function p4Log(j) {
  const box = $("#p4-log"); if (!box || !j.log) return;
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  const mmss = t => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
  box.hidden = false;
  box.textContent = j.log.map(l => `${mmss(l.t)}  ${l.stage}${l.total ? `  (${l.done}/${l.total})` : ""}`).join("\n");
  if (stick) box.scrollTop = box.scrollHeight;
  const el = j.status === "running" ? (Date.now() / 1000 - j.started) : (j.elapsed || 0);
  const last = j.log.at(-1);
  $("#p4-meta").hidden = false;
  $("#p4-meta").innerHTML = `${j.status === "running" ? "running" : esc(j.status)} · elapsed ${mmss(el)}${j.status === "running" && last ? ` · last update ${mmss(Math.max(0, el - last.t))} ago` : ""} · ${j.log.length} log lines`;
}
function p4Progress(j) {
  p4Log(j);
  const l = j.log.at(-1) || {}; const step = j.stage.startsWith("1/3") ? 0 : j.stage.startsWith("2/3") ? 1 : j.stage.startsWith("3/3") ? 2 : 0;
  $("#p4-bar").hidden = false;
  $("#p4-bar > div").style.width = `${Math.round((step + (l.total ? l.done / l.total : 0.5)) / 3 * 100)}%`; $("#p4-msg").innerHTML = `<span class="spin"></span> ${esc(j.stage)}`;
}
function renderP4(j) {
  if (!j || !j.status) return;
  if (j.status === "running") return p4Progress(j);
  p4Log(j);
  $("#p4-bar").hidden = true; $("#p4-go").disabled = false;
  if (j.status !== "done") { $("#p4-msg").innerHTML = `<span class="red">last run failed: ${esc(j.error || "")}</span>`; return; }
  const steps = j.result.steps || [];
  const hist = steps.find(s => s.step === "history") || {};
  const ev = (steps.find(s => s.step === "evaluation") || {}).strategies || {};
  const dl = Object.entries(hist.downloaded || {});
  $("#p4-out").innerHTML = `<p class="t3 xs">History: ${dl.length ? dl.map(([s, n]) => `${esc(s)} ${typeof n === "number" ? n + " candles" : esc(n)}`).join(", ") : "already stored"}${(hist.already_had || []).length ? " · already had " + hist.already_had.length + " stocks" : ""}</p>` +
    Object.entries(ev).map(([n, e]) => `<div class="panel"><b>${esc(n)}</b> — ${e.error ? `<span class="red">${esc(e.error)}</span>` : `baseline <span class="pill ${String(e.verdict).toLowerCase()}">${esc(e.verdict)}</span> ${esc(e.score)} · ${e.trades} trades · net ${inrS(e.net_pnl)} · win ${e.win_rate}% · holdout ${inrS(e.holdout_net)}
      <br>Checks: ${Object.entries(e.checks || {}).map(([k, v]) => `<span class="pill ${v ? "buy" : "sell"}">${esc(k.replaceAll("_", " "))}</span>`).join(" ")}
      <br>AI model: ${!e.model ? "—" : e.model.error ? `<span class="red">${esc(e.model.error)}</span>` : `${esc(e.model.status)} · AUC ${fmt(e.model.oos_auc, 3)} · hit rate top 40% ${fmt((e.model.hit_rate_top40pct || 0) * 100, 1)}% vs all ${fmt((e.model.hit_rate_all || 0) * 100, 1)}%`}`}</div>`).join("");
  $("#p4-msg").innerHTML = `<span class="green">last run finished ${esc(fmtTs(j.result.finished))}</span>`;
}
async function followP4(jid) {
  $("#p4-go").disabled = true;
  const j = await pollJob(jid, p4Progress);
  renderP4(j); loadPhases();
  if (j.status === "done") toast("Phase 4 setup finished", "buy");
}
$("#p4-go").onclick = async () => {
  $("#p4-out").innerHTML = "";
  try { const { job_id } = await api(`/api/phase4/setup?start=${$("#p4-start").value}`, { method: "POST" }); followP4(job_id); }
  catch (e) { $("#p4-msg").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};
api("/api/jobs/latest/phase4").then(j => { if (j.status === "running") followP4(j.id); else renderP4(j); }).catch(() => {});
async function loadRiskSettings() {
  const r = await api("/api/settings/risk");
  window._riskLabels = r.labels;
  $("#rs-form").innerHTML = Object.entries(r.values).map(([k, v]) => `<label>${esc(r.labels[k])}<input data-k="${k}" value="${esc(v)}" ${k === "no_entry_after" ? 'placeholder="HH:MM"' : 'type="number" step="any"'}></label>`).join("");
}
$("#rs-save").onclick = async () => {
  const vals = {}; $$("#rs-form [data-k]").forEach(i => vals[i.dataset.k] = i.dataset.k === "no_entry_after" ? i.value : parseFloat(i.value));
  try { await api("/api/settings/risk", { method: "PUT", json: vals }); $("#rs-msg").innerHTML = '<span class="green">saved — applies to the next call</span>'; toast("Risk settings saved", "buy"); loadRiskSettings(); loadRisk(); }
  catch (e) { $("#rs-msg").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};
const RISK_LABELS = { max_daily_loss: "Max loss per day", max_open_positions: "Max open positions", capital_per_trade: "Capital per trade", max_orders_per_sec: "Max orders per second",
  no_entry_after: "No new entries after", min_signal_score: "Min AI score to trade", max_capital_per_symbol: "Max capital per symbol" };
const MONEY_KEYS = /loss|capital|pnl/;
function riskVal(k, v) {
  if (v == null) return "—";
  if (typeof v === "number") return MONEY_KEYS.test(k) ? inr(v) : k === "min_signal_score" ? (v ? fmt(v * (v <= 1 ? 100 : 1), 0) + "%" : "off") : Number.isInteger(v) ? String(v) : fmt(v);
  return esc(String(v));
}
function renderRisk(r) {
  window._risk = r;
  const p = $("#rk-pill");
  p.className = "kill-pill " + (r.kill_switch ? "on" : "off");
  p.innerHTML = `<span class="dot"></span><span>Kill switch ${r.kill_switch ? "ON" : "OFF"}<br><small>${r.kill_switch ? "new orders stopped" + (r.kill_reason ? " — " + esc(r.kill_reason) : "") : "new paper orders allowed"}</small></span>`;
  const op = Object.keys(r.open_positions || {});
  const lab = k => (window._riskLabels && window._riskLabels[k] ? window._riskLabels[k].replace(/\s*\(Rs\)|\s*-\s*kill switch|\s*\(HH:MM\)|\s*\(0 = off\)/g, "") : RISK_LABELS[k] || k.replaceAll("_", " "));
  $("#rk-out").innerHTML = [
    ["Kill switch", r.kill_switch ? `<b class="red">ON</b>${r.kill_reason ? " · " + esc(r.kill_reason) : ""}` : `<span class="green">off</span>`],
    ["Trading day", esc(fmtTs(r.day, { date: true }))],
    ["Realised P&L today", `<span class="num ${cls(r.realized_pnl)}">${inrS(r.realized_pnl)}</span>`],
    ["Open positions", `${op.length}${op.length ? " · " + op.map(esc).join(", ") : ""}`],
  ].map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join("") + `<div class="kv-sec">Limits in force</div>` +
    Object.entries(r.limits || {}).map(([k, v]) => `<div>${esc(lab(k))}</div><div class="num">${riskVal(k, v)}</div>`).join("");
  if (curTab === "risk") renderSysStatus();
}
function renderSysStatus() {
  const h = window._health, f = window._feed, r = window._risk, w = window._wallet, c = window._lastCheck;
  const stat = (k, v, s = "", dot = "") => `<div class="stat"><div class="k">${k}</div><div class="v">${dot ? `<span class="dot ${dot}"></span>` : ""}${v}</div>${s ? `<div class="s" title="${esc(s)}">${esc(s)}</div>` : ""}</div>`;
  if (!h) { $("#sys-status").innerHTML = `<p class="t3">Loading status…</p>`; return; }
  $("#sys-status").innerHTML =
    stat("Orders", h.real_orders === "LOCKED" ? `<span class="amber" title="Paper trading only - real orders are locked">Paper · LOCKED</span>` : `<span class="red">REAL ORDERS ON</span>`, `trading mode: ${h.trading_mode}`) +
    stat("Live feed", esc(`${h.connector}${f ? " · " + f.status.replaceAll("_", " ") : ""}`), f && f.error ? f.error : h.live_error || (h.live_engine ? "engine running" : "engine stopped"),
      !f ? (h.connector === "simulated" ? "a" : "") : f.status === "connected" ? "g" : f.status === "waiting_for_login" ? "r" : "a") +
    stat("AI model", h.llm.ok ? "ready" : "offline", `${h.llm.model || ""}${h.llm.error ? " · " + h.llm.error : ""}`, h.llm.ok ? "g" : "r") +
    stat("Database", esc(h.db), h.ok ? "API healthy" : "API reports a problem", h.ok ? "g" : "r") +
    stat("Sandbox", esc(h.sandbox?.mode || "—"), h.sandbox ? `${h.sandbox.cores} cores${h.sandbox.note ? " · " + h.sandbox.note : ""}` : "") +
    (w ? stat("Day P&L (paper)", `<span class="num ${cls((w.today_realized || 0) + (w.open_mtm || 0))}">${inrS((w.today_realized || 0) + (w.open_mtm || 0))}</span>`, `equity ${inr(w.equity)}`) : "") +
    (r ? stat("Risk", `${Object.keys(r.open_positions || {}).length}/${r.limits?.max_open_positions ?? "—"} positions`, `daily loss limit ${inr(r.limits?.max_daily_loss)}`) : "") +
    (c ? stat("Last system check", `${c.summary.pass} pass · ${c.summary.warn} warn · ${c.summary.fail} fail`, fmtTs(c.at), c.summary.fail ? "r" : c.summary.warn ? "a" : "g") : "");
}
// activity log
function auditDetail(d) {
  let o; try { o = JSON.parse(d); } catch (e) { return { short: String(d ?? ""), full: String(d ?? "") }; }
  if (o && typeof o === "object" && !Array.isArray(o)) {
    const short = Object.entries(o).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : (k === "reason" ? reasonText(v) : v)}`).join(" · ");
    return { short, full: JSON.stringify(o, null, 2) };
  }
  return { short: String(d), full: JSON.stringify(o, null, 2) };
}
function renderAudit() {
  const a = window._audit || [];
  const act = $("#au-action").value, q = $("#au-q").value.trim().toLowerCase();
  const rows = a.filter(r => (!act || r.action === act) && (!q || `${r.actor} ${r.action} ${r.detail}`.toLowerCase().includes(q)));
  $("#audit tbody").innerHTML = rows.map((r, i) => { const d = auditDetail(r.detail); return `<tr><td class="mono">${esc(fmtTs(r.ts, { utc: true, sec: true }))}</td><td>${esc(r.actor)}</td><td>${esc(r.action)}</td>
    <td class="det" data-i="${i}" title="Click to expand" tabindex="0" role="button" aria-expanded="false">${esc(d.short.length > 120 ? d.short.slice(0, 120) + "…" : d.short)}</td></tr>`; }).join("")
    || `<tr><td colspan="4" class="empty">${a.length ? "Nothing matches this filter." : "No activity recorded yet."}</td></tr>`;
  $$("#audit .det").forEach(td => { const tog = () => { const r = rows[+td.dataset.i], d = auditDetail(r.detail); const open = !td.classList.contains("open");
    td.classList.toggle("open", open); td.setAttribute("aria-expanded", String(open)); td.textContent = open ? d.full : (d.short.length > 120 ? d.short.slice(0, 120) + "…" : d.short); };
    td.onclick = tog; td.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); tog(); } }; });
}
$("#au-action").onchange = renderAudit;
$("#au-q").oninput = renderAudit;
async function loadRisk() {
  loadPhases(); loadRiskSettings().then(() => window._risk && renderRisk(window._risk)).catch(() => {});
  renderSysStatus();
  if ($("#sc-quick").disabled) {} else api("/api/system/check/last").then(renderCheck).catch(() => {});
  try { renderRisk(await api("/api/risk")); } catch (e) { $("#rk-out").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
  const a = await api("/api/audit?limit=500");
  window._audit = a;
  const acts = [...new Set(a.map(r => r.action))].sort(), cur = $("#au-action").value;
  $("#au-action").innerHTML = `<option value="">All actions</option>` + acts.map(x => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("");
  renderAudit();
}
$("#rk-kill").onclick = async () => { if (confirm("Stop all new orders now?")) { await api("/api/risk/kill", { method: "POST" }); toast("Kill switch ON — new orders stopped", "err"); loadRisk(); } };
$("#rk-reset").onclick = async () => { await api("/api/risk/reset", { method: "POST" }); toast("Kill switch reset", ""); loadRisk(); };

function liveBanner(feed, rows) {
  const b = $("#live-banner"); const any = Object.values(rows || {})[0];
  if (feed.status === "waiting_for_login") {
    b.hidden = false;
    b.innerHTML = `Upstox login needed today (Upstox logins expire at 3:30 AM). Showing stored data${any && any.ts ? " up to " + esc(fmtTs(any.ts)) : ""}. <button type="button" class="sm" id="banner-login">Log in</button>`;
    $("#banner-login").onclick = () => showTab("data");
  } else if (feed.status !== "connected") { b.hidden = false; b.textContent = `Upstox feed: ${feed.status}${feed.error ? " - " + feed.error : ""}`; }
  else b.hidden = true;
  const c = feed.candles, n = $("#cnd-note");
  if (n) n.innerHTML = c && c.boundary ? `· official to ${esc(String(c.boundary).slice(11, 16))} <span title="minutes where the live-built candle differed from the exchange's and was replaced">(${c.corrections} fixed)</span>` : "";
}

$("#dh-go").onclick = async () => {
  $("#dh-bar").hidden = false; $("#dh-msg").innerHTML = '<span class="spin"></span> starting';
  try {
    const { job_id } = await api("/api/data/download-watchlist", { json: { start: $("#dh-start").value, interval: $("#dh-int").value } });
    const j = await pollJob(job_id, j => { const l = j.log.at(-1) || {}; $("#dh-bar > div").style.width = `${l.total ? Math.round(l.done / l.total * 100) : 0}%`; $("#dh-msg").innerHTML = `<span class="spin"></span> ${esc(j.stage)}`; });
    $("#dh-bar > div").style.width = "100%";
    $("#dh-msg").innerHTML = j.status === "done" ? `<span class="green">${j.result.total.toLocaleString("en-IN")} candles saved</span> ${Object.entries(j.result.rows).filter(([, v]) => typeof v === "string").map(([k, v]) => `<span class="red">${esc(k)}: ${esc(v)}</span>`).join(" ")}` : `<span class="red">${esc(j.error)}</span>`;
    if (j.status === "done") toast(`History download finished: ${j.result.total.toLocaleString("en-IN")} candles`, "buy");
    loadInventory();
  } catch (e) { $("#dh-msg").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
};

// ---------------------------------------------------------------- Upstox
async function loadUpstox() {
  try {
    const u = await api("/api/upstox/status");
    const f = u.feed;
    $("#ux-badge").innerHTML = !u.configured ? `<span class="red">not configured</span>` :
      u.logged_in ? `<span class="green">connected${u.account?.name ? " · " + esc(u.account.name) : ""}</span>` : `<span class="amber">login needed today</span>`;
    $("#ux-info").innerHTML = !u.configured
      ? `Add <code>UPSTOX_API_KEY</code> and <code>UPSTOX_API_SECRET</code> to <code>.env</code> (create an app at upstox.com/developer/apps with redirect URL <code>${esc(u.redirect_uri)}</code>), then restart.`
      : `Live feed: <b>${esc(u.live_connector)}</b>${f ? ` · ${esc(f.status)} · ${f.ticks} ticks · ${f.symbols} symbols${f.seconds_since_last_message != null ? ` · last tick ${f.seconds_since_last_message}s ago` : ""}${f.error ? ` · <span class="red">${esc(f.error)}</span>` : ""}` : ""}
         · orders: <b>${u.trading_mode === "live" && u.broker === "upstox" ? '<span class="red">LIVE via Upstox</span>' : "paper"}</b>
         ${u.live_connector !== "upstox" ? ` · set <code>LIVE_CONNECTOR=upstox</code> in .env to use the live feed` : ""}`;
    $("#ux-login").disabled = !u.configured;
  } catch (e) { $("#ux-badge").innerHTML = `<span class="red">${esc(e.message)}</span>`; }
}
$("#ux-login").onclick = async () => { try { const { url } = await api("/api/upstox/login-url", { method: "POST" }); location.href = url; } catch (e) { alert(e.message); } };
$("#ux-logout").onclick = async () => { await api("/api/upstox/logout", { method: "POST" }); loadUpstox(); };
$("#ux-token-go").onclick = async () => { try { await api("/api/upstox/token", { json: { access_token: $("#ux-token").value } }); $("#ux-token").value = ""; loadUpstox(); } catch (e) { alert(e.message); } };
if (new URLSearchParams(location.search).get("upstox") === "connected") {
  history.replaceState(null, "", "/"); setTimeout(() => showTab("data"), 300);
}

// ---------------------------------------------------------------- strategy info (indicators per strategy, descriptions, families)
let _siP = null;
function loadStratInfo() {
  if (!_siP) _siP = api("/api/strategy-indicators").then(r => { SI = r; return r; }).catch(e => { _siP = null; throw e; });
  return _siP;
}
const SPECIAL_DESC = {
  ai_pick: "Runs every strategy and lets the trained AI picker take only the best call per stock (needs a Strategy Lab run)",
  pair_trading: "Statistical arbitrage on correlated pairs discovered from history; both legs traded together",
};
function stratDesc(n) { return SPECIAL_DESC[n] || (SI && SI.descriptions && SI.descriptions[n]) || ""; }
const LIB_ORDER = ["reversal_rsi", "momentum", "breakout_sr", "scalping_vwap", "pivot_points", "orb", "gap_and_go", "flag_pattern", "ma_crossover", "pullback", "vwap_reversion", "supertrend_trend"];
const MINE = ["first_red_candle_zone", "hari_master_block"];
function logicGroups(names) {
  const origin = n => ((TS.strategies[n] || [])[0] || {}).origin;
  const g = { "Your strategies": [], "Intraday library": [], "AI": [], "Pairs": [] };
  MINE.forEach(n => names.includes(n) && g["Your strategies"].push(n));
  names.forEach(n => {
    if (MINE.includes(n)) return;
    if (n === "ai_pick") g.AI.push(n);
    else if (n === "pair_trading") g.Pairs.push(n);
    else if (LIB_ORDER.includes(n) || origin(n) === "builtin") g["Intraday library"].push(n);
    else g["Your strategies"].push(n);
  });
  g["Intraday library"].sort((a, b) => (LIB_ORDER.indexOf(a) + 1 || 99) - (LIB_ORDER.indexOf(b) + 1 || 99));
  return Object.entries(g).filter(([, l]) => l.length);
}
const famPill = f => f ? `<span class="fam">${esc(String(f).replaceAll("_", " "))}</span>` : "";

// ---------------------------------------------------------------- chart indicators
const IND_LABEL = { ema9: "EMA 9", ema20: "EMA 20", ema21: "EMA 21", ema50: "EMA 50", vwap: "VWAP ± bands", bollinger: "Bollinger", supertrend: "Supertrend", pivots: "Pivots (PP R1 S1 R2 S2)",
  cpr: "CPR", prev_day: "Prev day H/L/C", opening_range: "Opening range", rsi: "RSI 14", macd: "MACD", adx: "ADX / +DI / −DI", rvol: "Relative volume" };
const OSC = ["rsi", "macd", "adx", "rvol"];
// per line: color, width, style (0 solid, 2 dashed, 1 dotted)
const LSTYLE = {
  ema9: ["#f59e0b", 1, 0], ema20: ["#a78bfa", 1, 0], ema21: ["#c084fc", 1, 0], ema50: ["#f472b6", 1, 0],
  vwap: ["#22d3ee", 2, 0], vwap_up: ["#22d3ee", 1, 2], vwap_dn: ["#22d3ee", 1, 2],
  bb_up: ["#94a3b8", 1, 2], bb_mid: ["#64748b", 1, 1], bb_dn: ["#94a3b8", 1, 2], supertrend: ["#eab308", 1, 0],
  pp: ["#cbd5e1", 1, 1], r1: ["#f87171", 1, 2], r2: ["#fca5a5", 1, 2], s1: ["#4ade80", 1, 2], s2: ["#86efac", 1, 2],
  cpr_top: ["#a3a3a3", 1, 1], cpr_bot: ["#a3a3a3", 1, 1], prev_high: ["#fb923c", 1, 2], prev_low: ["#fb923c", 1, 2], prev_close: ["#fdba74", 1, 1],
  or_high: ["#2dd4bf", 1, 2], or_low: ["#2dd4bf", 1, 2],
  rsi: ["#c084fc", 1, 0], macd: ["#38bdf8", 1, 0], macd_signal: ["#f59e0b", 1, 0], macd_hist: ["#64748b", 1, 0],
  adx: ["#e2e8f0", 1, 0], pdi: ["#4ade80", 1, 0], mdi: ["#f87171", 1, 0], rvol: ["#38bdf8", 1, 0],
};
const IND_SWATCH = { ema9: "ema9", ema20: "ema20", ema21: "ema21", ema50: "ema50", vwap: "vwap", bollinger: "bb_up", supertrend: "supertrend", pivots: "pp", cpr: "cpr_top",
  prev_day: "prev_high", opening_range: "or_high", rsi: "rsi", macd: "macd", adx: "adx", rvol: "rvol" };
function indLoad() {
  let o = {}; try { o = JSON.parse(store.get("yx_ind") || "{}") || {}; } catch (e) {}
  return { auto: o.auto !== false, strat: o.strat || null, stratSet: !!o.stratSet, manual: Array.isArray(o.manual) ? o.manual : ["vwap"] };
}
function indSave() { store.set("yx_ind", JSON.stringify({ auto: IND.auto, strat: IND.strat, stratSet: IND.stratSet, manual: IND.manual })); }
function indStrat() { return IND.strat || TS.logic || "first_red_candle_zone"; }
function indActive() {
  if (!IND.auto) return IND.manual.slice();
  return SI ? (SI.by_strategy[indStrat()] || []).slice() : [];
}
function indPop(open) {
  const p = $("#ind-pop"); if (!p) return;
  if (open == null) open = p.hidden;
  p.hidden = !open; $("#ind-btn").setAttribute("aria-expanded", String(open));
  if (open) { indRenderPop(); (p.querySelector("input,select") || p).focus(); }
}
function indRenderPop() {
  $("#ind-n").textContent = indActive().length;
  if (!SI) return;
  const strats = Object.keys(SI.by_strategy);
  $("#ind-strat").innerHTML = strats.map(n => `<option value="${esc(n)}"${n === indStrat() ? " selected" : ""}>${esc(lname(n))}</option>`).join("");
  $("#ind-auto").checked = IND.auto; $("#ind-strat").disabled = !IND.auto;
  $("#ind-auto-note").textContent = IND.auto ? `Shows what ${lname(indStrat())} looks at${IND.stratSet ? "" : " (follows the paper-test logic)"}. Tick a box to choose your own.` : "Your own selection. Tick Auto to follow a strategy.";
  const on = new Set(indActive());
  const box = n => `<label class="chk${on.has(n) ? " on" : ""}"><input type="checkbox" data-ind="${esc(n)}"${on.has(n) ? " checked" : ""}><i class="sw" style="background:${LSTYLE[IND_SWATCH[n]]?.[0] || "#888"}"></i>${esc(IND_LABEL[n] || n)}</label>`;
  const av = SI.available || [];
  $("#ind-list").innerHTML = `<div class="ind-gh">On the price chart</div>${av.filter(n => !OSC.includes(n)).map(box).join("")}<div class="ind-gh">Lower pane</div>${av.filter(n => OSC.includes(n)).map(box).join("")}`;
  $$("#ind-list [data-ind]").forEach(i => i.onchange = () => {
    const cur = new Set(indActive()); i.checked ? cur.add(i.dataset.ind) : cur.delete(i.dataset.ind);
    IND.auto = false; IND.manual = [...cur]; indSave(); indRenderPop(); drawIndicators(selected, histMode);
  });
}
$("#ind-btn").onclick = e => { e.stopPropagation(); loadStratInfo().then(() => indRenderPop()).catch(() => {}); indPop(); };
$("#ind-x").onclick = () => { indPop(false); $("#ind-btn").focus(); };
$("#ind-auto").onchange = () => { IND.auto = $("#ind-auto").checked; if (!IND.auto) IND.manual = SI ? (SI.by_strategy[indStrat()] || []).slice() : IND.manual; indSave(); indRenderPop(); drawIndicators(selected, histMode); };
$("#ind-strat").onchange = () => { IND.strat = $("#ind-strat").value; IND.stratSet = true; indSave(); indRenderPop(); drawIndicators(selected, histMode); };
document.addEventListener("click", e => { const p = $("#ind-pop"); if (p && !p.hidden && !e.target.closest(".ind-wrap")) indPop(false); });

const lineOpts = (col, extra = {}) => { const [c, w, s] = LSTYLE[col] || ["#94a3b8", 1, 0];
  return { color: c, lineWidth: w, lineStyle: s, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...extra }; };
function ptsOf(arr, keep) {
  const seen = new Set(), out = [];
  (arr || []).forEach(([ts, v]) => { if (v == null || !isFinite(v)) return; const t = toUnix(String(ts).slice(0, 19)); if (seen.has(t) || (keep && !keep.has(t))) return; seen.add(t); out.push({ time: t, value: +v }); });
  return out.sort((a, b) => a.time - b.time);
}
// draws indicator `series` (API shape) onto a chart: overlays on `ch`, oscillators on `osc` (optional). Returns created series.
function addOverlays(ch, series, keep) {
  const made = [];
  Object.entries(series).forEach(([name, s]) => {
    if (!s.overlay) return;
    Object.entries(s.lines || {}).forEach(([col, arr]) => {
      const ls = ch.addLineSeries(lineOpts(col, { autoscaleInfoProvider: () => null, title: "" }));
      ls.setData(ptsOf(arr, keep)); made.push(ls);
    });
  });
  return made;
}
function indLegendHTML(names, oscNote = true) {
  return names.map(n => `<span class="il"><i class="sw" style="background:${LSTYLE[IND_SWATCH[n]]?.[0] || "#888"}"></i>${esc(IND_LABEL[n] || n)}${oscNote && OSC.includes(n) ? '<span class="t3"> ↓</span>' : ""}</span>`).join("");
}
let indMain = [], oscChart = null, oscSeries = [], _indSeq = 0, _oscSyncing = false;
function clearIndicators() {
  indMain.forEach(s => { try { chart.removeSeries(s); } catch (e) {} }); indMain = [];
  oscSeries.forEach(s => { try { oscChart.removeSeries(s); } catch (e) {} }); oscSeries = [];
  if ($("#osc")) $("#osc").hidden = true;
  if ($("#ind-legend")) $("#ind-legend").innerHTML = "";
}
function ensureOsc() {
  if (oscChart) return oscChart;
  oscChart = LightweightCharts.createChart($("#osc"), {
    layout: { background: { color: C.surface }, textColor: C.text, fontSize: 11 },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: C.border, visible: false }, rightPriceScale: { borderColor: C.border, minimumWidth: 72 },
    handleScroll: false, handleScale: false, crosshair: { horzLine: { visible: false } }, autoSize: true,
  });
  chart.applyOptions({ rightPriceScale: { minimumWidth: 72 } });
  chart.timeScale().subscribeVisibleLogicalRangeChange(r => { if (r && oscChart && !$("#osc").hidden) { try { oscChart.timeScale().setVisibleLogicalRange(r); } catch (e) {} } });
  return oscChart;
}
async function drawIndicators(sym, hist) {
  if (!sym || !chart) return;
  const seq = ++_indSeq;
  try { await loadStratInfo(); } catch (e) { return; }
  if (seq !== _indSeq) return;
  const names = indActive(); $("#ind-n").textContent = names.length;
  if (!names.length) { clearIndicators(); return; }
  let r;
  try {
    const q = hist ? `&interval=${hist.interval}&start=${hist.start}&end=${hist.end}&live_view=false` : "&interval=5m&live_view=true";
    r = await api(`/api/indicators?symbol=${encodeURIComponent(sym)}&names=${names.join(",")}${q}`);
  } catch (e) { if (seq === _indSeq) { clearIndicators(); $("#ind-legend").innerHTML = `<span class="red xs">indicators: ${esc(e.message)}</span>`; } return; }
  if (seq !== _indSeq || sym !== selected) return;
  clearIndicators();
  const series = r.series || {};
  indMain = addOverlays(chart, series, curBars.length ? new Set(curBars) : null);
  // oscillators: one stacked band per indicator, sharing the main chart's bar times so the logical ranges line up
  const oscs = OSC.filter(n => series[n]);
  if (oscs.length) {
    const el = $("#osc"); el.hidden = false; el.style.height = (oscs.length === 1 ? 140 : Math.min(300, 96 * oscs.length)) + "px";
    const oc = ensureOsc(), keep = new Set(curBars), k = oscs.length;
    const base = oc.addLineSeries({ color: "rgba(0,0,0,0)", lastValueVisible: false, priceLineVisible: false, priceScaleId: "base", crosshairMarkerVisible: false });
    base.setData(curBars.map(t => ({ time: t }))); oscSeries.push(base);
    oscs.forEach((n, i) => {
      const id = "o_" + n, m = { top: i / k + 0.06 / k, bottom: 1 - (i + 1) / k + 0.06 / k };
      Object.entries(series[n].lines || {}).forEach(([col, arr]) => {
        const data = ptsOf(arr, keep);
        let s;
        if (col === "macd_hist" || col === "rvol") {
          s = oc.addHistogramSeries({ priceScaleId: id, priceLineVisible: false, lastValueVisible: col === "rvol", base: 0 });
          s.setData(data.map(d => ({ ...d, color: col === "rvol" ? (d.value >= 1.5 ? "rgba(56,189,248,.8)" : "rgba(100,116,139,.55)") : d.value >= 0 ? "rgba(34,197,94,.55)" : "rgba(239,68,68,.55)" })));
        } else { s = oc.addLineSeries({ ...lineOpts(col), priceScaleId: id, lastValueVisible: col === "rsi" || col === "adx" }); s.setData(data); }
        oc.priceScale(id).applyOptions({ scaleMargins: m, borderVisible: false });
        if (col === "rsi") { [30, 70].forEach(v => s.createPriceLine({ price: v, color: "#475569", lineStyle: 2, lineWidth: 1, axisLabelVisible: false, title: String(v) })); }
        oscSeries.push(s);
      });
    });
    const lr = chart.timeScale().getVisibleLogicalRange(); if (lr) oc.timeScale().setVisibleLogicalRange(lr);
  }
  $("#ind-legend").innerHTML = indLegendHTML(names);
}

// ---------------------------------------------------------------- order flow
let ofChart = null, ofCum = null, ofBuy = null, ofSell = null, _ofBusy = false;
function dockSymbolChanged() {
  $("#dock-nnews").textContent = (NEWS_SUM[selected] || {}).n || 0;
  if (dockTab === "flow") loadOrderflow();
  if (dockTab === "news") loadNews();
}
async function loadOrderflow() {
  if (!selected || _ofBusy || $("#dock-flow").hidden) return;
  _ofBusy = true;
  const sym = selected;
  try { const r = await api(`/api/orderflow/${encodeURIComponent(sym)}`); if (sym === selected) renderOrderflow(r); }
  catch (e) { $("#of-stats").innerHTML = `<p class="red xs">${esc(e.message)}</p>`; }
  finally { _ofBusy = false; }
}
function ladderHTML(book) {
  if (!book || !(book.bids || []).length) return `<p class="t3 xs of-none">No live market depth for ${esc(selected || "")} right now. Depth comes from the Upstox full feed during market hours (09:15–15:30).</p>`;
  const all = [...book.bids, ...book.asks].map(x => +x[1] || 0), mx = Math.max(1, ...all);
  const side = (rows, k) => `<table class="lad ${k}"><thead><tr><th>${k === "bid" ? "Bid" : "Offer"}</th><th class="r">Qty</th></tr></thead><tbody>${rows.slice(0, 5).map(([p, q]) =>
    `<tr><td class="num">${fmt(p)}</td><td class="r"><span class="qb" style="width:${Math.round((+q || 0) / mx * 100)}%"></span><span class="qv">${Number(q || 0).toLocaleString("en-IN")}</span></td></tr>`).join("")}</tbody></table>`;
  const spread = book.asks[0] && book.bids[0] ? book.asks[0][0] - book.bids[0][0] : null;
  return `<div class="lad-w">${side(book.bids, "bid")}${side(book.asks, "ask")}</div>
    <div class="t3 xs lad-f">LTP <b class="t2">${fmt(book.ltp)}</b>${spread != null ? ` · spread ${fmt(spread)}` : ""} · total bid ${Number(book.tbq || 0).toLocaleString("en-IN")} / ask ${Number(book.tsq || 0).toLocaleString("en-IN")}</div>`;
}
function gaugeHTML(v) {
  if (v == null || isNaN(v)) return `<div class="gauge"><div class="g-track"></div></div><div class="t3 xs">—</div>`;
  const x = Math.max(-1, Math.min(1, v)), pct = (x + 1) / 2 * 100;
  const fill = x >= 0 ? `left:50%;width:${pct - 50}%` : `left:${pct}%;width:${50 - pct}%`;
  return `<div class="gauge" role="meter" aria-valuemin="-1" aria-valuemax="1" aria-valuenow="${x.toFixed(2)}" aria-label="Order book imbalance">
    <div class="g-track"><span class="g-fill ${x >= 0 ? "pos" : "neg"}" style="${fill}"></span><span class="g-mid"></span><span class="g-mark" style="left:${pct}%"></span></div>
    <div class="g-lab"><span>−1 sellers</span><b class="${cls(x)}">${x >= 0 ? "+" : MINUS}${Math.abs(x).toFixed(2)}</b><span>buyers +1</span></div></div>`;
}
function renderOrderflow(r) {
  const book = r.book, mins = r.minutes || [], s = r.summary || {};
  $("#of-empty").hidden = !!(book || mins.length);
  $("#of-ladder").innerHTML = ladderHTML(book);
  $("#of-book-ts").textContent = book && book.ts ? "· " + fmtTs(book.ts, { sec: true }) : "";
  // live summary covers the last 15 minutes of the feed; outside market hours fall back to the last 15 stored minutes
  const l15 = mins.slice(-15), lm = mins.at(-1) || {}, sum = k => l15.reduce((a, m) => a + (+m[k] || 0), 0);
  const stored = s.cum_delta == null && mins.length > 0;
  if (stored) { const bv = sum("buy_vol"), sv = sum("sell_vol");
    s.tbq_tsq = s.tbq_tsq ?? (lm.tsq ? lm.tbq / lm.tsq : null); s.buy_pct_15m = bv + sv ? bv / (bv + sv) * 100 : null; s.cum_delta = lm.cum_delta;
    s.big_buys_15m = sum("big_buys"); s.big_sells_15m = sum("big_sells"); }
  const imb = s.imbalance ?? (book ? book.imbalance : null) ?? lm.imbalance;
  const ratio = s.tbq_tsq ?? (book && book.tsq ? book.tbq / book.tsq : null);
  const stat = (k, v, c = "", sub = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  $("#of-stats").innerHTML = `<div class="of-h">Imbalance <span class="t3 xs">bid vs ask quantity in the book</span></div>${gaugeHTML(imb)}
    <div class="of-sg">
      ${stat("TBQ / TSQ", ratio == null ? "—" : fmt(ratio, 2), ratio > 1.1 ? "green" : ratio < 0.9 && ratio != null ? "red" : "", "total buy ÷ sell qty")}
      ${stat("Buy % (15 min)", s.buy_pct_15m == null ? "—" : fmt(s.buy_pct_15m, 1) + "%", s.buy_pct_15m > 55 ? "green" : s.buy_pct_15m < 45 && s.buy_pct_15m != null ? "red" : "", "traded at the ask")}
      ${stat("Cum. delta", s.cum_delta == null ? "—" : (s.cum_delta > 0 ? "+" : s.cum_delta < 0 ? MINUS : "") + Math.abs(Math.round(s.cum_delta)).toLocaleString("en-IN"), cls(s.cum_delta), "buy − sell volume today")}
      ${stat("Big prints (15 min)", `<span class="green">${s.big_buys_15m ?? 0}</span> / <span class="red">${s.big_sells_15m ?? 0}</span>`, "", "large buys / sells")}
    </div>
    <div class="t3 xs">${stored ? `<span class="amber">feed idle: figures are from the last 15 stored minutes</span> · ` : ""}${mins.length ? `${mins.length} minutes stored · last ${esc(fmtTs(mins.at(-1).ts))}${mins.at(-1).forming ? " (forming)" : ""}` : "no minutes stored yet"}</div>`;
  // cumulative delta line + buy/sell volume bars
  const el = $("#of-chart");
  if (!mins.length) { if (ofChart) { ofCum.setData([]); ofBuy.setData([]); ofSell.setData([]); } el.classList.add("of-blank"); return; }
  el.classList.remove("of-blank");
  if (!ofChart) {
    ofChart = LightweightCharts.createChart(el, { layout: { background: { color: C.surface }, textColor: C.text, fontSize: 11 }, grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: C.border }, rightPriceScale: { borderColor: C.border }, autoSize: true });
    ofBuy = ofChart.addHistogramSeries({ priceScaleId: "vol", color: "rgba(34,197,94,.45)", priceLineVisible: false, lastValueVisible: false });
    ofSell = ofChart.addHistogramSeries({ priceScaleId: "vol", color: "rgba(239,68,68,.45)", priceLineVisible: false, lastValueVisible: false });
    ofChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.62, bottom: 0 }, visible: false });
    ofCum = ofChart.addLineSeries({ color: C.info, lineWidth: 2, priceLineVisible: false });
  }
  const pts = []; const seen = new Set();
  mins.forEach(m => { const t = toUnix(String(m.ts).replace("T", " ").slice(0, 19)); if (!seen.has(t)) { seen.add(t); pts.push([t, m]); } });
  pts.sort((a, b) => a[0] - b[0]);
  ofCum.setData(pts.map(([t, m]) => ({ time: t, value: +m.cum_delta || 0 })));
  ofBuy.setData(pts.map(([t, m]) => ({ time: t, value: +m.buy_vol || 0 })));
  ofSell.setData(pts.map(([t, m]) => ({ time: t, value: -(+m.sell_vol || 0) })));
  if (!el.dataset.fit || el.dataset.fit !== selected) { ofChart.timeScale().fitContent(); el.dataset.fit = selected; }
}

// ---------------------------------------------------------------- news
const sentTone = s => s > 0.15 ? "pos" : s < -0.15 ? "neg" : "neu";
function newsDot(sym) {
  const x = NEWS_SUM[sym]; if (!x || !x.n) return "";
  const t = sentTone(x.sent), tip = `${x.n} news item${x.n > 1 ? "s" : ""}${x.announcements ? ` (${x.announcements} announcement${x.announcements > 1 ? "s" : ""})` : ""} · latest: ${x.latest ? x.latest.title : ""}`;
  return ` <span class="ndot ${t}" title="${esc(tip)}" aria-label="${esc(tip)}"></span>`;
}
async function loadNewsSummary() {
  try { NEWS_SUM = await api("/api/news/summary") || {}; } catch (e) { return; }
  if (Object.keys(statusRows).length) renderWatch(statusRows, window._simDay);
  if (selected) $("#dock-nnews").textContent = (NEWS_SUM[selected] || {}).n || 0;
}
async function loadNews() {
  if (!selected) { $("#nw-list").innerHTML = `<li class="empty">Pick a symbol in the watchlist.</li>`; return; }
  const sym = selected;
  try {
    const r = await api(`/api/news?symbol=${encodeURIComponent(sym)}&hours=72&limit=100`);
    if (sym !== selected) return;
    const items = r.items || [], last = r.last || {};
    $("#dock-nnews").textContent = items.length;
    $("#nw-meta").innerHTML = `${esc(sym)} · last 72 h · ${last.at ? "last updated " + esc(fmtTs(last.at, { utc: true })) + (last.saved != null ? ` (${last.saved} new)` : "") : "not refreshed yet in this session"}`;
    $("#nw-err").innerHTML = (last.errors || []).length ? `<span title="${esc(last.errors.join("\n"))}">${last.errors.length} source error${last.errors.length > 1 ? "s" : ""}: ${esc(String(last.errors[0]).slice(0, 80))}</span>` : "";
    $("#nw-list").innerHTML = items.map(n => {
      const t = sentTone(n.sentiment), lbl = t === "pos" ? "positive" : t === "neg" ? "negative" : "neutral";
      const safe = /^https?:\/\//i.test(n.url || "") ? n.url : null;
      return `<li><div class="nw-m"><span class="mono">${esc(fmtTs(n.ts, { utc: true }))}</span><span>${esc(n.source || "")}</span>${n.kind === "announcement" ? '<span class="pill info">announcement</span>' : ""}<span class="sent ${t}" title="sentiment ${n.sentiment != null ? Number(n.sentiment).toFixed(2) : "—"}">${lbl}</span></div>
        <div class="nw-t">${safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : esc(n.title)}</div></li>`;
    }).join("") || `<li class="empty">No headlines or exchange announcements for ${esc(sym)} in the last 72 hours. Press Refresh news to fetch them now.</li>`;
  } catch (e) { $("#nw-list").innerHTML = `<li class="empty red">${esc(e.message)}</li>`; }
}
$("#nw-refresh").onclick = async () => {
  const b = $("#nw-refresh"); b.disabled = true; b.innerHTML = '<span class="spin"></span> Refreshing…';
  try {
    const { job_id } = await api("/api/news/refresh", { method: "POST" });
    const j = await pollJob(job_id, j => { $("#nw-meta").innerHTML = `<span class="spin"></span> ${esc(j.stage || "fetching news…")}`; });
    if (j.status === "error") toast(`News refresh failed: ${esc(j.error || "")}`, "err"); else toast("News refreshed", "");
  } catch (e) { toast(esc(e.message), "err"); }
  b.disabled = false; b.textContent = "Refresh news";
  loadNews(); loadNewsSummary();
};

// ---------------------------------------------------------------- strategy lab
const LAB = { r: null, sort: { k: "net_pnl", dir: -1 }, reg: store.get("yx_labreg") || "regime", strat: null, trades: [], busy: false };
let _labInit = false;
async function loadLab() {
  loadStratInfo().catch(() => {});
  if (_labInit) return; _labInit = true;
  try {
    const j = await api("/api/jobs/latest/lab");
    if (j && j.status === "running") return followLab(j.id);
    if (j && j.status === "done" && j.result && j.result.leaderboard) { jobLog("#lab-log", "#lab-meta", j); return renderLab(j.result); }
  } catch (e) {}
  try { const r = await api("/api/lab/last"); if (r && r.leaderboard) renderLab(r); else $("#lab-out").innerHTML = `<div class="panel t3">No Strategy Lab run yet. Press <b>Run Strategy Lab</b> to backtest every strategy and train the AI picker.</div>`; }
  catch (e) { $("#lab-out").innerHTML = `<div class="panel t3">No Strategy Lab run yet. Press <b>Run Strategy Lab</b> to backtest every strategy and train the AI picker.</div>`; }
}
async function followLab(jid) {
  LAB.busy = true; $("#lab-go").disabled = true; $("#lab-go").innerHTML = '<span class="spin"></span> Running…';
  try {
    const j = await pollJob(jid, j => jobLog("#lab-log", "#lab-meta", j));
    jobLog("#lab-log", "#lab-meta", j);
    if (j.status === "error") { toast(`Strategy Lab failed: ${esc(j.error || "")}`, "err"); $("#lab-log").textContent += "\n" + (j.error || ""); }
    else if (j.result) { renderLab(j.result); toast(`Strategy Lab finished: ${(j.result.leaderboard || []).length} strategies ranked`, "buy"); scopeRefresh(); }
  } catch (e) { toast(esc(e.message), "err"); }
  LAB.busy = false; $("#lab-go").disabled = false; $("#lab-go").textContent = "Run Strategy Lab";
}
function scopeRefresh() { try { loadScope(); } catch (e) {} }
$("#lab-go").onclick = async () => {
  if (LAB.busy) return;
  try { const { job_id } = await api("/api/lab/run", { json: { start: $("#lab-start").value || "2024-01-01", include_pairs: $("#lab-pairs").checked } }); followLab(job_id); }
  catch (e) { toast(esc(e.message), "err"); }
};
const pct1 = f => f == null || isNaN(f) ? "—" : fmt(f * 100, 1) + "%";
function selectorHTML(s) {
  if (!s) return `<div class="panel"><div class="panel-h"><h2>AI strategy picker</h2><span class="pill">not trained</span></div><p class="t3">Not enough candidate trades to train the picker in this run (needs at least 200).</p></div>`;
  const champ = s.status === "champion";
  const ai = s.oos_ai_avg_per_trade, all = s.oos_all_avg_per_trade;
  const verdict = champ || s.beats_baseline
    ? `On held-back data the AI's picks made <b class="${cls(ai)}">${inrS(ai, 2)}</b> per trade against <b class="${cls(all)}">${inrS(all, 2)}</b> for taking every signal, so it is <b class="green">used for live calls</b>.`
    : `On held-back data the AI's picks made <b class="${cls(ai)}">${inrS(ai, 2)}</b> per trade against <b class="${cls(all)}">${inrS(all, 2)}</b> for taking every signal${ai > all ? " — better, but " + (ai <= 0 ? "still losing money" : "not by enough") : " — no better"}. So it stays in <b class="amber">shadow</b>: it watches and keeps learning, but does not decide trades yet.`
      + (s.ev_source === "regression" ? (((s.accuracy || {}).profit || {}).rank_ic_within_strategy ?? ((s.accuracy || {}).profit || {}).rank_ic ?? 0) < 0.05 ? ` Within each strategy its profit predictions barely line up with what trades actually made (rank IC ${fmt(((s.accuracy || {}).profit || {}).rank_ic_within_strategy ?? ((s.accuracy || {}).profit || {}).rank_ic, 3)}; needs 0.05), so the market features don't yet separate good trades from bad ones.` : ""
        : s.oos_auc != null && s.oos_auc < 0.56 ? ` Its AUC of ${fmt(s.oos_auc, 3)} is close to 0.5 (a coin flip), so the market features don't yet separate winners from losers well.` : "");
  const by = (s.oos_ai_pick_by_strategy || []).slice().sort((a, b) => b.count - a.count), mx = Math.max(1, ...by.map(x => x.count));
  const kp = (k, v, c = "", sub = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  return `<div class="panel ai-card ${champ ? "champ" : "shadow"}"><div class="panel-h"><h2>AI strategy picker</h2>
      <span class="pill ${champ ? "pass" : "review"}">${champ ? "champion · in use" : "shadow · not better than baseline yet"}</span>
      <span class="t3 xs">${s.trained_from ? `trained ${esc(fmtTs(tsDay(s.trained_from), { date: true }))} → ${esc(fmtTs(tsDay(s.trained_to), { date: true }))} · ` : ""}${Number(s.rows || 0).toLocaleString("en-IN")} candidate trades · ${Number(s.oos_rows || 0).toLocaleString("en-IN")} held back</span></div>
    <p class="ai-verdict">${verdict}</p>
    <div class="stat-grid">
      ${kp("Out-of-sample AUC", fmt(s.oos_auc, 3), "", "0.5 = coin flip")}
      ${kp("AI-picked trades", Number(s.oos_ai_pick_trades || 0).toLocaleString("en-IN"), "", `win ${pct1(s.oos_ai_pick_win_rate)} (all signals ${pct1(s.base_win_rate)})`)}
      ${kp("AI picks P&L", inrS(s.oos_ai_pick_pnl), cls(s.oos_ai_pick_pnl), `avg ${inrS(ai, 2)} / trade`)}
      ${kp("Every signal", inrS(s.oos_all_strategies_pnl), cls(s.oos_all_strategies_pnl), `avg ${inrS(all, 2)} / trade`)}
      ${kp("Best single strategy", esc(lname(s.oos_best_single_strategy)), "", `${inrS(s.oos_best_single_pnl)} on the same data`)}
    </div>
    ${accuracyHTML(s)}
    ${selectivityHTML(s)}
    ${by.length ? `<div class="ai-by"><div class="ts-h">What the AI picked (held-back data)</div>${by.map(x => `<div class="ai-row"><span class="ai-n" title="${esc(stratDesc(x._strategy))}">${esc(lname(x._strategy))}</span>
      <span class="ai-bar"><span style="width:${Math.round(x.count / mx * 100)}%"></span></span><span class="num xs">${x.count}</span><span class="num xs r ${cls(x.sum)}">${inrS(x.sum)}</span></div>`).join("")}</div>` : ""}</div>`;
}
function accuracyHTML(s) {
  const a = s.accuracy || {}, w = a.win_loss, p = a.profit;
  if (!w) return "";
  const R = x => x == null ? "—" : (x >= 0 ? "+" : "−") + fmt(Math.abs(x), 2) + "R";
  const kp = (k, v, c = "", sub = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  const better = (x, y) => x == null || y == null ? "" : x > y ? "green" : "red";
  return `<div class="ai-sel"><div class="ts-h">Prediction accuracy <span class="t3 xs">on ${Number(a.trades || 0).toLocaleString("en-IN")} held-back trades the models never trained on. R = profit or loss divided by the amount risked.</span></div>
    <div class="stat-grid">
      ${kp("Win / loss right", pct1(w.accuracy), better(w.accuracy, w.always_majority), `always guessing “${w.base_win_rate >= 0.5 ? "win" : "loss"}”: ${pct1(w.always_majority)}`)}
      ${kp(`Calls ≥ ${Math.round((w.min_p || 0) * 100)}% that won`, pct1(w.precision), better(w.precision, w.base_win_rate), `${Number(w.calls || 0).toLocaleString("en-IN")} calls · all trades won ${pct1(w.base_win_rate)}`)}
      ${kp("Win chance AUC", fmt(w.auc, 3), "", "0.5 = coin flip")}
      ${p ? kp("Profit model rank IC", fmt(p.rank_ic, 3), "", "all calls · 0 = no skill") : ""}
      ${p && p.rank_ic_within_strategy != null ? kp("Rank IC within a strategy", fmt(p.rank_ic_within_strategy, 3), p.rank_ic_within_strategy >= 0.05 ? "green" : "", "same strategy, different moments · 0.05+ is useful") : ""}
      ${p ? kp("Profit / loss direction right", pct1(p.sign_accuracy), "", `predicted profit on ${Number(p.predicted_positive || 0).toLocaleString("en-IN")}: they made ${R(p.actual_r_when_predicted_positive)} avg`) : ""}
    </div>
    ${p && p.quintiles ? `<div class="tscroll"><table class="tbl"><thead><tr><th>Trades ranked by predicted profit</th><th class="r">Trades</th><th class="r">Predicted</th><th class="r">Actual avg</th></tr></thead><tbody>
      ${p.quintiles.slice().reverse().map(q => `<tr><td>${["worst", "2nd worst", "middle", "2nd best", "best"][q.quintile - 1]} fifth</td><td class="r">${q.trades}</td><td class="r">${R(q.predicted_r)}</td><td class="r ${cls(q.actual_r)}"><b>${R(q.actual_r)}</b></td></tr>`).join("")}
    </tbody></table></div>` : ""}
    ${w.calibration && w.calibration.length ? `<div class="tscroll"><table class="tbl"><thead><tr><th>Predicted win chance</th><th class="r">Trades</th><th class="r">Predicted avg</th><th class="r">Actually won</th></tr></thead><tbody>
      ${w.calibration.map(c => `<tr><td>${Math.round(c.from * 100)}–${Math.round(c.to * 100)}%</td><td class="r">${c.trades}</td><td class="r">${pct1(c.predicted)}</td><td class="r"><b>${pct1(c.actual)}</b></td></tr>`).join("")}
    </tbody></table></div>` : ""}
    <p class="t3 xs">Expected profit per call comes from ${s.ev_source === "regression" ? "the <b>profit model</b> (it learns how much each call makes or loses after costs, per amount risked)" : "win chance × average win/loss of that strategy"}: it ranked the earlier held-back trades better. A useful model shows the best fifth clearly above the worst fifth in “Actual avg”.</p></div>`;
}
function selectivityHTML(s) {
  const t = s.selectivity || [];
  if (!t.length) return "";
  const v = s.validation || {}, inUse = LAB.aiThr && LAB.aiThr.in_use != null ? LAB.aiThr.in_use : s.min_p;
  const cell = x => x && x.trades ? `${x.trades}<span class="t3 xs"> · ${fmt(x.per_day, 2)}/day</span>` : `<span class="t3">0</span>`;
  return `<div class="ai-sel"><div class="ts-h">How picky should the AI be? <span class="t3 xs">a call is taken only if its win chance is at least this. Lower = more trades, usually worse quality.</span></div>
    <div class="tscroll"><table class="tbl"><thead><tr><th>Needs win chance ≥</th><th class="r">Trades (held-back)</th><th class="r">Win %</th><th class="r">Avg / trade</th><th class="r">Total</th><th class="r" title="t-statistic of the average: about 2 or more means it is unlikely to be luck">Confidence (t)</th><th class="r" title="Threshold chosen on the earlier half, judged on the later half it never saw">Later half avg</th><th></th></tr></thead><tbody>
    ${t.map(r => { const a = r.all, on = Math.abs(r.min_p - inUse) < 1e-9;
      return `<tr class="${on ? "sel" : ""}"><td><b>${Math.round(r.min_p * 100)}%</b>${v.passed && Math.abs(r.min_p - (s.min_p ?? -1)) < 1e-9 ? ' <span class="pill pass xs" title="chosen on earlier data, held up on later data">validated</span>' : ""}</td>
        <td class="r">${cell(a)}</td><td class="r">${pct1(a.win_rate)}</td><td class="r ${cls(a.avg)}">${a.trades ? inrS(a.avg, 0) : "—"}</td><td class="r ${cls(a.pnl)}">${a.trades ? inrS(a.pnl) : "—"}</td>
        <td class="r ${a.t_stat >= 2 ? "green" : a.t_stat != null && a.t_stat < 1 ? "t3" : ""}">${a.t_stat == null ? "—" : fmt(a.t_stat, 2)}</td>
        <td class="r ${cls(r.late.avg)}">${r.late.trades ? inrS(r.late.avg, 0) + `<span class="t3 xs"> (${r.late.trades})</span>` : "—"}</td>
        <td class="r">${on ? '<span class="pill pass xs">in use</span>' : `<button class="sm" data-thr="${r.min_p}">Use</button>`}</td></tr>`; }).join("")}
    </tbody></table></div>
    <p class="t3 xs">${v.passed ? `The ${Math.round(v.min_p * 100)}% threshold was chosen on the earlier half of the held-back data and still beat taking every signal on the later half (${v.late.trades} trades, avg ${inrS(v.late.avg, 0)} vs ${inrS(v.late_every_signal_avg, 0)}).`
      : `No threshold both made money on the earlier half and held up on the later half, so the picker is not trusted for live calls yet.`}
      ${LAB.aiThr && LAB.aiThr.override != null ? ` You overrode it to ${Math.round(LAB.aiThr.override * 100)}% — <button class="link" data-thr="">reset to validated</button>.` : ""}
      Changing it applies to the next rebuild and to live calls; it is not re-tested, so judge it by the held-back numbers above.</p></div>`;
}
async function loadAiThr() { try { LAB.aiThr = await api("/api/lab/ai-threshold"); } catch (e) { LAB.aiThr = null; } }
document.addEventListener("click", async ev => {
  const b = ev.target.closest("[data-thr]"); if (!b || !b.closest("#lab-out")) return;
  const v = b.dataset.thr === "" ? null : parseFloat(b.dataset.thr);
  try { LAB.aiThr = await api("/api/lab/ai-threshold", { method: "PUT", json: { min_p: v } });
    toast(v == null ? "AI picker: back to the validated threshold" : `AI picker now needs a ${Math.round(v * 100)}% win chance`, "buy"); renderLab(LAB.r); }
  catch (e) { toast(esc(e.message), "err"); }
});
const LB_COLS = [["strategy", "Strategy", 0], ["trades", "Trades", 1], ["win_rate", "Win %", 1], ["profit_factor", "PF", 1], ["net_pnl", "Net P&L", 1], ["expectancy", "Expectancy", 1], ["sharpe", "Sharpe", 1], ["max_drawdown", "Max DD", 1]];
function leaderHTML() {
  const { k, dir } = LAB.sort, rows = (LAB.r.leaderboard || []).slice();
  rows.sort((a, b) => { if (a.error || b.error) return a.error ? 1 : -1; const x = a[k], y = b[k];
    if (k === "strategy") return dir * String(x).localeCompare(String(y)); return dir * ((x ?? -1e18) - (y ?? -1e18)); });
  const fam = n => (LAB.r.leaderboard.find(r => r.strategy === n) || {}).family || (SI && SI.families && SI.families[n]);
  return `<div class="tscroll tall"><table class="tbl lb" id="lab-lb"><thead><tr>${LB_COLS.map(([c, l, num]) => `<th class="${num ? "r" : ""}" aria-sort="${c === k ? (dir > 0 ? "ascending" : "descending") : "none"}"><button type="button" class="th-b" data-k="${c}">${l}${c === k ? (dir > 0 ? " ▲" : " ▼") : ""}</button></th>`).join("")}</tr></thead><tbody>
    ${rows.map(r => r.error ? `<tr><td><b>${esc(lname(r.strategy))}</b></td><td colspan="7" class="red wrap">error: ${esc(r.error)}</td></tr>`
      : `<tr data-s="${esc(r.strategy)}" class="${r.strategy === LAB.strat ? "sel" : ""}" tabindex="0" title="${esc(stratDesc(r.strategy))}"><td><b>${esc(lname(r.strategy))}</b> ${famPill(r.family || fam(r.strategy))}</td>
        <td class="r">${r.trades ?? 0}</td><td class="r">${r.win_rate == null ? "—" : fmt(r.win_rate, 1) + "%"}</td><td class="r ${r.profit_factor >= 1 ? "green" : r.profit_factor != null ? "red" : ""}">${r.profit_factor == null ? "—" : fmt(r.profit_factor, 2)}</td>
        <td class="r ${cls(r.net_pnl)}"><b>${r.net_pnl == null ? "—" : inrS(r.net_pnl)}</b></td><td class="r ${cls(r.expectancy)}">${r.expectancy == null ? "—" : inrS(r.expectancy, 2)}</td>
        <td class="r ${cls(r.sharpe)}">${r.sharpe == null ? "—" : fmt(r.sharpe, 2)}</td><td class="r ${r.max_drawdown < 0 ? "red" : ""}">${r.max_drawdown == null ? "—" : inr(r.max_drawdown)}</td></tr>`).join("")}</tbody></table></div>`;
}
function wireLeader() {
  $$("#lab-lb .th-b").forEach(b => b.onclick = () => { const c = b.dataset.k; LAB.sort = { k: c, dir: LAB.sort.k === c ? -LAB.sort.dir : c === "strategy" ? 1 : -1 }; $("#lab-lb-w").innerHTML = leaderHTML(); wireLeader(); markScrolls(); });
  $$("#lab-lb tr[data-s]").forEach(tr => { tr.onclick = () => labTrades(tr.dataset.s); tr.onkeydown = e => { if (e.key === "Enter") labTrades(tr.dataset.s); }; });
}
async function labTrades(strat) {
  LAB.strat = strat; $$("#lab-lb tr[data-s]").forEach(tr => tr.classList.toggle("sel", tr.dataset.s === strat));
  const box = $("#lab-trades"); box.hidden = false;
  box.innerHTML = `<div class="panel-h"><h2>Recent lab trades · ${esc(lname(strat))}</h2><button type="button" class="x" id="lab-tr-x" aria-label="Close trades">×</button></div><p class="t3"><span class="spin"></span> loading…</p>`;
  $("#lab-tr-x").onclick = () => { box.hidden = true; LAB.strat = null; $$("#lab-lb tr.sel").forEach(t => t.classList.remove("sel")); };
  try {
    const rows = await api(`/api/lab/trades?strategy=${encodeURIComponent(strat)}&limit=200`);
    if (LAB.strat !== strat) return;
    LAB.trades = rows;
    TC_SRC.lab = rows.map(t => ({ symbol: t.symbol, day: tsDay(t.entry_ts), strategy: t.strategy, side: t.side, entry_ts: t.entry_ts, entry: t.entry, exit_ts: t.exit_ts, exit: t.exit, exit_reason: t.exit_reason, pnl: t.net_pnl, r_mult: t.r_mult }));
    box.innerHTML = `<div class="panel-h"><h2>Recent lab trades · ${esc(lname(strat))}</h2><span class="t3 xs">${rows.length} newest · click a trade to see it on the chart</span><button type="button" class="x" id="lab-tr-x" aria-label="Close trades">×</button></div>
      <p class="panel-sub">${esc(stratDesc(strat))}</p>
      <div class="tscroll tall"><table class="tbl" id="lab-tr"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th class="r">Price</th><th>Exit</th><th class="r">Price</th><th>Why</th><th class="r">R</th><th class="r">Net P&amp;L</th></tr></thead><tbody>
      ${rows.map((t, i) => `<tr data-i="${i}" class="clk"><td>${tcBtn("lab:" + i, t.symbol)}</td><td><span class="pill ${String(t.side).toLowerCase()}">${esc(t.side)}</span></td><td class="mono">${esc(fmtTs(t.entry_ts))}</td><td class="r">${fmt(t.entry)}</td>
        <td class="mono">${esc(tsDay(t.exit_ts) === tsDay(t.entry_ts) ? String(t.exit_ts).slice(11, 16) : fmtTs(t.exit_ts))}</td><td class="r">${fmt(t.exit)}</td><td>${reasonHTML(t.exit_reason)}</td>
        <td class="r ${cls(t.r_mult)}">${t.r_mult == null ? "—" : fmt(t.r_mult, 2)}</td><td class="r ${cls(t.net_pnl)}">${inrS(t.net_pnl)}</td></tr>`).join("") || `<tr><td colspan="9" class="empty">No trades for this strategy in the last run.</td></tr>`}</tbody></table></div>`;
    $("#lab-tr-x").onclick = () => { box.hidden = true; LAB.strat = null; $$("#lab-lb tr.sel").forEach(t => t.classList.remove("sel")); };
    $$("#lab-tr tr[data-i]").forEach(tr => tr.onclick = e => { if (!e.target.closest("[data-tc]")) openTradeFrom("lab:" + tr.dataset.i); });
    markScrolls();
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (e) { box.innerHTML += `<p class="red">${esc(e.message)}</p>`; }
}
function regimeHTML() {
  const reg = (LAB.r.regimes || {})[LAB.reg];
  if (!LAB.r.regimes) return `<p class="t3 xs">Regime tables come with a fresh run: press Run Strategy Lab (the saved summary keeps only the leaderboard).</p>`;
  if (!reg || !reg.length) return `<p class="t3 xs">No data for this view.</p>`;
  const buckets = [...new Set(reg.map(x => x.bucket))], strats = [...new Set(reg.map(x => x.strategy))];
  const cell = {}; reg.forEach(x => cell[x.strategy + "|" + x.bucket] = x);
  const mx = Math.max(1, ...reg.map(x => Math.abs(x.net_pnl || 0)));
  strats.sort((a, b) => buckets.reduce((s, k) => s + ((cell[b + "|" + k] || {}).net_pnl || 0), 0) - buckets.reduce((s, k) => s + ((cell[a + "|" + k] || {}).net_pnl || 0), 0));
  return `<div class="tscroll"><table class="tbl mx"><thead><tr><th>Strategy</th>${buckets.map(b => `<th class="r">${esc(b)}</th>`).join("")}</tr></thead><tbody>${strats.map(s => `<tr><td>${esc(lname(s))}</td>${buckets.map(b => {
    const x = cell[s + "|" + b]; if (!x) return `<td class="r t3">·</td>`;
    const a = Math.min(0.55, 0.08 + Math.abs(x.net_pnl) / mx * 0.5), bg = x.net_pnl >= 0 ? `rgba(34,197,94,${a.toFixed(2)})` : `rgba(239,68,68,${a.toFixed(2)})`;
    return `<td class="r" style="background:${bg}" title="${x.trades} trades · avg ${inrS(x.avg, 2)}">${inrS(x.net_pnl)}<div class="xs t2">${x.trades} tr</div></td>`; }).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function renderLab(r) {
  LAB.r = r;
  if (LAB.aiThr === undefined) { LAB.aiThr = null; loadAiThr().then(() => renderLab(LAB.r)); }
  $("#lab-when").textContent = r.finished ? `last run ${fmtTs(r.finished, { utc: true })} · from ${fmtTs(r.start, { date: true })} · ${(r.instruments || []).length} instruments · ${Number(r.candles || 0).toLocaleString("en-IN")} candles` : "";
  const best = Object.entries(r.best_by_symbol || {}).sort((a, b) => (b[1].net_pnl || 0) - (a[1].net_pnl || 0));
  const pairs = r.pairs || [];
  $("#lab-out").innerHTML = `${selectorHTML(r.selector)}
    <div class="panel"><div class="panel-h"><h2>Leaderboard</h2><span class="t3 xs">all history · after costs · click a header to sort, a row for its trades</span></div><div id="lab-lb-w">${leaderHTML()}</div></div>
    <div class="panel" id="lab-trades" hidden></div>
    <div class="grid2">
      <div class="panel"><div class="panel-h"><h2>Best strategy per stock</h2></div><div class="tscroll"><table class="tbl"><thead><tr><th>Stock</th><th>Best strategy</th><th class="r">Trades</th><th class="r">Net P&amp;L</th></tr></thead><tbody>
        ${best.map(([sym, x]) => `<tr><td><b>${esc(sym)}</b></td><td title="${esc(stratDesc(x.strategy))}">${esc(lname(x.strategy))}</td><td class="r">${x.trades ?? ""}</td><td class="r ${cls(x.net_pnl)}">${inrS(x.net_pnl)}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No stock had a profitable strategy.</td></tr>`}</tbody></table></div></div>
      <div class="panel"><div class="panel-h"><h2>Pairs found</h2><span class="t3 xs">for pair trading</span></div>
        ${pairs.length ? `<div class="tscroll"><table class="tbl"><thead><tr><th>Pair</th><th class="r">Correlation</th><th class="r">Hedge ratio β</th><th class="r">Half-life</th></tr></thead><tbody>${pairs.map(p => `<tr><td><b>${esc(p.a)}</b> / <b>${esc(p.b)}</b></td><td class="r">${fmt(p.corr, 3)}</td><td class="r">${fmt(p.beta, 3)}</td><td class="r">${p.half_life == null ? "—" : fmt(p.half_life, 1)}</td></tr>`).join("")}</tbody></table></div>`
          : `<p class="t3 xs">No pairs were correlated and mean-reverting enough in this run${$("#lab-pairs").checked ? "" : " (pair trading was not included)"}. Pair trading then falls back to the default index/sector pairs.</p>`}</div>
    </div>
    <div class="panel"><div class="panel-h"><h2>Who wins when</h2><span class="t3 xs">net P&amp;L of every candidate trade, by situation</span>
      <select id="lab-reg" class="sm" aria-label="Split by"><option value="regime">Market regime (ADX)</option><option value="tod">Time of day</option><option value="gap">Gap day vs normal day</option></select></div>
      <div id="lab-reg-w">${regimeHTML()}</div></div>`;
  $("#lab-reg").value = LAB.reg;
  $("#lab-reg").onchange = () => { LAB.reg = $("#lab-reg").value; store.set("yx_labreg", LAB.reg); $("#lab-reg-w").innerHTML = regimeHTML(); markScrolls(); };
  wireLeader();
  if (LAB.strat) labTrades(LAB.strat);
  setTimeout(markScrolls, 50);
}

// ---------------------------------------------------------------- trade chart modal
// TC_SRC[src] = rows of trades in a table; tcBtn("src:i") opens the chart with every trade of the same symbol + day from that table
function tcBtn(key, label) {
  const ico = `<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 12.5l3.5-4 3 2.5 5.5-7 1 .8-6.3 8.1-3-2.5-2.7 3.1z" fill="currentColor"/></svg>`;
  return label ? `<button type="button" class="tc-l" data-tc="${esc(key)}" title="Show this trade on the chart"><b>${esc(label)}</b>${ico}</button>`
    : `<button type="button" class="tc-i" data-tc="${esc(key)}" title="Show this trade on the chart" aria-label="Show this trade on the chart">${ico}</button>`;
}
document.addEventListener("click", e => { const b = e.target.closest("[data-tc]"); if (!b) return; e.preventDefault(); e.stopPropagation(); openTradeFrom(b.dataset.tc); });
function openTradeFrom(key) {
  const [src, i] = String(key).split(":"), list = TC_SRC[src] || [], t = list[+i]; if (!t) return;
  const same = list.filter(x => x.symbol === t.symbol && x.day === t.day && x.entry_ts && (x.chartable !== false));
  openTradeChart({ symbol: t.symbol, day: t.day, strategy: t.strategy, trades: same.length ? same : [t], focus: t });
}
let tcChart = null, tcSeries = null, tcLines = [], tcOver = [], _tcSeq = 0, _tcFocus = null;
const shortReason = r => { const s = String(r || ""); return s === "open" ? "open" : (REASONS[s] || reasonText(s)).replace(/ hit$/, "").replace("15:15 square-off", "15:15").slice(0, 18); };
async function openTradeChart(o) {
  const ov = $("#tc"); _tcFocus = document.activeElement;
  const seq = ++_tcSeq, trades = (o.trades || []).filter(t => t.entry_ts);
  ov.hidden = false; document.body.classList.add("modal-open");
  $("#tc-title").textContent = `${o.symbol} · ${fmtTs(o.day, { date: true })}`;
  $("#tc-sub").textContent = `${lname(o.strategy)} · 5-minute candles · ${trades.length} trade${trades.length === 1 ? "" : "s"}`;
  $("#tc-legend").innerHTML = `<span class="t3 xs"><span class="spin"></span> loading…</span>`;
  $("#tc-tbl tbody").innerHTML = trades.map(t => `<tr class="${t === o.focus ? "sel" : ""}"><td><span class="pill ${String(t.side).toLowerCase()}">${esc(t.side)}</span></td><td class="mono">${esc(String(t.entry_ts).slice(11, 16))}</td><td class="r">${fmt(t.entry)}</td>
    <td class="mono">${t.exit_ts ? esc(tsDay(t.exit_ts) === o.day ? String(t.exit_ts).slice(11, 16) : fmtTs(t.exit_ts)) : "—"}</td><td class="r">${t.exit != null ? fmt(t.exit) : "—"}</td><td>${reasonHTML(t.exit_reason)}</td>
    <td class="r">${t.stop_loss != null ? fmt(t.stop_loss) : "—"}</td><td class="r">${t.target != null ? fmt(t.target) : "—"}</td><td class="r ${cls(t.pnl)}">${t.pnl != null ? inrS(t.pnl) : "—"}</td></tr>`).join("");
  ov.querySelector("[data-close]").focus();
  if (!tcChart) {
    tcChart = LightweightCharts.createChart($("#tc-chart"), { layout: { background: { color: C.surface }, textColor: C.text }, grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: C.border }, rightPriceScale: { borderColor: C.border }, autoSize: true });
    tcSeries = tcChart.addCandlestickSeries({ upColor: C.buy, downColor: C.sell, borderVisible: false, wickUpColor: C.buy, wickDownColor: C.sell });
  }
  tcLines.forEach(l => tcSeries.removePriceLine(l)); tcLines = []; tcOver.forEach(s => tcChart.removeSeries(s)); tcOver = [];
  tcSeries.setData([]); tcSeries.setMarkers([]);
  try {
    const [r, si] = await Promise.all([api(`/api/chart?symbol=${encodeURIComponent(o.symbol)}&start=${o.day}&end=${o.day}&interval=5m`), loadStratInfo().catch(() => null)]);
    if (seq !== _tcSeq) return;
    const bars = (r.candles || []).map(c => ({ time: toUnix(c.ts), open: c.open, high: c.high, low: c.low, close: c.close }));
    tcSeries.setData(bars);
    if (!bars.length) { $("#tc-legend").innerHTML = `<span class="amber xs">No 5-minute candles stored for ${esc(o.symbol)} on this day.</span>`; return; }
    const times = bars.map(b => b.time);
    const snap = t => { let lo = 0, hi = times.length - 1, best = times[0]; while (lo <= hi) { const m = (lo + hi) >> 1; if (times[m] <= t) { best = times[m]; lo = m + 1; } else hi = m - 1; } return best; };
    const tu = ts => toUnix(String(ts).replace("T", " ").slice(0, 19));
    const mk = [];
    trades.forEach(t => {
      const buy = String(t.side).toUpperCase() === "BUY";
      mk.push({ time: snap(tu(t.entry_ts)), position: buy ? "belowBar" : "aboveBar", color: buy ? C.buy : C.sell, shape: buy ? "arrowUp" : "arrowDown", text: `${buy ? "BUY" : "SELL"} ${fmt(t.entry)}` });
      if (t.exit_ts && tsDay(t.exit_ts) === o.day) mk.push({ time: snap(tu(t.exit_ts)), position: buy ? "aboveBar" : "belowBar", color: t.pnl > 0 ? "#4ade80" : t.pnl < 0 ? "#f87171" : "#cbd5e1", shape: "circle", text: shortReason(t.exit_reason) });
    });
    tcSeries.setMarkers(mk.sort((a, b) => a.time - b.time));
    const f = o.focus && o.focus.entry_ts ? o.focus : trades[0];
    if (f) {
      if (f.stop_loss != null) tcLines.push(tcSeries.createPriceLine({ price: +f.stop_loss, color: C.sell, lineStyle: 2, lineWidth: 1, title: "stop" }));
      if (f.target != null) tcLines.push(tcSeries.createPriceLine({ price: +f.target, color: C.buy, lineStyle: 2, lineWidth: 1, title: "target" }));
      if (f.entry != null) tcLines.push(tcSeries.createPriceLine({ price: +f.entry, color: "#64748b", lineStyle: 1, lineWidth: 1, title: "entry", axisLabelVisible: false }));
    }
    tcChart.timeScale().fitContent();
    const names = si ? (si.by_strategy[o.strategy] || si.by_strategy.first_red_candle_zone || []).filter(n => !OSC.includes(n)) : [];
    $("#tc-legend").innerHTML = (names.length ? indLegendHTML(names, false) : "") + `<span class="il t3"><b class="green">▲</b> buy · <b class="red">▼</b> sell · ● exit</span>`;
    if (names.length) {
      try {
        const ir = await api(`/api/indicators?symbol=${encodeURIComponent(o.symbol)}&names=${names.join(",")}&interval=5m&start=${o.day}&end=${o.day}&live_view=false`);
        if (seq === _tcSeq) tcOver = addOverlays(tcChart, ir.series || {}, new Set(times));
      } catch (e) {}
    }
  } catch (e) { if (seq === _tcSeq) $("#tc-legend").innerHTML = `<span class="amber xs">No 5-minute candles available for ${esc(o.symbol)} on ${esc(fmtTs(o.day, { date: true }))}: ${esc(String(e.message).replace(/^"|"$/g, ""))}. The trades are listed below.</span>`; }
}
function closeTradeChart() {
  const ov = $("#tc"); if (!ov || ov.hidden) return;
  ov.hidden = true; document.body.classList.remove("modal-open"); _tcSeq++;
  if (_tcFocus && document.contains(_tcFocus)) try { _tcFocus.focus(); } catch (e) {}
}
$("#tc").onclick = e => { if (e.target.id === "tc" || e.target.closest("[data-close]")) closeTradeChart(); };
$("#tc").addEventListener("keydown", e => {   // keep Tab inside the dialog
  if (e.key !== "Tab") return;
  const f = $$("#tc button, #tc a[href]").filter(x => x.offsetParent); if (!f.length) return;
  if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f.at(-1).focus(); }
  else if (!e.shiftKey && document.activeElement === f.at(-1)) { e.preventDefault(); f[0].focus(); }
});

// ---------------------------------------------------------------- rebuild extras (AI picker, pairs)
function renderTodayExtra(r) {
  const el = $("#td-extra"), parts = [];
  if (r.ai) { const a = r.ai, champ = a.status === "champion";
    parts.push(`<div class="tdx"><span class="tdx-k">AI picker</span><span class="pill ${champ ? "pass" : "review"}">${esc(a.status || "—")}</span><span>model <b class="mono">${esc(a.model || "—")}</b></span>
      <span><b>${a.candidates ?? 0}</b> candidate calls → <b>${a.picked ?? 0}</b> picked</span>${a.min_p != null ? `<span class="t3 xs">needs ≥ ${Math.round(a.min_p * 100)}% win chance (Backtest → Strategy Lab to change)</span>` : ""}${champ ? "" : '<span class="t3 xs">shadow model: picks shown for testing only</span>'}</div>`); }
  if (r.pairs && r.pairs.length) parts.push(`<div class="tdx"><span class="tdx-k">Pairs</span>${r.pairs.map(p => `<span class="pair">${esc(p.a)} / ${esc(p.b)}${p.corr != null ? ` <span class="t3 xs">ρ ${fmt(p.corr, 2)}</span>` : ""}</span>`).join("")}</div>`);
  el.innerHTML = parts.join(""); el.hidden = !parts.length;
}

// ---------------------------------------------------------------- quick filter for long tables
const NO_QF = ["tc-tbl", "watch", "calls", "audit", "lv-pos", "w-open", "td-out", "ig-out", "sc-out", "ph-out", "fb-tags"];
function attachQuickFilters() {
  $$("table.tbl").forEach(tbl => {
    if (NO_QF.includes(tbl.id) || tbl.dataset.qf) return;
    const panel = tbl.closest(".panel"); if (!panel) return;
    tbl.dataset.qf = "1";
    let head = panel.querySelector(".panel-h");
    if (!head) { head = document.createElement("div"); head.className = "panel-h"; panel.prepend(head); }
    const inp = document.createElement("input"); inp.className = "qf"; inp.placeholder = "Filter rows…"; inp.setAttribute("aria-label", "Filter rows");
    head.appendChild(inp);
    const apply = () => {
      const words = inp.value.toLowerCase().split(/\s+/).filter(Boolean);
      $$("tbody tr", tbl).forEach(tr => { tr.hidden = words.length > 0 && !words.every(w => tr.textContent.toLowerCase().includes(w)); });
    };
    inp.oninput = apply;
    const tb = tbl.querySelector("tbody"); if (tb) new MutationObserver(apply).observe(tb, { childList: true });
  });
}

// ---------------------------------------------------------------- boot
attachQuickFilters(); initChart(); resumeCheck(); loadWatchNote(); health(); setInterval(health, 30000); connectWS(); loadCalls(); setInterval(loadCalls, 15000); loadStrategySelects();
loadTodaySignals(); setInterval(loadTodaySignals, 15000);
pollWallet(); setInterval(pollWallet, 10000);
setDock(dockTab); loadStratInfo().then(() => { if (Object.keys(TS.strategies).length) tsRender(); indRenderPop(); }).catch(() => {}); loadNewsSummary(); setInterval(loadNewsSummary, 5 * 60 * 1000); setInterval(() => { if (curTab === "live" && dockTab === "flow" && !document.hidden) loadOrderflow(); }, 3000);
renderEvents();
api("/api/live/status").then(s => s.rows && renderWatch(s.rows, s.sim_day)).catch(() => {});

$("#w-seg") && ($("#w-seg").onchange = () => renderWatch(statusRows));
$("#wl-fut").onclick = async () => {
  if (!confirm("Add the near-month futures of NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and of every stock in your watchlist? (1 lot per trade, paper only)")) return;
  try { const r = await api("/api/watchlist/futures", { method: "POST" });
    toast(`Added ${r.added.length} futures${r.missing.length ? " · no futures for " + r.missing.join(", ") : ""}`, "buy"); }
  catch (e) { toast(esc(e.message), "sell"); }
};
