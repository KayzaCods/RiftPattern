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


def _trade_match(conn, match_id="TEST"):
    return ten_player_match(conn, match_id, frame_times=[(i, i * 60_000) for i in range(7, 11)])


def _set_duo_distance(conn, match_id, distance):
    """Keep our bot duo at a known distance throughout the fixture's frames."""
    for pid, x in ((1, 10000.0), (5, 10000.0 + distance)):
        conn.execute(
            "UPDATE timeline_frames SET position_x=?, position_y=? "
            "WHERE match_id=? AND participant_id=?",
            (x, 4000.0, match_id, pid),
        )
    conn.commit()


def _timeline_with_jump(conn, match_id, tmp_path, *, frame_index, us_taken, enemy_taken,
                        duo_distance=1000.0):
    """11 frames (0..10), all zero except a single cumulative jump at `frame_index`.

    Frame `k` is the timeline snapshot at clock k*60s, so the damage delta
    frames[k]-frames[k-1] belongs to the clock interval [(k-1)*60s, k*60s].
    """
    frames = [damage_frame(timestamp=i * 60_000) for i in range(frame_index)]             # nothing yet
    jump = damage_frame(**{"1": us_taken, "6": enemy_taken})          # our pid1 vs enemy pid6
    for _ in range(11 - frame_index):
        frames.append({**jump, "timestamp": len(frames) * 60_000})                                           # cumulative: no new damage
    set_timeline_json(conn, match_id, tmp_path / "timeline.json", frames)
    _set_duo_distance(conn, match_id, duo_distance)


def _timeline_with_minute9(conn, match_id, tmp_path, *, us_taken, enemy_taken,
                           duo_distance=1000.0):
    """The exchange lands in the frame-9 delta, i.e. the clock interval [8, 9]."""
    _timeline_with_jump(conn, match_id, tmp_path, frame_index=9,
                        us_taken=us_taken, enemy_taken=enemy_taken,
                        duo_distance=duo_distance)


def test_lost_trade_is_flagged(conn, tmp_path):
    ids = _trade_match(conn)
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


def test_lost_trade_is_ignored_when_our_duo_is_far_apart(conn, tmp_path):
    ids = _trade_match(conn)
    _timeline_with_minute9(
        conn, ids["match_id"], tmp_path,
        us_taken=600, enemy_taken=200, duo_distance=4000.0,
    )

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_lost_trade_counts_when_our_duo_is_nearby(conn, tmp_path):
    ids = _trade_match(conn)
    _timeline_with_minute9(
        conn, ids["match_id"], tmp_path,
        us_taken=600, enemy_taken=200, duo_distance=1000.0,
    )

    assert len(TR.analyze_trades(conn, ids["match_id"])) == 1


def test_lost_trade_is_ignored_when_nearby_adc_is_outside_bot(conn, tmp_path):
    ids = _trade_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)
    conn.execute(
        "UPDATE timeline_frames SET position_x=?, position_y=? WHERE match_id=? "
        "AND participant_id=5",
        (10000.0, 6000.0, ids["match_id"]),  # jungle, 2000u from the player in bot
    )
    conn.commit()

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_lost_trade_tolerates_incomplete_adc_position(conn, tmp_path):
    ids = _trade_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)
    conn.execute(
        "INSERT INTO timeline_frames (match_id, frame_index, timestamp_ms, "
        "participant_id, position_x, position_y) VALUES (?,?,?,?,?,?)",
        (ids["match_id"], 99, 510_000, 5, 10000.0, None),
    )
    conn.commit()

    assert len(TR.analyze_trades(conn, ids["match_id"])) == 1


def test_lost_trade_counts_at_exact_duo_distance_boundary(conn, tmp_path):
    ids = _trade_match(conn)
    _timeline_with_minute9(
        conn, ids["match_id"], tmp_path,
        us_taken=600, enemy_taken=200, duo_distance=2500.0,
    )

    assert len(TR.analyze_trades(conn, ids["match_id"])) == 1


def test_lost_trade_has_wellformed_options(conn, tmp_path):
    ids = _trade_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)

    d = TR.analyze_trades(conn, ids["match_id"])[0]

    assert len(d.options) == 3
    assert [o.taken for o in d.options] == [False, True, False]
    assert set(d.context["state_features"]) == {"power", "info_risk", "wave_tempo", "objectives"}


def test_won_trade_is_recorded_positive(conn, tmp_path):
    ids = _trade_match(conn)
    # Mirror image: the enemy duo took 600, we took 100 -> a won exchange. These
    # are persisted flagged positive (hidden from the list, kept for Calibrar).
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=100, enemy_taken=600)

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    assert decisions[0].context["positive_trade"] is True
    assert "ganado" in decisions[0].moment.lower()


