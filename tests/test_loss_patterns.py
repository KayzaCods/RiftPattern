"""Pruebas sinteticas para patrones de derrota y su loader read-only."""
from __future__ import annotations

import json
import sqlite3

from lol_coach import db
from lol_coach import loss_patterns as LP
from scripts.loss_patterns import load_games


def game(*, win=False, duration_s=1800, gold=None, levels=None, kills=None,
         buildings=None, elites=None):
    return LP.GameView(
        match_id="m", win=win, duration_s=duration_s, our_id=1,
        team_of={1: 100, 2: 100, 3: 200, 4: 200},
        gold_by_min=gold if gold is not None else [{1: 500, 2: 500, 3: 500, 4: 500}],
        level_by_min=levels if levels is not None else [{1: 1, 2: 1, 3: 1, 4: 1}],
        kills=kills if kills is not None else [],
        buildings=buildings if buildings is not None else [],
        elites=elites if elites is not None else [],
    )


def test_team_diffs_clamp_to_last_frame_and_count_only_champion_killers():
    g = game(
        gold=[{1: 1000, 2: 2000, 3: 900, 4: 1000},
              {1: 2000, 2: 1000, 3: 1000, 4: 1500}],
        levels=[{1: 5, 2: 6, 3: 4, 4: 5}, {1: 8, 2: 7, 3: 9, 4: 8}],
        kills=[LP.KillEvent(10, 3, 1, (), None, None),
               LP.KillEvent(20, 1, 4, (), None, None),
               LP.KillEvent(30, 2, 0, (), None, None),
               LP.KillEvent(40, 4, 99, (), None, None)],
        buildings=[LP.BuildingEvent(10, 200, "TOWER_BUILDING"),
                   LP.BuildingEvent(20, 100, "TOWER_BUILDING"),
                   LP.BuildingEvent(30, 200, "INHIBITOR_BUILDING"),
                   LP.BuildingEvent(40, 100, "TOWER_BUILDING")],
    )
    assert LP.team_gold_diff(g, 0) == 1100
    assert LP.team_gold_diff(g, 99) == 500
    assert LP.team_level_diff(g, 99) == -2
    assert LP.kill_diff(g, 10) == 1
    assert LP.kill_diff(g, 30) == 0
    assert LP.kill_diff(g, 40) == 0  # killer no campeon se ignora
    assert LP.tower_diff(g, 10) == 1
    assert LP.tower_diff(g, 20) == 0
    assert LP.tower_diff(g, 30) == 0  # el inhibidor no cuenta
    assert LP.tower_diff(g, 40) == -1
    other_side = LP.GameView(
        match_id=g.match_id, win=g.win, duration_s=g.duration_s, our_id=3,
        team_of=g.team_of, gold_by_min=g.gold_by_min, level_by_min=g.level_by_min,
        kills=g.kills, buildings=g.buildings, elites=g.elites,
    )
    assert LP.team_gold_diff(other_side, 0) == -1100


def test_early_deaths_and_chains_include_gap_edge_but_exclude_cutoff():
    g = game(kills=[
        LP.KillEvent(0, 1, 3, (), 1, 1),
        LP.KillEvent(96_000, 1, 3, (), 1, 1),
        LP.KillEvent(192_001, 1, 3, (), 1, 1),
        LP.KillEvent(839_999, 1, 3, (), 1, 1),
        LP.KillEvent(840_000, 1, 3, (), 1, 1),
        LP.KillEvent(1, 2, 3, (), 1, 1),
    ])
    assert len(LP.our_deaths(g)) == 5
    assert LP.early_deaths(g) == 4
    assert LP.chained_deaths(g) == 1
    assert LP.chained_deaths(g, cutoff_ms=840_000, gap_ms=96_001) == 2


def test_wilson_interval_contains_rate_and_empty_sample_is_full():
    lo, hi = LP.wilson_ci(3, 10)
    assert lo < 0.3 < hi
    assert LP.wilson_ci(0, 0) == (0.0, 1.0)
    assert LP.wilson_ci(0, 10)[0] == 0.0
    assert LP.wilson_ci(10, 10)[1] == 1.0


