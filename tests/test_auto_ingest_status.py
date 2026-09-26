"""Offline contract tests for the timer -> sync pipeline -> persisted status.

All configuration, API, backup and processing entry points are replaced before
any orchestrator runs. Only a temporary status/log and in-memory SQLite are used.
"""
from __future__ import annotations

import importlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests


class TrackingConnection(sqlite3.Connection):
    closed = False

    def close(self):
        self.closed = True
        super().close()


@pytest.fixture
def flow(monkeypatch, tmp_path):
    def no_network(*args, **kwargs):
        raise AssertionError("Network is forbidden in status tests")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    # These script imports only define functions; suppress their stdout reconfigure.
    with monkeypatch.context() as imports:
        imports.setattr(sys, "stdout", io.StringIO())
        auto = importlib.import_module("auto_ingest")
        sync = importlib.import_module("sync_ascent")
    ingest = importlib.import_module("lol_coach.ingest")
    cfg = {"paths": {"sqlite_db": str(tmp_path / "unused.db"),
                     "data_raw": str(tmp_path / "raw"),
                     "ascent_recordings_db": str(tmp_path / "ascent.db")},
           "riot": {"api_key": "synthetic-secret", "routing": "test", "platform": "test"}}
    monkeypatch.setattr(auto, "ROOT", tmp_path)
    monkeypatch.setattr(auto, "DATA", tmp_path)
    monkeypatch.setattr(auto, "LOG", tmp_path / "ingest.log")
    monkeypatch.setattr(auto, "STATUS", tmp_path / "ingest_status.json")
    monkeypatch.setattr(auto, "load_config", Mock(return_value=cfg))
    monkeypatch.setattr(sync, "load_config", Mock(return_value=cfg))
    monkeypatch.setattr(auto, "_key_alive", Mock(return_value=True))
    monkeypatch.setattr(auto, "_daily_db_backup", Mock(return_value=None))
    monkeypatch.setattr(sync, "RiotAPI", Mock(return_value=object()))
    monkeypatch.setattr(sync, "load_accounts", Mock(return_value=object()))
    conn = sqlite3.connect(":memory:", factory=TrackingConnection)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE sessions (match_id TEXT);
        CREATE TABLE matches (match_id TEXT, game_start_ms INTEGER, analyzed_at_utc TEXT);
        CREATE TABLE clips (match_id TEXT, source TEXT);
    """)
    monkeypatch.setattr(sync, "connect", Mock(return_value=conn))
    recorder = Mock(return_value={"sessions_seen": 0, "sessions_new": 0,
                                 "matches_added": 0, "errors": []})
    monkeypatch.setattr(ingest, "run_ingest", recorder)
    real_ascent = sync.ingest_ascent.ingest_ascent
    ascent = Mock(return_value={"recordings": 0, "matches_pulled": 0, "linked": 0,
                               "unlinked": 0, "riot_errors": 0})
    monkeypatch.setattr(sync.ingest_ascent, "ingest_ascent", ascent)
    monkeypatch.setattr(sync.ingest_ascent_events, "ingest_events_for_db", Mock())
    monkeypatch.setattr(sync.analyze, "analyze_match", Mock(return_value=3))
    monkeypatch.setattr(sync.complete_clips, "normalize_full_replays", Mock(return_value=0))
    monkeypatch.setattr(sync.complete_clips, "complete_clips_for_decisions",
                        Mock(return_value={"short": 0, "full": 0, "none": 0}))
    yield SimpleNamespace(auto=auto, sync=sync, cfg=cfg, conn=conn, recorder=recorder,
                          ascent=ascent, real_ascent=real_ascent)
    if not conn.closed:
        conn.close()


def read_status(flow):
    assert flow.auto.main() == 0  # scheduler compatibility; JSON carries failure
    return json.loads(flow.auto.STATUS.read_text(encoding="utf-8"))


def test_nine_riot_failures_remain_partial_despite_live_key(flow):
    flow.ascent.return_value.update(riot_errors=9, riot_error_sample="synthetic-secret URL")
    status = read_status(flow)
    assert status["state"] == "partial"
    assert status["key_alive"] is True
    assert status["error"] == "ingestion_partial"
    assert status["ingested"] == 0
    assert status["errors"] == [{"stage": "ascent", "code": "riot_errors", "count": 9}]
    assert "synthetic-secret" not in flow.auto.STATUS.read_text(encoding="utf-8")
    assert "PARCIAL." in flow.auto.LOG.read_text(encoding="utf-8")
    assert "LISTO." not in flow.auto.LOG.read_text(encoding="utf-8")
    assert flow.conn.closed


def test_recorder_error_list_is_counted_without_copying_details(flow):
    flow.recorder.return_value["errors"] = ["synthetic-secret request", "private session path"]
    status = read_status(flow)
    assert status["state"] == "partial"
    assert status["errors"] == [{"stage": "recorder", "code": "recorder_errors", "count": 2}]
    assert "private" not in json.dumps(status)


def test_recorder_exception_keeps_ascent_running(flow):
    flow.recorder.side_effect = RuntimeError("synthetic-secret")
    status = read_status(flow)
    assert status["state"] == "partial"
    assert status["errors"] == [{"stage": "recorder", "code": "recorder_failed", "count": 1}]
    flow.ascent.assert_called_once()
    assert "synthetic-secret" not in json.dumps(status)


def test_riot_setup_failure_is_partial_and_keeps_local_analysis(flow):
    flow.sync.load_accounts.side_effect = RuntimeError("synthetic-secret")
    flow.conn.execute("INSERT INTO matches VALUES ('TEST_1', 0, NULL)")
    status = read_status(flow)
    assert status["state"] == "partial"
    assert status["key_alive"] is True
    assert status["ingested"] == 1
    assert status["errors"] == [{"stage": "riot_setup", "code": "riot_unavailable", "count": 1}]
    flow.recorder.assert_not_called()
    assert flow.ascent.call_args.args[3:5] == (None, None)


def test_idle_pipeline_completes_with_zero_matches_and_preserves_argv(flow, monkeypatch):
    argv = ["auto_ingest.py", "--all", "unrelated"]
    monkeypatch.setattr(sys, "argv", argv)
    flow.conn.execute("INSERT INTO matches VALUES ('ALREADY_ANALYZED', 0, 'done')")
    status = read_status(flow)
    assert status["state"] == "completed"
    assert status["ingested"] == 0
    assert status["error"] is None
    assert status["errors"] == []
    assert sys.argv is argv
    assert sys.argv == ["auto_ingest.py", "--all", "unrelated"]
    flow.sync.analyze.analyze_match.assert_not_called()


def test_counts_successful_analyses_including_zero_decision_matches(flow):
    flow.conn.executemany("INSERT INTO matches VALUES (?, ?, NULL)", [("TEST_1", 0), ("TEST_2", 1)])
    flow.sync.analyze.analyze_match.side_effect = [0, 5]
    flow.ascent.return_value.update(matches_pulled=7)
    result = flow.sync.run_sync()
    assert result == {"state": "completed", "ingested": 2, "decisions": 5, "errors": []}
    assert flow.conn.closed


def test_fatal_analysis_preserves_completed_progress(flow):
    flow.conn.executemany("INSERT INTO matches VALUES (?, ?, NULL)", [("TEST_1", 0), ("TEST_2", 1)])
    flow.sync.analyze.analyze_match.side_effect = [3, RuntimeError("synthetic-secret")]
    status = read_status(flow)
    assert status["state"] == "failed"
    assert status["error"] == "ingestion_failed"
    assert status["key_alive"] is True
    assert status["ingested"] == 1
    assert status["errors"] == [{"stage": "analysis", "code": "stage_failed", "count": 1}]
    assert "synthetic-secret" not in json.dumps(status)
    assert flow.conn.closed
    flow.sync.complete_clips.normalize_full_replays.assert_not_called()


def test_failed_precheck_stops_sync_but_keeps_backup(flow, monkeypatch):
    flow.auto._key_alive.return_value = False
    run_sync = Mock()
    monkeypatch.setattr(flow.sync, "run_sync", run_sync)
    status = read_status(flow)
    assert status["state"] == "failed"
    assert status["key_alive"] is False
    assert status["error"] == "api_key_expired"
    assert status["errors"] == [{"stage": "precheck", "code": "riot_precheck_failed", "count": 1}]
    run_sync.assert_not_called()
    flow.auto._daily_db_backup.assert_called_once()


def test_unknown_precheck_does_not_overrule_completed_pipeline(flow):
    flow.auto._key_alive.return_value = None
    status = read_status(flow)
    assert status["state"] == "completed"
    assert status["key_alive"] is None
    assert status["error"] is None


@pytest.mark.parametrize("stage", ["config", "precheck"])
def test_early_timer_exception_is_persisted_without_secrets(flow, stage):
    target = flow.auto.load_config if stage == "config" else flow.auto._key_alive
    target.side_effect = RuntimeError("synthetic-secret")
    status = read_status(flow)
    assert status["state"] == "failed"
    assert status["error"] == "ingestion_failed"
    assert status["errors"] == [{"stage": stage, "code": "stage_failed", "count": 1}]
    assert "synthetic-secret" not in json.dumps(status)
    flow.sync.connect.assert_not_called()


def test_import_failure_is_persisted(flow, monkeypatch):
    monkeypatch.setitem(sys.modules, "sync_ascent", None)
    status = read_status(flow)
    assert status["state"] == "failed"
    assert status["error"] == "ingestion_failed"
    assert status["errors"] == [{"stage": "sync", "code": "stage_failed", "count": 1}]


@pytest.mark.parametrize("state, code", [("completed", 0), ("partial", 2), ("failed", 1)])
def test_cli_maps_states_to_exit_codes_and_forwards_all(flow, monkeypatch, state, code):
    run_sync = Mock(return_value={"state": state})
    monkeypatch.setattr(flow.sync, "run_sync", run_sync)
    monkeypatch.setattr(sys, "argv", ["sync_ascent.py", "--all"])
    assert flow.sync.main() == code
    run_sync.assert_called_once_with(all_flag=True)


def test_cli_real_partial_is_nonzero(flow):
    flow.ascent.return_value["riot_errors"] = 9
    assert flow.sync.main() == 2


def test_missing_sync_config_is_failed_not_idle(flow):
    del flow.cfg["paths"]["ascent_recordings_db"]
    status = read_status(flow)
    assert status["state"] == "failed"
    assert status["error"] == "ingestion_failed"
    assert status["errors"] == [{"stage": "config", "code": "ascent_not_configured", "count": 1}]
    flow.sync.connect.assert_not_called()


@pytest.mark.parametrize("source", ["missing", "unreadable"])
def test_missing_or_unreadable_ascent_source_propagates_partial(flow, monkeypatch, tmp_path, source):
    if source == "unreadable":
        ascent_path = Path(flow.cfg["paths"]["ascent_recordings_db"])
        ascent_path.touch()
        source_conn = sqlite3.connect(":memory:")  # no recordings table
        monkeypatch.setattr(flow.sync.ingest_ascent, "_open_ascent_db_readonly",
                            Mock(return_value=(source_conn, tmp_path / "unused-copy")))
    monkeypatch.setattr(flow.sync.ingest_ascent, "ingest_ascent", flow.real_ascent)
    status = read_status(flow)
    expected = "ascent_db_missing" if source == "missing" else "recordings_unreadable"
    assert status["state"] == "partial"
    assert status["error"] == "ingestion_partial"
    assert status["errors"] == [{"stage": "ascent", "code": expected, "count": 1}]
    flow.sync.complete_clips.complete_clips_for_decisions.assert_called_once()
