"""Dark-mode HTML report renderer.

A single self-contained file — no CDN, no external assets — so it opens offline in
any browser. Layout is a fixed sidebar nav plus a scrollable content pane. All data
is injected from the same context the markdown/PDF writers use, so the three outputs
never disagree.
"""

import html as _html
import json


def _h(v) -> str:
    return _html.escape(str(v if v is not None else ""))


def _loc(v) -> str:
    if isinstance(v, dict):
        f = v.get("file", "")
        short = f.rsplit("/", 1)[-1] if f else ""
        ln = v.get("line", "")
        if short or ln != "":
            return f"{short}:{ln}" if ln != "" else short
        return v.get("input") or v.get("detail") or v.get("details") or json.dumps(v)
    return str(v if v is not None else "?")


SEV_CLASS = {
    "Critical": "sev-critical",
    "High": "sev-high",
    "Medium": "sev-medium",
    "Low": "sev-low",
    "Informational": "sev-info",
}
SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Informational": 4}

_CSS = """
:root{
  --bg:#0b0f17; --panel:#121826; --panel2:#0f1523; --line:#1e293b;
  --fg:#e5e9f0; --muted:#8894ab; --dim:#5b6a85; --accent:#5b8cff; --accent2:#7c9cff;
  --crit:#ff5d6c; --high:#ff9f43; --med:#ffd166; --low:#4ecca3; --info:#7c9cff;
  --ok:#4ecca3; --warn:#ffd166; --bad:#ff5d6c;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
code,.mono{font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace;font-size:.86em}

.layout{display:flex;min-height:100vh}
/* ---------- sidebar ---------- */
.sidebar{width:248px;flex:0 0 248px;position:sticky;top:0;height:100vh;overflow-y:auto;
  background:linear-gradient(180deg,#0d1220,#0b0f17);border-right:1px solid var(--line);
  padding:22px 16px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.brand .dot{width:10px;height:10px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 12px var(--accent)}
.brand h1{font-size:15px;margin:0;letter-spacing:.3px}
.target{font-size:11px;color:var(--muted);word-break:break-all;margin:0 0 18px 20px}
.nav a{display:flex;justify-content:space-between;align-items:center;gap:8px;
  padding:8px 10px;border-radius:8px;color:var(--muted);font-size:13px;margin-bottom:2px;
  transition:.15s}
.nav a:hover{background:var(--panel);color:var(--fg)}
.nav a.active{background:var(--panel);color:var(--fg);box-shadow:inset 2px 0 0 var(--accent)}
.nav .count{font-size:11px;color:var(--dim);background:var(--panel2);
  border:1px solid var(--line);border-radius:20px;padding:1px 8px;min-width:24px;text-align:center}
/* ---------- content ---------- */
.content{flex:1;min-width:0;padding:32px 40px 80px;max-width:1180px}
header.page{margin-bottom:26px}
header.page h2{margin:0 0 4px;font-size:26px;letter-spacing:-.4px}
header.page .meta{color:var(--muted);font-size:12.5px}
.pill{display:inline-block;background:var(--panel);border:1px solid var(--line);
  border-radius:20px;padding:2px 10px;font-size:11.5px;color:var(--muted);margin-right:6px}
section{margin-bottom:44px;scroll-margin-top:20px}
section h3{font-size:17px;margin:0 0 4px;display:flex;align-items:center;gap:10px}
section h3 .bar{width:3px;height:18px;background:var(--accent);border-radius:2px}
section .sub{color:var(--muted);font-size:12.5px;margin:0 0 16px}
/* ---------- stat cards ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:8px}
.card{background:linear-gradient(160deg,var(--panel),var(--panel2));border:1px solid var(--line);
  border-radius:14px;padding:16px 18px}
.card .n{font-size:28px;font-weight:600;letter-spacing:-.5px}
.card .l{color:var(--muted);font-size:12px;margin-top:2px}
.card.hot .n{color:var(--high)}
.card.crit .n{color:var(--crit)}
/* ---------- flow cards ---------- */
.flow{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  margin-bottom:12px;overflow:hidden}
.flow summary{list-style:none;cursor:pointer;padding:14px 18px;display:flex;align-items:center;gap:12px}
.flow summary::-webkit-details-marker{display:none}
.flow summary:hover{background:var(--panel2)}
.badge{font-size:10.5px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;
  padding:3px 9px;border-radius:6px;white-space:nowrap}
.sev-critical{background:rgba(255,93,108,.16);color:var(--crit);border:1px solid rgba(255,93,108,.35)}
.sev-high{background:rgba(255,159,67,.16);color:var(--high);border:1px solid rgba(255,159,67,.35)}
.sev-medium{background:rgba(255,209,102,.14);color:var(--med);border:1px solid rgba(255,209,102,.32)}
.sev-low{background:rgba(78,204,163,.14);color:var(--low);border:1px solid rgba(78,204,163,.32)}
.sev-info{background:rgba(124,156,255,.14);color:var(--info);border:1px solid rgba(124,156,255,.32)}
.flow .title{flex:1;min-width:0;font-size:13.5px}
.flow .title .src{color:var(--muted)}
.conf{font-size:11px;color:var(--dim);background:var(--panel2);border:1px solid var(--line);
  border-radius:6px;padding:2px 8px;white-space:nowrap}
.flow .body{padding:0 18px 16px;border-top:1px solid var(--line);
  background:var(--panel2);color:#cdd6e6;font-size:13.5px}
.flow .body p{margin:12px 0}
.kv{display:grid;grid-template-columns:96px 1fr;gap:6px 14px;margin:12px 0}
.kv .k{color:var(--muted);font-size:12px;padding-top:1px}
.kv .v{font-size:13px;word-break:break-word}
.chain{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}
.chip{background:#0a0e18;border:1px solid var(--line);border-radius:8px;padding:6px 10px;
  font-size:12px;color:#cbd5e1}
.chain .arrow{color:var(--accent);font-weight:700}
/* ---------- tables ---------- */
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;background:#0d1320;color:var(--muted);font-weight:600;font-size:11px;
  text-transform:uppercase;letter-spacing:.5px;padding:10px 12px;position:sticky;top:0;
  border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
td{padding:9px 12px;border-bottom:1px solid #131c2c;vertical-align:top;word-break:break-word}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--panel2)}
.status{font-weight:600}
.st-2,.st-3{color:var(--warn)}
.st-4,.st-5{color:var(--bad)}
.st-1{color:var(--info)}
.wall-y{color:var(--bad)} .wall-n{color:var(--ok)}
.toolbar{display:flex;gap:10px;margin:0 0 12px;flex-wrap:wrap}
.toolbar input,.toolbar select{background:var(--panel);border:1px solid var(--line);
  color:var(--fg);border-radius:9px;padding:8px 12px;font-size:12.5px;outline:none;min-width:180px}
.toolbar input:focus,.toolbar select:focus{border-color:var(--accent)}
.empty{color:var(--dim);font-size:13px;padding:14px 0}
.note{color:#b9c4d6;font-size:13px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:16px 18px;margin:0}
.err{color:var(--high);font-size:12px;font-family:ui-monospace,monospace;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:6px}
@media(max-width:900px){
  .sidebar{display:none}
  .content{padding:20px}
}
"""

