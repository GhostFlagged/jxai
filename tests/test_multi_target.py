"""Tests for the multi-target refactor.

The regressions that matter here are path and scope bugs: two targets sharing an
output directory, a run reading a file outside its own directory, or a probe
escaping the declared scope.
"""
import json
import pathlib

import pytest

from jxai import config, store
from jxai.runner import run_dir_for, slugify


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jxai.db")
    store.DB_PATH = tmp_path / "jxai.db"
    store.init_db()
    yield
    store.DB_PATH = config.DB_PATH


# --- config ----------------------------------------------------------------- #

def test_scope_normalization_strips_urls_and_ports():
    assert config.normalize_scope(["https://A.com/x", "b.com:8443", " * ", ""]) == ["a.com", "b.com"]


def test_scope_defaults_to_target_host(tmp_db):
    t = store.add_target("x", "https://app.example.org/")
    cfg = config.build_config(t, pathlib.Path("/tmp/run"))
    assert cfg["scope"] == ["app.example.org"]
    assert cfg["target"] == "https://app.example.org"


def test_report_paths_are_confined_to_run_dir(tmp_db):
    t = store.add_target("x", "https://example.com")
    cfg = config.build_config(t, pathlib.Path("/tmp/run7"))
    for key in ("output", "pdf", "html"):
        assert cfg["report"][key].startswith("/tmp/run7/")


def test_overrides_cannot_escape_the_run_dir(tmp_db):
    """A path injected via overrides must not redirect writes outside the run."""
    t = store.add_target("x", "https://example.com",
                         overrides={"report": {"output": "/etc/passwd"}})
    cfg = config.build_config(t, pathlib.Path("/tmp/run8"))
    assert cfg["report"]["output"] == "/tmp/run8/report.md"


def test_overrides_merge_rather_than_replace(tmp_db):
    t = store.add_target("x", "https://example.com",
                         overrides={"js_download": {"max_files": 200}})
    cfg = config.build_config(t, pathlib.Path("/tmp/run9"))
    assert cfg["js_download"]["max_files"] == 200
    # sibling keys from defaults survive the merge
    assert cfg["js_download"]["timeout"] == 15


def test_defaults_load_without_a_user_config():
    cfg = config.base_config()
    assert "js_download" in cfg and "target" not in cfg


# --- store ------------------------------------------------------------------ #

def test_target_crud(tmp_db):
    t = store.add_target("a", "https://a.com", scope=["a.com"], notes="n")
    assert store.get_target_by_name("a")["url"] == "https://a.com"
    store.update_target(t["id"], notes="changed", enabled=False)
    got = store.get_target(t["id"])
    assert got["notes"] == "changed" and got["enabled"] is False
    assert store.list_targets(enabled_only=True) == []
    store.delete_target(t["id"])
    assert store.get_target(t["id"]) is None


def test_rejects_bad_url_and_duplicate_name(tmp_db):
    with pytest.raises(ValueError):
        store.add_target("a", "ftp://a.com")
    store.add_target("a", "https://a.com")
    with pytest.raises(ValueError):
        store.add_target("a", "https://b.com")


def test_deleting_a_target_cascades_to_runs(tmp_db):
    t = store.add_target("a", "https://a.com")
    store.start_run(t["id"], "/tmp/x", "cli")
    store.delete_target(t["id"])
    assert store.list_runs() == []


def test_interrupted_runs_are_reconciled(tmp_db):
    t = store.add_target("a", "https://a.com")
    store.start_run(t["id"], "/tmp/x", "cli")
    assert store.recover_interrupted_runs() == 1
    assert store.list_runs()[0]["status"] == "interrupted"
    # reconciliation must be idempotent, not re-flag finished runs
    assert store.recover_interrupted_runs() == 0


# --- runner ----------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("Acme Corp", "acme-corp"),
    ("  ", "target"),
    ("../../etc/passwd", "etc-passwd"),
])
def test_slugify_is_safe_for_paths(raw, expected):
    assert slugify(raw) == expected
    assert "/" not in slugify(raw)


def test_run_dirs_are_unique_per_run(tmp_db):
    t = store.add_target("a", "https://a.com")
    assert run_dir_for(t, 1) != run_dir_for(t, 2)


# --- pipeline helpers ------------------------------------------------------- #

def test_findings_json_merges_model_and_static(tmp_db, tmp_path):
    from jxai.pipeline import _write_findings
    ctx = {
        "ai_findings": [{"severity": "High", "title": "DOM XSS", "reasoning": "untrusted sink"}],
        "deep_audit": {"flows": [{"source": "location.search", "sink": "innerHTML"}]},
    }
    _write_findings(ctx, tmp_path)
    rows = json.loads((tmp_path / "findings.json").read_text())
    assert {r["source"] for r in rows} == {"model", "static"}
    assert any(r["severity"] == "High" for r in rows)