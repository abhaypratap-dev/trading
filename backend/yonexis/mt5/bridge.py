"""Bridge between YONEXIS and the YonexisBridge Expert Advisor running inside MetaTrader 5.

Two transports, chosen in System -> MT5 connection:
  * file (Mac / local): the EA and YONEXIS share MT5's "Common Files" folder. YONEXIS drops one command file per
    order into YONEXIS/in/, the EA (polling every 25 ms) executes it and writes YONEXIS/out/<id>.res; it rewrites
    YONEXIS/state.json (account, positions, today's deals, quotes) twice a second. No network needed.
  * web (cloud): the EA calls POST https://<your dashboard>/api/mt5/bridge with its state and results; the reply
    carries the waiting commands (the request is held open up to 1 s, so a command leaves within milliseconds).
    MetaTrader only allows WebRequest to URLs added in Tools > Options > Expert Advisors, on ports 80/443.

Commands are one text line (easy for MQL5 to parse):  id|op|symbol|volume|sl|tp|ticket|magic|deviation|comment
Results and state come back as JSON.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

OPS = ("ping", "buy", "sell", "close", "close_partial", "modify", "close_all", "watch", "symbols")


def _num(v) -> str:
    return "" if v is None else (f"{v:.10g}" if isinstance(v, float) else str(v))


def command_line(cid: str, op: str, symbol: str = "", volume=None, sl=None, tp=None, ticket=None, magic=None,
                 deviation=None, comment: str = "", expires: int | None = None) -> str:
    """expires: unix time (UTC seconds) after which the EA refuses the command (no stale orders after an outage)."""
    clean = str(comment or "").replace("|", "/").replace("\n", " ")[:31]
    exp = int(expires if expires is not None else time.time() + COMMAND_TTL_S)
    return "|".join([cid, op, str(symbol or "").replace("|", ""), _num(volume), _num(sl), _num(tp), _num(ticket),
                     _num(magic), _num(deviation), clean, str(exp)])


COMMAND_TTL_S = 20          # an order not picked up by MT5 within 20 s is never executed later


class Bridge:
    """Common part: results land in self._results (id -> result); state is the EA's latest snapshot."""
    kind = "none"

    def __init__(self):
        self.state: dict = {}
        self.state_at = 0.0                     # time.time() of the last state received
        self.symbols: list[dict] = []
        self._results: dict[str, dict] = {}
        self._sent: dict[str, float] = {}       # id -> time sent (latency)
        self._lock = threading.RLock()

    # ------------------------------------------------------------- to implement
    def _deliver(self, line: str) -> None:
        raise NotImplementedError

    def _pump(self) -> None:
        """Move new results / state from the transport into memory."""

    # ------------------------------------------------------------- public
    def send(self, op: str, **f) -> str:
        if op not in OPS:
            raise ValueError(f"unknown MT5 command {op}")
        cid = uuid.uuid4().hex[:12]
        self._sent[cid] = time.time()
        self._deliver(command_line(cid, op, **f))
        return cid

    def take(self, cid: str) -> dict | None:
        self._pump()
        with self._lock:
            r = self._results.pop(cid, None)
            while len(self._results) > 500:              # answers nobody waits for (e.g. "watch")
                self._results.pop(next(iter(self._results)))
        if r is not None and cid in self._sent:
            r["latency_ms"] = round((time.time() - self._sent.pop(cid)) * 1000, 1)
        return r

    def call(self, op: str, timeout: float = 5.0, **f) -> dict:
        """Send and wait for the EA's answer (used by the dashboard buttons, never by the trading loop)."""
        cid = self.send(op, **f)
        end = time.time() + timeout
        while time.time() < end:
            r = self.take(cid)
            if r is not None:
                return r
            time.sleep(0.01)
        self._sent.pop(cid, None)
        return {"id": cid, "ok": False, "error": f"no answer from the MT5 Expert Advisor within {timeout:g}s "
                                                   f"({self.health()['message']})"}

    def age(self) -> float | None:
        self._pump()
        return (time.time() - self.state_at) if self.state_at else None

    def health(self) -> dict:
        a = self.age()
        if a is None:
            return {"connected": False, "message": "the Expert Advisor has not reported yet - is YonexisBridge attached to a chart with Algo Trading on?"}
        if a > 5:
            return {"connected": False, "age_s": round(a, 1), "message": f"last report {a:.0f}s ago - MT5 closed, EA removed or Algo Trading off"}
        return {"connected": True, "age_s": round(a, 2), "message": "connected"}


