"""Integration tests for the trade detector (trade_v1 / v2).

trade_v2 reads the RAW Riot timeline JSON (cumulative per-minute damageStats) to
judge bot-lane exchanges. These tests write a minimal timeline where exactly one
minute is a lopsided exchange and assert: a lost trade is flagged, a won trade is
recorded as positive (hidden from the list but kept for Calibrar), and a minute
below the damage threshold produces nothing.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

import pytest

from lol_coach.decisions import trade as TR

from conftest import damage_frame, insert_event, set_timeline_json, ten_player_match

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
try:
    import dashboard as _dash
except Exception:
    _dash = None

_needs_dash = pytest.mark.skipif(
    _dash is None, reason="requiere config.toml (dashboard carga config al importar)")

# Our bot duo is pid 1 (BOTTOM) + 5 (UTILITY); enemy duo is pid 6 + 10.
BAD_FRAME = 9                # the frame-9 delta = the clock interval [8, 9]
BAD_INTERVAL_START_MS = (BAD_FRAME - 1) * 60_000   # 480k: where the exchange BEGAN


def _timeline_with_jump(conn, match_id, tmp_path, *, frame_index, us_taken, enemy_taken):
    """11 frames (0..10), all zero except a single cumulative jump at `frame_index`.

    Frame `k` is the timeline snapshot at clock k*60s, so the damage delta
    frames[k]-frames[k-1] belongs to the clock interval [(k-1)*60s, k*60s].
    """
    frames = [damage_frame() for _ in range(frame_index)]             # nothing yet
    jump = damage_frame(**{"1": us_taken, "6": enemy_taken})          # our pid1 vs enemy pid6
    for _ in range(11 - frame_index):
        frames.append(jump)                                           # cumulative: no new damage
    set_timeline_json(conn, match_id, tmp_path / "timeline.json", frames)


def _timeline_with_minute9(conn, match_id, tmp_path, *, us_taken, enemy_taken):
    """The exchange lands in the frame-9 delta, i.e. the clock interval [8, 9]."""
    _timeline_with_jump(conn, match_id, tmp_path, frame_index=9,
                        us_taken=us_taken, enemy_taken=enemy_taken)


def test_lost_trade_is_flagged(conn, tmp_path):
    ids = ten_player_match(conn)
    # We took 600, the enemy duo 200: bilateral but clearly lost (diff 400>=300,
    # 600>=1.5*200).
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    d = decisions[0]
    assert d.detector_id == "trade_v1"
    assert d.game_time_ms == BAD_INTERVAL_START_MS
    assert d.context["positive_trade"] is False
    assert d.context["duo_damage_taken"] == 600
    assert d.context["enemy_duo_damage_taken"] == 200
    assert "perdido" in d.moment.lower()


def test_lost_trade_has_wellformed_options(conn, tmp_path):
    ids = ten_player_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)

    d = TR.analyze_trades(conn, ids["match_id"])[0]

    assert len(d.options) == 3
    assert [o.taken for o in d.options] == [False, True, False]
    assert set(d.context["state_features"]) == {"power", "info_risk", "wave_tempo", "objectives"}


def test_won_trade_is_recorded_positive(conn, tmp_path):
    ids = ten_player_match(conn)
    # Mirror image: the enemy duo took 600, we took 100 -> a won exchange. These
    # are persisted flagged positive (hidden from the list, kept for Calibrar).
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=100, enemy_taken=600)

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    assert decisions[0].context["positive_trade"] is True
    assert "ganado" in decisions[0].moment.lower()


def test_minute_below_damage_threshold_is_ignored(conn, tmp_path):
    ids = ten_player_match(conn)
    # 60 + 40 = 100 combined < MIN_EXCHANGE_DMG (250): not a real exchange.
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=60, enemy_taken=40)

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_no_timeline_json_returns_empty(conn):
    ids = ten_player_match(conn)
    # ten_player_match leaves timeline_json_path NULL -> nothing to analyze.
    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_unilateral_poke_is_not_a_trade(conn, tmp_path):
    """Real FP #1: 'aphelios hace un poco de dano, no hubo intercambio'. Combined
    damage clears MIN_EXCHANGE_DMG but the enemy duo took ~0: not a bilateral
    exchange, must not fire (previously fired as 'lost')."""
    ids = ten_player_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=100)

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_redside_position_is_not_a_trade(conn, tmp_path):
    """Real FP #2: 'estaba en redside no en bot'. Bilateral exchange (600/200)
    but our position throughout the [8, 9] interval is in the enemy half -> must
    not fire even though the old fallback quadrant/jungla bypass would have let
    it through. Both bounding frames are moved so the interval read is
    unambiguous whichever of them is closest to the sampling point."""
    ids = ten_player_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)
    conn.execute(
        "UPDATE timeline_frames SET position_x=?, position_y=? WHERE match_id=? "
        "AND participant_id=1 AND timestamp_ms IN (?, ?)",
        (11000, 6000, ids["match_id"], BAD_INTERVAL_START_MS, BAD_FRAME * 60_000),
    )
    conn.commit()

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_pushed_bot_lane_still_counts(conn, tmp_path):
    """Bot lane pushed deep into the enemy half is still a legitimate bot trade:
    map_lane classifies it as 'bot lane' even though the quadrant is on the
    enemy side."""
    ids = ten_player_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)
    conn.execute(
        "UPDATE timeline_frames SET position_x=?, position_y=? WHERE match_id=? "
        "AND participant_id=1 AND timestamp_ms IN (?, ?)",
        (13500, 2500, ids["match_id"], BAD_INTERVAL_START_MS, BAD_FRAME * 60_000),
    )
    conn.commit()

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    assert decisions[0].context["positive_trade"] is False
    assert "perdido" in decisions[0].moment.lower()


# --- Frame-index vs clock-interval semantics (off-by-one de un minuto) ---------
#
# The damage delta of frame m covers the clock interval [(m-1)*60s, m*60s]. The
# detector used to treat m as the instant of the exchange, which pushed every
# timestamp, death gate and position read one minute into the future.

def test_exchange_timestamp_is_the_interval_start_not_its_end(conn, tmp_path):
    """Damage that only appears in the frame-8 delta happened between 7:00 and
    8:00, so the decision must be stamped at 420_000 (where the clip should
    open), not at 480_000 (where the exchange had already finished)."""
    ids = ten_player_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=600, enemy_taken=200)

    d = TR.analyze_trades(conn, ids["match_id"])[0]

    assert d.game_time_ms == 420_000
    assert d.context["time_mmss"] == "7:00"
    assert d.context["minute_span"] == "min 7-8"
    assert d.context["span_minutes"] == 1


def _timeline_with_two_bad_minutes(conn, match_id, tmp_path, *, frame_index):
    """Two CONSECUTIVE bad minutes (frame_index and frame_index+1), each a lost
    exchange on its own, so analyze_trades merges them into a single two-minute
    run (see the merge loop in analyze_trades)."""
    frames = [damage_frame() for _ in range(frame_index)]
    first_jump = damage_frame(**{"1": 600, "6": 200})
    frames.append(first_jump)                                   # delta at frame_index: 600/200
    second_jump = damage_frame(**{"1": 1200, "6": 400})
    for _ in range(11 - frame_index - 1):
        frames.append(second_jump)                               # delta at frame_index+1: 600/200
    set_timeline_json(conn, match_id, tmp_path / "timeline.json", frames)


def test_two_minute_run_reports_its_true_span(conn, tmp_path):
    """A run spanning two consecutive bad minutes must report span_minutes=2 (and
    a "min A-B" text two minutes wide), not the fixed value the old dashboard
    code assumed by counting hyphens in the string."""
    ids = ten_player_match(conn)
    _timeline_with_two_bad_minutes(conn, ids["match_id"], tmp_path, frame_index=8)

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    d = decisions[0]
    assert d.context["minute_span"] == "min 7-9"
    assert d.context["span_minutes"] == 2


def test_our_duo_death_excludes_the_interval_that_contains_it(conn, tmp_path):
    """A death at 7:30 lives inside the interval [7, 8] (frame index 8), which
    is death_v1's turf. The old gate keyed it to 7 and skipped [6, 7] instead."""
    ids = ten_player_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=600, enemy_taken=200)
    insert_event(conn, ids["match_id"], 450_000, "CHAMPION_KILL",
                 {"victimId": 1, "killerId": 8})
    conn.commit()

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_duo_partner_death_does_not_exclude_the_interval(conn, tmp_path):
    """The gate excludes because death_v1 owns the play — but death_v1 only argues
    the PLAYER's deaths (`victimId != us_id: continue`). An interval where our duo
    PARTNER dies and we do not is nobody's: no death_v1 decision exists for it, and
    it is exactly a lost trade the player has agency over. It must fire."""
    ids = ten_player_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=600, enemy_taken=200)
    insert_event(conn, ids["match_id"], 450_000, "CHAMPION_KILL",
                 {"victimId": 5, "killerId": 8})       # our UTILITY partner, not us (pid 1)
    conn.commit()

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    assert decisions[0].game_time_ms == 420_000
    assert decisions[0].context["positive_trade"] is False


