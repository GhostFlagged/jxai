"""Run orchestration shared by the CLI and the web app.

Each run gets its own directory under data/runs/<target>-<id>/, so concurrent
scans never overwrite each other and a run's artifacts stay together.
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import shutil
import threading
from collections import deque
from typing import Any, Callable, Mapping

from . import store
from .config import DATA_DIR, build_config

log = logging.getLogger("jxai.runner")

RUNS_DIR = DATA_DIR / "runs"

_SLUG = re.compile(r"[^a-z0-9._-]+")
# Dot-runs and separators go: a target named "../../etc" must not steer the run
# directory, and the id suffix already guarantees uniqueness.
_UNSAFE = re.compile(r"\.{2,}|[/\\]")

ProgressFn = Callable[[str, str, dict[str, Any]], None]


def slugify(text: str) -> str:
    flat = _UNSAFE.sub(" ", text.strip().lower())
    return _SLUG.sub("-", flat).strip("-") or "target"


def run_dir_for(target: Mapping[str, Any], run_id: int) -> pathlib.Path:
    return RUNS_DIR / f"{slugify(target['name'])}-{run_id}"


def prune_old_runs(keep: int = 20) -> int:
    """Bound disk usage by removing the least-recent run directories beyond `keep`."""
    if keep <= 0 or not RUNS_DIR.exists():
        return 0
    dirs = sorted((p for p in RUNS_DIR.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    removed = 0
    for path in dirs[:-keep]:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def execute_run(
    run_id: int,
    target: Mapping[str, Any],
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run the pipeline for one target and finalize its run row."""
    from .pipeline import run_pipeline, summarize

    out_dir = run_dir_for(target, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    stages: list[dict[str, Any]] = []

    def on_progress(stage: str, status: str, detail: dict[str, Any]) -> None:
        stages.append({"stage": stage, "status": status, "detail": detail})
        # Persist per stage so the UI can poll and render partial progress; a run
        # that dies mid-flight still shows how far it got.
        store.update_stages(run_id, stages)
        if progress:
            progress(stage, status, detail)

    try:
        cfg = build_config(target, out_dir)
        ctx = run_pipeline(cfg, out_dir, on_progress=on_progress)
        stats = summarize(ctx)
        status = "partial" if stats["errors"] else "success"
        store.finish_run(run_id, status, stats=stats)
        return {"status": status, "stats": stats, "ctx": ctx}
    except Exception as exc:
        log.exception("run %s failed", run_id)
        store.finish_run(run_id, "failed", error=str(exc))
        raise


def create_run(target: Mapping[str, Any], trigger: str) -> dict[str, Any]:
    """Insert a run row and reserve its directory."""
    run = store.start_run(target["id"], "", trigger)
    out_dir = run_dir_for(target, run["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    return store.update_run_dir(run["id"], str(out_dir))


class RunQueue:
    """Runs scans on worker threads with a concurrency cap.

    The AI stages are expensive and rate-limited, so unbounded one-thread-per-click
    would pile up work nobody can afford. Beyond the cap, runs wait as 'queued' rows
    and start as slots free up, rather than being dropped or running unbounded.
    """

    def __init__(self, max_parallel: int = 2) -> None:
        self.max_parallel = max(1, int(max_parallel))
        self._lock = threading.Lock()
        self._active: set[int] = set()
        self._pending: deque[tuple[int, dict[str, Any]]] = deque()

    def submit(self, target: Mapping[str, Any]) -> dict[str, Any]:
        store.init_db()
        run = create_run(target, "web")
        with self._lock:
            if len(self._active) < self.max_parallel:
                self._active.add(run["id"])
                start = True
            else:
                self._pending.append((run["id"], dict(target)))
                start = False
        if start:
            self._spawn(run["id"], target)
        return run

    def _spawn(self, run_id: int, target: Mapping[str, Any]) -> None:
        threading.Thread(target=self._work, args=(run_id, dict(target)),
                         name=f"jxai-run-{run_id}", daemon=True).start()

    def _work(self, run_id: int, target: dict[str, Any]) -> None:
        try:
            execute_run(run_id, target)
        except Exception:
            pass  # execute_run already recorded the failure on the run row
        finally:
            self._release(run_id)

    def _release(self, run_id: int) -> None:
        with self._lock:
            self._active.discard(run_id)
            nxt = self._pending.popleft() if self._pending else None
            if nxt:
                self._active.add(nxt[0])
        if nxt:
            self._spawn(nxt[0], nxt[1])

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"active": len(self._active), "queued": len(self._pending),
                    "max_parallel": self.max_parallel}


# Process-wide queue. One is enough: the web app is single-process, and a second
# worker would need external locking anyway.
QUEUE = RunQueue(max_parallel=int(os.environ.get("JXAI_MAX_PARALLEL", "2")))