"""Sandboxed execution of strategy code.

Two modes:
  * local  - a child Python process with CPU, memory and file limits and a stripped environment.
  * queue  - the job is written to a shared folder and picked up by the `sandbox` container,
             which runs with `network_mode: none` (docker-compose). Default inside Docker.
Either way the child only receives the code, a job description and a data snapshot.
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pandas as pd

from yonexis.config import settings

from .static_check import check

BACKEND_DIR = Path(__file__).resolve().parents[2]
QUEUE_DIR = Path(os.getenv("SANDBOX_QUEUE_DIR", settings.DATA_DIR / "sandbox_queue"))
MODE = os.getenv("SANDBOX_MODE", "local")


def parallel_workers() -> int:
    """Processes a backtest may split its stocks across: SANDBOX_WORKERS (default: every CPU core Docker gives us)."""
    v = os.getenv("SANDBOX_WORKERS", "auto")
    n = os.cpu_count() or 1
    try:
        return max(1, min(int(v), n)) if v != "auto" else n
    except ValueError:
        return n


class SandboxError(RuntimeError):
    pass


HEARTBEAT = "worker_alive"
LAST_MODE = {"mode": MODE, "note": ""}


def worker_alive(max_age: float = 15.0) -> bool:
    """The sandbox container touches QUEUE_DIR/worker_alive every second while it is running."""
    try:
        return time.time() - (QUEUE_DIR / HEARTBEAT).stat().st_mtime < max_age
    except OSError:
        return False


def status() -> dict:
    return {"configured": MODE, "worker_alive": worker_alive() if MODE == "queue" else None, "cores": parallel_workers(), **LAST_MODE}


def _limits(cpu: int | None = None):  # runs in the child before exec (POSIX only)
    import resource
    cpu = int(cpu or settings.SANDBOX_TIMEOUT)

    mem = settings.SANDBOX_MEMORY_MB * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except (ValueError, OSError):
        pass
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
    os.setsid()


class _Tail:
    """Forward new lines of job_dir/progress.jsonl (written by the child) to a progress callback."""

    def __init__(self, job_dir: Path, cb):
        self.path, self.cb, self.pos = job_dir / "progress.jsonl", cb, 0

    def poll(self) -> None:
        if self.cb is None:
            return
        try:
            with open(self.path) as fh:
                fh.seek(self.pos)
                chunk = fh.read()
        except OSError:
            return
        end = chunk.rfind("\n")
        if end < 0:
            return
        self.pos += len(chunk[:end + 1].encode())
        for line in chunk[:end].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.pop("msg", "")
            rec.pop("t", None)
            try:
                self.cb(msg, rec)
            except Exception:
                pass


def execute_job_dir(job_dir: Path, timeout: int | None = None, on_progress=None) -> dict:
    if timeout is None:
        try:
            timeout = int(json.loads((job_dir / "job.json").read_text()).get("_timeout") or 0) or None
        except Exception:
            timeout = None
    env = {"PYTHONPATH": str(BACKEND_DIR), "PATH": "/usr/bin:/bin", "HOME": str(job_dir),
           "YONEXIS_DATA_DIR": str(job_dir / "_data"), "DATABASE_URL": "sqlite:///:memory:",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MPLBACKEND": "Agg",
           "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "YONEXIS_PARALLEL": str(parallel_workers())}
    limit = timeout or settings.SANDBOX_TIMEOUT
    tail = _Tail(job_dir, on_progress)
    proc = subprocess.Popen([sys.executable, "-m", "yonexis.sandbox.child", str(job_dir)], cwd=job_dir, env=env,
                            preexec_fn=(lambda: _limits(timeout)) if os.name == "posix" else None,
                            stdout=subprocess.DEVNULL, stderr=open(job_dir / "stderr.txt", "wb"))
    deadline = time.time() + limit
    while proc.poll() is None:
        tail.poll()
        if time.time() > deadline:
            proc.kill()
            proc.wait()
            out = {"ok": False, "error": f"timed out after {limit}s"}
            _write_result(job_dir, out)
            return out
        time.sleep(0.5)
    tail.poll()
    rp = job_dir / "result.json"
    if not rp.exists():
        out = {"ok": False, "crashed": True, "error": f"strategy process crashed (exit code {proc.returncode}): {_stderr_tail(job_dir)}"}
        _write_result(job_dir, out)
        return out
    return json.loads(rp.read_text())


def _stderr_tail(job_dir: Path, n: int = 1200) -> str:
    try:
        return (job_dir / "stderr.txt").read_bytes()[-n:].decode(errors="replace").strip() or "no error output"
    except OSError:
        return "no error output"


def _write_result(job_dir: Path, out: dict) -> None:
    try:
        (job_dir / "result.json").write_text(json.dumps(out))
    except OSError:
        pass


def _keep_error(job_dir: Path, out: dict) -> None:
    """Keep the last sandbox failure next to the logs (data/logs/sandbox_last_error.txt) for diagnosis."""
    try:
        d = settings.DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sandbox_last_error.txt").write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  mode={LAST_MODE.get('mode')}\n"
                                                 f"{out.get('error')}\n\n--- stderr ---\n{_stderr_tail(job_dir, 8000)}\n"
                                                 f"\n--- trace ---\n{out.get('trace', '')}\n")
    except OSError:
        pass


def run(code: str, data: dict[str, pd.DataFrame], job: dict, timeout: int | None = None, progress=None) -> dict:
    sc = check(code)
    if not sc["ok"]:
        return {"ok": False, "error": "static check failed", "static_check": sc}

    queue = MODE == "queue" and worker_alive()
    if MODE == "queue" and not queue:
        LAST_MODE.update(mode="local", note="sandbox container not answering: ran in a limited local process instead")
    else:
        LAST_MODE.update(mode=MODE, note="")
    if queue:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        job_dir = QUEUE_DIR / f"job-{uuid.uuid4().hex}"
        tmp = job_dir.with_suffix(".writing")
        tmp.mkdir()
    else:
        tmp = Path(tempfile.mkdtemp(prefix="yonexis-sbx-"))
        job_dir = tmp

    (tmp / "strategy.py").write_text(code)
    futs = [k for k in data if str(k).upper().endswith("-FUT")]
    if futs:                         # the sandbox has no instrument list: hand it the lot sizes
        from yonexis.backtest.engine import BacktestConfig, with_lot_sizes
        cfgd = dict(job.get("config") or {})
        lots = with_lot_sizes(BacktestConfig(lot_sizes=dict(cfgd.get("lot_sizes") or {})), futs).lot_sizes
        job = {**job, "config": {**cfgd, "lot_sizes": lots}}
    (tmp / "job.json").write_text(json.dumps({**job, "_timeout": timeout or settings.SANDBOX_TIMEOUT}, default=str))
    with open(tmp / "data.pkl", "wb") as fh:
        pickle.dump(data, fh)

    try:
        out = None
        if queue:
            tmp.rename(job_dir)            # atomic hand-off to the sandbox worker
            tail = _Tail(job_dir, progress)
            picked_by = time.time() + 20
            deadline = time.time() + (timeout or settings.SANDBOX_TIMEOUT) + 30
            while time.time() < deadline:
                rp = job_dir / "result.json"
                tail.poll()
                if (job_dir / "done").exists():
                    tail.poll()
                    out = json.loads(rp.read_text()) if rp.exists() else \
                        {"ok": False, "crashed": True, "error": f"strategy process crashed in the sandbox container: {_stderr_tail(job_dir)}"}
                    break
                if time.time() > picked_by and not (job_dir / "running").exists():
                    break                  # worker never picked it up: run it here instead
                time.sleep(0.5)
            if out is None and not (job_dir / "running").exists():
                LAST_MODE.update(mode="local", note="sandbox container did not pick up the job: ran in a limited local process instead")
                out = execute_job_dir(job_dir, timeout, progress)
            elif out is None:
                out = {"ok": False, "error": "sandbox worker did not finish in time"}
            if not out.get("ok") and out.get("crashed"):
                # the container itself failed (not the strategy): keep the evidence and run it here once instead
                _keep_error(job_dir, out)
                if progress:
                    progress(f"sandbox container failed ({out['error'][:160]}); running in a limited local process instead", {})
                LAST_MODE.update(mode="local", note="sandbox container crashed: ran in a limited local process instead "
                                                    "(details in data/logs/sandbox_last_error.txt)")
                for f in ("result.json", "done", "progress.jsonl", "stderr.txt"):
                    (job_dir / f).unlink(missing_ok=True)
                out = execute_job_dir(job_dir, timeout, progress)
        else:
            out = execute_job_dir(job_dir, timeout, progress)
        if not out.get("ok"):
            _keep_error(job_dir, out)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
    out["static_check"] = sc
    return out


def worker_loop() -> None:  # pragma: no cover - runs in the sandbox container
    """Entry point of the `sandbox` container: execute jobs dropped into QUEUE_DIR."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"sandbox worker watching {QUEUE_DIR}", flush=True)
    beat = QUEUE_DIR / HEARTBEAT

    def heartbeat():                 # keeps beating while a long job runs
        while True:
            try:
                beat.touch()
            except OSError as e:
                print(f"heartbeat failed: {e}", flush=True)
            time.sleep(1)
    import threading
    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        for d in sorted(QUEUE_DIR.glob("job-*")):
            if d.suffix == ".writing" or (d / "done").exists() or (d / "running").exists():
                continue
            try:
                (d / "running").touch()
                execute_job_dir(d)
                (d / "done").touch()
            except Exception as e:      # never let one bad job kill the worker
                print(f"job {d.name} failed: {e}", flush=True)
                try:
                    (d / "result.json").write_text(json.dumps({"ok": False, "error": f"sandbox worker: {e}"}))
                    (d / "done").touch()
                except OSError:
                    pass
            try:
                beat.touch()
            except OSError:
                pass
        time.sleep(0.3)


if __name__ == "__main__":  # pragma: no cover
    worker_loop()
