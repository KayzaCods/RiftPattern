"""Re-analisis en el sitio de un solo detector (#23 fases C y D).

El contrato que protege: solo cambian las filas del detector elegido y solo las
columnas que el detector produce; ids, marcas, clips, created_at y las claves de
context_json que escribe otro proceso (reward) sobreviven; si el detector no
reemite exactamente las mismas claves no se escribe nada; y es idempotente.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from lol_coach import inplace_reanalysis as R
from lol_coach.analysis import insert_decision
from lol_coach.decisions import death as D
from lol_coach.decisions.base import Decision, Option

from conftest import DEATH_MS, insert_event, ten_player_match

def _scene(conn):
    ids = ten_player_match(conn)
    mid = ids["match_id"]
    insert_event(conn, mid, DEATH_MS, "CHAMPION_KILL", {
        "killerId": 8, "victimId": 1, "assistingParticipantIds": [6],
        "position": {"x": 10000, "y": 4000}})
    fresh = D.analyze_deaths(conn, mid)[0]
    # Fila "vieja": mismo key, texto/opciones distintos, reward de compute_reward,
    # marca y clip que no deben moverse.
    ctx = dict(fresh.context, reward={"score": -1.5})
    old = Decision(detector_id=fresh.detector_id, match_id=mid, game_time_ms=fresh.game_time_ms,
                   moment="viejo", outcome="viejo", context=ctx, options=fresh.options,
                   argument="argumento viejo", clip_path="C:/clip.mp4", clip_offset_s=12.0)
    death_id = insert_decision(conn, old)
    other = Decision(detector_id="trade_v1", match_id=mid, game_time_ms=120_000, moment="t",
                     outcome="t", context={"a": 1}, options=[Option("A", "a", 0.5, taken=True)],
                     argument="trade intacto")
    trade_id = insert_decision(conn, other)
    conn.execute("UPDATE decisions SET user_feedback='agree', user_best_option=1, "
                 "user_feedback_note='nota', user_feedback_at_utc='2099-01-01' WHERE id=?",
                 (death_id,))
    conn.execute("UPDATE matches SET analyzed_at_utc='2026-01-01' WHERE match_id=?", (mid,))
    conn.commit()
    return mid, death_id, trade_id


def _row(conn, i):
    conn.row_factory = sqlite3.Row
    return dict(conn.execute("SELECT * FROM decisions WHERE id=?", (i,)).fetchone())


def test_dry_run_plans_without_writing(conn):
    _, death_id, _ = _scene(conn)
    before = _row(conn, death_id)
    plan = R.build_plan(conn, "death_v1")
    assert plan.rows == 1 and len(plan.changed) == 1
    assert set(plan.changed[0][1]) <= R.WRITABLE_COLUMNS
    assert plan.carried_keys["reward"] == 1
    assert _row(conn, death_id) == before


def test_apply_touches_only_detector_columns_and_keeps_marks_ids_clips_reward(conn):
    _, death_id, trade_id = _scene(conn)
    before, trade_before = _row(conn, death_id), _row(conn, trade_id)
    conn.isolation_level = None
    R.apply_plan(conn, "death_v1")
    after = _row(conn, death_id)
    for col in before:
        if col not in R.WRITABLE_COLUMNS:
            assert after[col] == before[col], col
    assert after["argument"] != "argumento viejo" and after["moment"] != "viejo"
    assert json.loads(after["context_json"])["reward"] == {"score": -1.5}
    assert _row(conn, trade_id) == trade_before
    # Idempotente: un segundo plan no encuentra nada que cambiar.
    assert R.build_plan(conn, "death_v1").changed == []


def test_key_mismatch_aborts_without_writing(conn):
    mid, death_id, _ = _scene(conn)
    conn.execute("UPDATE decisions SET game_time_ms = game_time_ms + 1 WHERE id=?", (death_id,))
    conn.commit()
    before = _row(conn, death_id)
    conn.isolation_level = None
    with pytest.raises(R.KeyMismatch):
        R.apply_plan(conn, "death_v1")
    assert _row(conn, death_id) == before


def test_validity_transition_is_reported_when_the_fingerprint_changes(conn):
    _, death_id, _ = _scene(conn)
    # La marca se guardo contra un menu con otro esqueleto: pasa a historica.
    conn.execute("UPDATE decisions SET user_feedback_fingerprint=? WHERE id=?",
                 ("v1|death_v1|disengage,disengage|t1", death_id))
    conn.commit()
    plan = R.build_plan(conn, "death_v1")
    assert sum(n for (a, b), n in plan.validity.items() if b == "options_changed") == 1


def test_trade_is_not_a_supported_detector(conn):
    with pytest.raises(ValueError):
        R.build_plan(conn, "trade_v1")