def test_win_rate_by_bucket_reports_counts_and_empty_bucket():
    games = [game(win=True), game(win=False), game(win=True)]
    rows = LP.win_rate_by_bucket(
        games, lambda g: g.win,
        [("wins", bool), ("losses", lambda value: not value), ("none", lambda _: False)],
    )
    assert rows[0]["n"] == 2 and rows[0]["wins"] == 2 and rows[0]["rate"] == 1
    assert rows[1]["n"] == 1 and rows[1]["wins"] == 0 and rows[1]["rate"] == 0
    assert rows[2]["n"] == 0 and rows[2]["rate"] is None
    assert rows[2]["lo"] == 0 and rows[2]["hi"] == 1


def test_death_rate_uses_partial_played_minutes_and_half_open_window():
    g = game(duration_s=15 * 60 + 30, kills=[
        LP.KillEvent(14 * 60_000, 1, 3, (), 1, 1),
        LP.KillEvent(15 * 60_000, 1, 3, (), 1, 1),
        LP.KillEvent(15 * 60_000 + 30_000, 1, 3, (), 1, 1),
        LP.KillEvent(16 * 60_000, 1, 3, (), 1, 1),
    ])
    rate = LP.death_rate_per_10min([g], lambda _g, _kill: True, (14, 16))
    assert rate == 3 / 1.5 * 10
    assert LP.death_rate_per_10min([g], lambda _g, _kill: True, (16, 20)) == 0


def test_bootstrap_is_reproducible_and_loss_rate_is_higher():
    losses = [game(duration_s=600, kills=[
        LP.KillEvent(i * 60_000, 1, 3, (), 1, 1) for i in range(1, count + 1)
    ]) for count in (1, 2, 3, 4)]
    wins = [game(win=True, duration_s=600, kills=[
        LP.KillEvent(60_000, 1, 3, (), 1, 1)
    ]) for _ in range(4)]
    args = (losses, wins, lambda _g, _k: True, (0, 10))
    result = LP.bootstrap_rate_diff(*args, iters=500, seed=19)
    assert result == LP.bootstrap_rate_diff(*args, iters=500, seed=19)
    assert result[0] > 0
    assert LP.bootstrap_rate_diff([], wins, lambda _g, _k: True, (0, 10)) == (None, None, None)


def test_objective_pit_risk_bands_window_radius_and_enemy_take():
    gold = [{1: 1000, 2: 1000, 3: 1000, 4: 1000}] * 16
    gold[14] = {1: 1000, 2: 1000, 3: 2500, 4: 2500}  # -3000
    gold[15] = {1: 1000, 2: 1000, 3: 4000, 4: 4000}  # -6000
    center = (1000, 2000)
    games = [
        # Justo despues del limite posterior: no se marca una muerte cercana.
        game(duration_s=16 * 60, gold=gold,
             kills=[LP.KillEvent(14 * 60_000 + 30_001, 1, 3, (), *center)],
             elites=[LP.EliteEvent(14 * 60_000, 200, "DRAGON", *center)]),
        # Dentro del limite temporal, pero un punto fuera del radio.
        game(duration_s=16 * 60, gold=gold,
             kills=[LP.KillEvent(14 * 60_000 - 60_000, 1, 3, (), 4001, 2000)],
             elites=[LP.EliteEvent(14 * 60_000, 100, "RIFTHERALD", *center)]),
        # En el tiempo del objetivo y a distancia exacta del radio.
        game(duration_s=16 * 60, gold=gold,
             kills=[LP.KillEvent(15 * 60_000, 1, 3, (), 1000, 5000)],
             elites=[LP.EliteEvent(15 * 60_000, 100, "BARON_NASHOR", *center)]),
    ]
    rows = LP.objective_pit_risk(games)
    by_label = {row["label"]: row for row in rows}
    assert by_label["(-6000, -3000]"]["n"] == 2
    assert by_label["(-6000, -3000]"]["enemy_taken_rate"] == 0.5
    assert by_label["(-6000, -3000]"]["death_rate"] == 0
    assert by_label["<= -6000"]["n"] == 1
    assert by_label["<= -6000"]["enemy_taken_rate"] == 0
    assert by_label["<= -6000"]["death_rate"] == 1
    assert by_label[">= +3000"]["n"] == 0


