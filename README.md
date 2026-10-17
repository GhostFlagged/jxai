# jxai

Multi-target JavaScript security audit pipeline with a web app for managing
targets, running scans, and browsing results.

Built for bug-bounty triage: point it at a target, and it downloads and beautifies
its JS bundles, audits them for source→sink flows and auth gaps, harvests API
endpoints, probes them for unauthenticated access, and writes a Markdown/HTML/PDF
report.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env      # then set SINOX_API_KEY
```

`SINOX_API_KEY` is the only required secret. Everything else has a working
default.

## Usage

### CLI

```bash
jxai add acme "https://app.acme.com" --scope "acme.com,*.acme.com"
jxai list
jxai run acme          # scan one target
jxai run all           # scan every enabled target
jxai serve             # start the web app on :8000
```

Only a URL is required. Scope defaults to the target's own host, so a
single-host program needs nothing more.

### Web app

```bash
jxai serve --host 127.0.0.1 --port 8000
```

- **Targets** — add, edit, disable, delete. URL and scope are all a
  single-host program needs.
- **Scans** — kick off a run from the target page. Scans execute on a worker
  thread with a concurrency cap, and the run page polls for live per-stage
  progress. A full scan takes minutes; the page follows along.
- **Results** — findings, discovered paths, probe results, and a viewer over the
  downloaded bundles, plus the generated HTML and PDF reports.

Scan artifacts land under `data/runs/<target>-<id>/` and stay with the run.

## Configuration

Layered, later wins: shipped `defaults.yaml`, then `~/.jxai/config.yaml` (your
machine), then the target record itself.

Per-stage tuning lives on the target as a JSON `overrides` blob, so one target
that needs a deeper crawl doesn't change the pipeline for everything else:

```bash
jxai edit acme --overrides '{"js_download": {"max_files": 200}}'
```

Report output paths are derived from the run directory and cannot be redirected
by overrides — a target record can't make the pipeline write outside its own run.

## Scope and safety

- Probing is confined to the target's scope; out-of-scope endpoints are recorded
  and never requested.
- A bare `*` scope entry is rejected rather than silently probing the internet.
- A run that crashes is reconciled to `interrupted` at startup, not left
  "running" forever.
- This tool actively sends requests to the target. Use it only on systems you
  are authorized to test, and stay inside your program's scope.

## Secrets

`.env` is gitignored and only `.env.example` is committed. `data/`, `output/`,
and `archive/` are also ignored: they contain the SQLite registry plus each
target's source code and live probe responses, which is exactly the material
that should not be public.

## Development

```bash
pip install -e ".[dev]"
pytest
jxai serve --reload
```

## Layout

| Path | Purpose |
| --- | --- |
| `jxai/config.py` | Layered config assembly; derives per-run report paths |
| `jxai/store.py` | SQLite registry: targets, runs |
| `jxai/runner.py` | Run directories, concurrency-capped worker queue, pruning |
| `jxai/pipeline.py` | Stage ordering, progress events, findings export |
| `jxai/web.py` | FastAPI routes |
| `skills/` | The audit stages; each reads/writes a shared context dict |
| `ai/` | Sinox client, prompts, model routing |