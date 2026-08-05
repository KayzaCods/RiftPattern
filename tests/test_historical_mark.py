"""Marca historica en la card (#25): cuando el conjunto de opciones cambia, la
marca sobrevive en la DB pero deja de entrenar al learner. Hasta ahora eso pasaba
en silencio — la card mostraba la marca vieja como si siguiera vigente.

Lo que se prueba aqui:
  - una marca cuyo fingerprint SIGUE casando no produce bloque historico;
  - una marca cuyo fingerprint NO casa lo produce, con la eleccion resuelta
    contra la lista VIEJA de action_ids (la del fingerprint, no la actual);
  - una marca sin fingerprint que el gate de timestamp declara stale lo produce
    con reason="no_fingerprint";
  - y, el test que impide la divergencia: el veredicto de la card coincide con
    el de `_decision_feedback_pairs` (n_stale) sobre las mismas filas.

Mismo skip-guard que test_trade_moment (importar dashboard carga config.toml).
"""
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

from lol_coach.decisions.features import action_fingerprint  # noqa: E402

from conftest import insert_match  # noqa: E402

_EARLY = "2026-01-01T00:00:00Z"
_LATE = "2026-02-01T00:00:00Z"


def _ctx(action_ids, taken_aid):
    return {
        "action": {
            "detector_id": "trade_v1",
            "action_id": taken_aid,
            "available_actions": [{"action_id": a} for a in action_ids],
        },
        "state_features": {"power": {"local_allies": 2, "local_enemies": 1}},
    }


def _options(action_ids, taken_idx):
    return [
        {"label": f"Opcion {a}" + (" (lo que hiciste)" if i == taken_idx else ""),
         "ev_score": 0.5}
        for i, a in enumerate(action_ids)
    ]


def _insert(conn, match_id, *, now_ids, now_taken_idx, stored_fp,
            best_option, note="nota del jugador", feedback="disagree",
            mark_type="decision", fb_at=_LATE, created_at=_EARLY, t_ms=420_000):
    """Una decision con marca. `now_ids` es el conjunto de opciones ACTUAL;
    `stored_fp` el fingerprint con el que se hizo la marca (puede ser de otro
    conjunto, o None para simular una marca vieja sin fingerprint)."""
    ctx = _ctx(now_ids, now_ids[now_taken_idx])
    opts = _options(now_ids, now_taken_idx)
    cur = conn.execute(
        "INSERT INTO decisions (match_id, detector_id, game_time_ms, moment, outcome, "
        "context_json, options_json, argument, created_at_utc, user_feedback, "
        "user_feedback_note, user_best_option, user_mark_type, user_feedback_at_utc, "
        "user_feedback_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, "trade_v1", t_ms, "momento", "resultado", json.dumps(ctx),
         json.dumps(opts), "argumento", created_at, feedback, note, best_option,
         mark_type, fb_at, stored_fp),
    )
    conn.commit()
    return cur.lastrowid


def _fp(action_ids, taken_idx):
    return action_fingerprint("trade_v1", _ctx(action_ids, action_ids[taken_idx])["action"],
                              _options(action_ids, taken_idx))


def _by_id(conn):
    return {d["id"]: d for d in dashboard.query_decisions(conn, None, None)}


# ------------------------------------------------------------------ vigente
def test_compatible_mark_has_no_historical_block(conn):
    insert_match(conn, match_id="A")
    ids = ["fight_entry", "disengage"]
    dec = _insert(conn, "A", now_ids=ids, now_taken_idx=0,
                  stored_fp=_fp(ids, 0), best_option=1)

    d = _by_id(conn)[dec]
    assert d.get("historical_mark") is None
    assert d["feedback"] == "disagree"
    assert d["best_option"] == 1


def test_extension_by_the_end_stays_current(conn):
    """fingerprint_compatible ya acepta opciones anadidas AL FINAL; la card debe
    respetar esa regla y NO rotular la marca de historica."""
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["fight_entry", "disengage", "attrition_trade"],
                  now_taken_idx=0, stored_fp=_fp(["fight_entry", "disengage"], 0),
                  best_option=1)

    assert _by_id(conn)[dec].get("historical_mark") is None


