"""FastAPI app: manage targets, launch scans, browse results.

Runs on a background thread pool (see jxai.runner) so the slow AI and probe stages
never block a request. Everything rendered is HTML via Jinja2; the only JSON
endpoints are the run-progress poller.
"""
from __future__ import annotations

import json
import logging
import pathlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import store
from .config import DATA_DIR
from .runner import QUEUE, prune_old_runs, run_dir_for

log = logging.getLogger("jxai.web")

BASE_DIR = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    # A previous crash leaves rows marked 'running' forever; reconcile them first.
    stale = store.recover_interrupted_runs()
    if stale:
        log.warning("marked %d interrupted run(s) from a previous process", stale)
    prune_old_runs(int(os.environ.get("JXAI_KEEP_RUNS", "20")))
    yield


app = FastAPI(title="jxai", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _ctx(request: Request, **extra) -> dict:
    return {"request": request, "queue": QUEUE.status(), **extra}


def _run_dir(run: dict) -> pathlib.Path:
    """Resolve and confine a run directory. Guards against a stale or tampered
    path in the DB escaping into arbitrary filesystem reads."""
    target = store.get_target(run["target_id"])
    if not target:
        raise HTTPException(404, "target no longer exists")
    expected = run_dir_for(target, run["id"]).resolve()
    try:
        actual = pathlib.Path(run["dir"]).resolve()
        actual.relative_to(expected.parent)
    except (ValueError, OSError):
        raise HTTPException(400, "run directory is outside its expected location")
    return actual


def _read_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _fmt_ts(value) -> str:
    if not value:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["ts"] = _fmt_ts
templates.env.globals["fmt_ts"] = _fmt_ts
templates.env.globals["has_key"] = lambda: bool(os.environ.get("SINOX_API_KEY"))


# --- pages ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    targets = store.list_targets()
    recent = store.list_runs(limit=25)
    by_target = {t["id"]: t for t in targets}
    for r in recent:
        r["target_name"] = by_target.get(r["target_id"], {}).get("name", "(deleted)")
    return templates.TemplateResponse(
        request, "dashboard.html", _ctx(request, targets=targets, runs=recent))


@app.get("/targets/new", response_class=HTMLResponse)
def target_form(request: Request):
    return templates.TemplateResponse(
        request, "target_form.html", _ctx(request, target=None, error=None))


@app.get("/targets/{target_id}/edit", response_class=HTMLResponse)
def target_edit(request: Request, target_id: int):
    target = store.get_target(target_id)
    if not target:
        raise HTTPException(404, "target not found")
    return templates.TemplateResponse(
        request, "target_form.html", _ctx(request, target=target, error=None))


@app.post("/targets")
async def target_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    scope: str = Form(""),
    notes: str = Form(""),
):
    entries = [s for s in (line.strip() for line in scope.splitlines()) if s]
    try:
        target = store.add_target(name, url, scope=entries, notes=notes)
    except Exception as exc:
        return templates.TemplateResponse(
        request, "target_form.html", _ctx(request, target=None, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(f"/targets/{target['id']}", status_code=303)


@app.post("/targets/{target_id}/edit")
async def target_update(
    request: Request,
    target_id: int,
    name: str = Form(...),
    url: str = Form(...),
    scope: str = Form(""),
    notes: str = Form(""),
    overrides: str = Form(""),
):
    entries = [s for s in (line.strip() for line in scope.splitlines()) if s]
    parsed: dict = {}
    if overrides.strip():
        try:
            parsed = json.loads(overrides)
            if not isinstance(parsed, dict):
                raise ValueError("overrides must be a JSON object")
        except ValueError as exc:
            return templates.TemplateResponse(
        request, "target_form.html", _ctx(request, target=store.get_target(target_id), error=f"invalid overrides: {exc}"),
                status_code=400,
            )
    try:
        store.update_target(target_id, name=name, url=url, scope=entries, notes=notes, overrides=parsed)
    except Exception as exc:
        return templates.TemplateResponse(
        request, "target_form.html", _ctx(request, target=store.get_target(target_id), error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@app.post("/targets/{target_id}/delete")
def target_delete(target_id: int):
    store.delete_target(target_id)
    return RedirectResponse("/", status_code=303)


@app.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(request: Request, target_id: int):
    target = store.get_target(target_id)
    if not target:
        raise HTTPException(404, "target not found")
    runs = store.list_runs(target_id, limit=25)
    latest = runs[0] if runs else None
    return templates.TemplateResponse(
        request, "target_detail.html", _ctx(request, target=target, runs=runs, latest=latest,
            ),
    )


@app.post("/targets/{target_id}/run")
def target_run(target_id: int):
    target = store.get_target(target_id)
    if not target:
        raise HTTPException(404, "target not found")
    if not os.environ.get("SINOX_API_KEY"):
        raise HTTPException(400, "SINOX_API_KEY is not set on the server")
    if not target["enabled"]:
        raise HTTPException(400, "target is disabled")
    run = QUEUE.submit(target)
    return RedirectResponse(f"/runs/{run['id']}", status_code=303)


# --- runs ------------------------------------------------------------------- #

@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    target = store.get_target(run["target_id"])
    directory = _run_dir(run)
    return templates.TemplateResponse(
        request, "run_detail.html", _ctx(request, run=run, target=target, directory=directory,
             files=sorted(p.name for p in directory.iterdir()) if directory.exists() else [],
             live=run["status"] == "running"),
    )


@app.get("/api/runs/{run_id}/progress")
def run_progress(run_id: int):
    """Polled by the run page while a scan is in flight."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return JSONResponse({
        "status": run["status"],
        "stages": run["stages"],
        "stats": run["stats"],
        "error": run["error"],
        "queue": QUEUE.status(),
    })


@app.get("/runs/{run_id}/findings", response_class=HTMLResponse)
def run_findings(request: Request, run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    directory = _run_dir(run)
    findings = _read_json(directory / "findings.json", [])
    return templates.TemplateResponse(
        request, "findings.html", _ctx(request, run=run, target=store.get_target(run["target_id"]),
                              findings=findings, directory=directory)
    )


@app.get("/runs/{run_id}/paths", response_class=HTMLResponse)
def run_paths(request: Request, run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    paths = _read_json(_run_dir(run) / "paths.json", [])
    return templates.TemplateResponse(
        request, "paths.html", _ctx(request, run=run, target=store.get_target(run["target_id"]), discovered=paths)
    )


@app.get("/runs/{run_id}/probes", response_class=HTMLResponse)
def run_probes(request: Request, run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    results = _read_json(_run_dir(run) / "results.json", [])
    return templates.TemplateResponse(
        request, "probes.html", _ctx(request, run=run, target=store.get_target(run["target_id"]), results=results)
    )


@app.get("/runs/{run_id}/js", response_class=HTMLResponse)
def run_js(request: Request, run_id: int):
    """Interactive viewer over the beautified bundles this run downloaded."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    js_dir = _run_dir(run) / "js_files"
    files = sorted(p.name for p in js_dir.iterdir() if p.is_file()) if js_dir.is_dir() else []
    return templates.TemplateResponse(
        request, "js_viewer.html", _ctx(request, run=run, target=store.get_target(run["target_id"]), files=files)
    )


@app.get("/api/runs/{run_id}/js")
def run_js_list(run_id: int):
    """Names of the downloaded bundles, as JSON for the viewer's file picker."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    js_dir = _run_dir(run) / "js_files"
    if not js_dir.is_dir():
        return JSONResponse([])
    return JSONResponse(sorted(p.name for p in js_dir.iterdir() if p.is_file()))


@app.get("/runs/{run_id}/js/view")
def run_js_view(run_id: int, name: str, offset: int = 0, limit: int = 2000):
    """Serve a slice of a downloaded bundle. Sliced because bundles run to
    hundreds of thousands of lines and the browser will not hold one."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    js_dir = (_run_dir(run) / "js_files").resolve()
    path = (js_dir / name).resolve()
    if path.parent != js_dir or not path.is_file():
        raise HTTPException(404, "no such file")
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    offset = max(0, min(offset, max(0, len(lines) - 1)))
    chunk = lines[offset:offset + limit]
    return PlainTextResponse(
        "\n".join(chunk), headers={"X-Total-Lines": str(len(lines)), "X-Offset": str(offset)}
    )


@app.get("/runs/{run_id}/artifact/{name}")
def run_artifact(run_id: int, name: str):
    """Serve a generated report (md/html/pdf). Filename-restricted to the known
    artifacts so this route cannot be used to read arbitrary run-directory files."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if name not in ("report.md", "report.html", "report.pdf", "paths.json",
                    "results.json", "deep_audit.json"):
        raise HTTPException(404, "unknown artifact")
    path = _run_dir(run) / name
    if not path.is_file():
        raise HTTPException(404, f"{name} was not produced by this run")
    media = {"report.md": "text/markdown", "report.html": "text/html",
             "report.pdf": "application/pdf"}.get(name, "application/json")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="ignore"), media_type=media)


@app.get("/healthz")
def healthz():
    return {"ok": True, "data_dir": str(DATA_DIR), "queue": QUEUE.status(),
            "has_key": bool(os.environ.get("SINOX_API_KEY"))}