def test_minute_below_damage_threshold_is_ignored(conn, tmp_path):
    ids = _trade_match(conn)
    # 60 + 40 = 100 combined < MIN_EXCHANGE_DMG (250): not a real exchange.
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=60, enemy_taken=40)

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_no_timeline_json_returns_empty(conn):
    ids = _trade_match(conn)
    # ten_player_match leaves timeline_json_path NULL -> nothing to analyze.
    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_unilateral_poke_is_not_a_trade(conn, tmp_path):
    """Real FP #1: 'aphelios hace un poco de dano, no hubo intercambio'. Combined
    damage clears MIN_EXCHANGE_DMG but the enemy duo took ~0: not a bilateral
    exchange, must not fire (previously fired as 'lost')."""
    ids = _trade_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=100)

    assert TR.analyze_trades(conn, ids["match_id"]) == []


def test_redside_position_is_not_a_trade(conn, tmp_path):
    """Real FP #2: 'estaba en redside no en bot'. Bilateral exchange (600/200)
    but our position throughout the [8, 9] interval is in the enemy half -> must
    not fire even though the old fallback quadrant/jungla bypass would have let
    it through. Both bounding frames are moved so the interval read is
    unambiguous whichever of them is closest to the sampling point."""
    ids = _trade_match(conn)
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
    ids = _trade_match(conn)
    _timeline_with_minute9(conn, ids["match_id"], tmp_path, us_taken=600, enemy_taken=200)
    conn.execute(
        "UPDATE timeline_frames SET position_x=?, position_y=? WHERE match_id=? "
        "AND participant_id IN (1, 5) AND timestamp_ms IN (?, ?)",
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
    ids = _trade_match(conn)
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
    frames = [damage_frame(timestamp=i * 60_000) for i in range(frame_index)]
    first_jump = damage_frame(**{"1": 600, "6": 200})
    frames.append({**first_jump, "timestamp": len(frames) * 60_000})                                   # delta at frame_index: 600/200
    second_jump = damage_frame(**{"1": 1200, "6": 400})
    for _ in range(11 - frame_index - 1):
        frames.append({**second_jump, "timestamp": len(frames) * 60_000})                               # delta at frame_index+1: 600/200
    set_timeline_json(conn, match_id, tmp_path / "timeline.json", frames)
    _set_duo_distance(conn, match_id, 1000.0)


def test_two_minute_run_reports_its_true_span(conn, tmp_path):
    """A run spanning two consecutive bad minutes must report span_minutes=2 (and
    a "min A-B" text two minutes wide), not the fixed value the old dashboard
    code assumed by counting hyphens in the string."""
    ids = _trade_match(conn)
    _timeline_with_two_bad_minutes(conn, ids["match_id"], tmp_path, frame_index=8)

    decisions = TR.analyze_trades(conn, ids["match_id"])

    assert len(decisions) == 1
    d = decisions[0]
    assert d.context["minute_span"] == "min 7-9"
    assert d.context["span_minutes"] == 2


def test_our_duo_death_excludes_the_interval_that_contains_it(conn, tmp_path):
    """A death at 7:30 lives inside the interval [7, 8] (frame index 8), which
    is death_v1's turf. The old gate keyed it to 7 and skipped [6, 7] instead."""
    ids = _trade_match(conn)
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
    ids = _trade_match(conn)
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
    ids = _trade_match(conn)
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
    ids = _trade_match(conn)
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
    ids = _trade_match(conn)
    _timeline_with_jump(conn, ids["match_id"], tmp_path, frame_index=8,
                        us_taken=600, enemy_taken=200)
    conn.execute(
        "INSERT INTO timeline_frames (match_id, frame_index, timestamp_ms, "
        "participant_id, position_x, position_y) VALUES (?,?,?,?,?,?)",
        (ids["match_id"], 99, 450_000, 1, 10000, 4000))       # bot lane, mid-interval
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

    ids_lost = _trade_match(conn)
    _timeline_with_minute9(conn, ids_lost["match_id"], lost_dir,
                           us_taken=600, enemy_taken=200)
    ids_won = _trade_match(conn, match_id="TEST_WON")
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
        pre_temporal={"contract": "trade_block_open_v1", "guarantee": "block_start_only",
                      "action_time_ms": None, "action_precedence": "unverified"},
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

    ids = _trade_match(conn)
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

# --- Frozen block-opening PRE contract ---------------------------------------

def _temporal_case(conn, tmp_path, *, opening=480190, merged=False):
    import json
    _trade_match(conn)
    if merged:
        _timeline_with_two_bad_minutes(conn, "TEST", tmp_path, frame_index=9)
    else:
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=600, enemy_taken=200)
    path = tmp_path / "timeline.json"
    frames = json.loads(path.read_text(encoding="utf-8"))["info"]["frames"]
    frames[7]["timestamp"] = 420188
    frames[8]["timestamp"] = opening
    frames[9]["timestamp"] = 540200
    for index in (7, 8, 9):
        conn.execute("UPDATE timeline_frames SET timestamp_ms=? WHERE frame_index=?",
                     (frames[index]["timestamp"], index))
    # Distinguishable opening, old and closing economy/positions.
    conn.execute("UPDATE timeline_frames SET level=3, total_gold=100 WHERE frame_index=7")
    conn.execute("UPDATE timeline_frames SET level=18, total_gold=20000 WHERE frame_index=9")
    conn.execute("UPDATE timeline_frames SET position_x=14000, position_y=14000 "
                 "WHERE frame_index=7 AND participant_id=8")
    set_timeline_json(conn, "TEST", path, frames)
    return frames