class FileBridge(Bridge):
    kind = "file"

    def __init__(self, common_dir: str | Path):
        super().__init__()
        self.root = Path(common_dir) / "YONEXIS"
        self.inbox, self.outbox = self.root / "in", self.root / "out"
        self._state_mtime = 0.0
        self._sym_mtime = 0.0

    def ready(self) -> tuple[bool, str]:
        base = self.root.parent
        if not base.exists():
            return False, f"MT5 Common Files folder not found at {base} - set MT5_COMMON_DIR in .env (scripts/mt5-setup.sh finds it) and restart"
        return True, str(base)

    def _deliver(self, line: str) -> None:
        """Commands are numbered files YONEXIS/c0000000001.txt, c0000000002.txt ...: the EA opens the next number
        (under Wine on a Mac it cannot list or open some files written by other programs: no .cmd names, no subfolder)."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._pump_state()
            pending = self._pending()
            ea_next = int(self.state.get("next_seq") or 0)
            seq = max(getattr(self, "_next", 0), ea_next, (max(pending) + 1) if pending else 0, 1)
            self._next = seq + 1
            tmp = self.root / f"c{seq:010d}.tmp"
            tmp.write_text(line + "\n", encoding="ascii", errors="replace")
            os.replace(tmp, self.root / f"c{seq:010d}.txt")      # atomic: the EA only reads c*.txt
            (self.root / "next.txt").write_text(str(seq), encoding="ascii")

    def _pending(self) -> list[int]:
        return [int(f.stem[1:]) for f in self.root.glob("c*.txt") if len(f.stem) == 11 and f.stem[1:].isdigit()]

    def _pump_state(self) -> None:
        try:
            st = self.root / "state.json"
            m = st.stat().st_mtime
            if m != self._state_mtime:
                self.state = json.loads(st.read_text(encoding="utf-8", errors="replace"))
                self._state_mtime = m
                self.state_at = time.time() if time.time() - m < 10 else m
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def cleanup(self) -> int:
        """Remove command files from older bridge versions (random hex names the EA never reads; some are all digits)."""
        n = 0
        for f in self.inbox.glob("*.cmd") if self.inbox.exists() else []:
            if True:                                     # older versions used in/*.cmd (MT5 cannot open .cmd files)
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        try:                                             # forget a hint left by an older version
            pending = self._pending() if self.root.exists() else []
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "next.txt").write_text(str(max(pending) if pending else 0), encoding="ascii")
        except OSError:
            pass
        return n

    def _pump(self) -> None:
        self._pump_state()
        try:
            sy = self.root / "symbols.json"
            m = sy.stat().st_mtime
            if m != self._sym_mtime:
                self.symbols = json.loads(sy.read_text(encoding="utf-8", errors="replace")).get("symbols", [])
                self._sym_mtime = m
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        for f in list(self.root.glob("r_*.json")) + (list(self.outbox.glob("*.res")) if self.outbox.exists() else []):
            try:
                r = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                f.unlink()
            except (json.JSONDecodeError, OSError):
                continue
            with self._lock:
                self._results[str(r.get("id"))] = r


class WebBridge(Bridge):
    kind = "web"

    def __init__(self):
        super().__init__()
        self._queue: deque[str] = deque()

    def ready(self) -> tuple[bool, str]:
        return True, "waiting for the EA to call /api/mt5/bridge"

    def _deliver(self, line: str) -> None:
        self._queue.append(line)

    def pending(self) -> list[str]:
        out = []
        while self._queue:
            out.append(self._queue.popleft())
        return out

    def on_poll(self, body: dict) -> None:
        """Called by the /api/mt5/bridge endpoint with what the EA sent."""
        if body.get("state"):
            self.state = body["state"]
            self.state_at = time.time()
        if body.get("symbols"):
            self.symbols = body["symbols"]
        for r in body.get("results") or []:
            with self._lock:
                self._results[str(r.get("id"))] = r


_bridge: Bridge | None = None
_bridge_key = None


def get_bridge() -> Bridge:
    """The bridge for the transport chosen in the settings (recreated when the setting changes)."""
    global _bridge, _bridge_key
    from yonexis.mt5 import config
    c = config.get()
    key = (c["transport"], config.common_dir())
    if _bridge is None or key != _bridge_key:
        _bridge = WebBridge() if c["transport"] == "web" else FileBridge(config.common_dir())
        _bridge_key = key
        if isinstance(_bridge, FileBridge):
            _bridge.cleanup()
    return _bridge
