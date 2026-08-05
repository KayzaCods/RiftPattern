"""Tests de exposicion sin muerte (#17 Fase 1, report-only).

El criterio: estas EXPUESTO si estas profundo (safety_ratio bajo: 1 = tu torre,
0 = torre enemiga) Y ademas hay MIAs locales (no ves de donde viene el peligro).
Ambas condiciones, no una: profundo con todos a la vista es juego normal, y MIAs
estando en tu mitad tampoco es exposicion.
"""
from __future__ import annotations

from conftest import insert_event, ten_player_match

from lol_coach import exposure as EXP


def test_is_exposed_requires_both_depth_and_mias():
    assert EXP.is_exposed(0.30, 3) is True
    assert EXP.is_exposed(0.30, 1) is False    # profundo pero los ves
    assert EXP.is_exposed(0.80, 3) is False    # MIAs pero en zona segura


def test_is_exposed_thresholds_are_inclusive():
    assert EXP.is_exposed(0.35, 2) is True     # justo en el limite: cuenta
    assert EXP.is_exposed(0.36, 2) is False
    assert EXP.is_exposed(0.35, 1) is False


def test_is_exposed_missing_data_is_never_exposed():
    # safety_ratio None pasa cuando no hay posicion; nunca inventamos exposicion.
    assert EXP.is_exposed(None, 3) is False
    assert EXP.is_exposed(0.20, None) is False


def test_severity_clamps_to_unit_interval():
    assert EXP.severity(0.42) == 0.42
    assert EXP.severity(None) == 0.0
    assert EXP.severity(1.5) == 1.0
    assert EXP.severity(-0.2) == 0.0


def _fact(ts=10 * 60_000, gold=0, champ="Sona", died=None, safety=0.2, mia=3, threat=0.7):
    return EXP.ExposureFacts(
        match_id="M", game_time_ms=ts, champion=champ,
        safety_ratio=safety, enemies_unseen=mia, local_threat=threat,
        local_gold_diff=gold, died_within_s=died,
    )


def _ep(start=10 * 60_000, gold=0, champ="Sona", died=False, n_frames=1):
    return EXP.ExposureEpisode(
        match_id="M", start_ms=start, end_ms=start + (n_frames - 1) * 60_000,
        n_frames=n_frames, champion=champ, peak_severity=0.7,
        worst_safety_ratio=0.2, max_enemies_unseen=3,
        gold_band=EXP.gold_band_of(gold), died=died,
    )


def test_phase_of_covers_the_whole_game():
    # A diferencia de trade_queue.phase_of (ventana de LINEAS min 2-14), aqui la
    # partida entera cuenta: la exposicion tardia es la mas frecuente (medido).
    assert EXP.phase_of(3 * 60_000) == "early"
    assert EXP.phase_of(10 * 60_000) == "mid"
    assert EXP.phase_of(25 * 60_000) == "late"


def test_gold_band_of_bins_local_gold_diff():
    assert EXP.gold_band_of(-500) == "behind"
    assert EXP.gold_band_of(0) == "even"
    assert EXP.gold_band_of(500) == "ahead"
    assert EXP.gold_band_of(None) == "even"


# --------------------------------------------------- segmentacion por episodios
def test_segment_merges_consecutive_frames_into_one_episode():
    # Una sobreextension de min 18->20 (frames a 60s) es UN episodio, no 3.
    frames = [_fact(ts=18 * 60_000), _fact(ts=19 * 60_000), _fact(ts=20 * 60_000)]
    eps = EXP.segment_episodes(frames)
    assert len(eps) == 1
    assert eps[0].n_frames == 3
    assert eps[0].start_ms == 18 * 60_000
    assert eps[0].end_ms == 20 * 60_000


def test_segment_splits_when_gap_exceeds_threshold():
    # 10:00 y 15:00 estan a 5 min (> EPISODE_GAP_MS) -> dos episodios distintos.
    frames = [_fact(ts=10 * 60_000), _fact(ts=15 * 60_000)]
    eps = EXP.segment_episodes(frames)
    assert len(eps) == 2
    assert [e.n_frames for e in eps] == [1, 1]


def test_segment_single_frame_is_a_one_frame_episode():
    eps = EXP.segment_episodes([_fact(ts=12 * 60_000)])
    assert len(eps) == 1
    assert eps[0].n_frames == 1
    assert eps[0].start_ms == eps[0].end_ms == 12 * 60_000


def test_segment_episode_died_if_any_frame_died():
    # La muerte pertenece al EPISODIO, no al minuto: muere en el frame de enmedio.
    frames = [_fact(ts=18 * 60_000), _fact(ts=19 * 60_000, died=8), _fact(ts=20 * 60_000)]
    eps = EXP.segment_episodes(frames)
    assert len(eps) == 1 and eps[0].died is True


def test_segment_episode_takes_peak_severity_and_worst_safety():
    frames = [
        _fact(ts=18 * 60_000, safety=0.30, mia=2, threat=0.50),
        _fact(ts=19 * 60_000, safety=0.10, mia=5, threat=0.90),   # el peor momento
        _fact(ts=20 * 60_000, safety=0.20, mia=3, threat=0.30),
    ]
    e = EXP.segment_episodes(frames)[0]
    assert e.peak_severity == 0.90
    assert e.worst_safety_ratio == 0.10
    assert e.max_enemies_unseen == 5