def _assert_abstains(conn, frames, tmp_path, monkeypatch, reason, *, end=9):
    from unittest.mock import Mock
    set_timeline_json(conn, "TEST", tmp_path / "timeline.json", frames)
    resolved = TR._resolve_trade_pre(conn, "TEST", frames, 9, end)
    assert (resolved.status, resolved.reason) == ("unavailable", reason)
    evaluate = Mock(side_effect=AssertionError("unavailable PRE reached evaluation"))
    monkeypatch.setattr(TR, "_evaluate", evaluate)
    assert TR.analyze_trades(conn, "TEST") == []
    evaluate.assert_not_called()


@pytest.mark.parametrize("offset", [-1, 0, 1, 190, 5000])
def test_pre_uses_actual_opening_and_keeps_nominal_identity(conn, tmp_path, offset):
    opening = 480000 + offset
    frames = _temporal_case(conn, tmp_path, opening=opening)
    pre = TR._resolve_trade_pre(conn, "TEST", frames, 9, 9)
    assert pre.status == "available"
    assert pre.state.game_time_ms == opening
    assert {p.pos_at_ms for p in pre.state.players.values()} == {opening}
    assert pre.state.players[1].level == 11
    assert pre.state.players[1].total_gold == 8000
    assert pre.state.players[8].pos_x == 10100
    d = TR.analyze_trades(conn, "TEST")[0]
    assert (d.detector_id, d.game_time_ms, d.context["time_mmss"]) == ("trade_v1", 480000, "8:00")
    assert d.context["minute_span"] == "min 8-9"
    assert d.context["pre_temporal"] == {
        "contract": "trade_block_open_v1", "anchor_kind": "measured_block_open",
        "nominal_block_start_ms": 480000, "opening_frame_index": 8,
        "target_ms": opening, "source_frame_ms": opening, "age_ms": 0,
        "status": "available", "guarantee": "block_start_only",
        "action_time_ms": None, "action_precedence": "unverified",
    }
    from lol_coach.decisions.features import build_state_features
    assert d.context["state_features"] == build_state_features(conn, "TEST", pre.state, 1, 100)
    # Shared selector still returns the causal older frame when queried before B.
    assert TR.build_world_state(conn, "TEST", opening - 1).players[1].pos_at_ms == 420188


@pytest.mark.parametrize("age", [1, 1000, 60000])
def test_pre_does_not_fall_back_to_older_frame(conn, tmp_path, monkeypatch, age):
    frames = _temporal_case(conn, tmp_path)
    conn.execute("DELETE FROM timeline_frames WHERE frame_index=8")
    conn.execute("UPDATE timeline_frames SET timestamp_ms=? WHERE frame_index=7", (480190-age,))
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "missing_opening_frame")


@pytest.mark.parametrize("offset", [1, 1000])
def test_pre_does_not_use_future_only_frames(conn, tmp_path, monkeypatch, offset):
    frames = _temporal_case(conn, tmp_path)
    conn.execute("DELETE FROM timeline_frames WHERE frame_index IN (7,8)")
    conn.execute("UPDATE timeline_frames SET timestamp_ms=? WHERE frame_index=9", (480190+offset,))
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "missing_opening_frame")


@pytest.mark.parametrize("all_players", [False, True])
def test_missing_opening_abstains_before_options(conn, tmp_path, monkeypatch, all_players):
    frames = _temporal_case(conn, tmp_path)
    conn.execute("DELETE FROM timeline_frames WHERE frame_index=8" +
                 ("" if all_players else " AND participant_id=10"))
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "missing_opening_frame")


@pytest.mark.parametrize("timestamp", [None, -1, True, "480190", 480190.0])
def test_invalid_raw_opening_has_explicit_reason(conn, tmp_path, monkeypatch, timestamp):
    frames = _temporal_case(conn, tmp_path)
    if timestamp is None:
        del frames[8]["timestamp"]
    else:
        frames[8]["timestamp"] = timestamp
    _assert_abstains(conn, frames, tmp_path, monkeypatch,
                     "missing_opening_timestamp" if timestamp is None else "invalid_window_order")


@pytest.mark.parametrize("timestamp", [480190, 480189, None, True, -1, "540200", 540200.0])
def test_invalid_raw_window_order_abstains(conn, tmp_path, monkeypatch, timestamp):
    frames = _temporal_case(conn, tmp_path)
    frames[9]["timestamp"] = timestamp
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "invalid_window_order")


