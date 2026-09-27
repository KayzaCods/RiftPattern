"""Pure-logic tests for death_v1: drive _evaluate with a hand-built DeathFacts,
no SQLite. This is the payoff of separating I/O from logic — the classification
branches are testable without synthesizing a whole match (test_death.py keeps
covering the end-to-end I/O path)."""
from __future__ import annotations

from lol_coach.decisions.death import DeathFacts, _evaluate
from lol_coach.decisions.features import taken_index


def _facts(**over) -> DeathFacts:
    """A DeathFacts with sane defaults (an engage_blind bot death); override per test."""
    base = dict(
        match_id="M", death_ms=600_000, death_pos=(10000, 4000),
        lane="bot lane", map_side="nuestro lado", killer="Elise",
        assisters=[], first_blood=False,
        our_state={"level": 11, "current_gold": 500, "total_gold": 8000, "cs": 100,
                   "kills_so_far": 1, "deaths_so_far": 2, "assists_so_far": 3},
        nearest_ally_champ=None, nearest_ally_dist=None, we_had_help=False,
        fighters_for=1, fighters_against=1,
        score_our_kills=2, score_enemy_kills=3,
        non_combatant_unseen=[], combatant_unseen=[],
        retreating=False, got_value=False, enemy_deaths=0, ally_deaths=0,
        ally_died_first=False, sacrifice_obj=None, top_damager=None,
        state_features={"power": {}, "info_risk": {}, "wave_tempo": {}, "objectives": {}},
        clip_path=None, clip_offset_s=None,
    )
    base.update(over)
    return DeathFacts(**base)


def test_evaluate_engage_blind_is_default_classification():
    d = _evaluate(_facts())
    assert d.detector_id == "death_v1"
    assert d.context["inferred_action_class"] == "engage_blind"
    assert taken_index(d.options) is not None


def test_evaluate_calculated_sacrifice_when_objective_bought():
    d = _evaluate(_facts(sacrifice_obj={"name": "la torre", "rel_s": -1.0}))
    assert d.context["inferred_action_class"] == "calculated_sacrifice"
    assert any("objetivo" in o.predicted_consequence.lower() or "torre" in o.label.lower()
               for o in d.options)


def test_evaluate_engage_worth_when_personal_value():
    d = _evaluate(_facts(got_value=True))
    assert d.context["inferred_action_class"] == "engage_worth"


def test_evaluate_engage_worth_when_team_won_the_local_fight():
    d = _evaluate(_facts(enemy_deaths=2, ally_deaths=0))
    assert d.context["inferred_action_class"] == "engage_worth"


# --------------------------------------------- "rindio" habla del RESULTADO
# `got_value` (una kill/asistencia tuya) cortocircuitaba la comparacion de
# bajas, asi que una pelea de 1-por-3 se etiquetaba "Engage que rindio". Medido
# sobre 540 decisiones: 73 (14%) salian perdiendo el intercambio, y el
# desacuerdo del jugador escalaba con ello (3% -> 17% -> 30%). Sus palabras:
# "una kill por dos muertes no se me hace que rindio".

def test_personal_kill_does_not_make_a_losing_exchange_worth_it():
    """El caso que motivo el cambio: matas a uno, mueren tres de los tuyos."""
    d = _evaluate(_facts(got_value=True, enemy_deaths=1, ally_deaths=3))
    assert d.context["inferred_action_class"] != "engage_worth"
    assert not any("rindió" in (o.label or "") for o in d.options)


def test_personal_kill_still_counts_on_an_even_exchange():
    """El empate se conserva: tu aporte (peel/CC) pudo ser lo que lo sostuvo."""
    d = _evaluate(_facts(got_value=True, enemy_deaths=1, ally_deaths=1))
    assert d.context["inferred_action_class"] == "engage_worth"


def test_personal_kill_counts_when_the_exchange_is_favourable():
    d = _evaluate(_facts(got_value=True, enemy_deaths=2, ally_deaths=1))
    assert d.context["inferred_action_class"] == "engage_worth"


def test_a_losing_exchange_falls_through_to_the_class_that_describes_it():
    """No basta con dejar de llamarlo "rindio": tiene que caer en la clase que
    SI describe lo que paso. Con un aliado caido antes, es support_lost."""
    d = _evaluate(_facts(got_value=True, enemy_deaths=0, ally_deaths=1,
                         ally_died_first=True))
    assert d.context["inferred_action_class"] == "support_lost"

    d2 = _evaluate(_facts(got_value=True, enemy_deaths=1, ally_deaths=2))
    assert d2.context["inferred_action_class"] == "trade_lost"


def test_team_winning_the_fight_never_needs_a_personal_kill():
    """La otra mitad de la regla sigue viva: si el equipo gana el intercambio,
    rindio aunque tu no puntuaras (peel, CC, escudo)."""
    d = _evaluate(_facts(got_value=False, enemy_deaths=3, ally_deaths=1))
    assert d.context["inferred_action_class"] == "engage_worth"


def test_evaluate_support_lost_when_ally_fell_first():
    d = _evaluate(_facts(ally_died_first=True, ally_deaths=1))
    assert d.context["inferred_action_class"] == "support_lost"


def test_evaluate_trade_lost_on_two_sided_skirmish():
    d = _evaluate(_facts(ally_deaths=1))
    assert d.context["inferred_action_class"] == "trade_lost"


