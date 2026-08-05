"""Archivo permanente del feedback huerfano (#19).

`persist_decisions` borra y reinserta las decisiones de una partida, y solo
rescata el feedback cuya clave estable (detector_id, game_time_ms) reaparece.
Antes, lo que no reaparecia se perdia para siempre y solo se contaba en
`stats["dropped"]`. Estos tests fijan el contrato nuevo: antes de descartarlo,
se vuelca integro a `orphan_feedback`.
"""
from __future__ import annotations

import json

from conftest import insert_match

from lol_coach.analysis import persist_decisions
from lol_coach.decisions.base import Decision, Option

NOTE = (
    "Aqui me adelante sin vision del jungla enemigo; el intercambio parecia "
    "ganado pero perdi el control de la oleada y me castigaron 20s despues."
)


def _dec(t_ms, detector_id="trade_v1"):
    return Decision(
        detector_id=detector_id, match_id="TEST", game_time_ms=t_ms,
        moment=f"Intercambio en bot @ {t_ms // 60000}:00",
        outcome="lost",
        context={"action": {"action_id": "fight_entry"}},
        options=[Option("fight_entry", "Pelear ya", 0.42),
                 Option("disengage", "Salir", 0.61)],
        argument="Con el jungla sin ver, salir vale mas.",
    )


def _mark(conn, note=NOTE):
    conn.execute(
        "UPDATE decisions SET user_feedback='disagree', user_feedback_note=?, "
        "user_feedback_at_utc='2026-07-20T10:00:00+00:00', user_best_option=1, "
        "user_mark_type='decision', user_feedback_fingerprint='fp1' "
        "WHERE match_id='TEST'",
        (note,),
    )
    conn.commit()


def _orphans(conn):
    return conn.execute(
        "SELECT * FROM orphan_feedback ORDER BY id").fetchall()


def test_orphan_feedback_table_exists(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orphan_feedback)")}
    assert {"match_id", "detector_id", "game_time_ms", "user_feedback",
            "user_feedback_note", "user_feedback_at_utc",
            "user_feedback_fingerprint", "user_mark_type", "user_best_option",
            "options_json", "moment", "argument", "dropped_at_utc",
            "reason"} <= cols


def test_key_not_reemitted_archives_note_options_and_moment(conn):
    insert_match(conn, "TEST")
    persist_decisions(conn, "TEST", [_dec(840_000)])
    _mark(conn)

    # El detector arreglado emite la misma jugada un minuto antes: la clave vieja
    # no reaparece.
    n, stats = persist_decisions(conn, "TEST", [_dec(780_000)])

    assert stats["dropped"] == 1
    assert stats["orphaned"] == 1
    rows = _orphans(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["match_id"] == "TEST"
    assert r["detector_id"] == "trade_v1"
    assert r["game_time_ms"] == 840_000
    assert r["user_feedback"] == "disagree"
    assert r["user_feedback_note"] == NOTE          # integra, sin truncar
    assert r["user_best_option"] == 1
    assert r["user_mark_type"] == "decision"
    assert r["user_feedback_fingerprint"] == "fp1"
    assert r["user_feedback_at_utc"] == "2026-07-20T10:00:00+00:00"
    assert r["moment"] == "Intercambio en bot @ 14:00"
    assert r["argument"] == "Con el jungla sin ver, salir vale mas."
    assert [o["label"] for o in json.loads(r["options_json"])] == [
        "fight_entry", "disengage"]
    assert r["reason"] == "key_not_reemitted"
    assert r["dropped_at_utc"]


def test_key_reemitted_is_preserved_and_not_archived(conn):
    insert_match(conn, "TEST")
    persist_decisions(conn, "TEST", [_dec(840_000)])
    _mark(conn)

    n, stats = persist_decisions(conn, "TEST", [_dec(840_000)])

    assert stats["kept"] == 1
    assert stats["dropped"] == 0
    assert stats["orphaned"] == 0
    assert _orphans(conn) == []
    note = conn.execute(
        "SELECT user_feedback_note FROM decisions WHERE match_id='TEST'"
    ).fetchone()[0]
    assert note == NOTE


def test_archiving_is_idempotent_across_repeated_reanalysis(conn):
    insert_match(conn, "TEST")
    persist_decisions(conn, "TEST", [_dec(840_000)])
    _mark(conn)
    persist_decisions(conn, "TEST", [_dec(780_000)])
    assert len(_orphans(conn)) == 1

    # Re-marcar la clave vieja y re-analizar otra vez: misma marca (mismo
    # user_feedback_at_utc) -> no se duplica la fila del archivo.
    persist_decisions(conn, "TEST", [_dec(840_000)])
    _mark(conn)
    n, stats = persist_decisions(conn, "TEST", [_dec(780_000)])

    assert stats["orphaned"] == 0          # nada nuevo escrito
    assert len(_orphans(conn)) == 1


def test_orphaned_counts_several_detectors(conn):
    insert_match(conn, "TEST")
    persist_decisions(conn, "TEST", [_dec(840_000), _dec(900_000, "death_v1")])
    _mark(conn)                                     # marca ambas

    n, stats = persist_decisions(conn, "TEST", [_dec(780_000)])

    assert stats["dropped"] == 2
    assert stats["orphaned"] == 2
    assert {r["detector_id"] for r in _orphans(conn)} == {"trade_v1", "death_v1"}