_JS = """
function filterTable(inputId, tableId){
  const q=(document.getElementById(inputId).value||'').toLowerCase();
  document.querySelectorAll('#'+tableId+' tbody tr').forEach(tr=>{
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}
function sortTable(tableId, col){
  const tb=document.querySelector('#'+tableId+' tbody');
  const rows=[...tb.rows];
  const asc = tb.dataset['sort'+col] !== 'asc';
  tb.dataset['sort'+col]= asc ? 'asc':'desc';
  rows.sort((a,b)=>{
    const x=a.cells[col].textContent.trim(), y=b.cells[col].textContent.trim();
    const nx=parseFloat(x), ny=parseFloat(y);
    let r = (!isNaN(nx)&&!isNaN(ny)) ? nx-ny : x.localeCompare(y);
    return asc?r:-r;
  });
  rows.forEach(r=>tb.appendChild(r));
}
document.addEventListener('DOMContentLoaded',()=>{
  const links=[...document.querySelectorAll('.nav a')];
  const secs=[...document.querySelectorAll('section')];
  const obs=new IntersectionObserver(es=>{
    es.forEach(e=>{
      if(e.isIntersecting){
        links.forEach(l=>l.classList.toggle('active', l.getAttribute('href')==='#'+e.target.id));
      }
    });
  },{rootMargin:'-15% 0px -75% 0px'});
  secs.forEach(s=>obs.observe(s));
});
"""


def _stat(n, label, cls=""):
    return f'<div class="card {cls}"><div class="n">{_h(n)}</div><div class="l">{_h(label)}</div></div>'


def _severity_badge(sev):
    sev = sev or "Low"
    return f'<span class="badge {SEV_CLASS.get(sev, "sev-low")}">{_h(sev)}</span>'