def test_evaluate_disengage_failed_when_retreating():
    d = _evaluate(_facts(retreating=True))
    assert d.context["inferred_action_class"] == "disengage_failed"


def test_evaluate_fog_gank_adds_niebla_line():
    d = _evaluate(_facts(combatant_unseen=[("Elise", 25.0)]))
    assert "desde la niebla" in d.argument
    assert d.context["combatant_enemies_unseen"] == [{"champion": "Elise", "unseen_s": 25}]


def test_evaluate_takes_no_conn():
    # Structural guard: _evaluate's only parameter is `facts` — it never touches a DB.
    import inspect
    assert list(inspect.signature(_evaluate).parameters) == ["facts"]


# ------------------------------------------------ #23 Fase C: eje luchar/huir
_BRANCHES = {
    "engage_blind": {},
    "engage_worth": {"got_value": True},
    "trade_lost": {"ally_deaths": 1},
    "disengage_failed": {"retreating": True},
    "support_lost": {"ally_died_first": True, "ally_deaths": 1},
    "calculated_sacrifice": {"sacrifice_obj": {"name": "la torre", "rel_s": -1.0}},
}
# Orden (action_ids) e indice tomado que congelan el fingerprint de las marcas.
# Solo disengage_failed cambia: antes era [disengage, disengage] con t1.
_FROZEN = {
    "engage_blind": (["fight_entry", "disengage"], 0),
    "engage_worth": (["fight_entry", "disengage"], 0),
    "calculated_sacrifice": (["fight_entry", "disengage"], 0),
    "trade_lost": (["disengage", "fight_entry"], 1),
    "support_lost": (["disengage", "fight_entry"], 1),
    "disengage_failed": (["fight_entry", "disengage"], 1),
}
_STRONG = {"power": {"local_level_diff": 3, "local_gold_diff": 1500,
                     "ally_count_nearby": 3, "enemy_count_nearby": 1},
           "info_risk": {"enemies_unseen": 0, "wards_nearby": 2},
           "wave_tempo": {}, "objectives": {}}
_WEAK = {"power": {"local_level_diff": -3, "local_gold_diff": -1500,
                   "ally_count_nearby": 1, "enemy_count_nearby": 3},
         "info_risk": {"enemies_unseen": 5, "wards_nearby": 0},
         "wave_tempo": {}, "objectives": {}}


def _aids(d):
    return [a["action_id"] for a in d.context["action"]["available_actions"]]


def test_every_branch_offers_a_real_axis_in_its_frozen_order():
    for name, over in _BRANCHES.items():
        d = _evaluate(_facts(**over))
        assert d.context["inferred_action_class"] == name
        ids, taken = _FROZEN[name]
        assert _aids(d) == ids, name
        assert taken_index(d.options) == taken, name
        assert len(set(ids)) == 2, f"{name}: el eje repite una accion"


def test_no_label_self_describes_or_narrates_the_outcome():
    banned = ("hiciste", "rindió", "cayó", "te alcanzaron", "lo perdiste",
              "tarde", "arreglo", "defendible")
    for name, over in _BRANCHES.items():
        for o in _evaluate(_facts(**over)).options:
            text = (o.label + " " + o.predicted_consequence).lower()
            assert not any(b in text for b in banned), (name, o.label)


def test_recommendation_follows_the_local_setup_not_the_branch():
    """Con ventaja, luchar (entrar, llevarte el objetivo) supera a huir; sin
    ventaja, al reves -- en TODAS las ramas. Antes la rama fijaba el ganador."""
    for name, over in _BRANCHES.items():
        for sf, aggressive_wins in ((_STRONG, True), (_WEAK, False)):
            d = _evaluate(_facts(state_features=sf, **over))
            ev = {a: o.ev_score for a, o in zip(_aids(d), d.options)}
            assert (ev["fight_entry"] > ev["disengage"]) is aggressive_wins, (name, ev)


def test_labels_are_the_same_whichever_branch_so_the_menu_predicts_nothing():
    """Las cuatro ramas de pelea comparten etiquetas: el menu no delata la rama
    (que sale del desenlace) ni, por tanto, la opcion de max EV."""
    fight_branches = ("engage_blind", "engage_worth", "trade_lost", "disengage_failed")
    labels = {name: sorted(o.label for o in _evaluate(_facts(**_BRANCHES[name])).options)
              for name in fight_branches}
    assert len({tuple(v) for v in labels.values()}) == 1, labels


def test_without_setup_signal_the_axis_is_even():
    d = _evaluate(_facts())          # state_features sin senal -> setup None
    assert [o.ev_score for o in d.options] == [0.55, 0.55]


def test_tied_axis_reads_the_same_whatever_the_branch_order():
    """Con EV empatado el argumento no puede decir "fue acertada" solo porque la
    tomada va primero en esa rama (max() elige la primera)."""
    for name, over in _BRANCHES.items():
        arg = _evaluate(_facts(**over)).argument          # sin senal: 0.55/0.55
        assert "no marcan una alternativa claramente superior" in arg, name
        assert "fue acertada" not in arg, name


def test_disengage_failed_keeps_the_timing_lesson_without_a_verdict():
    arg = _evaluate(_facts(retreating=True)).argument
    assert "Ibas de salida cuando te alcanzaron" in arg
    pre = arg.split("Las heurísticas")[0]
    assert "EV" not in pre and "acertada" not in pre
