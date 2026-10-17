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
        "ai_findings": [{"severity": "High", "verdict": "confirmed", "title": "DOM XSS",
                         "reasoning": "untrusted sink"}],
        "deep_audit": {"flows": [{"source": "location.search", "sink": "innerHTML"}]},
    }
    _write_findings(ctx, tmp_path)
    rows = json.loads((tmp_path / "findings.json").read_text())
    assert {r["source"] for r in rows} == {"model", "static"}


def test_findings_match_what_the_report_renders(tmp_db, tmp_path):
    """The UI and report.md must not disagree. report.md lists only findings the
    model marked confirmed/likely, so the export has to apply the same filter."""
    from jxai.pipeline import _write_findings
    ctx = {
        "ai_findings": [
            {"severity": "High", "verdict": "confirmed", "url": "https://x/api/a", "confidence": 0.9},
            {"severity": "Low", "verdict": "rejected", "url": "https://x/api/b", "confidence": 0.2},
            {"verdict": "likely", "url": "https://x/api/c"},
        ],
        "deep_audit": {"flows": [
            {"source": "a.js:1 (q)", "sink": "a.js:2 (eval)", "severity": "Medium",
             "confidence": "confirmed", "reasoning": "r", "exploit": "e"},
        ]},
    }
    _write_findings(ctx, tmp_path)
    rows = json.loads((tmp_path / "findings.json").read_text())
    model_urls = {r["url"] for r in rows if r["source"] == "model"}
    assert model_urls == {"https://x/api/a", "https://x/api/c"}  # the rejected one is dropped
    assert rows[0]["severity"] == "High"  # severity-sorted, most severe first


def test_findings_carry_flow_severity_and_confidence(tmp_db, tmp_path):
    """Regression: flows were exported as "Unrated" with no confidence, which made
    the UI show materially less than the report."""
    from jxai.pipeline import _write_findings
    ctx = {"deep_audit": {"flows": [
        {"source": "a.js:1 (q)", "sink": "a.js:2 (eval)",
         "severity": "High", "confidence": "confirmed", "exploit": "curl ..."},
    ]}}
    _write_findings(ctx, tmp_path)
    (f,) = json.loads((tmp_path / "findings.json").read_text())
    assert f["severity"] == "High"
    assert f["confidence"] == "confirmed"
    assert "q" in f["title"] and "eval" in f["title"]   # identity, not a generic label
    assert "Exploit:" in f["detail"]                     # exploit sketch is evidence too


def test_probe_counts_exclude_spa_shell(tmp_db):
    """A 200 that is just the SPA index page is not an interesting finding."""
    from fastapi.testclient import TestClient

    from jxai.runner import run_dir_for
    from jxai.web import app

    t = store.add_target("a", "https://a.com")
    run = store.start_run(t["id"], "", "cli")
    run_dir = run_dir_for(t, run["id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    store.update_run_dir(run["id"], str(run_dir))

    (run_dir / "results.json").write_text(json.dumps([
        {"url": "https://a.com/api", "method": "GET", "probes": [
            {"probe": "no-auth", "status": 200, "auth_wall": False, "spa_shell": False},
            {"probe": "no-auth", "status": 200, "auth_wall": False, "spa_shell": True},
            {"probe": "no-auth", "status": 401, "auth_wall": True, "spa_shell": False},
        ]},
    ]))
    with TestClient(app) as c:
        resp = c.get(f"/runs/{run['id']}/probes")
        assert resp.status_code == 200, resp.text
        page = resp.text
    assert "1</strong> reached 200 with no auth wall" in page