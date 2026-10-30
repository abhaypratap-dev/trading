"""Runs inside the sandbox process: load untrusted strategy code, run the requested job, write JSON.

Usage: python -I -m yonexis.sandbox.child <job_dir>
<job_dir> contains strategy.py, job.json and data.pkl; result.json is written back.
"""
from __future__ import annotations

import importlib.util
import json
import pickle
import sys
import traceback
from pathlib import Path


def main(job_dir: str) -> None:
    d = Path(job_dir)
    out = d / "result.json"
    try:
        job = json.loads((d / "job.json").read_text())
        with open(d / "data.pkl", "rb") as fh:
            data = pickle.load(fh)

        # Load the strategy module *after* reading inputs.
        spec = importlib.util.spec_from_file_location("user_strategy", d / "strategy.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from yonexis.backtest import BacktestConfig, CostModel, full_evaluation, run_backtest
        from yonexis.sdk import Strategy

        classes = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy
                   and v.__module__ == mod.__name__]       # only classes defined in this code, not imported bases
        if not classes:
            raise ValueError("no Strategy subclass found")
        cls = classes[-1]
        cfg_kwargs = job.get("config", {})
        costs = CostModel(**cfg_kwargs.pop("costs", {}))
        cfg = BacktestConfig(costs=costs, **cfg_kwargs)

        plog = d / "progress.jsonl"

        def progress(msg, info=None):
            import time as _t
            with open(plog, "a") as fh:
                fh.write(json.dumps({"t": _t.time(), "msg": msg, **(info or {})}) + "\n")

        if job["kind"] == "quick":
            res = run_backtest(cls, data, job.get("params"), cfg)
            payload = res.to_dict(include_trades=True)
            payload["signals"] = res.signals[:2000]
        elif job["kind"] == "full":
            payload = full_evaluation(cls, data, job.get("params"), cfg, param_space=job.get("param_space"),
                                      n_trials_so_far=job.get("n_trials_so_far", 1),
                                      baseline_net_pnl=job.get("baseline_net_pnl"),
                                      wf_train_days=job.get("wf_train_days", 120), wf_test_days=job.get("wf_test_days", 30),
                                      progress=progress)
        elif job["kind"] == "describe":
            payload = {}
        else:
            raise ValueError(f"unknown job kind {job['kind']}")
        payload["strategy"] = {"name": cls.name, "description": cls.description, "params": cls.params}
        out.write_text(json.dumps({"ok": True, "result": payload}, default=str))
    except Exception as e:
        out.write_text(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-4000:]}))


if __name__ == "__main__":
    main(sys.argv[1])