@pytest.mark.parametrize("mutation", ["timestamp", "index", "duplicate", "unknown_player"])
def test_inconsistent_opening_mapping_abstains(conn, tmp_path, monkeypatch, mutation):
    frames = _temporal_case(conn, tmp_path)
    if mutation == "timestamp":
        conn.execute("UPDATE timeline_frames SET timestamp_ms=480191 WHERE frame_index=8 AND participant_id=10")
    elif mutation == "index":
        conn.execute("UPDATE timeline_frames SET frame_index=88 WHERE frame_index=8 AND participant_id=10")
    elif mutation == "unknown_player":
        conn.execute("UPDATE timeline_frames SET participant_id=11 WHERE frame_index=8 AND participant_id=10")
    else:
        conn.execute("INSERT INTO timeline_frames (match_id, frame_index, timestamp_ms, participant_id) "
                     "VALUES ('TEST', 88, 480190, 10)")
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "inconsistent_frame_mapping")


@pytest.mark.parametrize("offset,reason", [(-1, "stale_pre"), (1, "future_pre"), (None, "missing_opening_frame")])
def test_world_state_must_confirm_all_exact_sources(conn, tmp_path, monkeypatch, offset, reason):
    frames = _temporal_case(conn, tmp_path)
    real_build = TR.build_world_state
    def altered(*args):
        ws = real_build(*args)
        ws.players[10].pos_at_ms = None if offset is None else 480190 + offset
        return ws
    monkeypatch.setattr(TR, "build_world_state", altered)
    _assert_abstains(conn, frames, tmp_path, monkeypatch, reason)


def test_merged_run_uses_first_opening_and_abstains_as_a_whole(conn, tmp_path, monkeypatch):
    frames = _temporal_case(conn, tmp_path, merged=True)
    d, = TR.analyze_trades(conn, "TEST")
    assert d.context["minute_span"] == "min 8-10"
    assert d.context["span_minutes"] == 2
    assert d.context["duo_damage_taken"] == 1200
    assert d.context["pre_temporal"]["target_ms"] == 480190
    conn.execute("DELETE FROM timeline_frames WHERE frame_index=8 AND participant_id=10")
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "missing_opening_frame", end=10)


def test_intermediate_boundary_of_merged_run_must_be_ordered(conn, tmp_path, monkeypatch):
    frames = _temporal_case(conn, tmp_path, merged=True)
    frames[9]["timestamp"] = frames[10]["timestamp"] + 1
    _assert_abstains(conn, frames, tmp_path, monkeypatch, "invalid_window_order", end=10)


def test_pre_ignores_nearer_future_frame_and_changed_outcome(conn, tmp_path, monkeypatch):
    frames = _temporal_case(conn, tmp_path)
    before, = TR.analyze_trades(conn, "TEST")
    conn.execute("UPDATE timeline_frames SET timestamp_ms=480191, total_gold=999999, level=1 WHERE frame_index=9")
    frames[9]["timestamp"] = 480191
    frames[9]["participantFrames"]["1"]["damageStats"]["physicalDamageTaken"] = 1000
    frames[10]["participantFrames"]["1"]["damageStats"]["physicalDamageTaken"] = 1000
    insert_event(conn, "TEST", 550000, "CHAMPION_KILL", {"victimId": 5, "killerId": 8})
    monkeypatch.setattr(TR, "_our_stats_at", lambda *args: {"hp_pct": 0.1, "mana_pct": 0.1})
    set_timeline_json(conn, "TEST", tmp_path / "timeline.json", frames)
    after, = TR.analyze_trades(conn, "TEST")
    assert before.context["pre_temporal"] == after.context["pre_temporal"]
    assert before.context["state_features"] == after.context["state_features"]
    assert before.context["duo_damage_taken"] != after.context["duo_damage_taken"]
    assert before.context["consequence"] != after.context["consequence"]
    assert before.context["our_hp_pct_end"] != after.context["our_hp_pct_end"]