def render(ctx, summary="") -> str:
    cfg = ctx["config"]
    results = ctx.get("auth_results", []) or []
    paths = ctx.get("paths", []) or []
    js_files = ctx.get("js_files", []) or []
    deep_out = ctx.get("deep_audit") or {}
    flows = deep_out.get("flows") or []
    gaps = deep_out.get("auth_gaps") or []
    sources = deep_out.get("sources") or []
    sinks = deep_out.get("sinks") or []
    ai_findings = ctx.get("ai_findings", []) or []
    skipped = ctx.get("auth_skipped_out_of_scope", []) or []
    errors = ctx.get("errors", []) or []
    target = cfg.get("target", "")
    gen = _gen_time()

    strong = [f for f in ai_findings if f.get("verdict") in ("confirmed", "likely")]
    flows_sorted = sorted(flows, key=lambda x: SEV_ORDER.get(x.get("severity", "Low"), 9))

    sev_counts = {}
    for f in flows_sorted:
        sev_counts[f.get("severity", "Low")] = sev_counts.get(f.get("severity", "Low"), 0) + 1
    top = next((s for s in ("Critical", "High", "Medium", "Low") if sev_counts.get(s)), "Low")

    nav = [
        ("overview", "Overview", ""),
        ("unauth", "Unauth Findings", len(strong)),
        ("flows", "Static Flows", len(flows)),
        ("gaps", "Auth Gaps", len(gaps)),
        ("probes", "Probe Results", len(results)),
        ("sources", "Sources", len(sources)),
        ("sinks", "Sinks", len(sinks)),
        ("paths", "Discovered Paths", len(paths)),
        ("errors", "Errors", len(errors)),
    ]
    nav_html = "".join(
        f'<a href="#{sid}"><span>{_h(label)}</span>'
        + (f'<span class="count">{n}</span>' if isnum else "")
        + "</a>"
        for sid, label, n in nav
        for isnum in [isinstance(n, int)]
    )

    # ---------- overview ----------
    ov = ["<section id='overview'>",
          "<h3><span class='bar'></span>Overview</h3>",
          "<p class='sub'>Pre-submission triage of the client bundle: static source-to-sink "
          "flows plus live unauthenticated probes.</p>",
          "<div class='stats'>",
          _stat(len(paths), "API paths"),
          _stat(len(flows), "Static flows"),
          _stat(len(results), "Probed URLs"),
          _stat(len(sources), "Sources"),
          _stat(len(sinks), "Sinks"),
          _stat(len(gaps), "Auth gaps"),
          "</div>"]
    if summary:
        clean = "\n".join(
            l for l in summary.splitlines()
            if not l.strip().startswith("#")
            and l.replace("*", "").replace("_", "").strip().lower().rstrip(":") not in ("executive summary", "summary")
        ).replace("**", "").replace("`", "").strip()
        ov.append(f"<p class='note' style='margin-top:18px'>{_h(clean)}</p>")
    ov.append("</section>")

    # ---------- unauth findings ----------
    ua = ["<section id='unauth'>", "<h3><span class='bar'></span>Unauthenticated Access Findings</h3>"]
    if not strong:
        ua.append("<p class='empty'>No confirmed or likely unauthenticated access findings.</p>")
    else:
        ua.append(_probe_findings_table(strong))
    ua.append("</section>")

    # ---------- flows ----------
    fl = ["<section id='flows'>", "<h3><span class='bar'></span>Static Analysis Flows</h3>",
          f"<p class='sub'>{len(flows_sorted)} source-to-sink flows recovered from the bundle, "
          "ranked by severity.</p>"]
    if not flows_sorted:
        fl.append("<p class='empty'>Deep audit returned no source-to-sink flows.</p>")
    else:
        for f in flows_sorted:
            fl.append(_flow_card(f))
    fl.append("</section>")

    # ---------- gaps ----------
    gp = ["<section id='gaps'>", "<h3><span class='bar'></span>Auth Gaps</h3>",
          "<p class='sub'>Endpoints or routes with no visible client-side guard, or presence-only checks.</p>"]
    if gaps:
        rows = ""
        for g in gaps:
            if not isinstance(g, dict):
                continue
            rows += (f"<tr><td class='mono'>{_h(g.get('route', g.get('id', '-')))}</td>"
                     f"<td>{_h(g.get('issue', ''))}</td>"
                     f"<td>{_h(g.get('confidence', ''))}</td></tr>")
        gp.append(f"<div class='tablewrap'><table id='tbl-gaps'><thead><tr>"
                  f"<th onclick=\"sortTable('tbl-gaps',0)\">Route</th>"
                  f"<th onclick=\"sortTable('tbl-gaps',1)\">Issue</th>"
                  f"<th onclick=\"sortTable('tbl-gaps',2)\">Confidence</th>"
                  f"</tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        gp.append("<p class='empty'>No auth gaps reported.</p>")
    gp.append("</section>")

    # ---------- probes ----------
    pr = ["<section id='probes'>", "<h3><span class='bar'></span>Probe Results</h3>",
          f"<p class='sub'>{len(results)} in-scope URLs probed with no-auth and empty-bearer requests.</p>"]
    if results:
        pr.append("<div class='toolbar'>"
                  "<input id='probe-q' placeholder='Filter by URL / status…' oninput=\"filterTable('probe-q','tbl-probes')\">"
                  "</div>")
        rows = ""
        for r in results:
            url = r.get("url", "")
            for p in r.get("probes", []):
                st = str(p.get("status", "ERR"))
                wall = "yes" if p.get("auth_wall") else "no"
                rows += (f"<tr><td class='mono'>{_h(url)}</td>"
                         f"<td>{_h(p.get('probe', '?'))}</td>"
                         f"<td class='status st-{_h(st[:1])}'>{_h(st)}</td>"
                         f"<td>{_h(p.get('length', '-'))}</td>"
                         f"<td class='wall-{'y' if wall == 'yes' else 'n'}'>{wall}</td></tr>")
        pr.append(f"<div class='tablewrap'><table id='tbl-probes'><thead><tr>"
                  f"<th onclick=\"sortTable('tbl-probes',0)\">URL</th><th>Probe</th>"
                  f"<th onclick=\"sortTable('tbl-probes',2)\">Status</th>"
                  f"<th onclick=\"sortTable('tbl-probes',3)\">Length</th><th>Auth wall</th>"
                  f"</tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        pr.append("<p class='empty'>No probes executed.</p>")
    pr.append("</section>")

    # ---------- sources / sinks ----------
    def inv(sid, title, items, note):
        out = [f"<section id='{sid}'>", f"<h3><span class='bar'></span>{_h(title)}</h3>",
               f"<p class='sub'>{_h(note)}</p>"]
        if not items:
            out.append("<p class='empty'>None collected.</p>")
            out.append("</section>")
            return out
        out.append(f"<div class='toolbar'><input id='{sid}-q' placeholder='Filter…' "
                   f"oninput=\"filterTable('{sid}-q','tbl-{sid}')\"></div>")
        rows = ""
        for o in sorted(items, key=_line_sort):
            note_txt = (o.get("note") or o.get("detail") or o.get("details") or "")
            rows += (f"<tr><td class='mono'>{_h(o.get('line', '?'))}</td>"
                     f"<td class='mono'>{_h(o.get('kind') or o.get('category') or '?')}</td>"
                     f"<td>{_h(str(note_txt)[:400])}</td></tr>")
        out.append(f"<div class='tablewrap'><table id='tbl-{sid}'><thead><tr>"
                   f"<th onclick=\"sortTable('tbl-{sid}',0)\">Line</th><th>Kind</th><th>Note</th>"
                   f"</tr></thead><tbody>{rows}</tbody></table></div>")
        out.append("</section>")
        return out

    src_sec = inv("sources", "Observed Sources", sources,
                  "User-controllable inputs found in the bundle.")
    snk_sec = inv("sinks", "Observed Sinks", sinks,
                  "Dangerous consumers — DOM, navigation, storage, request, and auth sinks.")

    # ---------- paths ----------
    pa = ["<section id='paths'>", "<h3><span class='bar'></span>Discovered Paths</h3>",
          f"<p class='sub'>{len(paths)} endpoints and client routes harvested from the bundle.</p>",
          "<div class='toolbar'><input id='path-q' placeholder='Filter by path / host…' "
          "oninput=\"filterTable('path-q','tbl-paths')\"></div>"]
    rows = ""
    for p in paths:
        rows += (f"<tr><td class='mono'>{_h(p.get('path', ''))}</td>"
                 f"<td class='mono'>{_h(', '.join(p.get('methods', [])) or 'GET')}</td>"
                 f"<td>{_h(', '.join(p.get('kinds', [])))}</td>"
                 f"<td>{_h(', '.join(p.get('vias', [])))}</td></tr>")
    pa.append(f"<div class='tablewrap'><table id='tbl-paths'><thead><tr>"
              f"<th onclick=\"sortTable('tbl-paths',0)\">Path</th>"
              f"<th onclick=\"sortTable('tbl-paths',1)\">Methods</th><th>Kinds</th><th>Via</th>"
              f"</tr></thead><tbody>{rows}</tbody></table></div>")
    if skipped:
        pa.append(f"<p class='sub' style='margin-top:16px'>{len(skipped)} out-of-scope URLs skipped "
                  "(not probed).</p>")
    pa.append("</section>")

    err_sec = ["<section id='errors'>", "<h3><span class='bar'></span>Pipeline Errors</h3>"]
    if errors:
        for e in errors:
            err_sec.append(f"<div class='err'>{_h(e)}</div>")
    else:
        err_sec.append("<p class='empty'>None.</p>")
    err_sec.append("</section>")

    top_badge = _severity_badge(top) if flows else ""
    body = "".join(ov + ua + fl + gp + pr + src_sec + snk_sec + pa + err_sec)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bug Bounty JS Analysis — {_h(target)}</title>
<style>{_CSS}</style></head>
<body><div class="layout">
<aside class="sidebar">
  <div class="brand"><span class="dot"></span><h1>JxAI Analysis</h1></div>
  <p class="target">{_h(target)}</p>
  <nav class="nav">{nav_html}</nav>
  <p class="target" style="margin-top:22px">Generated<br>{_h(gen)}<br>Model t-</p>
</aside>
<main class="content">
  <header class="page">
    <h2>Bug Bounty JS Analysis Report</h2>
    <div class="meta">
      <span class="pill">{_h(target)}</span>
      <span class="pill">{len(js_files)} JS file(s)</span>
      <span class="pill">{len(paths)} paths</span>
      <span class="pill">{len(flows)} flows</span>
      {('' if not flows else "<span class='pill'>highest: " + _h(top) + "</span>")}
    </div>
  </header>
  {body}
</main>
</div><script>{_JS}</script></body></html>"""


def _line_sort(o):
    v = o.get("line", "?")
    return (0, int(v)) if isinstance(v, int) else (1, str(v))


def _flow_card(f):
    sev = f.get("severity", "Low")
    src = _loc(f.get("source"))
    snk = _loc(f.get("sink"))
    # flows often carry only source/sink; the pair is the most descriptive title
    title = f.get("type") or f.get("title") or f"{src} → {snk}"
    conf = f.get("confidence", "?")
    chain = (f"<div class='chain'><span class='chip'>{_h(src)}</span>"
             f"<span class='arrow'>&#8594;</span><span class='chip'>{_h(snk)}</span></div>")
    body = [f"<div class='kv'><div class='k'>Source</div><div class='v mono'>{_h(src)}</div>",
            f"<div class='k'>Sink</div><div class='v mono'>{_h(snk)}</div>",
            f"<div class='k'>Confidence</div><div class='v'>{_h(conf)}</div></div>"]
    if f.get("reasoning"):
        body.append(f"<p>{_h(f['reasoning'])}</p>")
    ex = f.get("exploit") or f.get("exploit_sketch")
    if ex:
        body.append(f"<p><b style='color:var(--high)'>Exploit sketch.</b> {_h(ex)}</p>")
    if f.get("remediation"):
        body.append(f"<p><b style='color:var(--ok)'>Remediation.</b> {_h(f['remediation'])}</p>")
    has_named_type = bool(f.get("type") or f.get("title"))
    subtitle = f" <span class='src'>· {_h(src)}</span>" if has_named_type else ""
    return (f"<details class='flow'><summary>{_severity_badge(sev)}"
            f"<span class='title'>{_h(title)}{subtitle}</span>"
            f"<span class='conf'>{_h(conf)}</span></summary>"
            f"<div class='body'>{chain}{''.join(body)}</div></details>")


def _probe_findings_table(strong):
    rows = ""
    for f in sorted(strong, key=lambda x: -float(x.get("confidence", 0) or 0)):
        rows += (f"<tr><td class='status'>{_h(f.get('verdict', ''))}</td>"
                 f"<td>{_h(f.get('confidence', ''))}</td>"
                 f"<td class='mono'>{_h(f.get('url', ''))}</td>"
                 f"<td>{_h(f.get('reasoning', ''))}</td>"
                 f"<td>{_h(f.get('next_step', ''))}</td></tr>")
    return (f"<div class='tablewrap'><table id='tbl-unauth'><thead><tr>"
            f"<th>Verdict</th><th>Conf.</th><th>Endpoint</th><th>Reasoning</th><th>Next step</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>")


def _gen_time() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
