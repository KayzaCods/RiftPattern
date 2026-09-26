"""Backend del sub-proyecto D (tira de minimapa): `clip_source` y el recorte.

Dos piezas del diseno de la tira de minimapa:

  - `/api/decisions` expone `clip_source`. Es una extension ADITIVA: la
    prueba central no es que el campo nuevo aparezca, sino que ninguno de los
    anteriores desaparezca (los contratos /api/* solo admiten extensiones aditivas).
  - la geometria de recorte se guarda POR FUENTE en fracciones 0..1.
    Los ceros son el estado "sin calibrar", no un valor por defecto: un recorte
    degenerado no se guarda y, si ya estuviera en el fichero, no se sirve. Esa
    es la unica defensa contra recortar la esquina equivocada en silencio, que
    es el modo de fallo que el diseno nombra explicitamente.

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

from conftest import insert_match  # noqa: E402

_CTX = {"action": {"detector_id": "death_v1", "action_id": "disengage",
                   "available_actions": [{"action_id": "disengage"}]},
        "state_features": {"power": {"local_allies": 2, "local_enemies": 1}}}
_OPTS = [{"label": "Huir", "ev_score": 0.5}, {"label": "Luchar", "ev_score": 0.4}]


def _insert_decision(conn, match_id="A", *, clip_path=None, offset_s=None, t_ms=600_000):
    cur = conn.execute(
        "INSERT INTO decisions (match_id, detector_id, game_time_ms, moment, outcome, "
        "context_json, options_json, argument, created_at_utc, clip_path, clip_offset_s) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, "death_v1", t_ms, "momento", "resultado", json.dumps(_CTX),
         json.dumps(_OPTS), "argumento", "2026-01-01T00:00:00Z", clip_path, offset_s),
    )
    conn.commit()
    return cur.lastrowid


def _insert_clip(conn, path, source):
    conn.execute(
        "INSERT INTO clips (path, source, discovered_at_utc) VALUES (?,?,?)",
        (path, source, "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def _by_id(conn):
    return {d["id"]: d for d in dashboard.query_decisions(conn, None, None)}


# ------------------------------------------------------------- clip_source
def test_clip_source_comes_from_the_clips_table(conn):
    insert_match(conn, match_id="A")
    _insert_clip(conn, "/videos/partida.mp4", "ascent_full")
    dec = _insert_decision(conn, clip_path="/videos/partida.mp4", offset_s=612.5)

    d = _by_id(conn)[dec]
    assert d["clip_source"] == "ascent_full"
    assert d["has_clip"] is True
    assert d["clip_offset_s"] == 612.5


def test_each_source_is_reported_separately(conn):
    """`ascent_full` y `replays_lol_full` son grabadores distintos y pueden
    diferir en geometria: la tira no puede tratarlos como uno solo."""
    insert_match(conn, match_id="A")
    _insert_clip(conn, "/videos/ascent.mp4", "ascent_full")
    _insert_clip(conn, "/videos/overwolf.mp4", "replays_lol_full")
    a = _insert_decision(conn, clip_path="/videos/ascent.mp4", t_ms=100_000)
    b = _insert_decision(conn, clip_path="/videos/overwolf.mp4", t_ms=200_000)

    served = _by_id(conn)
    assert served[a]["clip_source"] == "ascent_full"
    assert served[b]["clip_source"] == "replays_lol_full"


def test_decision_without_clip_has_no_source(conn):
    insert_match(conn, match_id="A")
    dec = _insert_decision(conn, clip_path=None)

    d = _by_id(conn)[dec]
    assert d["clip_source"] is None
    assert d["has_clip"] is False


def test_clip_path_without_a_clips_row_degrades_to_none(conn):
    """Hoy no hay ninguna (medido: 0 huerfanas), pero un clip_path que ya no
    figure en `clips` no puede romper /api/decisions: la tira simplemente no se
    dibuja para esa decision."""
    insert_match(conn, match_id="A")
    dec = _insert_decision(conn, clip_path="/videos/borrado.mp4")

    d = _by_id(conn)[dec]
    assert d["clip_source"] is None
    assert d["has_clip"] is True


def test_the_extension_is_additive(conn):
    """El contrato de /api/decisions no puede perder campos. Si este test falla,
    el frontend deja de recibir algo que ya usaba."""
    insert_match(conn, match_id="A")
    _insert_clip(conn, "/videos/partida.mp4", "ascent_full")
    dec = _insert_decision(conn, clip_path="/videos/partida.mp4")

    previos = {
        "id", "detector", "match", "champion", "role", "cohort", "date",
        "game_time_ms", "time", "minute_span", "objective", "objective_type",
        "situation", "moment", "outcome", "ev_taken", "ev_optimal", "ev_gap",
        "options", "ctx", "argument", "has_clip", "clip_offset_s", "reward_score",
        "power_index", "power_label", "hef", "feedback", "feedback_note",
        "best_option", "mark_type", "historical_mark", "user_notes",
    }
    assert previos <= set(_by_id(conn)[dec])


# ------------------------------------------------------ minimap_crop
@pytest.fixture
def crop(tmp_path):
    return tmp_path / "minimap_crop.json"


def _rect(x=0.75, y=0.6, w=0.24, h=0.38):
    return {"x": x, "y": y, "w": w, "h": h}


def _with_sources(conn):
    insert_match(conn, match_id="A")
    _insert_clip(conn, "/videos/ascent.mp4", "ascent_full")
    _insert_clip(conn, "/videos/overwolf.mp4", "replays_lol_full")


def test_missing_file_means_nothing_is_calibrated(crop):
    assert dashboard.load_minimap_crop(crop) == {}


def test_save_then_load_roundtrip(conn, crop):
    _with_sources(conn)
    dashboard.save_minimap_crop(conn, "ascent_full", _rect(), path=crop)

    assert dashboard.load_minimap_crop(crop) == {"ascent_full": _rect()}


def test_saving_one_source_preserves_the_other(conn, crop):
    _with_sources(conn)
    dashboard.save_minimap_crop(conn, "ascent_full", _rect(), path=crop)
    dashboard.save_minimap_crop(conn, "replays_lol_full", _rect(x=0.7, y=0.55), path=crop)

    got = dashboard.load_minimap_crop(crop)
    assert set(got) == {"ascent_full", "replays_lol_full"}
    assert got["ascent_full"]["x"] == 0.75
    assert got["replays_lol_full"]["x"] == 0.7


def test_recalibrating_replaces_the_previous_rect(conn, crop):
    _with_sources(conn)
    dashboard.save_minimap_crop(conn, "ascent_full", _rect(), path=crop)
    dashboard.save_minimap_crop(conn, "ascent_full", _rect(x=0.1, y=0.1, w=0.2, h=0.2),
                                path=crop)

    assert dashboard.load_minimap_crop(crop) == {"ascent_full": _rect(0.1, 0.1, 0.2, 0.2)}


@pytest.mark.parametrize("bad, motivo", [
    (_rect(w=0.0), "ancho cero = sin calibrar, no es un recorte"),
    (_rect(h=0.0), "alto cero = sin calibrar"),
    (_rect(x=0.9, w=0.2), "se sale por la derecha"),
    (_rect(y=0.9, h=0.2), "se sale por abajo"),
    (_rect(x=-0.1), "x negativa"),
    (_rect(y=-0.1), "y negativa"),
    (_rect(w=-0.3), "ancho negativo"),
    (_rect(x=float("nan")), "NaN"),
    (_rect(w=float("inf")), "infinito"),
    ({"x": 0.1, "y": 0.1, "w": 0.2}, "falta h"),
    ({"x": "0.1", "y": 0.1, "w": 0.2, "h": 0.2}, "x no es numero"),
])
def test_invalid_rect_is_rejected(conn, crop, bad, motivo):
    _with_sources(conn)
    with pytest.raises(ValueError):
        dashboard.save_minimap_crop(conn, "ascent_full", bad, path=crop)
    assert not crop.exists(), f"no debe escribir nada al rechazar: {motivo}"


def test_unknown_source_is_rejected(conn, crop):
    """Solo se calibra una fuente que exista en `clips`: si no, un POST con
    cualquier cadena escribiria claves basura en un fichero versionado."""
    _with_sources(conn)
    with pytest.raises(ValueError):
        dashboard.save_minimap_crop(conn, "grabador_inventado", _rect(), path=crop)
    assert not crop.exists()


def test_source_with_path_characters_is_rejected(conn, crop):
    _with_sources(conn)
    for hostil in ("../../etc/passwd", "ascent_full/../x", "a\\b", "con espacio"):
        with pytest.raises(ValueError):
            dashboard.save_minimap_crop(conn, hostil, _rect(), path=crop)
    assert not crop.exists()


def test_uncalibrated_entries_are_not_served(crop):
    """El fichero de ejemplo trae ceros. Cargarlos como si fueran
    un recorte haria que la tira dibujase un rectangulo vacio en vez de avisar."""
    crop.write_text(json.dumps({
        "ascent_full": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
        "replays_lol_full": _rect(),
    }), encoding="utf-8")

    assert dashboard.load_minimap_crop(crop) == {"replays_lol_full": _rect()}


def test_corrupt_file_degrades_to_uncalibrated(crop):
    crop.write_text("{no es json", encoding="utf-8")
    assert dashboard.load_minimap_crop(crop) == {}


def test_garbage_entries_are_dropped_not_served(crop):
    crop.write_text(json.dumps({
        "ascent_full": "no soy un rectangulo",
        "otra": {"x": 0.5, "y": 0.5, "w": 2.0, "h": 0.1},
        "replays_lol_full": _rect(),
    }), encoding="utf-8")

    assert dashboard.load_minimap_crop(crop) == {"replays_lol_full": _rect()}


def test_saved_file_is_readable_json_with_sorted_keys(conn, crop):
    _with_sources(conn)
    dashboard.save_minimap_crop(conn, "replays_lol_full", _rect(), path=crop)
    dashboard.save_minimap_crop(conn, "ascent_full", _rect(), path=crop)

    raw = json.loads(crop.read_text(encoding="utf-8"))
    assert list(raw) == ["ascent_full", "replays_lol_full"]


def test_save_returns_the_full_calibration_state(conn, crop):
    _with_sources(conn)
    out = dashboard.save_minimap_crop(conn, "ascent_full", _rect(), path=crop)
    assert out == {"ok": True, "crop": {"ascent_full": _rect()}}


def test_clip_sources_lists_what_can_be_calibrated(conn):
    """La vista de calibracion necesita saber que fuentes existen de verdad; no
    puede llevar la lista hardcodeada o callaria un grabador nuevo."""
    _with_sources(conn)
    _insert_decision(conn, clip_path="/videos/ascent.mp4")

    assert dashboard.clip_sources(conn) == ["ascent_full", "replays_lol_full"]


# ------------------------------------------------- muestra por fuente
def test_the_sample_is_the_median_not_an_extreme(conn):
    """Calibrar exige pintar un frame REAL de esa fuente, y no vale cualquiera:
    al principio el minimapa esta vacio y al final la pantalla de fin de partida
    puede taparlo. Se elige la mediana del offset."""
    _with_sources(conn)
    _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=90.0, t_ms=100_000)
    medio = _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=900.0,
                             t_ms=200_000)
    _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=2540.0, t_ms=300_000)

    muestras = {s["source"]: s for s in dashboard.clip_source_samples(conn)}
    assert muestras["ascent_full"]["decisions"] == 3
    assert muestras["ascent_full"]["sample_id"] == medio
    assert muestras["ascent_full"]["sample_offset_s"] == 900.0


def test_a_source_without_usable_decisions_reports_no_sample(conn):
    """Un grabador registrado en `clips` cuyas decisiones no tienen clip no se
    puede calibrar: la vista debe poder decirlo en vez de abrir un video vacio."""
    _with_sources(conn)
    _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=300.0)

    muestras = {s["source"]: s for s in dashboard.clip_source_samples(conn)}
    assert muestras["replays_lol_full"]["decisions"] == 0
    assert muestras["replays_lol_full"]["sample_id"] is None


def test_a_decision_without_offset_cannot_be_the_sample(conn):
    """Sin `clip_offset_s` no se sabe a que segundo buscar: ese clip no sirve
    para calibrar aunque exista en disco."""
    _with_sources(conn)
    _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=None, t_ms=100_000)
    bueno = _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=450.0,
                             t_ms=200_000)

    muestras = {s["source"]: s for s in dashboard.clip_source_samples(conn)}
    assert muestras["ascent_full"]["decisions"] == 1
    assert muestras["ascent_full"]["sample_id"] == bueno


def test_samples_report_whether_the_source_is_calibrated(conn, crop):
    _with_sources(conn)
    _insert_decision(conn, clip_path="/videos/ascent.mp4", offset_s=300.0)
    dashboard.save_minimap_crop(conn, "ascent_full", _rect(), path=crop)

    muestras = {s["source"]: s
                for s in dashboard.clip_source_samples(conn, crop_path=crop)}
    assert muestras["ascent_full"]["calibrated"] is True
    assert muestras["replays_lol_full"]["calibrated"] is False
