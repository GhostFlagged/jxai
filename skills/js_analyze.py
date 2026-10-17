import re, json, os
from .base import Skill

# --------------------------------------------------------------------------- #
# Endpoint extraction
#
# Bundles rarely store endpoints as `"/api/v1/users"` string literals. They store
# them as axios-relative templates behind a baseURL, called through a minified
# instance alias, or declared as route-config objects. Matching only literal
# /api-prefixed strings finds nothing on a real production bundle, so each
# strategy below targets one of those storage forms.
# --------------------------------------------------------------------------- #

# Libraries and docs that show up in absolute-URL scans but are not endpoints.
_URL_NOISE = (
    "w3.org", "reactjs.org", "reactrouter.com", "github.com", "npmjs.com",
    "schema.org", "localhost", "127.0.0.1", "example.com", "mozilla.org",
    "react.dev", "remix.run", "stackoverflow.com", "google.com",
)

# Quoted or templated string beginning with a slash. Captures the path prefix and
# stops at the first interpolation so `${encodeURIComponent(id)}` becomes a param.
_PATH_LITERAL = re.compile(r'(["\'`])((?:/[A-Za-z0-9_.\-]+)+/?(?:\$\{[^}]*\}|\$\{[^}]*\})?(?:/[A-Za-z0-9_.\-$\{\}]+)*/?)')

# `${...}` interpolation, normalized to a positional param.
_INTERP = re.compile(r"\$\{[^}]*\}")

# axios/fetch call sites: any receiver alias (axios, nt, api, client, ...).
_CALL_SITE = re.compile(
    r'\b(?:axios|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\.\s*'
    r'(?:get|post|put|delete|patch|head|options|request)\s*\(\s*(["\'`])'
)
_FETCH_SITE = re.compile(r'\bfetch\s*\(\s*(["\'`])')

# Route-config objects (React Router, Vue Router, Next file routes).
_ROUTE_FIELD = re.compile(r'\bpath\s*:\s*(["\'`])')

# baseURL assignment, either inline in axios.create or via a variable reference.
_BASEURL_INLINE = re.compile(r'baseURL\s*:\s*(["\'`])((?:[^"\'`])+?)\1')
_BASEURL_IDENT = re.compile(r'\bbaseURL\s*:\s*([A-Za-z_$][\w$]*)')

# _BASE_NAME (any `var x = "https://..."`) is deliberately removed: it matched
# React's own error-decoder helper (`var a = "https://reactjs.org/docs/..."`),
# which polluted all_base_roots with a third-party host and made every api path
# resolve to the wrong domain. Only baseURL: markers are trusted now.

_ALIAS_INSTANCE = re.compile(
    r'([A-Za-z_$][\w$]*)\s*=\s*(?:axios|[A-Za-z_$][\w$]*)\.create\s*\('
)


def _norm(path: str) -> str:
    """Normalize an extracted path: strip quotes, params, numeric IDs, dup slashes."""
    p = path.strip().strip("\"'`").strip()
    if not p or not p.startswith(("/", "http")):
        return ""
    p = _INTERP.sub("{p}", p)
    # deep_audit sometimes reports the template without the `$`: `{encodeURIComponent(id)}`.
    # Collapse any remaining brace expression to the same positional param so the two
    # forms dedupe to one canonical path.
    p = re.sub(r"\{[^}]*\}", "{p}", p)
    p = re.sub(r"/\d+(?=/|$)", "/{id}", p)
    if not p.startswith("http"):
        p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def _is_noise(url: str) -> bool:
    low = url.lower()
    return any(n in low for n in _URL_NOISE)