def test_segment_of_nothing_is_empty():
    assert EXP.segment_episodes([]) == []


# ------------------------------------------------- aggregate SOBRE episodios
def test_aggregate_splits_survived_from_died_by_episode():
    # La fraccion SOBREVIVIDA es la senal nueva, ahora contada por EPISODIO.
    agg = EXP.aggregate([_ep(died=True), _ep(), _ep(), _ep()])
    assert agg["n"] == 4
    assert agg["n_died"] == 1
    assert agg["n_survived"] == 3
    assert agg["pct_survived"] == 75.0


def test_aggregate_reports_raw_frames_and_cluster_size():
    # Continuidad: el N crudo de frames y el tamano medio de cluster (recordatorio
    # de por que la FP se mide sobre episodios, no frames autocorrelados).
    agg = EXP.aggregate([_ep(n_frames=3), _ep(n_frames=1)])
    assert agg["n"] == 2                 # episodios
    assert agg["n_frames"] == 4          # frames crudos
    assert agg["frames_per_episode"] == 2.0


def test_aggregate_distributions_and_density_by_episode():
    ex = [
        _ep(start=3 * 60_000, gold=-500, champ="Sona"),
        _ep(start=25 * 60_000, gold=500, champ="Sona"),
        _ep(start=25 * 60_000, gold=0, champ="Ashe"),
    ]
    agg = EXP.aggregate(ex, n_matches=2)
    assert agg["by_phase"] == {"early": 1, "late": 2}
    assert agg["by_champion"] == {"Sona": 2, "Ashe": 1}
    assert agg["by_gold_band"] == {"behind": 1, "ahead": 1, "even": 1}
    assert agg["per_match"] == 1.5


def test_aggregate_of_nothing_is_zeroed_not_a_crash():
    agg = EXP.aggregate([], n_matches=0)
    assert agg["n"] == 0
    assert agg["pct_survived"] == 0.0
    assert agg["per_match"] == 0.0
    assert agg["n_frames"] == 0
    assert agg["frames_per_episode"] == 0.0
    assert agg["by_phase"] == {}


# --------------------------------------------------- escaneo e2e (con DB real)
# Geometria del match sintetico (conftest): somos pid 1, equipo 100 (base abajo-
# izquierda). Torres reales de features.TOWERS.
#   (13500, 4500) = pegado a la torre bot ENEMIGA -> safety_ratio ~0.07 (profundo)
#                   y MIAs locales = Caitlyn (~3373u, < THREAT_RADIUS 5500) + la
#                   jungla enemiga (siempre cuenta) = 2  -> EXPUESTO
#   posicion por defecto (10000, 4000) -> safety_ratio ~0.56 -> NO expuesto
FRAME_EXPOSED_MS = 540_000


def _move(conn, match_id, pid, ts, x, y):
    conn.execute(
        "UPDATE timeline_frames SET position_x=?, position_y=? "
        "WHERE match_id=? AND participant_id=? AND timestamp_ms=?",
        (x, y, match_id, pid, ts))
    conn.commit()


def test_scan_match_flags_only_the_deep_blind_frame(conn):
    ids = ten_player_match(conn)
    _move(conn, ids["match_id"], 1, FRAME_EXPOSED_MS, 13500, 4500)

    found = EXP.scan_match(conn, ids["match_id"])

    assert [e.game_time_ms for e in found] == [FRAME_EXPOSED_MS]
    e = found[0]
    assert e.safety_ratio <= EXP.MAX_SAFETY_RATIO
    assert e.enemies_unseen >= EXP.MIN_ENEMIES_UNSEEN
    assert e.champion == "Jinx"
    assert e.died_within_s is None          # sobrevivio: la senal nueva


def test_scan_match_skips_frames_where_we_were_dead(conn):
    ids = ten_player_match(conn)
    _move(conn, ids["match_id"], 1, FRAME_EXPOSED_MS, 13500, 4500)
    # Muerte 2s antes del frame: cualquier tiempo de respawn nos deja muertos ahi,
    # asi que el frame expuesto no cuenta (no habia decision que tomar).
    insert_event(conn, ids["match_id"], FRAME_EXPOSED_MS - 2_000, "CHAMPION_KILL",
                 {"killerId": 8, "victimId": 1, "position": {"x": 13500, "y": 4500}})
    conn.commit()

    assert EXP.scan_match(conn, ids["match_id"]) == []


def test_scan_match_attributes_a_death_inside_the_window(conn):
    ids = ten_player_match(conn)
    _move(conn, ids["match_id"], 1, FRAME_EXPOSED_MS, 13500, 4500)
    # Muerte 8s DESPUES del frame expuesto: dentro de DEATH_WINDOW_MS (15s).
    insert_event(conn, ids["match_id"], FRAME_EXPOSED_MS + 8_000, "CHAMPION_KILL",
                 {"killerId": 8, "victimId": 1, "position": {"x": 13500, "y": 4500}})
    conn.commit()

    found = EXP.scan_match(conn, ids["match_id"])

    assert len(found) == 1
    assert found[0].died_within_s == 8
