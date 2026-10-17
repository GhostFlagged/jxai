"""The audit pipeline, reusable from both the CLI and the web app.

Extracted from main.py so that adding a target is a data operation rather than a
pipeline rewrite. Ordering is the contract; each skill reads and writes the shared
ctx dict.
"""
from __future__ import annotations

import logging
import pathlib
import traceback
from typing import Any, Callable, Mapping

from skills.auth_test import AuthTestSkill
from skills.ai_decision_gate import AiDecisionGateSkill
from skills.ai_js_triage import AiJsTriageSkill
from skills.ai_triage_results import AiTriageResultsSkill
from skills.deep_audit import DeepAuditSkill
from skills.jev_classify import JevClassifySkill
from skills.js_analyze import JsAnalyzeSkill
from skills.js_download import JsDownloadSkill
from skills.report import ReportSkill

log = logging.getLogger("jxai.pipeline")

ProgressFn = Callable[[str, str, dict[str, Any]], None]


def build_pipeline() -> list:
    return [
        JsDownloadSkill(),         # download + beautify (deterministic)
        AiJsTriageSkill(),         # ranks files before the deep pass
        DeepAuditSkill(),          # deep audit (sources/sinks/flows/api/auth)
        JsAnalyzeSkill(),          # regex endpoint harvesting
        AuthTestSkill(),           # live probing (always on)
        JevClassifySkill(),        # cheap deterministic prefilter over probe results
        AiDecisionGateSkill(),     # should we spend model tokens on triage?
        AiTriageResultsSkill(),    # triage of Jev-surfaced probes
        ReportSkill(),             # deterministic report over all evidence
    ]


def run_pipeline(
    config: Mapping[str, Any],
    output_dir: str | pathlib.Path,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Execute every stage against one target.

    Returns the final ctx. A failing stage is recorded and skipped rather than
    aborting the run — a report built from partial evidence beats no report.
    """
    out_dir = pathlib.Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx: dict[str, Any] = {
        "config": dict(config),
        "output_dir": str(out_dir),
        "errors": [],
    }
    stages: list[dict[str, Any]] = []

    def emit(stage: str, status: str, detail: dict[str, Any] | None = None) -> None:
        stages.append({"stage": stage, "status": status, "detail": detail or {}})
        if on_progress:
            on_progress(stage, status, detail or {})

    for skill in build_pipeline():
        if skill.name == "ai_triage_results" and ctx.get("escalate") is False:
            log.info("decision gate blocked escalation; skipping model triage")
            emit(skill.name, "skipped", {"reason": "decision gate declined escalation"})
            continue

        emit(skill.name, "running")
        try:
            out = skill.run(ctx) or {}
        except Exception as exc:
            log.error("%s failed: %s", skill.name, exc, exc_info=True)
            ctx["errors"].append(f"{skill.name}: {exc}")
            emit(skill.name, "failed", {"error": str(exc), "traceback": traceback.format_exc()[-2000:]})
            continue
        ctx.update(out)
        emit(skill.name, "done")

    ctx["stages"] = stages
    _write_findings(ctx, out_dir)
    return ctx


def _write_findings(ctx: Mapping[str, Any], out_dir: pathlib.Path) -> None:
    """Flatten findings and static flows into one list the UI can rank and filter.

    The report renders these separately (model findings carry a severity, static
    flows do not); the UI needs them unified to sort by triage value.
    """
    import json

    deep = ctx.get("deep_audit") or {}
    rows: list[dict[str, Any]] = []
    for f in ctx.get("ai_findings") or []:
        if not isinstance(f, dict):
            continue
        rows.append({
            "source": "model",
            "severity": f.get("severity") or "Informational",
            "title": f.get("title") or f.get("issue") or "",
            "detail": f.get("reasoning") or f.get("detail") or "",
            "url": f.get("url") or f.get("location") or "",
            "remediation": f.get("remediation") or "",
            "confidence": f.get("confidence") or "",
        })
    for flow in deep.get("flows") or []:
        if not isinstance(flow, dict):
            continue
        rows.append({
            "source": "static",
            "severity": "Unrated",
            "title": f"{flow.get('source', 'source')} → {flow.get('sink', 'sink')}",
            "detail": flow.get("exploit") or flow.get("detail") or flow.get("note") or "",
            "url": str(flow.get("location") or flow.get("file") or ""),
            "remediation": flow.get("remediation") or "",
            "confidence": "",
        })
    try:
        (out_dir / "findings.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write findings.json: %s", exc)


def summarize(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """Numbers the UI and run history show. Errors are stats, not a failure."""
    deep = ctx.get("deep_audit") or {}
    flows = deep.get("flows") or []
    findings = ctx.get("ai_findings") or []
    return {
        "js_files": len(ctx.get("js_files") or []),
        "paths": len(ctx.get("paths") or []),
        "probes": len(ctx.get("auth_results") or []),
        "flows": len(flows),
        "findings": len(findings),
        "critical": sum(1 for f in findings if str(f.get("severity", "")).lower() == "critical"),
        "errors": list(ctx.get("errors") or []),
        "report": ctx.get("report_path") or "",
    }