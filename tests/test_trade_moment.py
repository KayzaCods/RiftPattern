"""trade_moment_ms window sizing (defecto 1): la ventana de busqueda del clip debe
usar la longitud REAL del run (`span_minutes`), no `minute_span.count("-") + 1`
(que con el formato fijo "min A-B" del detector arreglado siempre da 2). Mismo
skip-guard que test_ingest_status (importar dashboard carga config.toml)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

try:
    import dashboard
except Exception:
    dashboard = None

pytestmark = pytest.mark.skipif(
    dashboard is None, reason="requiere config.toml (dashboard carga config al importar)"
)

from conftest import insert_match

_NOW = "2026-01-01T00:00:00Z"


def _trade_decision(conn, match_id, t_ms, context, *, session_dir="somedir"):
    conn.execute(
        "INSERT INTO sessions (match_id, session_dir, start_utc, ingested_at_utc) "
        "VALUES (?, ?, ?, ?)",
        (match_id, session_dir, _NOW, _NOW),
    )
    cur = conn.execute(
        "INSERT INTO decisions (match_id, detector_id, game_time_ms, context_json, "
        "options_json, argument, created_at_utc) VALUES (?,?,?,?,?,?,?)",
        (match_id, "trade_v1", t_ms, json.dumps(context), "[]", "", _NOW),
    )
    conn.commit()
    return cur.lastrowid


def test_span_minutes_field_drives_the_window(conn, monkeypatch):
    """A run with the explicit `span_minutes` field must open a window of exactly
    that many minutes, not two (the old hyphen-counting bug)."""
    insert_match(conn, match_id="A")
    dec_id = _trade_decision(
        conn, "A", 420_000,
        {"minute_span": "min 7-8", "span_minutes": 1},
    )

    seen = {}

    def fake_hp_pct_series_windowed(session_dir, t0, t1):
        seen["t0"], seen["t1"] = t0, t1
        return []

    monkeypatch.setattr(dashboard, "hp_pct_series_windowed", fake_hp_pct_series_windowed)
    dashboard._TRADE_MOMENT_CACHE.clear()

    dashboard.trade_moment_ms(conn, dec_id)

    assert seen["t0"] == 420.0
    assert seen["t1"] == 480.0            # 60s window, not 120s


def test_two_minute_run_gets_a_120s_window(conn, monkeypatch):
    insert_match(conn, match_id="B")
    dec_id = _trade_decision(
        conn, "B", 420_000,
        {"minute_span": "min 7-9", "span_minutes": 2},
    )

    seen = {}

    def fake_hp_pct_series_windowed(session_dir, t0, t1):
        seen["t0"], seen["t1"] = t0, t1
        return []

    monkeypatch.setattr(dashboard, "hp_pct_series_windowed", fake_hp_pct_series_windowed)
    dashboard._TRADE_MOMENT_CACHE.clear()

    dashboard.trade_moment_ms(conn, dec_id)

    assert seen["t0"] == 420.0
    assert seen["t1"] == 540.0            # 120s window


def test_old_decision_without_span_minutes_falls_back_to_parsed_text(conn, monkeypatch):
    """Decisiones viejas sin `span_minutes`: se recupera de `minute_span` en vez
    de contar guiones (que con "min 7-8" siempre daria 2, doblando la ventana)."""
    insert_match(conn, match_id="C")
    dec_id = _trade_decision(
        conn, "C", 420_000,
        {"minute_span": "min 7-8"},           # sin span_minutes: decision vieja
    )

    seen = {}

    def fake_hp_pct_series_windowed(session_dir, t0, t1):
        seen["t0"], seen["t1"] = t0, t1
        return []

    monkeypatch.setattr(dashboard, "hp_pct_series_windowed", fake_hp_pct_series_windowed)
    dashboard._TRADE_MOMENT_CACHE.clear()

    dashboard.trade_moment_ms(conn, dec_id)

    assert seen["t1"] == 480.0             # 60s: 8-7=1 minute parsed from the text


def test_span_minutes_from_text_handles_missing_or_malformed_input():
    assert dashboard._span_minutes_from_text("min 7-8") == 1
    assert dashboard._span_minutes_from_text("min 7-9") == 2
    assert dashboard._span_minutes_from_text(None) == 1
    assert dashboard._span_minutes_from_text("") == 1
    assert dashboard._span_minutes_from_text("garbage") == 1
