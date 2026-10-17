"""Config assembly for a single scan.

A run's effective config is a three-layer merge: shipped defaults.yaml, the user's
~/.jxai/config.yaml (optional local overrides), then the target record itself.
Keeping the target's scope and per-stage overrides in the database means adding a
target never means hand-editing YAML.
"""
from __future__ import annotations

import copy
import os
import pathlib
from typing import Any, Dict, Iterable, Mapping

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULTS_PATH = pathlib.Path(__file__).resolve().parent / "defaults.yaml"
USER_CONFIG_PATH = pathlib.Path(
    os.environ.get("JXAI_CONFIG", pathlib.Path.home() / ".jxai" / "config.yaml")
)
DATA_DIR = pathlib.Path(os.environ.get("JXAI_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "jxai.db"

# Files a run writes. Skill output_dir is derived from these, so per-target
# isolation only depends on the run directory being unique.
RUN_FILES = ("report.md", "report.pdf", "report.html", "paths.json", "results.json", "deep_audit.json")


def _read_yaml(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge override into a copy of base; nested dicts merge, everything else replaces."""
    out = copy.deepcopy(dict(base))
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def base_config() -> Dict[str, Any]:
    """Defaults plus any machine-local overrides, minus the keys a target supplies."""
    cfg = deep_merge(_read_yaml(DEFAULTS_PATH), _read_yaml(USER_CONFIG_PATH))
    # A target always provides its own URL; never let a stale local file pin one.
    cfg.pop("target", None)
    cfg.pop("scope", None)
    return cfg


def normalize_scope(scope: Iterable[str] | None) -> list[str]:
    """Clean user-entered scope entries into host globs.

    A bare '*' is rejected: it would silently widen probing to the entire internet,
    which is never what someone meant and is dangerous in an active probe.
    """
    cleaned = []
    for entry in scope or []:
        host = str(entry).strip().lower()
        if not host or host == "*":
            continue
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        host = host.split("/")[0].split(":")[0]
        if host and host not in cleaned:
            cleaned.append(host)
    return cleaned


def build_config(target: Mapping[str, Any], run_dir: pathlib.Path) -> Dict[str, Any]:
    """Full pipeline config for one target, with all report paths inside run_dir.

    `target` is a row from the targets table: url, scope, plus a free-form
    overrides blob for per-target stage tuning.
    """
    cfg = base_config()
    cfg["target"] = target["url"].rstrip("/")

    scope = normalize_scope(target.get("scope") or [])
    # No explicit scope means "whatever host the target resolves to", which keeps
    # single-target onboarding to one field while still refusing to probe elsewhere.
    if not scope:
        from urllib.parse import urlparse
        host = urlparse(cfg["target"]).hostname
        scope = [host] if host else []
    cfg["scope"] = scope

    overrides = target.get("overrides") or {}
    if isinstance(overrides, str):
        overrides = _read_yaml_str(overrides)
    cfg = deep_merge(cfg, overrides or {})

    # Report destinations are derived, never taken from user input — a path
    # injected here would let a target record write outside its own run directory.
    run_dir = pathlib.Path(run_dir)
    cfg["report"] = deep_merge(
        cfg.get("report", {}),
        {
            "output": str(run_dir / "report.md"),
            "pdf": str(run_dir / "report.pdf"),
            "html": str(run_dir / "report.html"),
        },
    )
    return cfg


def _read_yaml_str(text: str) -> Dict[str, Any]:
    loaded = yaml.safe_load(text)
    return loaded if isinstance(loaded, dict) else {}


def load_file_config(path: str | os.PathLike[str]) -> Dict[str, Any]:
    """Config for a plain one-off CLI run against a config.yaml, as before."""
    cfg = deep_merge(base_config(), _read_yaml(pathlib.Path(path)))
    if not cfg.get("target"):
        raise ValueError(f"no target in {path}")
    cfg["target"] = cfg["target"].rstrip("/")
    if not cfg.get("scope"):
        from urllib.parse import urlparse
        host = urlparse(cfg["target"]).hostname
        cfg["scope"] = [host] if host else []
    return cfg