class JsAnalyzeSkill(Skill):
    """Deterministic endpoint harvest. Runs before any AI pass and feeds both the
    deep audit and the live prober, so it deliberately over-collects: every
    candidate is tagged with the strategy and alias that produced it so the
    report can show provenance.
    """

    name = "js_analyze"

    def run(self, ctx):
        cfg = ctx["config"]
        target = cfg["target"].rstrip("/")
        js_files = ctx.get("js_files", [])

        priority = ctx.get("ai_js_priority") or ctx.get("claude_js_priority")
        if not priority:
            priority = [f["path"] for f in js_files]
        known = {f["path"] for f in js_files}
        priority = [p for p in priority if p in known] or [f["path"] for f in js_files]

        found: dict[str, dict] = {}  # normalized url -> provenance

        def record(raw: str, source_file: str, kind: str, via: str, method: str = ""):
            norm = _norm(raw)
            if not norm or len(norm) < 2:
                return
            if norm.startswith("http") and _is_noise(norm):
                return
            entry = found.setdefault(norm, {"sources": set(), "kinds": set(), "vias": set(),
                                            "methods": set()})
            entry["sources"].add(os.path.basename(source_file))
            entry["kinds"].add(kind)
            entry["vias"].add(via)
            if method:
                entry["methods"].add(method.upper())

        all_base_roots = set()

        for path in priority:
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue

            # Resolve baseURL roots first so relative paths can be joined later.
            base_roots = set()
            for m in _BASEURL_INLINE.finditer(text):
                base_roots.add(m.group(2))
            for m in _BASEURL_IDENT.finditer(text):
                ident = re.escape(m.group(1))
                for v in re.finditer(rf'\b(?:var|let|const)?\s*{ident}\s*=\s*(["\'`])([^"\'`]+)\1', text):
                    base_roots.add(v.group(2))
            all_base_roots.update(base_roots)

            aliases = {m.group(1) for m in _ALIAS_INSTANCE.finditer(text)}

            # 1) Absolute URLs anywhere.
            for m in re.finditer(r'(["\'`])((?:https?|wss?)://[^"\'`\s]+)\1', text):
                record(m.group(2), path, "absolute", "literal")

            # 2) Call sites — the highest-signal strategy. Value must start with
            #    / or http, which also filters out object keys and header names.
            #    The HTTP verb is captured here too so auth_test can send a
            #    matching request instead of blindly GET-ing everything.
            for regex, via_kind in ((_CALL_SITE, "call"), (_FETCH_SITE, "fetch")):
                for m in regex.finditer(text):
                    tail = text[m.end(1):m.end(1) + 200]
                    lit = re.match(r'((?:/[A-Za-z0-9_.\-]+)+/?(?:\$\{[^}]*\}[A-Za-z0-9_.\-/]*)*)', tail)
                    if not lit:
                        continue
                    recv = m.group(0).split(".")[0].strip()
                    via = f"{recv}.call" if regex is _CALL_SITE else "fetch"
                    if via_kind == "call" and recv in aliases:
                        via = f"{recv}(axios.create instance)"
                    # method: pull the verb off the receiver if it's an axios-style call
                    method_m = re.search(r'\.(get|post|put|delete|patch|head|options)\s*\(', m.group(0))
                    method = method_m.group(1).upper() if method_m else "GET"
                    record(lit.group(1), path, "api", via, method=method)

            # 3) Route-config path fields.
            for m in _ROUTE_FIELD.finditer(text):
                tail = text[m.end(1):m.end(1) + 160]
                lit = re.match(r'(/[A-Za-z0-9_.\-:/*]*)', tail)
                if lit and lit.group(1) not in ("/",):
                    record(lit.group(1), path, "route", "route-config")

            # 4) Any quoted /path with at least one more segment — catches
            #    endpoints held in plain constants or config objects.
            for m in _PATH_LITERAL.finditer(text):
                raw = m.group(2)
                if raw.count("/") >= 2 and "${" in raw or raw.count("/") >= 3:
                    record(raw, path, "literal", "string-literal")


        # 5) After harvesting every file, join only API-provenance relative paths
        #    onto every discovered baseURL root — this mirrors axios's baseURL+url
        #    concat. Pure route-only paths (/dashboard, /login) are client routes
        #    that are handled separately against `target`, not the API baseURL.
        from urllib.parse import urljoin as _urljoin
        for norm in [n for n in list(found) if not n.startswith("http")]:
            rel_meta = found[norm]
            kinds = rel_meta.get("kinds") or set()
            # only API-class paths hit the API baseURL; route-only stays on the SPA host
            if not (kinds & {"api", "api-deep", "literal"}):
                continue
            for root in all_base_roots:
                if not root.startswith("http") or _is_noise(root):
                    continue
                joined = _urljoin(root.rstrip("/") + "/", norm.lstrip("/"))
                if not joined.startswith("http"):
                    continue
                entry = found.setdefault(joined, {"sources": set(), "kinds": set(), "vias": set(),
                                                  "methods": set()})
                entry["sources"].update(rel_meta["sources"] | {"joined"})
                entry["kinds"].update(rel_meta["kinds"] | {"api-joined"})
                entry["vias"].add(f"baseURL+{norm}")
                entry["methods"].update(rel_meta.get("methods") or {"GET"})

        # Merge structured api_surface from a prior deep-audit pass (authoritative).
        for ep in (ctx.get("deep_audit", {}) or {}).get("api_surface", []):
            raw = (ep.get("url_template") or ep.get("path") or "").strip()
            if not raw:
                continue
            method = str(ep.get("method", "")).upper()
            if raw.startswith("http"):
                record(raw, "deep_audit", "api-deep", f"{method} call site", method=method or "GET")
            elif raw.startswith("/"):
                record(raw, "deep_audit", "api-deep", f"{method} call site", method=method or "GET")

        paths = []
        api_root = next((r.rstrip("/") for r in all_base_roots if r.startswith("http")), None)

        # Drop relative duplicates: when a path was successfully joined onto an
        # absolute api_root, the absolute joined key is the canonical entry. Keeping
        # both produced duplicate rows for one endpoint and double-counted probes.
        if api_root:
            joined_abs = {n for n in found if n.startswith("http")}
            for norm in [n for n in list(found) if not n.startswith("http")]:
                cand = _urljoin(api_root + "/", norm.lstrip("/"))
                if cand in joined_abs:
                    del found[norm]
        for norm, meta in sorted(found.items()):
            if norm.startswith("http"):
                full = norm
            elif api_root and ("api" in meta["kinds"] or "api-deep" in meta["kinds"]
                               or "api-joined" in meta["kinds"]):
                full = _urljoin(api_root + "/", norm.lstrip("/"))
            else:
                full = target + norm
            paths.append({
                "path": norm,
                "url": full,
                "sources": sorted(meta["sources"]),
                "kinds": sorted(meta["kinds"]),
                "vias": sorted(meta["vias"]),
                "methods": sorted(meta.get("methods") or {"GET"}),
            })

        os.makedirs(ctx["output_dir"], exist_ok=True)
        with open(os.path.join(ctx["output_dir"], "paths.json"), "w") as f:
            json.dump(paths, f, indent=2)

        return {"paths": paths}