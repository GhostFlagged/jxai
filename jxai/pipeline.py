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
    """Flatten model findings and static flows into one ranked list for the UI.

    Model findings already carry a severity and a verdict, so only verified ones
    (confirmed/likely) are surfaced — matching what report.md does. Static flows
    carry their own severity/confidence and are always listed, since nothing else
    in the UI would show them. A finding the UI hides but the report prints (or
    vice versa) is a discrepancy worth avoiding.
    """
    import json

    deep = ctx.get("deep_audit") or {}
    rows: list[dict[str, Any]] = []

    for f in ctx.get("ai_findings") or []:
        if not isinstance(f, dict):
            continue
        if str(f.get("verdict", "")).lower() not in ("confirmed", "likely"):
            continue
        rows.append({
            "source": "model",
            "severity": f.get("severity") or "Informational",
            "confidence": f.get("confidence") or "",
            "verdict": f.get("verdict") or "",
            "title": f.get("title") or f.get("url") or "unauthenticated access",
            "detail": f.get("reasoning") or "",
            "url": f.get("url") or "",
            "remediation": f.get("remediation") or "",
            "next_step": f.get("next_step") or "",
        })

    for flow in deep.get("flows") or []:
        if not isinstance(flow, dict):
            continue
        source, sink = _loc(flow.get("source")), _loc(flow.get("sink"))
        rows.append({
            "source": "static",
            "severity": flow.get("severity") or "Unrated",
            "confidence": flow.get("confidence") or "",
            "verdict": "",
            # Flows carry no name field, so the source->sink pair is the finding's
            # identity. Truncated to the line reference; full text is in the detail.
            "title": f"{source} → {sink}",
            "detail": "\n\n".join(x for x in (
                flow.get("reasoning") or "",
                f"Exploit: {flow.get('exploit') or flow.get('exploit_sketch')}"
                if (flow.get("exploit") or flow.get("exploit_sketch")) else "",
            ) if x),
            "url": f"{source} -> {sink}",
            "remediation": flow.get("remediation") or "",
            "next_step": "",
        })

    rows.sort(key=lambda r: (_SEV_RANK.get(str(r["severity"]).lower(), 9),
                             str(r["source"])))
    try:
        (out_dir / "findings.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write findings.json: %s", exc)


_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "informational": 4, "unrated": 5}


def _loc(v: Any) -> str:
    """Render a source/sink that may be a string or a {file,line} dict."""
    if isinstance(v, dict):
        f = v.get("file", "")
        short = f.rsplit("/", 1)[-1] if f else ""
        ln = v.get("line", "")
        if short or ln:
            return f"{short}:{ln}" if ln != "" else short
        return v.get("input") or v.get("detail") or str(v)
    return str(v) if v is not None else "?"


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