
import os, json, datetime
from .base import Skill
from ai.client import SinoxClient
from ai.router import ModelRouter
from ai.prompts import REPORT_WRITER_SYSTEM

SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Informational": 4}


def _loc(v) -> str:
    """Render a source/sink that may be a string or a {file,line} dict."""
    if isinstance(v, dict):
        f = v.get("file", "")
        short = os.path.basename(f) if f else ""
        ln = v.get("line", "")
        if short or ln:
            return f"{short}:{ln}" if ln != "" else short
        # fall back to any descriptive fields
        return v.get("input") or v.get("detail") or v.get("details") or str(v)
    return str(v) if v is not None else "?"


class ReportSkill(Skill):
    """Single report combining both surfaces the pipeline covers:
    static taint flows (deep_audit) and live unauthenticated-access probes.

    All tables are deterministic so the evidence is never invented. Sonnet writes
    only the executive summary, which is advisory prose over facts already listed.
    Writes both report.md and report.pdf.
    """

    name = "report"

    def __init__(self):
        self.ai = None
        try:
            self.ai = SinoxClient()
            self.model = ModelRouter.get("report_writer")
        except Exception:
            self.model = None

    def run(self, ctx):
        cfg = ctx["config"]
        rep = cfg.get("report", {})
        results = ctx.get("auth_results", [])
        paths = ctx.get("paths", [])
        js_files = ctx.get("js_files", [])
        flows = (ctx.get("deep_audit", {}) or {}).get("flows", []) or []
        gaps = (ctx.get("deep_audit", {}) or {}).get("auth_gaps", []) or []
        ai_findings = ctx.get("ai_findings", []) or []
        skipped = ctx.get("auth_skipped_out_of_scope", []) or []
        deep_out = ctx.get("deep_audit") or {}

        lines = []
        lines.append("# Bug Bounty JS Analysis Report")
        lines.append("")
        lines.append(f"**Target:** `{cfg['target']}`  ")
        lines.append(f"**Generated:** {datetime.datetime.now(datetime.timezone.utc).isoformat()}  ")
        lines.append(f"**Model:** `t-claude-sonnet-5`  ")
        lines.append(f"**JS files analyzed:** {len(js_files)}  ")
        lines.append(f"**API paths discovered:** {len(paths)}  ")
        lines.append(f"**Paths probed:** {len(results)}  ")
        if skipped:
            lines.append(f"**Skipped (out of scope):** {len(skipped)}  ")
        lines.append("")

        summary = self._summary(cfg, flows, ai_findings, gaps)
        if summary:
            clean_lines = []
            for l in summary.splitlines():
                s = l.strip()
                if s.startswith("#"):
                    continue
                if s.replace("*", "").replace("_", "").strip().lower().rstrip(":") in ("executive summary", "summary"):
                    continue
                clean_lines.append(l)
            clean = "\n".join(clean_lines).strip()
            lines += ["## Executive Summary", "", clean, ""]

        strong = [f for f in ai_findings if f.get("verdict") in ("confirmed", "likely")]
        lines += ["## Unauthenticated Access Findings", ""]
        if not strong:
            lines += ["_No confirmed or likely unauthenticated access findings._", ""]
        else:
            lines += ["| Verdict | Confidence | Endpoint | Next step |", "|---|---|---|---|"]
            for f in sorted(strong, key=lambda x: -float(x.get("confidence", 0) or 0)):
                lines.append(
                    f"| {f.get('verdict')} | {f.get('confidence')} | `{f.get('url','')}` | "
                    f"{f.get('next_step','')} |"
                )
            lines.append("")
            for f in strong:
                lines.append(f"### `{f.get('url','')}`")
                lines.append(f"- **Verdict:** {f.get('verdict')} (confidence {f.get('confidence')})  ")
                lines.append(f"- **Reasoning:** {f.get('reasoning','')}  ")
                lines.append(f"- **Next step:** {f.get('next_step','')}")
                lines.append("")

        lines += ["## Static Analysis Flows", ""]
        if not flows:
            lines += ["_Deep audit returned no source-to-sink flows._", ""]
        else:
            for f in sorted(flows, key=lambda x: SEV_ORDER.get(x.get("severity", "Low"), 9)):
                lines.append(f"### [{f.get('severity','?')}] {f.get('type', f.get('title','flow'))}")
                lines.append(f"- **Confidence:** {f.get('confidence','?')}  ")
                lines.append(f"- **Source:** `{_loc(f.get('source'))}`  ")
                lines.append(f"- **Sink:** `{_loc(f.get('sink'))}`  ")
                lines.append(f"- **Reasoning:** {f.get('reasoning','')}  ")
                ex = f.get("exploit") or f.get("exploit_sketch")
                if ex:
                    lines.append(f"- **Exploit sketch:** {ex}")
                if f.get("remediation"):
                    lines.append(f"- **Remediation:** {f['remediation']}")
                lines.append("")

        if gaps:
            lines += ["## Auth Gaps", ""]
            lines += ["| Route | Issue | Confidence |", "|---|---|---|"]
            for g in gaps:
                route = g.get("route", g.get("id", "-")) if isinstance(g, dict) else str(g)
                issue = g.get("issue", "") if isinstance(g, dict) else ""
                conf = g.get("confidence", "") if isinstance(g, dict) else ""
                lines.append(f"| `{route}` | {issue} | {conf} |")
            lines.append("")

        lines += ["## Probe Results", ""]
        if not results:
            lines += ["_No probes executed (no in-scope paths)._", ""]
        else:
            lines += ["| URL | Probe | Status | Length | Auth wall |", "|---|---|---|---|---|"]
            for r in results:
                for p in r.get("probes", []):
                    lines.append(
                        f"| `{r['url']}` | {p.get('probe','?')} | {p.get('status','ERR')} | "
                        f"{p.get('length','-')} | {'yes' if p.get('auth_wall') else 'no'} |"
                    )
            lines.append("")

        lines += ["## Discovered Paths", ""]
        for p in paths:
            src = ", ".join(p.get("sources", []))
            via = ", ".join(p.get("vias", []))
            meth = ", ".join(p.get("methods", [])) or "GET"
            lines.append(f"- `{p['path']}`  _(from: {src}; via: {via}; method: {meth})_")

        if skipped:
            lines += ["", "## Skipped (Out of Scope)", ""]
            for u in skipped[:120]:
                lines.append(f"- `{u}`")
            if len(skipped) > 120:
                lines.append(f"_…and {len(skipped)-120} more._")
            lines.append("")

        # ----- source/sink inventories (show whether we captured postMessage etc) -----
        deep_out = deep_out or {}
        for bucket, title in (("sources", "Observed Sources"), ("sinks", "Observed Sinks")):
            items = deep_out.get(bucket) or []
            if not items:
                continue
            lines += ["", f"## {title}", "", f"_Collected from the real JS bundle; showing top {min(len(items), 200)} by line._", "",
                      "| Line | Kind | Note |", "|---|---|---|"]
            # sort deterministically: numeric lines first, then lex
            def _sort_key(o):
                v = o.get("line", "?")
                return (0, int(v)) if isinstance(v, int) else (1, str(v))
            for o in sorted(items, key=_sort_key)[:200]:
                note = (o.get("note") or o.get("detail") or o.get("details") or "").replace("\n"," ").replace("|","/")[:160]
                lines.append(f"| `{o.get('line','?')}` | `{o.get('kind') or o.get('category') or '?'}` | {note} |")
            lines.append("")
            if len(items) > 200:
                lines.append(f"_…and {len(items)-200} more {bucket}._\n")

        if ctx.get("errors"):
            lines += ["", "## Pipeline Errors", ""]
            for e in ctx["errors"]:
                lines.append(f"- {e}")

        out_dir = ctx.get("output_dir") or "output"
        out = rep.get("output") or os.path.join(out_dir, "report.md")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as f:
            f.write("\n".join(lines))

        pdf = rep.get("pdf") or os.path.join(out_dir, "report.pdf")
        pdf_ok = self._write_pdf(pdf, cfg, js_files, paths, results, skipped, flows, gaps, ai_findings, summary, ctx.get("errors"), deep_out)

        ret = {"report_path": out}
        if pdf_ok:
            ret["report_pdf"] = pdf_ok

        html = rep.get("html") or os.path.join(out_dir, "report.html")
        try:
            from .report_html import render as render_html
            os.makedirs(os.path.dirname(html) or ".", exist_ok=True)
            with open(html, "w", encoding="utf-8") as f:
                f.write(render_html(ctx, summary))
            ret["report_html"] = html
        except Exception as e:
            ctx.setdefault("errors", []).append(f"report_html: {e}")

        return ret

    # ----- pdf -----
    def _summary(self, cfg, flows, ai_findings, gaps):
        if self.ai is None or not self.model:
            return ""
        try:
            facts = {
                "target": cfg["target"],
                "static_flow_count": len(flows),
                "top_flows": [
                    {"severity": f.get("severity"), "type": f.get("type"),
                     "summary": f.get("reasoning", "")}
                    for f in flows[:8]
                ],
                "unauth_findings": [
                    {"url": f.get("url"), "verdict": f.get("verdict"),
                     "reasoning": f.get("reasoning", "")}
                    for f in ai_findings[:8]
                ],
                "auth_gap_count": len(gaps),
            }
            text = self.ai.chat(
                self.model,
                [
                    {"role": "system", "content": REPORT_WRITER_SYSTEM},
                    {"role": "user", "content": (
                        "Write a 3-5 sentence executive summary for a bug-bounty "
                        "pre-submission triage report. Be conservative and do not "
                        "claim impact the evidence does not support.\n\n"
                        f"Facts:\n{json.dumps(facts, indent=2)}"
                    )},
                ],
                max_tokens=900,
            )
            return text.strip()
        except Exception:
            return ""
    def _write_pdf(self, out_path, cfg, js_files, paths, results, skipped, flows, gaps, ai_findings, summary, errors, deep_out=None):
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib import colors
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
            from reportlab.lib.enums import TA_LEFT, TA_CENTER
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            import html as _html
        except Exception as e:
            return None

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        def esc(s): return _html.escape(str(s or ""))

        styles = getSampleStyleSheet()
        sTitle = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=16, spaceAfter=2*mm, leading=18)
        sH1 = ParagraphStyle("H1X", parent=styles["Heading1"], fontSize=12, spaceBefore=6*mm, spaceAfter=3*mm, textColor=colors.HexColor("#1f2937"))
        sH2 = ParagraphStyle("H2X", parent=styles["Heading2"], fontSize=10, spaceBefore=4*mm, spaceAfter=2*mm, textColor=colors.HexColor("#374151"))
        sBody = ParagraphStyle("BodyX", parent=styles["BodyText"], fontSize=8, leading=11, spaceAfter=2*mm)
        sSmall = ParagraphStyle("SmallX", parent=sBody, fontSize=7, leading=10, textColor=colors.HexColor("#4b5563"))
        sMono = ParagraphStyle("MonoX", parent=sBody, fontSize=6.5, leading=8, fontName="Courier")
        sTableCell = ParagraphStyle("CellX", parent=sBody, fontSize=6.5, leading=8, wordWrap="CJK")
        sTableHead = ParagraphStyle("HeadX", parent=sTableCell, textColor=colors.white, fontName="Helvetica-Bold", fontSize=7)

        def P(text, style):
            return Paragraph(text, style)

        def table_style():
            return TableStyle([
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0,0), (-1,0), colors.white),
                ("FONTSIZE", (0,0), (-1,-1), 6.5),
                ("LEADING", (0,0), (-1,-1), 8),
                ("ALIGN", (0,0), (-1,-1), "LEFT"),
                ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#d1d5db")),
                ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f3f4f6")]),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("LEFTPADDING", (0,0), (-1,-1), 4),
                ("RIGHTPADDING", (0,0), (-1,-1), 4),
                ("TOPPADDING", (0,0), (-1,-1), 3),
                ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ])

        def mk_table(headers, rows, col_widths=None):
            hdr = [P(f"<b>{esc(h)}</b>", sTableHead) for h in headers]
            data = [hdr]
            for r in rows:
                data.append([P(esc(c), sTableCell) for c in r])
            t = Table(data, colWidths=col_widths, repeatRows=1)
            t.setStyle(table_style())
            return t

        strong = [f for f in ai_findings if f.get("verdict") in ("confirmed", "likely")]
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        def footer(canvas, doc):
            canvas.saveState()
            canvas.setFont("Helvetica", 7)
            canvas.setFillColor(colors.HexColor("#6b7280"))
            canvas.drawCentredString(105*mm, 8*mm, f"Target: {cfg['target']}  |  Model: t-claude-sonnet-5  |  {now}")
            canvas.drawRightString(202*mm, 8*mm, f"{doc.page}")
            canvas.restoreState()

        doc = SimpleDocTemplate(out_path, pagesize=A4,
                                leftMargin=14*mm, rightMargin=14*mm, topMargin=14*mm, bottomMargin=16*mm,
                                title="Bug Bounty JS Analysis Report", author="JxAI",
                                subject=cfg["target"])
        story = []
        cw = doc.width
        story.append(P("Bug Bounty JS Analysis Report", sTitle))
        story.append(P(esc(cfg.get("target","")), sMono))
        story.append(Spacer(1, 1*mm))
        meta = [
            ["Generated", now],
            ["Model", "t-claude-sonnet-5"],
            ["JS files", str(len(js_files))],
            ["API paths discovered", str(len(paths))],
            ["Paths probed", str(len(results))],
            ["Out-of-scope skipped", str(len(skipped or []))],
            ["AI findings", str(len(strong))],
            ["Static flows", str(len(flows))],
            ["Auth gaps", str(len(gaps))],
        ]
        mv = [P(f"<b>{esc(k)}</b>", sBody) for k,_ in meta]
        story.append(Table([[mv, P("<br/>".join(esc(v) for _,v in meta), sBody)]],
                            colWidths=[38*mm, cw-38*mm]))
        story.append(Spacer(1, 4*mm))

        if summary:
            story.append(P("Executive Summary", sH1))
            clean = summary.replace("`", "").replace("**", "").replace("__", "")
            # drop leading markdown heading line if present (e.g. "## Executive Summary")
            lines = [l for l in clean.splitlines()]
            while lines and (lines[0].lstrip().startswith("#")
                             or lines[0].strip().lower().rstrip(":") in
                             ("executive summary", "summary")):
                lines.pop(0)
            clean = "\n".join(lines).strip()
            for para in clean.split("\n\n"):
                para = para.strip()
                if para:
                    story.append(P(esc(para), sBody))

        # Findings
        story.append(P("Unauthenticated Access Findings", sH1))
        if not strong:
            story.append(P("No confirmed or likely unauthenticated access findings.", sSmall))
        else:
            rows = [(f.get("verdict",""), str(f.get("confidence","")), f.get("url","")[:78], (f.get("next_step","") or "")[:70]) for f in sorted(strong, key=lambda x: -float(x.get("confidence",0) or 0))]
            story.append(mk_table(["Verdict","Conf.","Endpoint","Next step"], rows, col_widths=[18*mm, 12*mm, 65*mm, cw-95*mm]))
            for f in strong:
                story.append(Spacer(1, 2*mm))
                story.append(P(f"<b>{esc(f.get('url',''))}</b>", sH2))
                story.append(P(f"<b>Verdict:</b> {esc(f.get('verdict',''))}  ({esc(str(f.get('confidence','')))})", sBody))
                story.append(P(f"<b>Reasoning:</b> {esc(f.get('reasoning',''))}", sBody))
                story.append(P(f"<b>Next step:</b> {esc(f.get('next_step',''))}", sSmall))

        # Static flows
        story.append(P("Static Analysis Flows", sH1))
        if not flows:
            story.append(P("No source-to-sink flows reported by the deep audit.", sSmall))
        else:
            for f in sorted(flows, key=lambda x: SEV_ORDER.get(x.get("severity","Low"), 9)):
                story.append(P(f"[{esc(str(f.get('severity','?')))}] {esc(str(f.get('type', f.get('title','flow'))))} \u2014 {esc(str(f.get('confidence','?')))}", sH2))
                story.append(P(f"<b>Source:</b> {esc(_loc(f.get('source')))} &nbsp; <b>Sink:</b> {esc(_loc(f.get('sink')))}", sSmall))
                if f.get("reasoning"): story.append(P(esc(f["reasoning"]), sBody))
                ex = f.get("exploit") or f.get("exploit_sketch")
                if ex: story.append(P(f"<b>Exploit:</b> {esc(ex)}", sSmall))
                if f.get("remediation"): story.append(P(f"<b>Fix:</b> {esc(f['remediation'])}", sSmall))

        if gaps:
            story.append(P("Auth Gaps", sH1))
            rows = [(g.get("route","")[:55], (g.get("issue","") or "")[:80], str(g.get("confidence",""))) for g in gaps if isinstance(g, dict)]
            story.append(mk_table(["Route","Issue","Conf."], rows, col_widths=[45*mm, 100*mm, 20*mm]))

        deep_out = deep_out or {}
        for bucket, title in (("sources","Observed Sources"), ("sinks","Observed Sinks")):
            items = deep_out.get(bucket) or []
            if not items:
                continue
            story.append(P(title, sH1))
            def _sort_key(o):
                v = o.get("line", "?")
                return (0, int(v)) if isinstance(v, int) else (1, str(v))
            rows = [(str(o.get("line","?")),
                     (o.get("kind") or o.get("category") or "?")[:22],
                     (o.get("note") or o.get("detail") or o.get("details") or "")[:120])
                    for o in sorted(items, key=_sort_key)[:120]]
            story.append(mk_table(["Line","Kind","Note"], rows, col_widths=[18*mm, 40*mm, cw-58*mm]))
            if len(items) > 120:
                story.append(P(f"...and {len(items)-120} more {bucket} in report.md.", sSmall))

        story.append(P("Probe Results", sH1))
        if not results:
            story.append(P("No in-scope probes executed.", sSmall))
        else:
            t = []
            for r in results:
                for p in r.get("probes", []):
                    url = r.get("url","")
                    if len(url) > 62:
                        # keep host + path tail readable
                        sep = url.rfind("/", url.rfind("/")+1)
                        url = url[:12] + "..." + url[sep:62+sep] if sep > 12 else "..." + url[-62:]
                    t.append((url[:62], p.get("probe","?")[:12], str(p.get("status","ERR")), str(p.get("length","-"))[:10], "yes" if p.get("auth_wall") else "no"))
            story.append(mk_table(["URL","Probe","Status","Length","Auth wall"], t, col_widths=[70*mm, 22*mm, 15*mm, 18*mm, 18*mm]))

        story.append(P("Discovered Paths", sH1))
        rows = [(p.get("path","")[:42], ", ".join(p.get("methods",[])) or "GET", ", ".join(p.get("kinds",[]))[:22]) for p in paths]
        story.append(mk_table(["Path","Methods","Kinds"], rows, col_widths=[52*mm, 42*mm, cw-94*mm]))
        if skipped:
            story.append(P("Out-of-scope paths (not probed)", sH1))
            rows = [(u[:78], "") for u in skipped[:140]]
            story.append(mk_table(["Skipped URL",""], rows, col_widths=[cw-8*mm, 8*mm]))
            if len(skipped) > 140:
                story.append(P(esc(f"...and {len(skipped)-140} more."), sSmall))

        errs = errors or []
        if errs:
            story.append(P("Pipeline Errors", sH1))
            for e in errs:
                story.append(P(esc(e), sSmall))

        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return out_path