# ---------------------------------------------------------------- historica
def test_reordered_options_produce_a_historical_block(conn):
    """La marca se hizo contra ['fight_entry','disengage'] con best_option=1 →
    eligio `disengage`. Hoy el orden es el inverso, asi que el indice 1 apunta a
    otra cosa: hay que resolverlo contra la lista VIEJA."""
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["disengage", "fight_entry"], now_taken_idx=0,
                  stored_fp=_fp(["fight_entry", "disengage"], 0), best_option=1,
                  note="me fui porque el jungla enemigo estaba en el rio")

    hm = _by_id(conn)[dec]["historical_mark"]
    assert hm is not None
    assert hm["reason"] == "options_changed"
    assert hm["chosen_action_id"] == "disengage"          # lista VIEJA, no la actual
    assert hm["chosen_action_label"] == "retirarse / no pelear"
    assert hm["previous_action_ids"] == ["pelear / entrar", "retirarse / no pelear"]
    assert hm["note"] == "me fui porque el jungla enemigo estaba en el rio"
    assert hm["feedback"] == "disagree"
    assert hm["mark_type"] == "decision"
    assert hm["marked_at"] == _LATE


def test_renamed_action_id_shows_the_old_id_verbatim(conn):
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["fight_entry", "disengage"], now_taken_idx=0,
                  stored_fp=_fp(["fight_entry", "accion_vieja"], 0), best_option=1)

    hm = _by_id(conn)[dec]["historical_mark"]
    assert hm["chosen_action_id"] == "accion_vieja"
    assert hm["chosen_action_label"] == "accion_vieja"   # id desconocido, tal cual
    assert hm["previous_action_ids"] == ["pelear / entrar", "accion_vieja"]


def test_out_of_range_index_leaves_the_choice_unresolved(conn):
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["disengage", "fight_entry"], now_taken_idx=0,
                  stored_fp=_fp(["fight_entry", "disengage"], 0), best_option=7)

    hm = _by_id(conn)[dec]["historical_mark"]
    assert hm["chosen_action_id"] is None
    assert hm["previous_action_ids"] == ["pelear / entrar", "retirarse / no pelear"]


def test_mark_without_fingerprint_before_reanalysis_is_historical(conn):
    """Sin fingerprint solo queda el gate conservador de timestamp: si la marca
    es ANTERIOR a la encarnacion actual de la decision, no cuenta."""
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["fight_entry", "disengage"], now_taken_idx=0,
                  stored_fp=None, best_option=1, fb_at=_EARLY, created_at=_LATE)

    hm = _by_id(conn)[dec]["historical_mark"]
    assert hm["reason"] == "no_fingerprint"
    assert hm["chosen_action_id"] is None
    assert hm["previous_action_ids"] == []


def test_mark_without_fingerprint_after_creation_stays_current(conn):
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["fight_entry", "disengage"], now_taken_idx=0,
                  stored_fp=None, best_option=1, fb_at=_LATE, created_at=_EARLY)

    assert _by_id(conn)[dec].get("historical_mark") is None


def test_decision_without_best_option_never_gets_the_block(conn):
    """El bloque habla de la OPCION marcada; sin `user_best_option` no hay indice
    que se pueda haber movido."""
    insert_match(conn, match_id="A")
    dec = _insert(conn, "A", now_ids=["disengage", "fight_entry"], now_taken_idx=0,
                  stored_fp=_fp(["fight_entry", "disengage"], 0), best_option=None)

    assert _by_id(conn)[dec].get("historical_mark") is None


# --------------------------------------------- la card y el learner de acuerdo
def test_card_verdict_matches_the_learner_gate(conn):
    """El test que impide la divergencia: sobre el MISMO conjunto de filas (todas
    entrenables), el numero de bloques historicos debe ser exactamente el
    `n_stale` que `_decision_feedback_pairs` reporta como `n_excluded_stale`."""
    insert_match(conn, match_id="A")
    ids = ["fight_entry", "disengage"]
    # vigentes
    _insert(conn, "A", now_ids=ids, now_taken_idx=0, stored_fp=_fp(ids, 0),
            best_option=1, t_ms=100_000)
    _insert(conn, "A", now_ids=["fight_entry", "disengage", "attrition_trade"],
            now_taken_idx=0, stored_fp=_fp(ids, 0), best_option=1, t_ms=200_000)
    _insert(conn, "A", now_ids=ids, now_taken_idx=0, stored_fp=None, best_option=1,
            fb_at=_LATE, created_at=_EARLY, t_ms=300_000)
    # historicas
    _insert(conn, "A", now_ids=["disengage", "fight_entry"], now_taken_idx=0,
            stored_fp=_fp(ids, 0), best_option=1, t_ms=400_000)
    _insert(conn, "A", now_ids=ids, now_taken_idx=1, stored_fp=_fp(ids, 0),
            best_option=1, t_ms=500_000)
    _insert(conn, "A", now_ids=ids, now_taken_idx=0, stored_fp=None, best_option=1,
            fb_at=_EARLY, created_at=_LATE, t_ms=600_000)

    cards = [d for d in dashboard.query_decisions(conn, None, None)
             if d.get("historical_mark")]
    _, stats = dashboard._decision_feedback_pairs(conn)

    assert len(cards) == 3
    assert stats["n_stale"] == len(cards)