def test_visible_deficit_rules_precision_and_recall_match_manual_counts():
    gold = []
    levels = []
    for diff in (-4000, -4000, 0):
        gold.append({1: 1000, 2: 1000, 3: 1000 + max(0, -diff // 2),
                     4: 1000 + max(0, -diff // 2)})
        levels.append({1: 5, 2: 5, 3: 5, 4: 5})
    g = game(duration_s=17 * 60, gold=gold, levels=levels)
    # Minutos 14 y 15: deficit; minuto 16: paridad.
    g.gold_by_min = [*([gold[0]] * 15), gold[0], gold[2]]
    g.level_by_min = [*([levels[0]] * 17)]
    g.level_by_min[16] = {1: 5, 2: 5, 3: 7, 4: 7}
    g.kills = ([LP.KillEvent(14 * 60_000, 1, 3, (), 0, 0) for _ in range(5)]
               + [LP.KillEvent(15 * 60_000, 3, 1, (), 0, 0) for _ in range(5)]
               + [LP.KillEvent(16 * 60_000, 1, 3, (), 0, 0) for _ in range(5)])
    g.buildings = [LP.BuildingEvent(16 * 60_000, 100, "TOWER_BUILDING") for _ in range(2)]
    rows = LP.visible_deficit_rules([g])
    kills = rows[0]
    assert kills["n_flagged"] == 2
    assert kills["precision"] == 0.5
    assert kills["recall"] == 0.5
    assert rows[1]["precision"] == 0
    assert rows[1]["recall"] == 0
    assert rows[2]["n_flagged"] == 1
    assert rows[3]["precision"] == 0
    assert rows[3]["recall"] == 0
    assert LP.visible_deficit_rules([], min_minute=14)[0]["recall"] is None


def test_zero_exposure_at_cutoff_does_not_create_a_death_rate():
    boundary_loss = game(duration_s=14 * 60, kills=[
        LP.KillEvent(14 * 60_000, 1, 3, (), 1, 1),
    ])
    loss_without_deaths = game(duration_s=15 * 60)
    win_without_deaths = game(win=True, duration_s=15 * 60)
    losses = [boundary_loss, loss_without_deaths]
    wins = [win_without_deaths]
    predicate = lambda _game, _kill: True
    assert LP.death_rate_per_10min(losses, predicate, (14, float("inf"))) == 0
    assert LP.bootstrap_rate_diff(
        losses, wins, predicate, (14, float("inf")), iters=200, seed=1
    ) == (0, 0, 0)


def _insert_match(conn, match_id, cohort, queue_id=420, duration=900):
    conn.execute(
        "INSERT INTO matches(match_id, game_creation_ms, game_start_ms, game_duration_s, "
        "queue_id, our_puuid, our_cohort, ingested_at_utc) "
        "VALUES (?, 0, 0, ?, ?, 'p1', ?, 'now')",
        (match_id, duration, queue_id, cohort),
    )
    for participant_id, puuid, team_id, win in (
        (1, "p1", 100, 1), (2, "p2", 100, 1),
        (3, "p3", 200, 0), (4, "p4", 200, 0),
    ):
        conn.execute(
            "INSERT INTO participants(match_id, participant_id, puuid, team_id, "
            "champion_name, win) VALUES (?, ?, ?, ?, 'Sona', ?)",
            (match_id, participant_id, puuid, team_id, win),
        )
    for minute in range(16):
        for participant_id in range(1, 5):
            conn.execute(
                "INSERT INTO timeline_frames(match_id, frame_index, timestamp_ms, "
                "participant_id, total_gold, level) VALUES (?, ?, ?, ?, ?, ?)",
                (match_id, minute, minute * 60_000, participant_id,
                 minute * 100 + participant_id, 1 + minute // 2),
            )


def test_load_games_from_temporary_real_schema_is_read_only_and_filters_cohorts(tmp_path):
    path = tmp_path / "matches.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    _insert_match(conn, "own", "master")
    _insert_match(conn, "emerald", "emerald")
    _insert_match(conn, "reference", "challenger")
    _insert_match(conn, "flex", "master", queue_id=440)
    _insert_match(conn, "short", "master", duration=299)
    conn.execute(
        "INSERT INTO timeline_events(match_id, frame_index, timestamp_ms, type, "
        "participant_id, payload_json) VALUES ('own', 14, 840000, 'CHAMPION_KILL', "
        "3, ?)",
        (json.dumps({"victimId": 1, "killerId": 3, "assistingParticipantIds": [],
                     "position": {"x": 123, "y": 456}}),),
    )
    conn.execute(
        "INSERT INTO timeline_events(match_id, frame_index, timestamp_ms, type, "
        "participant_id, payload_json) VALUES ('own', 14, 840000, 'ELITE_MONSTER_KILL', "
        "NULL, ?)",
        (json.dumps({"killerId": 3, "monsterType": "DRAGON",
                     "position": {"x": 500, "y": 600}}),),
    )
    conn.commit()
    before = conn.total_changes
    games = load_games(conn)
    assert {g.match_id for g in games} == {"own", "emerald"}
    own = next(g for g in games if g.match_id == "own")
    assert own.our_id == 1 and own.team_of[1] == 100 and own.win is True
    assert len(own.gold_by_min) == 16 and own.gold_by_min[14][4] == 1404
    assert own.kills == [LP.KillEvent(840_000, 1, 3, (), 123, 456)]
    assert own.elites == [LP.EliteEvent(840_000, 200, "DRAGON", 500, 600)]
    assert conn.total_changes == before
    conn.close()


def test_main_rejects_output_hardlink_to_database(tmp_path, monkeypatch, capsys):
    import errno
    import pytest
    from scripts import loss_patterns as report

    db_path = tmp_path / "matches.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(db.SCHEMA)
    conn.close()
    original = db_path.read_bytes()
    out_path = tmp_path / "report.md"
    try:
        out_path.hardlink_to(db_path)
    except OSError as exc:
        if (getattr(exc, "winerror", None) in (5, 1314)
                or exc.errno in (errno.EPERM, errno.EACCES, errno.ENOTSUP)):
            pytest.skip("El entorno no permite crear enlaces duros")
        raise
    monkeypatch.setattr(report, "load_config", lambda: {"paths": {"sqlite_db": str(db_path)}})
    monkeypatch.setattr(report.sys, "argv", ["loss_patterns.py", "--out", str(out_path)])

    with pytest.raises(SystemExit) as error:
        report.main()

    assert error.value.code == 2
    assert "distinto de la DB" in capsys.readouterr().err
    assert db_path.read_bytes() == original
    assert out_path.read_bytes() == original


def test_main_writes_report_with_read_only_database(tmp_path, monkeypatch, capsys):
    import pytest
    from scripts import loss_patterns as report

    db_path = tmp_path / "matches.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(db.SCHEMA)
        _insert_match(conn, "own", "master")
    conn.close()
    original = db_path.read_bytes()
    out_path = tmp_path / "report.md"
    out_path.write_text("informe anterior", encoding="utf-8")
    monkeypatch.setattr(report, "load_config", lambda: {"paths": {"sqlite_db": str(db_path)}})
    monkeypatch.setattr(report.sys, "argv", ["loss_patterns.py", "--out", str(out_path)])
    original_build = report.build

    def checked_build(read_conn):
        assert read_conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_conn.execute("CREATE TABLE forbidden_write(value INTEGER)")
        return original_build(read_conn)

    monkeypatch.setattr(report, "build", checked_build)

    assert report.main() == 0

    markdown = out_path.read_text(encoding="utf-8")
    assert "# Patrones de derrota" in markdown
    assert "| master | 1 | 0 | 1 |" in markdown
    assert "IC Wilson 95%" in markdown
    assert "IC bootstrap percentil 95%" in markdown
    assert "Partidas: 1 | derrotas: 0 | victorias: 1" in capsys.readouterr().out
    assert db_path.read_bytes() == original
    assert not list(tmp_path.glob(".report.md.*.tmp"))
