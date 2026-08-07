"""#10: capa I/O del progreso conductual — _overextension_per_match cuenta bien
por partida y excluye la referencia. Mismo skip-guard que test_ingest_status
(importar dashboard carga config.toml en import-time)."""
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

from conftest import insert_match

_NOW = "2026-01-01T00:00:00Z"


def test_query_matches_includes_own_participant_kda(conn):
    insert_match(conn, "KDA", our_puuid="mine")
    conn.execute(
        "INSERT INTO participants (match_id, participant_id, puuid, team_id, "
        "team_position, champion_name, win, kills, deaths, assists) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("KDA", 1, "mine", 100, "BOTTOM", "Jinx", 1, 7, 2, 11),
    )
    conn.commit()

    matches = dashboard.query_matches(conn)

    assert len(matches) == 1
    assert matches[0]["match_id"] == "KDA"
    assert matches[0]["kills"] == 7
    assert matches[0]["deaths"] == 2
    assert matches[0]["assists"] == 11


def _decision(conn, match_id, detector, t_ms, ctx=None):
    conn.execute(
        "INSERT INTO decisions (match_id, detector_id, game_time_ms, context_json, "
        "options_json, argument, created_at_utc) VALUES (?,?,?,?,?,?,?)",
        (match_id, detector, t_ms, json.dumps(ctx or {}), "[]", "", _NOW),
    )


def test_query_matches_detector_counts_suman_decisions_count(conn):
    """Los 4 conteos comparten el JOIN, asi que heredan la exclusion de
    `positive_trade`. Si se contaran aparte, el trade ganado entraria en el
    conteo del detector pero no en decisions_count y la fila mostraria una
    distribucion que no suma."""
    insert_match(conn, "M1", our_puuid="mine")
    _decision(conn, "M1", "death_v1", 100)
    _decision(conn, "M1", "death_v1", 200)
    _decision(conn, "M1", "objective_readiness_v1", 300)
    _decision(conn, "M1", "trade_v1", 400)
    _decision(conn, "M1", "tempo_v1", 500)
    _decision(conn, "M1", "trade_v1", 600, {"positive_trade": True})  # excluido
    conn.commit()

    m = dashboard.query_matches(conn)[0]

    assert m["detector_counts"] == {
        "death_v1": 2, "objective_readiness_v1": 1, "trade_v1": 1, "tempo_v1": 1,
    }
    assert sum(m["detector_counts"].values()) == m["decisions_count"] == 5


def test_query_matches_expone_rol_y_game_start_crudo(conn):
    insert_match(conn, "R1", our_puuid="mine")
    conn.execute("UPDATE matches SET game_start_ms=1730000000000 WHERE match_id='R1'")
    conn.execute(
        "INSERT INTO participants (match_id, participant_id, puuid, team_id, "
        "team_position, champion_name, win) VALUES (?,?,?,?,?,?,?)",
        ("R1", 1, "mine", 100, "UTILITY", "Sona", 1),
    )
    conn.commit()

    m = dashboard.query_matches(conn)[0]

    assert m["role"] == "UTILITY"
    # Crudo: la fecha formateada no sirve para calcular antiguedad en el front.
    assert m["game_start_ms"] == 1730000000000


def test_partida_sin_decisiones_conserva_rol_y_ficha(conn):
    """Una partida puede no tener ni una decision. El rol sale del participante,
    no de las decisiones, asi que la fila sigue siendo reconocible."""
    insert_match(conn, "VACIA", our_puuid="mine")
    conn.execute(
        "INSERT INTO participants (match_id, participant_id, puuid, team_id, "
        "team_position, champion_name, win, kills, deaths, assists) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("VACIA", 1, "mine", 100, "JUNGLE", "Belveth", 0, 3, 8, 4),
    )
    conn.commit()

    m = dashboard.query_matches(conn)[0]

    assert m["decisions_count"] == 0
    assert sum(m["detector_counts"].values()) == 0
    assert m["role"] == "JUNGLE"
    assert m["champion"] == "Belveth"
    assert m["win"] is False
    assert (m["kills"], m["deaths"], m["assists"]) == (3, 8, 4)


def test_partida_antigua_sin_team_position_no_rompe(conn):
    insert_match(conn, "VIEJA", our_puuid="mine")
    conn.execute(
        "INSERT INTO participants (match_id, participant_id, puuid, team_id, "
        "team_position, champion_name, win) VALUES (?,?,?,?,?,?,?)",
        ("VIEJA", 1, "mine", 100, None, "Sona", 1),
    )
    conn.commit()

    assert dashboard.query_matches(conn)[0]["role"] == "—"


def _death(conn, match_id, t_ms, action_class, power_index):
    ctx = {"inferred_action_class": action_class,
           "state_features": {"power": {"power_index": power_index}}}
    conn.execute(
        "INSERT INTO decisions (match_id, detector_id, game_time_ms, context_json, "
        "options_json, argument, created_at_utc) VALUES (?,?,?,?,?,?,?)",
        (match_id, "death_v1", t_ms, json.dumps(ctx), "[]", "", _NOW),
    )


def test_counts_per_match_and_excludes_reference(conn):
    # Partida propia A: 3 muertes -> 1 incidente (engage_blind con lead; el
    # engage_worth con lead NO cuenta; el engage_blind sin lead tampoco).
    insert_match(conn, "A")
    conn.execute("UPDATE matches SET game_start_ms=1000 WHERE match_id='A'")
    _death(conn, "A", 100, "engage_blind", 0.70)
    _death(conn, "A", 200, "engage_worth", 0.90)
    _death(conn, "A", 300, "engage_blind", 0.30)
    # Partida propia B: sin muertes (cuenta como partida con 0/0).
    insert_match(conn, "B")
    conn.execute("UPDATE matches SET game_start_ms=2000 WHERE match_id='B'")
    # Partida challenger: se excluye entera aunque tenga incidentes.
    conn.execute(
        "INSERT INTO matches (match_id, game_creation_ms, game_start_ms, "
        "game_duration_s, queue_id, our_puuid, our_cohort, ingested_at_utc) "
        "VALUES ('REF', 0, 3000, 1800, 420, 'puuid_ref', 'challenger', ?)", (_NOW,))
    _death(conn, "REF", 100, "engage_blind", 0.90)
    conn.commit()

    pm = dashboard._overextension_per_match(conn, None)

    assert [m["match_id"] for m in pm] == ["A", "B"]   # cronologico, sin REF
    assert pm[0]["deaths"] == 3 and pm[0]["incidents"] == 1
    assert pm[1]["deaths"] == 0 and pm[1]["incidents"] == 0


def test_compute_progress_none_with_short_history(conn):
    insert_match(conn, "A")
    _death(conn, "A", 100, "engage_blind", 0.70)
    conn.commit()
    # 1 partida < WINDOW -> sin serie -> bloque None (la UI no pinta nada raro).
    assert dashboard.compute_progress(conn, None) is None


def test_reference_cohort_request_falls_back_to_own(conn):
    # Pedir cohort=challenger no debe producir progreso del jugador de referencia.
    insert_match(conn, "A")
    _death(conn, "A", 100, "engage_blind", 0.70)
    conn.execute(
        "INSERT INTO matches (match_id, game_creation_ms, game_start_ms, "
        "game_duration_s, queue_id, our_puuid, our_cohort, ingested_at_utc) "
        "VALUES ('REF', 0, 3000, 1800, 420, 'puuid_ref', 'challenger', ?)", (_NOW,))
    conn.commit()
    pm = dashboard._overextension_per_match(conn, "challenger")
    assert [m["match_id"] for m in pm] == ["A"]