def test_pre_event_cutoffs_and_historical_lookbacks(conn, tmp_path):
    frames = _temporal_case(conn, tmp_path)
    b = frames[8]["timestamp"]
    for ts in (b-1, b, b+1):
        insert_event(conn, "TEST", ts, "WARD_PLACED", {"creatorId": 1, "wardType": "YELLOW_TRINKET"})
        insert_event(conn, "TEST", ts, "CHAMPION_KILL", {"killerId": 2, "victimId": 7})
    # Visibility uses strict < B; B/B+1 appearances cannot replace B-1.
    for ts in (b-1, b, b+1):
        insert_event(conn, "TEST", ts, "WARD_KILL", {"killerId": 8})
    for pid, cs in ((1, 60), (6, 80)):
        conn.execute("INSERT INTO timeline_frames (match_id,frame_index,timestamp_ms,participant_id,"
                     "position_x,position_y,minions_killed,jungle_minions_killed) VALUES (?,?,?,?,?,?,?,?)",
                     ("TEST", 5, b-180000, pid, 10000, 4000, cs, 0))
    # Snapshot immediately after B must not enter economy or current CS.
    conn.execute("UPDATE timeline_frames SET timestamp_ms=?, minions_killed=9999 WHERE frame_index=9", (b+1,))
    pre = TR._resolve_trade_pre(conn, "TEST", frames, 9, 9)
    assert pre.state.players[2].kills == 2
    assert pre.state.players[7].deaths == 2
    d, = TR.analyze_trades(conn, "TEST")
    sf = d.context["state_features"]
    assert sf["info_risk"]["wards_nearby"] == 2
    assert sf["info_risk"]["enemy_jg_unseen_s"] == 0.0  # B-1 rounds to zero
    from lol_coach.context import last_event_appearance
    assert last_event_appearance(conn, "TEST", 8, b) == b-1
    assert sf["wave_tempo"]["cs_last_3min_diff"] == 20
    assert sf["wave_tempo"]["wave_state"] is not None


def test_pre_metadata_roundtrip_does_not_upgrade_legacy(conn, tmp_path):
    import copy
    import json
    from lol_coach.analysis import insert_decision
    _temporal_case(conn, tmp_path)
    d, = TR.analyze_trades(conn, "TEST")
    legacy = copy.deepcopy(d)
    legacy.context.pop("pre_temporal")
    old_id = insert_decision(conn, legacy)
    new_id = insert_decision(conn, d)
    rows = {r["id"]: json.loads(r["context_json"]) for r in conn.execute("SELECT id,context_json FROM decisions")}
    assert "pre_temporal" not in rows[old_id]
    assert rows[new_id]["pre_temporal"] == d.context["pre_temporal"]
    assert rows[new_id]["pre_temporal"]["action_precedence"] == "unverified"

@pytest.mark.parametrize("start,end", [(0, 9), (9, 8), (9, 11)])
def test_pre_rejects_unavailable_or_empty_index_range(conn, tmp_path, start, end):
    frames = _temporal_case(conn, tmp_path)
    pre = TR._resolve_trade_pre(conn, "TEST", frames, start, end)
    assert (pre.status, pre.reason) == ("unavailable", "invalid_window_order")


def test_exact_timestamps_preserve_numerical_evaluation(conn, tmp_path):
    from lol_coach.decisions.features import build_state_features
    _temporal_case(conn, tmp_path, opening=480000)
    d, = TR.analyze_trades(conn, "TEST")
    sf = build_state_features(conn, "TEST", TR.build_world_state(conn, "TEST", 480000), 1, 100)
    expected = TR._evaluate(TR.TradeFacts(
        match_id="TEST", t_ms=480000, m_start=9, m_end=9, positive=False,
        d_us=600, d_en=200, hp_pct=None, mana_pct=None, consequence=None,
        state_features=sf, pre_temporal=d.context["pre_temporal"],
    ))
    assert d == expected


def test_pre_ignores_later_frame_closer_to_nominal_boundary(conn, tmp_path):
    opening, closing, nominal = 479810, 479999, 480000
    frames = _temporal_case(conn, tmp_path, opening=opening)
    frames[9]["timestamp"] = closing
    conn.execute(
        "UPDATE timeline_frames SET timestamp_ms=?, level=18, total_gold=20000 WHERE frame_index=9",
        (closing,),
    )
    conn.execute(
        "UPDATE timeline_frames SET position_x=14000, position_y=14000 "
        "WHERE frame_index=9 AND participant_id=8"
    )
    set_timeline_json(conn, "TEST", tmp_path / "timeline.json", frames)
    assert opening < closing < nominal
    assert abs(closing - nominal) < abs(opening - nominal)
    pre = TR._resolve_trade_pre(conn, "TEST", frames, 9, 9)
    assert pre.status == "available"
    assert {p.pos_at_ms for p in pre.state.players.values()} == {opening}
    assert pre.state.players[1].level == 11
    assert pre.state.players[1].total_gold == 8000
    assert pre.state.players[8].pos_x == 10100
    d, = TR.analyze_trades(conn, "TEST")
    assert d.game_time_ms == nominal
    assert d.context["pre_temporal"]["target_ms"] == opening
    assert d.context["pre_temporal"]["source_frame_ms"] == opening
    from lol_coach.decisions.features import build_state_features
    assert d.context["state_features"] == build_state_features(conn, "TEST", pre.state, 1, 100)


