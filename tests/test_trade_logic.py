"""Pure-logic tests for trade_v1: drive _evaluate with a hand-built TradeFacts,
no timeline JSON / DB. The minute-scan I/O stays in analyze_trades /
_gather_trade_facts; test_trade.py keeps covering it end-to-end."""
from __future__ import annotations

from lol_coach.decisions.trade import TradeFacts, _evaluate
from lol_coach.decisions.features import taken_index


def _facts(**over) -> TradeFacts:
    base = dict(
        match_id="M", t_ms=540_000, m_start=9, m_end=9, positive=False,
        d_us=600, d_en=100, hp_pct=None, mana_pct=None, consequence=None,
        state_features={"power": {}, "info_risk": {}, "wave_tempo": {}, "objectives": {}},
    )
    base.update(over)
    return TradeFacts(**base)


def test_evaluate_lost_trade():
    d = _evaluate(_facts())
    assert d.detector_id == "trade_v1"
    assert d.context["positive_trade"] is False
    assert d.context["duo_damage_taken"] == 600
    assert "perdido" in d.moment.lower()
    assert taken_index(d.options) is not None


def test_evaluate_won_trade_is_positive():
    d = _evaluate(_facts(positive=True, d_us=100, d_en=600))
    assert d.context["positive_trade"] is True
    assert "ganado" in d.moment.lower()


def test_evaluate_lost_trade_appends_the_consequence():
    d = _evaluate(_facts(consequence="muerte de tu duo en los 90s siguientes"))
    assert d.context["consequence"] == "muerte de tu duo en los 90s siguientes"
    # the "what you did" option (index 1) carries the consequence, capitalized
    assert "Muerte de tu duo" in d.options[1].predicted_consequence
    assert "La consecuencia llego" in d.argument or "La consecuencia lleg" in d.argument


def test_evaluate_lost_trade_mana_dump_is_reasoned_by_mana_not_life():
    # Small life diff (< BAD_MIN_DIFF) but dumped mana: the reason is mana, not life.
    d = _evaluate(_facts(d_us=200, d_en=150, mana_pct=0.05))
    assert "sin man" in d.options[1].predicted_consequence.lower()
    assert "intercambio de vida" not in d.options[1].predicted_consequence.lower()


def test_evaluate_takes_no_conn():
    import inspect
    assert list(inspect.signature(_evaluate).parameters) == ["facts"]


def test_lost_trade_offers_attrition_option_last():
    dec = _evaluate(_facts(
        hp_pct=0.7, mana_pct=0.6,
        state_features={"power": {}, "info_risk": {},
                        "wave_tempo": {"wave_state": "pushed_into_enemy"}, "objectives": {}}))
    ids = [a["action_id"] for a in dec.context["action"]["available_actions"]]
    assert ids == ["disengage", "fight_entry", "attrition_trade"]
    # el indice tomado no se movio: sigue siendo fight_entry (lo que hiciste)
    assert dec.context["action"]["action_id"] == "fight_entry"
    assert len(dec.options) == 3


def test_won_trade_offers_the_same_three_options():
    """Inverso del contrato viejo (#23): el trade ganado ofrecia 2 opciones y sin
    desgaste, porque el MENU lo elegia el desenlace. Ahora el eje es el mismo."""
    dec = _evaluate(_facts(positive=True, d_us=100, d_en=600))
    ids = [a["action_id"] for a in dec.context["action"]["available_actions"]]
    assert ids == ["disengage", "fight_entry", "attrition_trade"]
    assert dec.context["action"]["action_id"] == "fight_entry"
    assert [o.taken for o in dec.options] == [False, True, False]


def test_attrition_ev_needs_a_next_play_and_the_setup_to_pay_it():
    """El EV del desgaste sale del setup local por EV_ATTRITION_FACTOR, y solo si
    hay una jugada proxima que cobre el HP; sin ella cae al suelo fijo. Antes se
    sumaban bonos heuristicos (shove +0.15, mana +0.10, hp bajo -0.15)."""
    from lol_coach.decisions.trade import (
        EV_ATTRITION_FACTOR, EV_ATTRITION_NO_PLAY, EV_FLOOR, EV_SPAN)

    even = {"power": {}, "info_risk": {}, "objectives": {}}   # sin senal: setup 0.5
    hi = _evaluate(_facts(
        hp_pct=0.7, mana_pct=0.6,
        state_features={**even, "wave_tempo": {"wave_state": "pushed_into_enemy"}}))
    assert abs(hi.options[2].ev_score
               - round(EV_FLOOR + EV_SPAN * 0.5 * EV_ATTRITION_FACTOR, 2)) < 1e-9
    # Misma oleada, mismo HP: sin jugada proxima el desgaste no compra nada.
    lo = _evaluate(_facts(
        hp_pct=0.7, mana_pct=0.6,
        state_features={**even, "wave_tempo": {"wave_state": "frozen_near_tower"}}))
    assert abs(lo.options[2].ev_score - EV_ATTRITION_NO_PLAY) < 1e-9
