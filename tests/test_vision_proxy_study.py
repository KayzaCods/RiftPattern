"""Marco de validacion del proxy de vision pre-objetivo.

El proxy de `objective_readiness_v1` decide si una ward estaba "cerca del pit"
mirando donde estaba QUIEN la puso, en el frame mas cercano. Los frames son por
minuto, asi que esa posicion arrastra hasta 60 s de retraso. De ahi sale el
"70% de objetivos sin ward propia cerca del pit" y no se sabia si describe un
habito o un error de medicion.

Lo que se prueba aqui es el MARCO de muestreo, porque es donde estan las tres
decisiones que hacen que el estudio mida algo:

  - si no pusiste ninguna ward en la ventana, el cero es correcto POR
    CONSTRUCCION: ese caso no puede refutar al proxy y no debe entrar;
  - si un aliado warde en el mismo hueco entre frames, el icono nuevo no se te
    puede atribuir y el caso tampoco entra;
  - una ward posterior a -15 s no tiene frame despues que la muestre.

Mismo skip-guard que test_historical_mark: importar dashboard carga config.toml.
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

from conftest import ten_player_match  # noqa: E402

KILL_MS = 900_000          # 15:00
US = 1                     # participante propio en ten_player_match
ALLY = 5                   # mismo equipo (100)
ENEMY = 8                  # equipo 200


def _decision(conn, match_id="TEST", *, your_wards=0, clip="/v/x.mp4", offset=900.0,
              t_ms=KILL_MS):
    ctx = {
        "your_wards_near_pit": your_wards,
        "action": {"detector_id": "objective_readiness_v1", "action_id": "cede",
                   "available_actions": [{"action_id": "cede"}]},
        "state_features": {"power": {"local_allies": 2, "local_enemies": 1}},
    }
    opts = [{"label": "Ceder", "ev_score": 0.5}, {"label": "Forzar", "ev_score": 0.4}]
    cur = conn.execute(
        "INSERT INTO decisions (match_id, detector_id, game_time_ms, moment, outcome, "
        "context_json, options_json, argument, created_at_utc, clip_path, clip_offset_s) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, "objective_readiness_v1", t_ms, "momento", "resultado",
         json.dumps(ctx), json.dumps(opts), "argumento", "2026-01-01T00:00:00Z",
         clip, offset))
    if clip:
        conn.execute(
            "INSERT OR IGNORE INTO clips (path, source, discovered_at_utc) VALUES (?,?,?)",
            (clip, "ascent_full", "2026-01-01T00:00:00Z"))
    conn.commit()
    return cur.lastrowid


def _ward(conn, creator, offset_s, *, match_id="TEST", wtype="SIGHT_WARD", kill=KILL_MS):
    """Una ward puesta a `offset_s` del objetivo (negativo = antes)."""
    conn.execute(
        "INSERT INTO timeline_events (match_id, frame_index, timestamp_ms, type, "
        "participant_id, payload_json) VALUES (?,?,?,?,?,?)",
        (match_id, 0, int(kill + offset_s * 1000), "WARD_PLACED", None,
         json.dumps({"creatorId": creator, "wardType": wtype})))
    conn.commit()


def _frame(conn):
    return dashboard.vision_proxy_frame(conn)


# --------------------------------------------------- el caso que SI se estudia
def test_your_ward_alone_in_an_interval_enters_the_frame(conn):
    ten_player_match(conn)
    dec = _decision(conn)
    _ward(conn, US, -70)

    f = _frame(conn)
    assert len(f) == 1
    assert f[0]["id"] == dec
    assert f[0]["interval"] == [-90, -45]
    assert f[0]["your_ward_offsets_s"] == [-70.0]
    assert f[0]["n_your_wards_in_window"] == 1


def test_the_second_interval_also_counts(conn):
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -30)

    f = _frame(conn)
    assert len(f) == 1
    assert f[0]["interval"] == [-45, -15]


# ------------------------------------------- exclusiones: por que no bastan
def test_without_your_wards_the_zero_is_correct_by_construction(conn):
    """El caso mas importante: si no pusiste ninguna ward, el proxy no puede
    estar subcontando. Mirarlo en el video seria gastar tiempo en un caso que
    no puede refutar nada."""
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, ALLY, -70)      # warde un aliado, no tu

    assert _frame(conn) == []


def test_an_ally_in_the_same_interval_makes_it_ambiguous(conn):
    """Dos iconos nuevos en el mismo hueco: el que aparezca cerca del pit no se
    puede atribuir. El caso no entra."""
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -70)
    _ward(conn, ALLY, -60)      # mismo intervalo [-90,-45)

    assert _frame(conn) == []


def test_an_ally_in_the_other_interval_does_not_contaminate(conn):
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -70)        # intervalo [-90,-45)
    _ward(conn, ALLY, -30)      # intervalo [-45,-15): no estorba

    f = _frame(conn)
    assert len(f) == 1
    assert f[0]["interval"] == [-90, -45]


def test_an_enemy_ward_never_contaminates(conn):
    """Las wards enemigas no se ven en el minimapa, asi que no crean ambiguedad."""
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -70)
    _ward(conn, ENEMY, -60)

    assert len(_frame(conn)) == 1


def test_a_ward_after_the_last_frame_is_not_verifiable(conn):
    """La tira cierra en -15 s. Una ward de -8 s no aparece en ningun frame
    posterior, asi que no se puede confirmar ni desmentir."""
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -8)

    assert _frame(conn) == []


def test_a_ward_before_the_window_is_not_verifiable(conn):
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -100)

    assert _frame(conn) == []


def test_undefined_ward_type_is_not_a_ward(conn):
    """Invariante 8 del proyecto: `wardType UNDEFINED` se filtra siempre."""
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -70, wtype="UNDEFINED")

    assert _frame(conn) == []


def test_objectives_with_wards_already_counted_are_out(conn):
    ten_player_match(conn)
    _decision(conn, your_wards=2)
    _ward(conn, US, -70)

    assert _frame(conn) == []


def test_without_clip_or_margin_there_is_nothing_to_look_at(conn):
    ten_player_match(conn)
    _decision(conn, clip=None, offset=None, t_ms=KILL_MS)
    _ward(conn, US, -70)
    assert _frame(conn) == []

    ten_player_match(conn, match_id="M2")
    _decision(conn, match_id="M2", offset=40.0, t_ms=KILL_MS)
    _ward(conn, US, -70, match_id="M2")
    assert [x for x in _frame(conn) if x["key"].startswith("M2")] == []


# ------------------------------------------------------- muestra reproducible
def test_the_sample_order_is_deterministic(conn):
    ten_player_match(conn)
    for i in range(6):
        t = KILL_MS + i * 120_000
        _decision(conn, t_ms=t)
        _ward(conn, US, -70, kill=t)

    a = [d["key"] for d in dashboard.query_vision_study(conn)["moments"]]
    b = [d["key"] for d in dashboard.query_vision_study(conn)["moments"]]
    assert a == b and len(a) == 6


def test_the_sample_freezes_and_new_cases_do_not_displace_it(conn):
    """El fallo que esto evita: el marco crece con cada partida ingerida, y si la
    muestra se recalcula, casos nuevos entran y otros salen MIENTRAS se etiqueta.
    El denominador se movería y el intervalo final no significaría nada."""
    ten_player_match(conn)
    for i in range(4):
        t = KILL_MS + i * 120_000
        _decision(conn, t_ms=t)
        _ward(conn, US, -70, kill=t)

    antes = [d["key"] for d in dashboard.query_vision_study(conn, limit=3)["moments"]]
    assert len(antes) == 3

    # llega una tanda de partidas nuevas al marco
    ten_player_match(conn, match_id="NUEVA")
    for i in range(6):
        t = KILL_MS + i * 120_000
        _decision(conn, match_id="NUEVA", t_ms=t)
        _ward(conn, US, -70, match_id="NUEVA", kill=t)

    out = dashboard.query_vision_study(conn, limit=3)
    assert [d["key"] for d in out["moments"]] == antes
    assert out["frame_total"] == 10       # el marco sí crece
    assert out["frozen"] == 3             # la muestra no


def test_freezing_preserves_the_order(conn):
    ten_player_match(conn)
    for i in range(5):
        t = KILL_MS + i * 120_000
        _decision(conn, t_ms=t)
        _ward(conn, US, -70, kill=t)

    a = [d["key"] for d in dashboard.query_vision_study(conn)["moments"]]
    b = [d["key"] for d in dashboard.query_vision_study(conn)["moments"]]
    assert a == b


def test_labels_made_before_freezing_survive(conn):
    ten_player_match(conn)
    for i in range(3):
        t = KILL_MS + i * 120_000
        _decision(conn, t_ms=t)
        _ward(conn, US, -70, kill=t)
    primero = dashboard.query_vision_study(conn)["moments"][0]
    dashboard.set_vision_label(conn, primero["id"], "near")

    ten_player_match(conn, match_id="NUEVA")
    _decision(conn, match_id="NUEVA")
    _ward(conn, US, -70, match_id="NUEVA")

    out = dashboard.query_vision_study(conn)
    marcado = [d for d in out["moments"] if d["key"] == primero["key"]]
    assert len(marcado) == 1
    assert marcado[0]["vision_label"] == "near"
    assert out["n_marked"] == 1


def test_a_case_that_leaves_the_frame_is_reported_not_hidden(conn):
    """Si un caso congelado deja de resolverse, es una BAJA de la muestra. Que
    desapareciera en silencio encogeria el denominador sin que nadie lo supiera."""
    ten_player_match(conn)
    for i in range(3):
        t = KILL_MS + i * 120_000
        _decision(conn, t_ms=t)
        _ward(conn, US, -70, kill=t)
    out = dashboard.query_vision_study(conn)
    assert out["frozen"] == 3 and out["dropped"] == 0

    victima = out["moments"][0]
    conn.execute("DELETE FROM decisions WHERE id=?", (victima["id"],))
    conn.commit()

    out2 = dashboard.query_vision_study(conn)
    assert out2["frozen"] == 3          # la muestra sigue siendo de 3
    assert out2["total"] == 2
    assert out2["dropped"] == 1


def test_the_sample_is_capped_and_reports_the_full_frame(conn):
    ten_player_match(conn)
    for i in range(5):
        t = KILL_MS + i * 120_000
        _decision(conn, t_ms=t)
        _ward(conn, US, -70, kill=t)

    out = dashboard.query_vision_study(conn, limit=3)
    assert out["total"] == 3
    assert out["frame_total"] == 5


def test_the_strip_needs_these_fields_to_render(conn):
    """Sin `clip_source` y `clip_offset_s` la tira no se dibuja, y sin tira no
    hay estudio."""
    ten_player_match(conn)
    _decision(conn)
    _ward(conn, US, -70)

    d = dashboard.query_vision_study(conn)["moments"][0]
    assert d["clip_source"] == "ascent_full"
    assert d["has_clip"] is True
    assert d["clip_offset_s"] == 900.0
    assert d["detector"] == "objective_readiness_v1"


# ------------------------------------------------------------------ etiquetas
def test_labels_persist_by_stable_key(conn):
    ten_player_match(conn)
    dec = _decision(conn)
    _ward(conn, US, -70)

    assert dashboard.set_vision_label(conn, dec, "near")["ok"] is True
    out = dashboard.query_vision_study(conn)
    assert out["moments"][0]["vision_label"] == "near"
    assert out["n_marked"] == 1

    # el id cambia al re-analizar; la etiqueta no debe perderse
    key = out["moments"][0]["key"]
    conn.execute("DELETE FROM decisions WHERE id=?", (dec,))
    nuevo = _decision(conn)
    _ward(conn, US, -70)   # el evento se duplica en el re-analisis real
    assert nuevo != dec
    m = dashboard.query_vision_study(conn)["moments"][0]
    assert m["key"] == key
    assert m["vision_label"] == "near"


def test_an_invalid_label_is_rejected(conn):
    ten_player_match(conn)
    dec = _decision(conn)
    _ward(conn, US, -70)

    assert "error" in dashboard.set_vision_label(conn, dec, "quiza")
    assert dashboard.query_vision_study(conn)["moments"][0]["vision_label"] is None


def test_none_clears_the_label(conn):
    ten_player_match(conn)
    dec = _decision(conn)
    _ward(conn, US, -70)

    dashboard.set_vision_label(conn, dec, "far")
    dashboard.set_vision_label(conn, dec, None)
    assert dashboard.query_vision_study(conn)["moments"][0]["vision_label"] is None