# --- Scanner extraction (_collect_trade_candidates) ---------------------------
#
# Equivalence method: the module as it was at 827bd07 (the last commit before the
# extraction) is loaded from git into a throwaway module inside the same package,
# so it runs against the very same helpers, fixtures and DB. Every scenario is
# fed to both versions and their decisions are compared through repr(asdict(d)):
# repr keeps key order and distinguishes 1 from 1.0 and True from 1, so any
# drift in values, types or ordering fails. The scenarios cover every exit of the
# scanner (early returns, each guard, exclusion/won sets, merge, snapshots, PRE
# abstention) plus a multi-run match that exercises the merge on both sides.

import dataclasses as _dc
import importlib.util as _ilu
import json as _json
import subprocess as _sp

_HEAD_REV = "827bd07"
_REPO = _Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def trade_head():
    try:
        source = _sp.run(
            ["git", "show", f"{_HEAD_REV}:lol_coach/decisions/trade.py"],
            cwd=_REPO, capture_output=True, check=True, text=True, encoding="utf-8",
        ).stdout
    except (OSError, _sp.CalledProcessError):
        pytest.skip(f"git history with {_HEAD_REV} is not available")
    assert "_collect_trade_candidates" not in source  # really the pre-extraction code
    name = "lol_coach.decisions._trade_at_" + _HEAD_REV
    module = _ilu.module_from_spec(_ilu.spec_from_loader(name, loader=None))
    module.__package__ = "lol_coach.decisions"
    _sys.modules[name] = module
    try:
        exec(compile(source, f"<{_HEAD_REV}:trade.py>", "exec"), module.__dict__)
        yield module
    finally:
        _sys.modules.pop(name, None)


def _serialize(decisions):
    return [repr(_dc.asdict(d)) for d in decisions]


def _add_session(conn, match_id, session_dir):
    conn.execute(
        "INSERT INTO sessions (match_id, session_dir, start_utc, ingested_at_utc) "
        "VALUES (?,?,?,?)", (match_id, str(session_dir), "2026-01-01T00:00:00Z",
                             "2026-01-01T00:00:00Z"))
    conn.commit()


def _snapshot(gt, hp, max_hp, mana, max_mana):
    return {"data": {"gameData": {"gameTime": gt}, "activePlayer": {"championStats": {
        "currentHealth": hp, "maxHealth": max_hp,
        "resourceValue": mana, "resourceMax": max_mana}}}}


# Per-frame (our pid1, enemy pid6) damage DELTAS of the multi-run match.
_RICH_DELTAS = {
    3: (600, 200),       # bad
    4: (700, 200.5),     # bad, consecutive -> merged with 3 (float stays raw)
    5: (200, 700),       # good
    6: (150, 100),       # enemy took too little: unilateral, skipped
    8: (800, 250),       # bad
    9: (200, 800),       # good
    10: (250, 850),      # good, consecutive goods are NOT merged
    11: (400, 300),      # not lost, but oom at its closing edge -> bad; adjacent to a good
}
_RICH_SNAPSHOTS = [(180.0, 700, 1000, 600, 1000),   # start of the merged run: must NOT win
                   (240.0, 400, 1000, 50, 1000), (720.0, 900, 1000, 100, 1000)]


def _rich_match(conn, tmp_path, *, with_session=True):
    ten_player_match(conn, "TEST", frame_times=[(i, i * 60_000) for i in range(16)])
    frames, us, en = [], 0, 0
    for i in range(16):
        d_us, d_en = _RICH_DELTAS.get(i, (0, 0))
        us, en = us + d_us, en + d_en
        frames.append(damage_frame(timestamp=i * 60_000, **{"1": us, "6": en}))
    set_timeline_json(conn, "TEST", tmp_path / "timeline.json", frames)
    _set_duo_distance(conn, "TEST", 1000.0)
    if with_session:
        session = tmp_path / "session"
        session.mkdir()
        (session / "snapshots.jsonl").write_text(
            "".join(_json.dumps(_snapshot(*s)) + "\n" for s in _RICH_SNAPSHOTS),
            encoding="utf-8")
        _add_session(conn, "TEST", session)
    return frames


