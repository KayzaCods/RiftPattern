"""El indice de la opcion tomada viene de un campo explicito, no del texto.

Contrato (#23, fase A): cada `Option` lleva `taken: bool`; `taken_index` lo
prioriza y solo cae al regex `TAKEN_MARKER` para filas persistidas ANTES de que
el campo existiera. Los detectores marcan EXACTAMENTE una opcion por decision:
ninguna -> el fingerprint pasa a `t?` y se invalidan todas las marcas de ese
detector; dos -> el indice tomado se mueve silenciosamente al primero y el
fingerprint cambia igual.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lol_coach.decisions.base import Option
from lol_coach.decisions.features import TAKEN_MARKER as _MARKER, taken_index

DB = Path(__file__).resolve().parents[1] / "data" / "lol_coach.db"


# ------------------------------------------------------------- taken_index
def test_explicit_taken_flag_wins_over_prose():
    opts = [
        Option("Luchar: quedarte y pelear", "...", 0.4),
        Option("Huir: salir antes de comprometerte", "...", 0.6, taken=True),
    ]
    assert taken_index(opts) == 1


def test_falls_back_to_the_legacy_marker_when_no_flag_is_set():
    # Filas ya persistidas en options_json no tienen el campo: el regex sigue vivo.
    opts = [
        {"label": "Retirarte / no pelear", "ev_score": 0.6},
        {"label": "Engage que rindio (lo que hiciste)", "ev_score": 0.4},
    ]
    assert taken_index(opts) == 1


def test_explicit_flag_wins_over_the_marker_in_another_label():
    # El flag manda aunque otra opcion arrastre el texto viejo.
    opts = [
        {"label": "Engage que rindio (lo que hiciste)", "ev_score": 0.4},
        {"label": "Retirarte", "ev_score": 0.6, "taken": True},
    ]
    assert taken_index(opts) == 1


def test_no_flag_and_no_marker_is_none():
    opts = [Option("Luchar", "...", 0.4), Option("Huir", "...", 0.6)]
    assert taken_index(opts) is None


# --------------------------------------- invariante: exactamente una tomada
def _taken_flags(options) -> list[bool]:
    return [bool(getattr(o, "taken", False)) for o in options]


def _assert_exactly_one_taken(decision, what: str):
    flags = _taken_flags(decision.options)
    assert sum(flags) == 1, f"{what}: {sum(flags)} opciones con taken=True ({flags})"
    # Y el flag apunta al mismo sitio que el marcador de texto historico, que es
    # lo que congelo el fingerprint de las filas ya marcadas.
    marker_idx = next(
        (i for i, o in enumerate(decision.options)
         if _MARKER.search(o.label or "")), None)
    if marker_idx is not None:
        assert flags.index(True) == marker_idx, (
            f"{what}: el flag ({flags.index(True)}) no coincide con el marcador "
            f"de texto ({marker_idx})")


def test_death_marks_exactly_one_option_in_every_branch():
    from tests.test_death_logic import _facts
    from lol_coach.decisions.death import _evaluate
    cases = {
        "engage_blind": {},
        "calculated_sacrifice": {"sacrifice_obj": {"name": "la torre", "rel_s": -1.0}},
        "engage_worth": {"got_value": True},
        "engage_worth_team": {"enemy_deaths": 2, "ally_deaths": 0},
        "support_lost": {"ally_died_first": True, "ally_deaths": 1},
        "trade_lost": {"ally_deaths": 1},
        "disengage_failed": {"retreating": True},
    }
    for name, over in cases.items():
        _assert_exactly_one_taken(_evaluate(_facts(**over)), f"death/{name}")


def test_trade_marks_exactly_one_option_in_every_branch():
    from tests.test_trade_logic import _facts
    from lol_coach.decisions.trade import _evaluate
    cases = {
        "lost": {},
        "won": {"positive": True, "d_us": 100, "d_en": 600},
        "lost_shoving": {
            "hp_pct": 0.7, "mana_pct": 0.6,
            "state_features": {"power": {}, "info_risk": {},
                               "wave_tempo": {"wave_state": "pushed_into_enemy"},
                               "objectives": {}}},
    }
    for name, over in cases.items():
        _assert_exactly_one_taken(_evaluate(_facts(**over)), f"trade/{name}")


def test_tempo_marks_exactly_one_option_in_every_branch():
    from tests.test_tempo_logic import _facts
    from lol_coach.decisions.tempo import _evaluate
    cases = {
        "lost_reachable": {},
        "dominant_win": {"our_kills": 3, "our_deaths": 0},
        "could_not_arrive": {"could_arrive": False},
        "farm_jungle_stay": {"stay_action": "farm_jungle"},
    }
    for name, over in cases.items():
        _assert_exactly_one_taken(_evaluate(_facts(**over)), f"tempo/{name}")


def test_objective_readiness_marks_exactly_one_option_in_every_branch():
    from tests.test_objective_readiness_logic import _facts
    from lol_coach.decisions.objective_readiness import _evaluate
    cases = {
        "taken_with_setup": {},
        "must_defend_ceded": dict(
            ctx={"taken_by": "enemigo", "at_pit": False, "can_contest": False},
            taken_by_us=False, must_defend=True),
        "must_defend_forced": dict(
            ctx={"taken_by": "enemigo", "at_pit": True, "can_contest": False},
            taken_by_us=False, must_defend=True),
        "dominating_controlled": dict(
            ctx={"objective_dominated": True}, presence_optional=True),
        "dominating_too_far": dict(
            ctx={"objective_uncontested": True, "too_far": True, "at_pit": False},
            presence_optional=True),
        "taken_low_setup": dict(ctx={
            "team_wards_near_pit": 0, "team_sweeps_near_pit": 0,
            "your_wards_near_pit": 0, "your_sweeps_near_pit": 0,
            "vision_diff": 0, "allies_near_pit": 0, "at_pit": False}),
        "contest_lost_with_setup": dict(
            ctx={"taken_by": "enemigo", "at_pit": True}, taken_by_us=False),
        "contest_lost_no_setup": dict(ctx={
            "taken_by": "enemigo", "at_pit": True, "team_wards_near_pit": 0,
            "vision_diff": 0, "allies_near_pit": 0}, taken_by_us=False),
        "traded_for": dict(ctx={
            "taken_by": "enemigo", "at_pit": False, "too_far": True,
            "traded_for": "las larvas"}, taken_by_us=False),
        "not_there_low_setup": dict(ctx={
            "taken_by": "enemigo", "at_pit": False, "too_far": True,
            "team_wards_near_pit": 0, "vision_diff": 0, "allies_near_pit": 0},
            taken_by_us=False),
        "not_there_team_cede": dict(ctx={
            "taken_by": "enemigo", "at_pit": False, "too_far": True,
            "team_wards_near_pit": 1, "team_sweeps_near_pit": 1, "vision_diff": 1,
            "allies_near_pit": 0}, taken_by_us=False),
        "not_there_your_gap": dict(ctx={
            "taken_by": "enemigo", "at_pit": False, "too_far": True,
            "team_wards_near_pit": 1, "team_sweeps_near_pit": 1, "vision_diff": 1,
            "allies_near_pit": 2}, taken_by_us=False),
    }
    for name, over in cases.items():
        _assert_exactly_one_taken(_evaluate(_facts(**over)), f"objective/{name}")


# --------------------------------------------------- red de seguridad sobre la DB
@pytest.mark.skipif(not DB.exists(), reason="needs the real DB")
def test_every_persisted_decision_still_resolves_a_taken_option():
    """Red de seguridad: si un detector deja de marcar la opcion tomada, el
    fingerprint pasa a 't?' y TODAS las marcas de ese detector se invalidan."""
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    missing = []
    try:
        for r in conn.execute(
            "SELECT id, detector_id, options_json FROM decisions "
            "WHERE options_json IS NOT NULL"
        ):
            opts = json.loads(r["options_json"])
            if taken_index(opts) is None:
                missing.append((r["id"], r["detector_id"]))
    finally:
        conn.close()
    assert not missing, f"{len(missing)} decisiones sin opcion tomada: {missing[:5]}"
