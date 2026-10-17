#!/usr/bin/env python3
"""jxai CLI.

Subcommands:
  add / list / edit / remove   manage targets in the registry
  run [NAME|all]               run a scan (foreground, one target at a time)
  scan                         one-off run against a target URL, no registry entry
  serve                        start the web app
  prune                        delete old run directories
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

from jxai import store                          # noqa: E402
from jxai.config import DATA_DIR, base_config, load_file_config  # noqa: E402
from jxai.runner import prune_old_runs          # noqa: E402


def _need_key() -> None:
    if not os.environ.get("SINOX_API_KEY"):
        print("SINOX_API_KEY is not set. Export it or add it to .env.", file=sys.stderr)
        sys.exit(2)


def _as_id(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _resolve(name: str) -> dict | None:
    store.init_db()
    return store.get_target_by_name(name) or store.get_target(_as_id(name))


# --- commands --------------------------------------------------------------- #

def cmd_add(args) -> int:
    store.init_db()
    overrides = json.loads(args.overrides) if args.overrides else {}
    target = store.add_target(
        args.name, args.url,
        scope=[s.strip() for s in (args.scope or "").split(",") if s.strip()],
        overrides=overrides, notes=args.notes or "",
    )
    scope = ", ".join(target["scope"]) or "target host"
    print(f"added #{target['id']} {target['name']} -> {target['url']} (scope: {scope})")
    return 0


def cmd_list(args) -> int:
    store.init_db()
    targets = store.list_targets()
    if not targets:
        print("no targets yet. Add one with: jxai add NAME URL")
        return 0
    for t in targets:
        runs = store.list_runs(t["id"], limit=1)
        last = f"last: {runs[0]['status']}" if runs else "never run"
        flag = "" if t["enabled"] else " [disabled]"
        print(f"#{t['id']:<4} {t['name']:<22} {t['url']:<38} {last}{flag}")
    return 0


def cmd_edit(args) -> int:
    target = _resolve(args.name)
    if not target:
        print(f"no target named {args.name!r}", file=sys.stderr)
        return 1
    fields: dict = {}
    if args.url:
        fields["url"] = args.url
    if args.scope is not None:
        fields["scope"] = [s.strip() for s in args.scope.split(",") if s.strip()]
    if args.notes is not None:
        fields["notes"] = args.notes
    if args.overrides:
        fields["overrides"] = json.loads(args.overrides)
    if args.enable:
        fields["enabled"] = True
    if args.disable:
        fields["enabled"] = False
    if not fields:
        print("nothing to change; pass one of --url --scope --notes --overrides --enable --disable")
        return 1
    store.update_target(target["id"], **fields)
    print(f"updated {target['name']}")
    return 0


def cmd_remove(args) -> int:
    store.init_db()
    target = _resolve(args.name)
    if not target:
        print(f"no target named {args.name!r}", file=sys.stderr)
        return 1
    store.delete_target(target["id"])
    print(f"deleted {target['name']}")
    return 0


def cmd_run(args) -> int:
    _need_key()
    store.init_db()
    if not args.name or args.name == "all":
        targets = store.list_targets(enabled_only=True)
        if not targets:
            print("no enabled targets")
            return 1
    else:
        target = _resolve(args.name)
        if not target:
            print(f"no target named {args.name!r}", file=sys.stderr)
            return 1
        targets = [target]

    from jxai.runner import execute_run
    logging.getLogger("jxai").setLevel(logging.INFO)

    failures = 0
    for t in targets:
        if not t["enabled"]:
            print(f"skipping disabled target {t['name']}")
            continue
        try:
            result = execute_run(t["id"], t)
        except Exception as exc:
            print(f"scan of {t['name']} failed: {exc}", file=sys.stderr)
            failures += 1
            continue
        stats = result["stats"]
        print(f"\n{t['name']}: {result['status']} (run #{store.list_runs(t['id'], limit=1)[0]['id']})")
        print(f"  js files {stats['js_files']} | paths {stats['paths']} | probes {stats['probes']}"
              f" | findings {stats['findings']} ({stats['critical']} critical)")
        for stage in result["ctx"].get("stages", []):
            if stage["status"] in ("failed", "skipped"):
                print(f"  {stage['status']:8} {stage['stage']}")
        if stats.get("report"):
            print(f"  report    {stats['report']}")
        if result["status"] != "success":
            failures += 1
    return 1 if failures else 0


def cmd_scan(args) -> int:
    """One-off scan of a URL that is never written to the registry."""
    _need_key()
    from jxai.pipeline import run_pipeline, summarize

    if args.config:
        cfg = load_file_config(args.config)
    else:
        from urllib.parse import urlparse
        cfg = base_config()
        cfg["target"] = args.url.rstrip("/")
        if not cfg.get("scope"):
            host = urlparse(cfg["target"]).hostname
            cfg["scope"] = [host] if host else []

    out_dir = DATA_DIR / "adhoc"
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = run_pipeline(cfg, out_dir)
    stats = summarize(ctx)
    print(f"js={stats['js_files']} paths={stats['paths']} probes={stats['probes']} "
          f"findings={stats['findings']}")
    print(f"artifacts: {out_dir}")
    return 0 if not stats["errors"] else 1


def cmd_serve(args) -> int:
    import uvicorn
    uvicorn.run("jxai.web:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_prune(args) -> int:
    removed = prune_old_runs(args.keep)
    print(f"removed {removed} run director{'y' if removed == 1 else 'ies'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jxai", description="Multi-target JS security audit pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add a target")
    a.add_argument("name")
    a.add_argument("url")
    a.add_argument("--scope", help="comma-separated hosts or *.domain globs")
    a.add_argument("--notes")
    a.add_argument("--overrides", help="JSON object of per-stage config overrides")
    a.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="list targets")
    lst.set_defaults(func=cmd_list)

    e = sub.add_parser("edit", help="edit a target")
    e.add_argument("name")
    e.add_argument("--url")
    e.add_argument("--scope")
    e.add_argument("--notes")
    e.add_argument("--overrides")
    e.add_argument("--enable", action="store_true")
    e.add_argument("--disable", action="store_true")
    e.set_defaults(func=cmd_edit)

    d = sub.add_parser("remove", help="delete a target and its run history")
    d.add_argument("name")
    d.set_defaults(func=cmd_remove)

    r = sub.add_parser("run", help="run a scan for one target, or all enabled targets")
    r.add_argument("name", nargs="?", help="target name or id; omit or pass 'all' for every enabled target")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("scan", help="one-off scan of a URL, not saved to the registry")
    s.add_argument("url", nargs="?")
    s.add_argument("--config", help="legacy config.yaml to use instead of a URL argument")
    s.set_defaults(func=cmd_scan)

    v = sub.add_parser("serve", help="start the web app")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--reload", action="store_true")
    v.set_defaults(func=cmd_serve)

    pr = sub.add_parser("prune", help="delete run directories beyond the newest N")
    pr.add_argument("--keep", type=int, default=20)
    pr.set_defaults(func=cmd_prune)
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    args = build_parser().parse_args()
    try:
        code = args.func(args)
    except (ValueError, sqlite3.IntegrityError) as exc:
        # User-facing errors (bad URL, duplicate name) are not crashes; show them
        # as one line instead of a traceback.
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    sys.exit(code)


if __name__ == "__main__":
    main()