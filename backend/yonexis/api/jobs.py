"""In-process background jobs for long tasks (agent builds, full evaluations, training)."""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from typing import Any, Callable

JOBS: dict[str, dict[str, Any]] = {}
log = logging.getLogger("yonexis.jobs")


def submit(kind: str, fn: Callable[[Callable[[str, dict], None]], Any]) -> str:
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "running", "stage": "queued", "log": [], "started": time.time(), "result": None}

    def progress(stage: str, info: dict) -> None:
        if stage == "partial":
            JOBS[jid]["partial"] = info.get("results")
            return
        JOBS[jid]["stage"] = stage
        log.info("%s: %s", kind, stage)
        if len(JOBS[jid]["log"]) > 2000:
            del JOBS[jid]["log"][:500]
        JOBS[jid]["log"].append({"t": round(time.time() - JOBS[jid]["started"], 1), "stage": stage,
                                 **{k: v for k, v in info.items() if k != "code"}})

    def target():
        try:
            JOBS[jid]["result"] = fn(progress)
            JOBS[jid]["status"] = "done"
        except Exception as e:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = f"{type(e).__name__}: {e}"
            JOBS[jid]["trace"] = traceback.format_exc()[-3000:]
        JOBS[jid]["elapsed"] = round(time.time() - JOBS[jid]["started"], 1)
        _save(JOBS[jid])

    threading.Thread(target=target, daemon=True).start()
    return jid


def _path(kind: str):
    from yonexis.config import settings
    d = settings.DATA_DIR / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}.json"


def _save(j: dict) -> None:
    """Keep the last finished job of each kind on disk, so its result survives a page reload or a restart."""
    try:
        _path(j["kind"]).write_text(json.dumps(j, default=str))
    except Exception:
        pass


def latest(kind: str) -> dict | None:
    js = [j for j in JOBS.values() if j["kind"] == kind]
    if js:
        return max(js, key=lambda j: j["started"])
    try:
        return json.loads(_path(kind).read_text())
    except Exception:
        return None


def get(jid: str) -> dict | None:
    if jid in JOBS:
        return JOBS[jid]
    from yonexis.config import settings
    for f in (settings.DATA_DIR / "jobs").glob("*.json"):
        try:
            j = json.loads(f.read_text())
            if j.get("id") == jid:
                return j
        except Exception:
            pass
    return None
