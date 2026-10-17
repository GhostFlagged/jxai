import json, requests, os, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from .base import Skill

requests.packages.urllib3.disable_warnings()

# What we put into {p}/{id}/:runId placeholders so the request hits real handler routing.
_PLACEHOLDER_VALUE = "1"

# Regex used in scope matching (mirrors the glob in js_analyze but here we enforce it).
_SKIP_METHODS = ("OPTIONS",)

def _in_scope(host: str, scope: list[str]) -> bool:
    """True if host matches any scope entry (exact or *.domain)."""
    if not scope:
        return True
    h = host.lower()
    for pat in scope:
        p = pat.strip().lstrip("*.").lower()
        if pat.strip().startswith("*."):
            if h == p or h.endswith("." + p):
                return True
        elif h == pat.strip().lower():
            return True
    return False


def _concrete(path: str) -> str:
    """Fill every placeholder so the probe hits real routing.

    Extractors emit several placeholder forms — `{p}`, `{id}`, `{tenantId}`,
    `{encodeURIComponent(id)}` (a template that did not resolve), and `:runId`.
    A generic brace replacement plus a `:param` fallback covers them all; leaving
    any unresolved produced probes against literal `{...}` URLs (405s).
    """
    p = re.sub(r"\{[^}]+\}", _PLACEHOLDER_VALUE, path)   # {p}, {id}, {encodeURIComponent(id)}, ...
    p = re.sub(r":\w+", _PLACEHOLDER_VALUE, p)            # :id, :runId
    return p


def _is_spa_shell(status: int, ctype: str, body: str) -> bool:
    """Heuristic: 200 text/html that is just the index page (SPA fallback), not API data."""
    if status != 200 or "text/html" not in (ctype or "").lower():
        return False
    # SPA index pages contain <!doctype / <html ... and are smallish
    low = body[:1200].lower()
    return ("<!doctype" in low or "<html" in low) and ("application/json" not in body.lower())


class AuthTestSkill(Skill):
    name = "auth_test"

    def __init__(self):
        self.session = requests.Session()

    def _test_one(self, url: str, method: str, cfg):
        # limit to the two probes; scope check happens in run() before we queue
        probes = [
            ("no-auth", {}),
            ("empty-bearer", {"Authorization": "Bearer "}),
        ]
        results = []
        for label, extra in probes:
            headers = dict(cfg.get("headers", {}))
            headers.update(extra)
            try:
                # For state-changing verbs we send an empty JSON body so the server
                # treats the request as well-formed for that verb.
                json_body = {} if method in ("POST", "PUT", "PATCH") else None
                kwargs = dict(headers=headers, timeout=cfg["timeout"],
                              verify=False, allow_redirects=False)
                if json_body is not None:
                    kwargs["json"] = json_body
                r = self.session.request(method, url, **kwargs)
                snippet_len = int(cfg.get("snippet_length", 1000))
                body = r.text[:snippet_len]
                results.append({
                    "probe": label,
                    "method": method,
                    "status": r.status_code,
                    "length": len(r.content),
                    "content_type": r.headers.get("Content-Type", ""),
                    "snippet": body,
                    "headers": {k.lower(): v for k, v in r.headers.items()},
                    "auth_wall": r.status_code in (401, 403, 407)
                                 or str(r.status_code) in ("401", "403")
                                 or "login" in (r.headers.get("Location") or "").lower(),
                    "spa_shell": _is_spa_shell(r.status_code, r.headers.get("Content-Type", ""), r.text),
                })
            except Exception as e:
                results.append({"probe": label, "method": method, "error": str(e)})
        return {"url": url, "method": method, "probes": results}

    def run(self, ctx):
        paths = ctx.get("paths", [])
        cfg = ctx["config"]["auth_test"]
        scope = ctx["config"].get("scope", [])
        skip_exts = tuple(cfg.get("skip_extensions", []))

        # Build url->methods map before deduping.
        seen = {}  # concrete_url -> {method, kind}
        to_probe = []
        skipped_out_of_scope = set()

        for p in paths:
            raw_url = p["url"]
            if raw_url.lower().endswith(skip_exts):
                continue
            # derive concrete url (fill {p}/{id}) and pick primary method from extractor
            raw_path = p["path"]
            method = (p.get("methods") or ["GET"])[0]
            concrete_path = _concrete(raw_path)
            # rebuild url with concrete path, reuse scheme+host from discovered url
            # If `path` is already an absolute URL (baseURL-joined entries), use it
            # verbatim. Re-prepending scheme://netloc to an absolute path produced
            # double-host URLs like app1.zmd-24.irhttps://app1.zmd-24.ir/api/...
            if concrete_path.startswith(("http://", "https://")):
                concrete_url = concrete_path
            else:
                parsed = urlparse(raw_url)
                if parsed.scheme and parsed.netloc:
                    concrete_url = f"{parsed.scheme}://{parsed.netloc}{concrete_path}"
                else:
                    concrete_url = concrete_path

            host = urlparse(concrete_url).hostname or ""
            if host and not _in_scope(host, scope):
                skipped_out_of_scope.add(concrete_url)
                continue
            if method in _SKIP_METHODS:
                continue
            if concrete_url not in seen:
                seen[concrete_url] = method
                to_probe.append((concrete_url, method))

        # Sort for stable output
        to_probe = sorted(set(to_probe))

        if not to_probe:
            return {"auth_results": [], "auth_skipped_out_of_scope": sorted(skipped_out_of_scope)}

        results = []
        with ThreadPoolExecutor(max_workers=int(cfg.get("concurrency", 6))) as ex:
            futs = {ex.submit(self._test_one, u, m, cfg): (u, m) for u, m in to_probe}
            for fut in as_completed(futs):
                results.append(fut.result())

        def sort_key(r):
            codes = [pr.get("status", 999) for pr in r["probes"] if "status" in pr]
            best = min(codes) if codes else 999
            # interesting first: 2xx without auth_wall, then everything else
            any_200_no_wall = any(pr.get("status") == 200 and not pr.get("auth_wall") and not pr.get("spa_shell")
                                  for pr in r["probes"])
            return (0 if any_200_no_wall else 1, 0 if best < 400 else 1, best, r["url"])
        results.sort(key=sort_key)

        import pathlib as _pl
        _pl.Path(ctx["output_dir"]).mkdir(parents=True, exist_ok=True)
        with open(os.path.join(ctx["output_dir"], "results.json"), "w") as f:
            json.dump(results, f, indent=2)

        return {"auth_results": results,
                "auth_skipped_out_of_scope": sorted(skipped_out_of_scope)}
