"""Static safety and correctness checks for uploaded / AI-written strategy code.

This is the first line of defence; the sandbox process (resource limits, no
network, separate container) is the second.
"""
from __future__ import annotations

import ast

ALLOWED_IMPORTS = {
    "math", "statistics", "datetime", "typing", "dataclasses", "collections", "functools", "itertools",
    "numpy", "pandas", "yonexis.sdk", "yonexis", "__future__",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "input",
                   "breakpoint", "exit", "quit", "setattr", "delattr", "memoryview"}
FORBIDDEN_ATTRS = {"to_pickle", "read_pickle", "to_csv", "to_parquet", "to_excel", "read_csv", "read_excel",
                   "read_sql", "to_sql", "read_html", "read_json", "to_json", "system", "popen", "load", "save"}
LOOKAHEAD_HINTS = ("shift(-", ".iloc[i + 1", ".iloc[i+1", "iloc[-0", ".tail(-")


def check(code: str) -> dict:
    """Returns {"ok": bool, "errors": [...], "warnings": [...], "strategy_classes": [...]}."""
    errors, warnings, classes = [], [], []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"ok": False, "errors": [f"syntax error line {e.lineno}: {e.msg}"], "warnings": [], "strategy_classes": []}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in {m.split(".")[0] for m in ALLOWED_IMPORTS} or (a.name.startswith("yonexis") and a.name not in ALLOWED_IMPORTS and not a.name.startswith("yonexis.sdk")):
                    errors.append(f"line {node.lineno}: import {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod not in ALLOWED_IMPORTS and not mod.startswith("yonexis.sdk"):
                errors.append(f"line {node.lineno}: from {mod} import ... is not allowed")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if isinstance(fn, ast.Name) and name in FORBIDDEN_CALLS:
                errors.append(f"line {node.lineno}: call to {name}() is not allowed")
            if isinstance(fn, ast.Attribute) and name in FORBIDDEN_ATTRS:
                errors.append(f"line {node.lineno}: .{name}() is not allowed (no file or network I/O)")
            if isinstance(fn, ast.Name) and name == "getattr" and len(node.args) > 1:
                a = node.args[1]
                if not (isinstance(a, ast.Constant) and isinstance(a.value, str) and not a.value.startswith("_")):
                    errors.append(f"line {node.lineno}: dynamic getattr is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr not in ("__init__", "__name__"):
            errors.append(f"line {node.lineno}: access to {node.attr} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr == "_hist":
            errors.append(f"line {node.lineno}: ctx._hist is private; use ctx.history (prevents look-ahead)")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            warnings.append(f"line {node.lineno}: global/nonlocal state; prefer ctx.state")
        elif isinstance(node, ast.ClassDef):
            bases = [b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "" for b in node.bases]
            if "Strategy" in bases or "IntradayStrategy" in bases:
                classes.append(node.name)
                has_on_candle = any(isinstance(n, ast.FunctionDef) and n.name in ("on_candle", "setup") for n in node.body)
                has_name = any(isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "name" for t in n.targets) for n in node.body)
                if not has_on_candle:
                    errors.append(f"class {node.name}: must define on_candle(self, ctx, candle) (or setup() for IntradayStrategy)")
                if not has_name:
                    errors.append(f"class {node.name}: must set a `name = '...'` class attribute")

    squashed = code.replace(" ", "")
    for hint in LOOKAHEAD_HINTS:
        if hint.replace(" ", "") in squashed:
            warnings.append(f"possible look-ahead pattern: {hint!r}")
    if not classes:
        errors.append("no class subclassing Strategy found")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "strategy_classes": classes}
