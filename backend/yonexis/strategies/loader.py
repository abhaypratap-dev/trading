"""Load strategies stored in the database (uploaded, imported or AI-built).

Only code that passed the static check AND was approved by the admin is loaded
in-process for the live loop. Everything else runs in the sandbox.
"""
from __future__ import annotations

import types

from yonexis import db
from yonexis.sandbox import check
from yonexis.sdk import Strategy

LIVE_STATUSES = ("paper", "live_small", "live")


def load_code(code: str) -> type[Strategy]:
    sc = check(code)
    if not sc["ok"]:
        raise ValueError(f"static check failed: {sc['errors']}")
    mod = types.ModuleType("yonexis_user_strategy")
    exec(compile(code, "<strategy>", "exec"), mod.__dict__)  # noqa: S102 - admin-approved, statically checked code only
    classes = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy
                   and v.__module__ == mod.__name__]       # only classes defined in this code, not imported bases
    if not classes:
        raise ValueError("no Strategy subclass")
    return classes[-1]


def latest_versions() -> dict[str, dict]:
    rows = db.fetch(db.strategies, order=db.strategies.c.version)
    out: dict[str, dict] = {}
    for r in rows:
        out[r["name"]] = r
    return out


def active_live() -> list[tuple[dict, type[Strategy]]]:
    out = []
    for r in latest_versions().values():
        if r["status"] in LIVE_STATUSES:
            try:
                out.append((r, load_code(r["code"])))
            except Exception:
                continue
    return out


# ---------------------------------------------------------------------- paper-test scope
SCOPE_KEY = "paper_scope"


def get_scope() -> dict | None:
    """Admin's choice in Paper Wallet: {"symbols": [...], "strategy": name, "version": n}. None = everything in paper."""
    sc = db.get_setting(SCOPE_KEY)
    return sc if isinstance(sc, dict) and sc.get("strategy") else None


def scope_strategy(sc: dict | None = None) -> dict | None:
    sc = sc or get_scope()
    if not sc or sc.get("strategy") in ("ai_pick", "pair_trading"):
        return None
    rows = [r for r in db.fetch(db.strategies, db.strategies.c.name == sc["strategy"], order=db.strategies.c.version)]
    if not rows:
        return None
    v = sc.get("version")
    return next((r for r in rows if r["version"] == v), rows[-1]) if v else rows[-1]


def trading_strategies() -> list[tuple[dict, type[Strategy]]]:
    """Strategies the live engine trades with: the scoped one if the admin chose one, else every strategy in paper.
    "ai_pick" = every built-in strategy (the AI picker filters their calls); "pair_trading" = none (pairs run separately)."""
    sc = get_scope()
    if sc and sc.get("strategy") == "pair_trading":
        return []
    if sc and sc.get("strategy") == "ai_pick":
        from yonexis.strategies import REGISTRY
        out = []
        for n, r in latest_versions().items():
            if n in REGISTRY and r["status"] != "retired":
                try:
                    out.append((r, load_code(r["code"])))
                except Exception:
                    continue
        return out
    row = scope_strategy()
    if row is not None:
        try:
            return [(row, load_code(row["code"]))]
        except Exception:
            return []
    return active_live()


def in_scope(symbol: str) -> bool:
    sc = get_scope()
    return not sc or not sc.get("symbols") or symbol.upper() in {s.upper() for s in sc["symbols"]}
