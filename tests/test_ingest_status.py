"""Pipeline health API and rendered banner, using synthetic local data only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from conftest import ten_player_match
from lol_coach import config


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_config", lambda: {
        "paths": {"sqlite_db": str(tmp_path / "synthetic.db")},
    })
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)
    path = Path(__file__).resolve().parents[1] / "scripts" / "dashboard.py"
    spec = importlib.util.spec_from_file_location("dashboard_ingest_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dead_key_surfaces_with_days_since_ingest(dashboard, conn, tmp_path):
    ten_player_match(conn)
    status = tmp_path / "ingest_status.json"
    status.write_text(json.dumps({
        "last_run": "2026-07-01 15:31:32", "key_alive": False,
        "ingested": 0, "error": "api_key_expired",
    }), encoding="utf-8")
    res = dashboard.query_ingest_status(conn, status_path=status)
    assert res["key_alive"] is False
    assert res["error"] == "api_key_expired"
    assert res["state"] is None
    assert res["last_ingested_utc"].startswith("2026-01-01")
    assert isinstance(res["days_since_ingest"], int) and res["days_since_ingest"] > 0


@pytest.mark.parametrize("contents", [None, "{bad json"])
def test_missing_or_unreadable_status_reports_unknown(dashboard, conn, tmp_path, contents):
    ten_player_match(conn)
    status = tmp_path / "status.json"
    if contents is not None:
        status.write_text(contents, encoding="utf-8")
    res = dashboard.query_ingest_status(conn, status_path=status)
    assert res["key_alive"] is None
    assert res["state"] is None
    assert res["errors"] == []
    assert res["days_since_ingest"] is not None


def test_legacy_alive_key_does_not_imply_pipeline_completed(dashboard, conn, tmp_path):
    status = tmp_path / "ingest_status.json"
    status.write_text(json.dumps({"key_alive": True, "ingested": 2}), encoding="utf-8")
    res = dashboard.query_ingest_status(conn, status_path=status)
    assert res["key_alive"] is True
    assert res["ingested"] == 2
    assert res["state"] is None
    assert res["days_since_ingest"] is None


@pytest.mark.parametrize("state,ingested,error,errors", [
    ("partial", 2, "ingestion_partial", [{"stage": "sync_ascent", "code": "ingest_failed", "count": 9}]),
    ("failed", 0, "ingestion_failed", [{"stage": "sync_ascent", "code": "stage_failed", "count": 1}]),
    ("completed", 0, None, []),
])
def test_pipeline_state_is_independent_of_live_key(dashboard, conn, tmp_path, state, ingested, error, errors):
    status = tmp_path / "ingest_status.json"
    payload = {"key_alive": True, "last_run": "2026-09-20T15:31:32Z",
               "state": state, "ingested": ingested, "error": error, "errors": errors}
    status.write_text(json.dumps(payload), encoding="utf-8")
    res = dashboard.query_ingest_status(conn, status_path=status)
    assert res == {**payload, "last_ingested_utc": None, "days_since_ingest": None}


def test_banner_renders_pipeline_failures_with_live_key(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the dashboard banner")
    html = (Path(__file__).resolve().parents[1] / "scripts" / "dashboard.html").read_text(encoding="utf-8")
    start = html.index("function IngestStatusBanner(")
    end = html.index("function App(){", start)
    # Execute the actual component, capturing React.createElement calls as a tree.
    harness = """
const assert = require('node:assert/strict');
const h = (tag, props, ...children) => ({tag, props, children});
""" + html[start:end] + """
const text = node => node == null ? '' : typeof node === 'string' ? node : node.children.map(text).join('');
const render = ingest => IngestStatusBanner({ingest});
const common = {key_alive:true, last_run:'2026-09-20T15:31:32Z', days_since_ingest:99};
const partial = render({...common,state:'partial',error:'ingestion_partial',errors:[{stage:'sync_ascent',code:'ingest_failed',count:9}]});
assert.equal(partial.tag, 'div');
assert.match(text(partial), /Ingesta parcial/);
assert.match(text(partial), /2026-09-20T15:31:32Z/);
assert.doesNotMatch(text(partial), /99|API key|expirada|muerta/);
assert.match(text(render({...common,state:'failed',error:'ingestion_failed'})), /La ingesta falló/);
assert.equal(render({...common,state:'completed',ingested:0,error:null,errors:[]}), null);
assert.equal(render({key_alive:true}), null);
assert.equal(render({key_alive:null,state:null}), null);
assert.equal(render(null), null);
assert.match(text(render({key_alive:false,error:'api_key_expired'})), /comprobación de la API key/);
const legacy = text(render({...common,error:'https://example.invalid/?api_key=SECRET'}));
assert.match(legacy, /La ingesta falló/);
assert.doesNotMatch(legacy, /SECRET|example.invalid/);
"""
    path = tmp_path / "banner.cjs"
    path.write_text(harness, encoding="utf-8")
    subprocess.run([node, str(path)], check=True, capture_output=True, text=True)