def test_our_duo_death_in_the_next_interval_does_not_exclude_this_one(conn, tmp_path):
    """Counterpart of the test above: a death at 8:30 belongs to [8, 9] and must
    leave the [7, 8] exchange alone (otherwise the gate is just over-excluding)."""
    ids = ten_player_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=600, enemy_taken=200)
    insert_event(conn, ids["match_id"], 510_000, "CHAMPION_KILL",
                 {"victimId": 1, "killerId": 8})
    conn.commit()

    d = TR.analyze_trades(conn, ids["match_id"])[0]

    assert d.game_time_ms == 420_000
    assert d.context["positive_trade"] is False


def test_enemy_kill_marks_the_interval_that_contains_it_as_won(conn, tmp_path):
    """An enemy duo kill at 7:30 makes the [7, 8] exchange a won trade even
    though the damage alone is even (200 vs 200: neither lost nor won)."""
    ids = ten_player_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=200, enemy_taken=200)
    insert_event(conn, ids["match_id"], 450_000, "CHAMPION_KILL",
                 {"victimId": 6, "killerId": 1})
    conn.commit()

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    assert decisions[0].context["positive_trade"] is True
    assert decisions[0].game_time_ms == 420_000


def test_position_is_sampled_inside_the_interval_not_at_its_end(conn, tmp_path):
    """We were in bot lane while trading (7:30) and had already walked to the
    enemy half by the time the frame closed (8:00). The trade is a bot trade:
    the position must be read from inside the interval, not from its edge."""
    ids = ten_player_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=600, enemy_taken=200)
    conn.execute(
        "INSERT INTO timeline_frames (match_id, frame_index, timestamp_ms, "
        "participant_id, position_x, position_y) VALUES (?,?,?,?,?,?)",
        (ids["match_id"], 7, 450_000, 1, 10000, 4000))       # bot lane, mid-interval
    conn.execute(
        "UPDATE timeline_frames SET position_x=?, position_y=? WHERE match_id=? "
        "AND participant_id=1 AND timestamp_ms=?",
        (11000, 6000, ids["match_id"], 480_000))             # red side, interval edge
    conn.commit()

    d = TR.analyze_trades(conn, ids["match_id"])[0]

    assert d.game_time_ms == 420_000