def _scenario(name, conn, tmp_path):
    if name == "no_match":
        return
    if name == "no_timeline":
        _trade_match(conn)
        return
    if name == "not_bot":
        _trade_match(conn)
        conn.execute("UPDATE participants SET team_position='MIDDLE' WHERE participant_id=1")
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=600, enemy_taken=200)
        return
    if name == "duo_incomplete":
        _trade_match(conn)
        conn.execute("UPDATE participants SET team_position='JUNGLE' WHERE participant_id=10")
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=600, enemy_taken=200)
        return
    if name == "unreadable_timeline":
        _trade_match(conn)
        (tmp_path / "timeline.json").write_text("{not json", encoding="utf-8")
        conn.execute("UPDATE matches SET timeline_json_path=?", (str(tmp_path / "timeline.json"),))
        conn.commit()
        return
    if name == "too_few_frames":
        _trade_match(conn)
        set_timeline_json(conn, "TEST", tmp_path / "timeline.json",
                          [damage_frame(timestamp=i * 60_000) for i in range(3)])
        return
    if name == "lost":
        _trade_match(conn)
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=600, enemy_taken=200)
    elif name == "won":
        _trade_match(conn)
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=200, enemy_taken=600)
    elif name == "below_threshold":
        _trade_match(conn)
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=120, enemy_taken=100)
    elif name == "unilateral":
        _trade_match(conn)
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=600, enemy_taken=50)
    elif name == "duo_far":
        _trade_match(conn)
        _timeline_with_minute9(conn, "TEST", tmp_path, us_taken=600, enemy_taken=200,
                               duo_distance=4000.0)
    elif name == "merged_two_minutes":
        _trade_match(conn)
        _timeline_with_two_bad_minutes(conn, "TEST", tmp_path, frame_index=8)
    elif name == "player_death":
        _trade_match(conn)
        _timeline_with_jump(conn, "TEST", tmp_path, frame_index=8, us_taken=600, enemy_taken=200)
        insert_event(conn, "TEST", 450_000, "CHAMPION_KILL", {"victimId": 1, "killerId": 8})
    elif name == "enemy_kill":
        _trade_match(conn)
        _timeline_with_jump(conn, "TEST", tmp_path, frame_index=8, us_taken=300, enemy_taken=250)
        insert_event(conn, "TEST", 450_000, "CHAMPION_KILL", {"victimId": 6, "killerId": 1})
    elif name == "pre_offset_merged":
        _temporal_case(conn, tmp_path, opening=480190, merged=True)
    elif name == "pre_abstains":
        _temporal_case(conn, tmp_path)
        conn.execute("DELETE FROM timeline_frames WHERE frame_index=8 AND participant_id=3")
    elif name == "rich":
        _rich_match(conn, tmp_path)
        # Partner death after the first run: a consequence, not an exclusion.
        insert_event(conn, "TEST", 300_000, "CHAMPION_KILL", {"victimId": 5, "killerId": 6})
        # Our own death inside the interval of frame 9 excludes that good minute.
        insert_event(conn, "TEST", 510_000, "CHAMPION_KILL", {"victimId": 1, "killerId": 8})
    elif name == "rich_no_session":
        _rich_match(conn, tmp_path, with_session=False)
    else:
        raise AssertionError(name)
    conn.commit()


_SCENARIOS = ["no_match", "no_timeline", "not_bot", "duo_incomplete", "unreadable_timeline",
              "too_few_frames", "lost", "won", "below_threshold", "unilateral", "duo_far",
              "merged_two_minutes", "player_death", "enemy_kill", "pre_offset_merged",
              "pre_abstains", "rich", "rich_no_session"]
_EMITS = {"lost", "won", "merged_two_minutes", "enemy_kill", "pre_offset_merged", "rich",
          "rich_no_session"}


@pytest.mark.parametrize("name", _SCENARIOS)
def test_extraction_is_equivalent_to_pre_extraction_detector(conn, tmp_path, trade_head, name):
    _scenario(name, conn, tmp_path)
    expected = trade_head.analyze_trades(conn, "TEST")
    actual = TR.analyze_trades(conn, "TEST")
    assert _serialize(actual) == _serialize(expected)
    # Guard against a vacuous pass: the scenarios meant to emit really do.
    assert bool(expected) == (name in _EMITS)


def test_rich_match_emits_every_run_in_order(conn, tmp_path):
    _scenario("rich", conn, tmp_path)
    spans = [d.context["minute_span"] for d in TR.analyze_trades(conn, "TEST")]
    assert spans == ["min 2-4", "min 4-5", "min 7-8", "min 9-10", "min 10-11"]


def _rich_loaders(frames, calls):
    def timeline_loader(path):
        calls.append(("timeline", path))
        return {"info": {"frames": frames}}

    def snapshot_loader(session_dir):
        calls.append(("snapshots", session_dir))
        return [(gt, _snapshot(gt, *rest)["data"]) for gt, *rest in _RICH_SNAPSHOTS]

    def has_resource(champion):
        calls.append(("resource", champion))
        return True

    return timeline_loader, snapshot_loader, has_resource


