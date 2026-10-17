import os, re, requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import jsbeautifier
from .base import Skill

JS_URL_PATTERN = re.compile(r"""["']([^"']+\.js(?:\?[^"']*)?)["']""")
BEAUTIFY_OPTS = jsbeautifier.default_options()
BEAUTIFY_OPTS.indent_size = 2
BEAUTIFY_OPTS.preserve_newlines = True
BEAUTIFY_OPTS.max_preserve_newlines = 2


class JsDownloadSkill(Skill):
    name = "js_download"

    def run(self, ctx):
        target = ctx["config"]["target"]
        cfg = ctx["config"]["js_download"]
        outdir = os.path.join(ctx["output_dir"], "js_files")
        os.makedirs(outdir, exist_ok=True)

        headers = {"User-Agent": cfg["user_agent"]}
        session = requests.Session()
        session.headers.update(headers)

        try:
            r = session.get(target, headers=headers, timeout=cfg["timeout"], verify=False)
        except Exception as e:
            return {"js_files": [], "js_errors": [f"homepage fetch failed: {e}"]}

        script_urls = set()

        # <script src>, plus string literals in inline scripts (dynamically injected files)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all("script", src=True):
            script_urls.add(urljoin(target, tag["src"]))
        for tag in soup.find_all("script"):
            if tag.string:
                for m in JS_URL_PATTERN.findall(tag.string):
                    script_urls.add(urljoin(target, m))

        # Bundlers that list their own assets
        for guess in ("/asset-manifest.json", "/manifest.json", "/vite-manifest.json"):
            try:
                mr = session.get(urljoin(target, guess), headers=headers, timeout=cfg["timeout"], verify=False)
                if mr.status_code == 200:
                    for m in re.findall(r'"([^"]+\.js)"', mr.text):
                        script_urls.add(urljoin(target, m))
            except Exception:
                pass

        downloaded = []
        errors = []
        max_bytes = cfg["max_size_kb"] * 1024

        for i, url in enumerate(sorted(script_urls)[: cfg["max_files"]]):
            if urlparse(url).scheme not in ("http", "https"):
                continue
            try:
                resp = session.get(url, headers=headers, timeout=cfg["timeout"], verify=False)
                if resp.status_code != 200:
                    errors.append(f"{url} -> {resp.status_code}")
                    continue
                if len(resp.content) > max_bytes:
                    errors.append(f"{url} -> too large ({len(resp.content)//1024}KB), skipped")
                    continue

                name = self._safe_name(url)
                path = os.path.join(outdir, f"{i:03d}_{name}")
                raw = resp.text
                try:
                    pretty = jsbeautifier.beautify(raw, BEAUTIFY_OPTS)
                except Exception:
                    pretty = raw  # beautifier chokes on some minified blobs
                with open(path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(pretty)

                downloaded.append({
                    "url": url,
                    "path": path,
                    "size": len(resp.content),
                    "pretty_size": len(pretty),
                    "lines": pretty.count("\n") + 1,
                })
            except Exception as e:
                errors.append(f"{url} -> {e}")

        return {"js_files": downloaded, "js_errors": errors}

    @staticmethod
    def _safe_name(url: str) -> str:
        """Basename, with query string folded in so cached-but-distinct files don't collide."""
        parsed = urlparse(url)
        name = os.path.basename(parsed.path) or "index.js"
        if parsed.query:
            digest = f"_{abs(hash(parsed.query)) & 0xffffffff:08x}"
            name = re.sub(r"\.js$", f"{digest}.js", name)
        return re.sub(r"[^a-zA-Z0-9._-]", "_", name) or "index.js"