# --- Un solo eje, gane o pierda (#23) ----------------------------------------

TRADE_AXIS = ["disengage", "fight_entry", "attrition_trade"]


def test_won_and_lost_trades_offer_the_same_axis(conn, tmp_path):
    """El menu no lo puede elegir el desenlace: si ganaste, las tres opciones
    siguen siendo las mismas. Antes un trade ganado ofrecia solo 2 y una de ellas
    era 'Trade ganado (lo que hiciste)', que no es una eleccion."""
    lost_dir, won_dir = tmp_path / "lost", tmp_path / "won"
    lost_dir.mkdir()
    won_dir.mkdir()

    ids_lost = ten_player_match(conn)
    _timeline_with_minute9(conn, ids_lost["match_id"], lost_dir,
                           us_taken=600, enemy_taken=200)
    ids_won = ten_player_match(conn, match_id="TEST_WON")
    _timeline_with_minute9(conn, ids_won["match_id"], won_dir,
                           us_taken=100, enemy_taken=600)

    lost = TR.analyze_trades(conn, ids_lost["match_id"])[0]
    won = TR.analyze_trades(conn, ids_won["match_id"])[0]

    assert won.context["positive_trade"] is True
    assert lost.context["positive_trade"] is False
    for d in (won, lost):
        ids = [a["action_id"] for a in d.context["action"]["available_actions"]]
        assert ids == TRADE_AXIS
        # Exactamente una tomada, y es la misma en ambos: hubo intercambio.
        assert [o.taken for o in d.options] == [False, True, False]
        # Y ningun label describe el desenlace ni se autoetiqueta.
        for o in d.options:
            low = o.label.lower()
            assert "lo que hiciste" not in low
            assert "hiciste" not in low
            assert "ganado" not in low
            assert "perdi" not in low


def _facts_with(*, power_index_inputs, info_risk=None, wave_state=None, positive=False):
    """Un TradeFacts minimo para probar `_evaluate` sin tocar la DB."""
    return TR.TradeFacts(
        match_id="M", t_ms=420_000, m_start=8, m_end=8, positive=positive,
        d_us=600, d_en=200, hp_pct=None, mana_pct=None, consequence=None,
        state_features={
            "power": power_index_inputs,
            "info_risk": info_risk or {},
            "wave_tempo": {"wave_state": wave_state},
            "objectives": {},
        },
    )


def test_recommendation_follows_local_advantage_not_the_outcome():
    """Con ventaja local, tradear puntua por encima de esperar; sin ventaja, al
    reves. El desenlace no entra en la recomendacion: se comprueba con el MISMO
    setup en un trade ganado y en uno perdido."""
    strong = {"local_level_diff": 2, "local_gold_diff": 1200, "entry_hp_pct": 0.95,
              "ally_count_nearby": 2, "enemy_count_nearby": 1}
    weak = {"local_level_diff": -2, "local_gold_diff": -1200, "entry_hp_pct": 0.35,
            "ally_count_nearby": 1, "enemy_count_nearby": 3}

    for positive in (True, False):
        d_strong = TR._evaluate(_facts_with(power_index_inputs=strong, positive=positive))
        d_weak = TR._evaluate(_facts_with(power_index_inputs=weak, positive=positive))
        assert d_strong.options[1].ev_score > d_strong.options[0].ev_score   # tradear > esperar
        assert d_weak.options[0].ev_score > d_weak.options[1].ev_score       # esperar > tradear