def test_collector_returns_merged_runs_with_their_raw_facts(conn, tmp_path):
    frames = _rich_match(conn, tmp_path)
    calls = []
    tl, sl, hr = _rich_loaders(frames, calls)

    inv = TR._collect_trade_candidates(conn, "TEST", timeline_loader=tl,
                                       snapshot_loader=sl, has_resource=hr)

    assert (inv.match_id, inv.us_id, inv.our_team) == ("TEST", 1, 100)
    assert sorted(inv.my_duo) == [1, 5] and sorted(inv.enemy_duo) == [6, 10]
    assert set(inv.parts) == set(range(1, 11)) and inv.frames == frames
    none = {"hp_pct": None, "mana_pct": None}
    assert inv.candidates == [
        TR.TradeCandidate(3, 4, "bad", 1300, 400.5, {"hp_pct": 0.4, "mana_pct": 0.05}),
        TR.TradeCandidate(5, 5, "good", 200, 700, {"hp_pct": 0.4, "mana_pct": 0.05}),
        TR.TradeCandidate(8, 8, "bad", 800, 250, none),
        TR.TradeCandidate(9, 9, "good", 200, 800, none),
        TR.TradeCandidate(10, 10, "good", 250, 850, none),
        TR.TradeCandidate(11, 11, "bad", 400, 300, {"hp_pct": 0.9, "mana_pct": 0.1}),
    ]
    assert type(inv.candidates[0].d_en) is float  # raw sum, never int()-ed here
    assert calls == [("resource", "Jinx"), ("timeline", str(tmp_path / "timeline.json")),
                     ("snapshots", str(tmp_path / "session"))]


def test_collector_never_resolves_pre_or_evaluates(conn, tmp_path, monkeypatch):
    _scenario("rich", conn, tmp_path)

    def boom(*a, **k):
        raise AssertionError("collector reached PRE/facts/evaluation")

    for name in ("_resolve_trade_pre", "_gather_trade_facts", "_evaluate",
                 "build_world_state", "build_state_features", "build_action", "setup_score"):
        monkeypatch.setattr(TR, name, boom)

    inv = TR._collect_trade_candidates(conn, "TEST")

    assert [(c.m_start, c.m_end, c.kind) for c in inv.candidates] == [
        (3, 4, "bad"), (5, 5, "good"), (8, 8, "bad"), (10, 10, "good"), (11, 11, "bad")]


def test_collector_reads_sources_only_through_injected_loaders(conn, tmp_path, monkeypatch):
    frames = _rich_match(conn, tmp_path)
    # Stored locations that do not exist: only the loaders can answer for them.
    conn.execute("UPDATE matches SET timeline_json_path='frozen/timeline.json'")
    conn.execute("UPDATE sessions SET session_dir='frozen/session'")
    conn.commit()

    def forbidden(*a, **k):
        raise AssertionError("collector touched the filesystem or the default readers")

    monkeypatch.setattr(_Path, "read_text", forbidden)
    monkeypatch.setattr(TR, "load_snapshots", forbidden)
    monkeypatch.setattr(TR, "has_meaningful_resource", forbidden)
    calls = []
    tl, sl, hr = _rich_loaders(frames, calls)

    inv = TR._collect_trade_candidates(conn, "TEST", timeline_loader=tl,
                                       snapshot_loader=sl, has_resource=hr)

    assert calls == [("resource", "Jinx"), ("timeline", "frozen/timeline.json"),
                     ("snapshots", "frozen/session")]
    assert inv.candidates[0].stats == {"hp_pct": 0.4, "mana_pct": 0.05}


def test_collector_default_loaders_are_the_detector_readers(conn, tmp_path, monkeypatch):
    _scenario("rich", conn, tmp_path)
    seen = []
    real = TR.load_snapshots
    monkeypatch.setattr(TR, "load_snapshots", lambda d: seen.append(d) or real(d))

    inv = TR._collect_trade_candidates(conn, "TEST")

    assert seen == [tmp_path / "session"]
    assert inv.candidates[0].stats["hp_pct"] == 0.4


def test_collector_skips_snapshot_loader_without_session(conn, tmp_path):
    frames = _rich_match(conn, tmp_path, with_session=False)
    calls = []
    tl, sl, hr = _rich_loaders(frames, calls)

    inv = TR._collect_trade_candidates(conn, "TEST", timeline_loader=tl,
                                       snapshot_loader=sl, has_resource=hr)

    assert [c for c in calls if c[0] == "snapshots"] == []
    assert all(c.stats == {"hp_pct": None, "mana_pct": None} for c in inv.candidates)


@pytest.mark.parametrize("name", ["no_match", "no_timeline", "not_bot", "duo_incomplete",
                                  "unreadable_timeline", "too_few_frames"])
def test_collector_returns_none_where_the_detector_gives_up(conn, tmp_path, name):
    _scenario(name, conn, tmp_path)
    assert TR._collect_trade_candidates(conn, "TEST") is None


@pytest.mark.parametrize("exc", [OSError, ValueError])
def test_collector_returns_none_when_timeline_loader_fails(conn, tmp_path, exc):
    _scenario("lost", conn, tmp_path)

    def failing(path):
        raise exc("unreadable")

    assert TR._collect_trade_candidates(conn, "TEST", timeline_loader=failing) is None


def test_collector_returns_empty_inventory_when_nothing_qualifies(conn, tmp_path):
    _scenario("below_threshold", conn, tmp_path)
    inv = TR._collect_trade_candidates(conn, "TEST")
    assert inv is not None and inv.candidates == [] and len(inv.frames) == 11
