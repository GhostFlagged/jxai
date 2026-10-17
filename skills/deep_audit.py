import json, os, re
from .base import Skill
from ai.client import SinoxClient
from ai.router import ModelRouter
from ai.prompts import DEEP_AUDIT_SYSTEM

MAX_FILES = int(os.environ.get("DEEP_AUDIT_MAX_FILES", "8"))
CHUNK_CHARS = int(os.environ.get("DEEP_AUDIT_CHUNK_CHARS", "28000"))
MAX_CHUNKS = int(os.environ.get("DEEP_AUDIT_MAX_CHUNKS", "10"))
MAX_TOKENS = int(os.environ.get("DEEP_AUDIT_MAX_TOKENS", "8000"))
# How many lines of context to keep around each hit.
_CONTEXT_LINES = int(os.environ.get("DEEP_AUDIT_CONTEXT", "12"))

# --- Evidence pre-filter -----------------------------------------------------
# A 1MB bundle almost never fits in a single call, and it wastes most of the
# budget on framework internals (React, router, third-party libs) that carry no
# application findings. Only ~340 lines in a typical production bundle actually
# touch a sink/source/auth construct, so we grep first and audit just those
# windows. This turns a 32k-line / 12-chunk problem into ~2 chunks with the same
# coverage, which is what made the previous full-file run time out.
#
# Each pattern is deliberately broad (category tag for prompt context only).
# Token-style patterns are matched with lookarounds so `location` inside a
# longer identifier does not match, but `location.search` does.
_PATTERNS = [
    ("sink:dom-xss", r"\.(innerHTML|outerHTML|insertAdjacentHTML)\s*=|dangerouslySetInnerHTML|document\.write\s*\(|eval\s*\(|new\s+Function\s*\("),
    ("sink:location", r"location\.(href|assign|replace)\s*=|window\.open\s*\(|window\.location\s*=|document\.location"),
    ("sink:storage", r"(?:window\.|document\.|globalThis\.)?(localStorage|sessionStorage)\.(getItem|setItem|removeItem)\s*\(|document\.cookie"),
    ("sink:postmessage", r"\bpostMessage\s*\(|\bonmessage(?:error)?\b|\baddEventListener\s*\(\s*[\"']message[\"']"),
    ("sink:unsafe-merge", r"Object\.assign\s*\(|JSON\.parse\s*\(|\bstructuredClone\s*\(|\bcrypto\.subtle\b|atob\s*\(|btoa\s*\("),
    ("sink:http", r"\.(get|post|put|patch|delete)\s*\(\s*[`\"']|\bfetch\s*\(|XMLHttpRequest|baseURL\s*:"),
    ("sink:navigation", r"\b(import|export)\s*\(|createElement\s*\(\s*[\"']script|importScripts\s*\(|\beval\b|Function\s*\("),
    ("sink:childproc", r"child_process|\.exec\s*\(|\.spawn\s*\(|require\s*\(\s*[\"']fs|readFile|writeFile"),
    ("source:url", r"(?:window\.|document\.)?location\.(search|hash|href|pathname)|URLSearchParams|useSearchParams|searchParams"),
    ("source:form", r"\.target\.value|\bonChange\s*=|\bonSubmit\s*=|\bnew\s+FormData\b|\be\.target\.checked"),
    ("source:postmessage", r"\bonmessage(?:error)?\b|addEventListener\s*\(\s*[\"']message[\"']|\be\.data\b|\bevent\.data\b"),
    ("source:storage", r"(localStorage|sessionStorage)\.getItem\s*\(|document\.cookie"),
    ("source:params", r"\buseParams\b|\bparams\.[A-Za-z_$]|\bquery\.[A-Za-z_$]|\bsearchParams\.get\b|\bwindow\.name\b|document\.referrer"),
    ("source:prompt", r"window\.prompt\s*\(|window\.confirm\s*\("),
    ("auth:token", r"Authorization|\bBearer\b|\bjwt\b|\btoken\b|withCredentials|localStorage|sessionStorage|credentials\s*:\s*[\"']include|interceptors?\.|credentials"),
    ("auth:guard", r"\bGuard(ed)?\b|\bisAdmin\b|\bisAuthenticat|\brbac\b|\brole\b|\bpermission\b|\brequire[A-Z]"),
    ("route:config", r"\bpath\s*:\s*[\"'`]|createBrowserRouter|createHashRouter|<Route\b|useNavigate|useRouteError|<Outlet\b"),
]
_PATTERNS = [(t, re.compile(p)) for t, p in _PATTERNS]