def test_attrition_only_scores_when_a_next_play_pays_for_it():
    """El desgaste no es "el objetivo de trade": solo es una opcion real si hay
    una jugada proxima que cobre el HP. Con la oleada empujada (crashear y
    resetear) puntua con el setup; sin ella, cae al suelo."""
    strong = {"local_level_diff": 2, "local_gold_diff": 1200, "entry_hp_pct": 0.95,
              "ally_count_nearby": 2, "enemy_count_nearby": 1}
    with_play = TR._evaluate(_facts_with(power_index_inputs=strong,
                                         wave_state="pushed_into_enemy"))
    without = TR._evaluate(_facts_with(power_index_inputs=strong,
                                       wave_state="frozen_near_tower"))

    assert with_play.context["next_play_available"] is True
    assert without.context["next_play_available"] is False
    assert with_play.options[2].ev_score > without.options[2].ev_score
    assert without.options[2].ev_score == TR.EV_ATTRITION_NO_PLAY

    # Sin lectura de oleada el EV cae igual (conservador), pero la prosa NO puede
    # afirmar que no habia jugada: ~38% de las decisiones no tienen wave_state.
    blind = TR._evaluate(_facts_with(power_index_inputs=strong, wave_state=None))
    assert blind.options[2].ev_score == TR.EV_ATTRITION_NO_PLAY
    assert "no se ve la jugada" not in blind.options[2].predicted_consequence.lower()
    assert "sin lectura de la oleada" in blind.options[2].predicted_consequence.lower()
    # Y con jugada proxima sigue por debajo de tradear limpio con el mismo setup:
    # desgastar es aceptar quedar peor a cambio de algo.
    assert with_play.options[2].ev_score < with_play.options[1].ev_score


@_needs_dash
def test_old_trade_mark_survives_option_extension_through_learner_gate(conn, tmp_path):
    """Gate-level regression for the Task 3 extension (2-id -> 3-id options).

    A real lost-trade decision (via analyze_trades) has action_ids
    [disengage, fight_entry, attrition_trade], taken=fight_entry (index 1). We
    simulate a mark made back when only [disengage, fight_entry] existed: its
    stored fingerprint has 2 ids. _decision_feedback_pairs must accept it (not
    count it as n_stale) because fingerprint_compatible treats the 3rd id as a
    prefix-extension. A second row with a REORDERED 2-id fingerprint proves the
    test is a real gate check: that one must fall into n_stale.
    """
    from lol_coach.analysis import persist_decisions
    from lol_coach.decisions.features import action_fingerprint

    ids = ten_player_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)
    d = TR.analyze_trades(conn, ids["match_id"])[0]
    assert [a["action_id"] for a in d.context["action"]["available_actions"]] == [
        "disengage", "fight_entry", "attrition_trade"]
    taken_idx = next(i for i, o in enumerate(d.options) if o.taken)
    assert taken_idx == 1

    old_action = {"available_actions": [{"action_id": "disengage"}, {"action_id": "fight_entry"}]}
    old_opts_dicts = [{"label": o.label, "ev_score": o.ev_score, "taken": o.taken}
                      for o in d.options[:2]]
    old_fp = action_fingerprint("trade_v1", old_action, old_opts_dicts)
    reordered_action = {
        "available_actions": [{"action_id": "fight_entry"}, {"action_id": "disengage"}]}
    reordered_opts_dicts = [old_opts_dicts[1], old_opts_dicts[0]]
    reordered_fp = action_fingerprint("trade_v1", reordered_action, reordered_opts_dicts)

    persist_decisions(conn, ids["match_id"], [d])
    conn.execute(
        "UPDATE decisions SET user_best_option=0, user_mark_type='decision', "
        "user_feedback_fingerprint=? WHERE match_id=? AND detector_id='trade_v1'",
        (old_fp, ids["match_id"]))
    conn.commit()

    pairs, stats = _dash._decision_feedback_pairs(conn)
    assert len(pairs) == 1
    assert stats["n_stale"] == 0

    conn.execute(
        "UPDATE decisions SET user_feedback_fingerprint=? "
        "WHERE match_id=? AND detector_id='trade_v1'",
        (reordered_fp, ids["match_id"]))
    conn.commit()

    pairs2, stats2 = _dash._decision_feedback_pairs(conn)
    assert len(pairs2) == 0
    assert stats2["n_stale"] == 1