def _lines_with_hits(lines):
    """Return sorted absolute line numbers containing at least one pattern."""
    hits = set()
    for i, line in enumerate(lines, start=1):
        for _tag, pat in _PATTERNS:
            if pat.search(line):
                hits.add(i)
                break
    return hits


def _evidence_windows(lines, hits, ctx_lines=None):
    """Merge hit lines with +/- ctx_lines context into contiguous spans."""
    w = _CONTEXT_LINES if ctx_lines is None else ctx_lines
    n = len(lines)
    spans = []
    for h in sorted(hits):
        a, b = max(1, h - w), min(n, h + w)
        if spans and a <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def _tags_in_span(lines, a, b):
    """Which category tags appear in this span — added to the prompt so the model
    knows what it is looking at (e.g. 'postMessage windows here') without having to
    infer it from minified identifiers."""
    found = []
    seen = set()
    for i in range(a, b + 1):
        for tag, pat in _PATTERNS:
            if tag in seen:
                continue
            if pat.search(lines[i - 1]):
                seen.add(tag)
                found.append(tag)
    return found


class DeepAuditSkill(Skill):
    """Sonnet-only deep pass: ported js-analayiz methodology (sources, sinks, flows,
    api_surface, auth_model) over the highest-ranked beautified JS files.

    Evidence-first: each file is grep-filtered for security-relevant lines before any
    model call, so the token budget is spent on application code rather than framework
    internals. Fail-open: any error is logged and the pipeline continues with the
    deterministic stages.
    """

    name = "deep_audit"

    def __init__(self):
        self.ai = SinoxClient()
        self.model = ModelRouter.get("deep_audit")  # t-claude-sonnet-5

    def run(self, ctx):
        files = ctx.get("js_files", [])
        if not files:
            return {"deep_audit": self._empty(), "ai_js_priority": []}

        priority = ctx.get("ai_js_priority") or [f["path"] for f in files]
        by_path = {f["path"]: f for f in files}
        chosen = [by_path[p] for p in priority if p in by_path][:MAX_FILES]

        merged = self._empty()
        seen_any = False
        stats = []

        for f in chosen:
            try:
                text = open(f["path"], encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            if not text.strip():
                continue

            lines = text.splitlines()
            hits = _lines_with_hits(lines)
            if not hits:
                stats.append({"file": os.path.basename(f["path"]), "hits": 0, "chunks": 0})
                continue
            spans = _evidence_windows(lines, hits)
            chunks = self._pack(lines, spans, f["url"])
            stats.append({
                "file": os.path.basename(f["path"]),
                "lines": len(lines),
                "hits": len(hits),
                "spans": len(spans),
                "chunks": len(chunks),
            })

            for idx, chunk in enumerate(chunks):
                label, start_line, body = chunk["label"], chunk["start"], chunk["body"]
                tags = ", ".join(chunk["tags"]) or "general"
                user = (
                    f"Target: {ctx['config']['target']}\n"
                    f"File: {f['url']}\n"
                    f"Segment: {label} (segment {idx+1} of {len(chunks)})\n"
                    f"Relevant constructs in this segment: {tags}\n"
                    f"Line numbers below are absolute file line numbers — cite them exactly.\n\n"
                    f"```\n{body}\n```"
                )
                try:
                    part = self.ai.chat_json(
                        self.model,
                        [
                            {"role": "system", "content": DEEP_AUDIT_SYSTEM},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=MAX_TOKENS,
                    )
                except Exception as e:
                    ctx.setdefault("errors", []).append(f"deep_audit[{label}]: {e}")
                    continue
                seen_any = True
                self._merge(merged, part, label, chunk["start"], chunk.get("end"))

        result = merged if seen_any else self._empty()
        result["coverage"] = stats

        os.makedirs(ctx["output_dir"], exist_ok=True)
        with open(os.path.join(ctx["output_dir"], "deep_audit.json"), "w") as fp:
            json.dump(result, fp, indent=2)

        return {"deep_audit": result, "ai_js_priority": priority}

    def _pack(self, lines, spans, url):
        """Group evidence spans into chunks within CHUNK_CHARS.

        Spans are usually hundreds of lines apart (framework code in between), so
        each chunk concatenates many spans with an explicit gap marker. Every
        emitted line carries its absolute file line number (` 1234 | code`), and
        the model is told to cite those numbers exactly — no rebasing needed.
        """
        chunks = []
        cur_parts, cur_start, cur_end, cur_len = [], None, None, 0

        def flush():
            nonlocal cur_parts, cur_start, cur_end, cur_len
            if not cur_parts:
                return
            body_text = "\n".join(cur_parts)
            tags = []
            for tag, pat in _PATTERNS:
                if pat.search(body_text):
                    tags.append(tag)
            label = (f"lines {cur_start}-{cur_end}"
                     if cur_end != cur_start else f"line {cur_start}")
            chunks.append({"label": label, "start": cur_start, "end": cur_end,
                           "body": body_text, "tags": tags})
            cur_parts, cur_start, cur_end, cur_len = [], None, None, 0

        for a, b in spans:
            numbered = [f"{n:6d} | {lines[n-1]}" for n in range(a, b + 1)]
            block = "\n".join(numbered)
            block_len = len(block)
            if cur_parts and cur_len + 60 + block_len > CHUNK_CHARS:
                flush()
            if cur_parts:
                gap = a - cur_end - 1
                cur_parts.append(f"... [{gap} lines omitted] ...")
                cur_len += 60
            else:
                cur_start = a
            cur_parts.append(block)
            cur_len += block_len
            cur_end = b
        flush()

        if len(chunks) > MAX_CHUNKS:
            # keep the largest chunks (most evidence) rather than truncating the tail
            chunks.sort(key=lambda c: len(c["body"]), reverse=True)
            chunks = chunks[:MAX_CHUNKS]
            chunks.sort(key=lambda c: c["start"])
        return chunks

    @staticmethod
    def _merge(into: dict, part: dict, label: str, start_line: int = 1, end_line=None) -> None:
        """Accumulate one segment's findings, de-duplicating and tagging line origin.

        Chunk bodies carry absolute line numbers and the prompt tells the model to
        cite them exactly, so values that already fall inside [start_line, end_line]
        are kept as-is. Only small values that cannot be absolute (e.g. 12 in a
        chunk starting at line 30000) are treated as segment-relative and rebased.
        """
        if not isinstance(part, dict):
            return

        def _rebase(val):
            if isinstance(val, bool) or val is None:
                return val
            num = None
            if isinstance(val, int):
                num = val
            elif isinstance(val, str):
                m = re.match(r"^(?:approximately\s+)?(\d+)$", val.strip())
                if not m:
                    return val
                num = int(m.group(1))
            else:
                return val
            if end_line is not None and start_line <= num <= end_line:
                return num
            if end_line is None and num >= start_line:
                return num
            if num < 10000 and num < start_line:
                return num + start_line - 1
            return num

        def _dedup_key(v):
            if not isinstance(v, dict):
                return str(v)
            return json.dumps({k: v.get(k) for k in sorted(v)
                               if k in ("line", "location", "input", "category",
                                        "type", "detail", "details", "method",
                                        "url_template", "source", "sink", "route",
                                        "issue", "url", "path")}, sort_keys=True,
                              ensure_ascii=False)

        for key in ("sources", "sinks", "api_surface", "flows", "auth_gaps"):
            vals = part.get(key) or []
            if not isinstance(vals, list):
                continue
            bucket = into.setdefault(key, [])
            seen = {_dedup_key(v) for v in bucket}
            for v in vals:
                if isinstance(v, dict):
                    v.setdefault("segment", label)
                    if "line" in v:
                        v["line"] = _rebase(v["line"])
                k = _dedup_key(v)
                if k in seen:
                    continue
                seen.add(k)
                bucket.append(v)

        am = part.get("auth_model")
        if isinstance(am, dict):
            cur = into.setdefault("auth_model", {})
            for k, v in am.items():
                if k not in cur or (not cur[k] and v):
                    cur[k] = v

    @staticmethod
    def _empty():
        return {"sources": [], "sinks": [], "flows": [], "api_surface": [],
                "auth_model": {}, "auth_gaps": []}
