"""El dashboard no muestra el juicio del programa antes de elegir.

En death_v1 y tempo_v1 la ultima frase del `argument` persistido compara lo que
hiciste con la opcion de mayor EV, y en menus binarios eso nombra la respuesta.
`splitArgument` (scripts/dashboard.html) la separa por marcadores literales para
bajarla a la etapa 04, tras el gate. Estos tests fijan:

1. que cada marcador sigue existiendo literal en la plantilla del detector (si
   alguien reescribe la frase, el corte dejaria de ocurrir EN SILENCIO y la
   recomendacion volveria a la etapa 01);
2. que, con argumentos compuestos por las funciones reales de los detectores, el
   corte deja la comparacion entera en `reveal` y nada de ella en `pre`, para
   cualquier orden de EV.

Mismo contrato para las ETIQUETAS de opcion: `pickerLabel` quita en el picker los
sufijos de veredicto literales de objective_readiness.py ("— fue lo correcto"…).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lol_coach.decisions import death, tempo
from lol_coach.decisions.base import Option

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "scripts" / "dashboard.html").read_text(encoding="utf-8")


def _js_list(name: str) -> list[str]:
    block = re.search(r"const " + name + r" = \[(.*?)\];", HTML, re.S)
    assert block, f"{name} no encontrado en dashboard.html"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_every_marker_is_literal_in_a_detector_template():
    sources = "".join((ROOT / "lol_coach" / "decisions" / f).read_text(encoding="utf-8")
                      for f in ("death.py", "tempo.py"))
    markers = _js_list("REVEAL_MARKERS")
    assert len(markers) == 4
    for m in markers:
        assert m in sources, f"marcador sin plantilla: {m!r}"


def test_label_verdict_suffixes_only_survive_in_legacy_rows():
    """Desde la Fase D del #23 las plantillas de objective ya no llevan veredicto
    en la etiqueta: `pickerLabel` queda solo para filas persistidas antes (o
    generadas por un checkout sin la Fase D). Si una plantilla vuelve a escribir
    uno de estos sufijos, este test lo ve."""
    src = (ROOT / "lol_coach" / "decisions" / "objective_readiness.py").read_text(encoding="utf-8")
    suffixes = _js_list("LABEL_VERDICT_SUFFIXES")
    assert len(suffixes) == 5
    for suf in suffixes:
        assert suf not in src, f"sufijo de veredicto de vuelta en la plantilla: {suf!r}"


def _death_ctx() -> dict:
    return {
        "time_mmss": "12:34", "lane": "bot", "map_side": "lado enemigo",
        "killer": "Rengar", "assisters": [], "first_blood": False,
        "our_state": {"level": 9, "current_gold": 850, "cs": 40,
                      "kills_so_far": 1, "deaths_so_far": 2, "assists_so_far": 5},
        "nearest_ally": {"champion": None}, "fight_ratio_apparent": "1v2",
        "non_combatant_enemies_unseen": [],
    }


def _tempo_ctx() -> dict:
    return {
        "time_mmss": "20:00", "fight_position": {"lane": "mid", "side": "centro"},
        "your_position_at_fight_start": {"lane": "top", "side": "aliado"},
        "fight_outcome_for_us": "1-2", "fight_duration_s": 12,
        "distance_to_fight_units": 4000, "estimated_travel_s": 11,
        "could_arrive_in_time": True, "your_activity": {"description": "farmeando"},
        "objective_at_stake": None, "fight_participants": {"our_team": [], "enemy": []},
    }


def _death_argument(best_is_taken: bool) -> str:
    # La tercera plantilla ("Las heuristicas no marcan una alternativa") exige
    # best != taken con EV no mayor: inalcanzable con max(), 0 filas en la DB.
    # Se cubre solo por el test de marcador literal.
    evs = (0.70, 0.30) if best_is_taken else (0.30, 0.70)
    options = [Option("Permanecer/engage", "x", evs[0], taken=True),
               Option("Desenganchar a tiempo", "y", evs[1])]
    return death._compose_argument(_death_ctx(), options)


def _tempo_argument(rotate_better: bool) -> str:
    evs = (0.75, 0.45) if rotate_better else (0.40, 0.60)
    options = [Option("Rotar a la pelea", "x", evs[0]),
               Option("Permanecer donde estabas", "y", evs[1], taken=True)]
    return tempo._compose_argument(_tempo_ctx(), options)


def _run_js(texts: list[str]) -> list[dict]:
    """Ejecuta el `splitArgument` y el `pickerLabel` REALES del dashboard."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node es necesario para ejecutar el corte del dashboard")
    start = HTML.index("const REVEAL_MARKERS = [")
    end = HTML.index("function HefPanel(", start)
    script = HTML[start:end] + (
        "\nconst inputs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "\nprocess.stdout.write(JSON.stringify(inputs.map(t =>"
        " ({...splitArgument(t), label: pickerLabel(t)}))));\n")
    out = subprocess.run([node, "-e", script], input=json.dumps(texts),
                         check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)


def test_split_moves_the_whole_ev_comparison_out_of_stage_01():
    cases = [_death_argument(False), _death_argument(True),
             _tempo_argument(True), _tempo_argument(False)]
    expected_start = ["Opción de mayor EV:", "Lo que hiciste ES la opción de mayor EV",
                      "Opción de mayor EV:", "Tu decisión de quedarte estaba justificada"]
    for text, parts, head in zip(cases, _run_js(cases), expected_start):
        assert parts["reveal"].startswith(head), (text, parts)
        assert parts["pre"] + " " + parts["reveal"] == text
        # Nada de la comparacion queda en la etapa 01.
        assert "EV" not in parts["pre"], parts["pre"]
        assert parts["pre"], "la lectura de la situacion no puede quedar vacia"


def test_no_ev_comparison_leaks_into_stage_01_for_any_ev_ordering():
    # Mas alla de los casos canonicos: cualquier orden de EV (incluidos empates)
    # debe dejar `pre` sin comparacion. Si una plantilla nueva de comparacion
    # entra sin marcador, este test la ve: todas mencionan "EV".
    grid = [0.2, 0.45, 0.5, 0.7]
    texts = []
    for a in grid:
        for b in grid:
            for taken_first in (True, False):
                texts.append(death._compose_argument(_death_ctx(), [
                    Option("A", "x", a, taken=taken_first),
                    Option("B", "y", b, taken=not taken_first)]))
            texts.append(tempo._compose_argument(_tempo_ctx(), [
                Option("Rotar a la pelea", "x", a),
                Option("Permanecer donde estabas", "y", b, taken=True)]))
    for text, parts in zip(texts, _run_js(texts)):
        assert "EV" not in parts["pre"], text


def test_argument_without_marker_is_left_whole():
    text = "A los 5:10 intercambiaste en bot. Recibiste 320 de daño y el duo enemigo 280."
    (parts,) = _run_js([text])
    assert (parts["pre"], parts["reveal"]) == (text, "")
    empty = _run_js([""])[0]
    assert (empty["pre"], empty["reveal"]) == ("", "")


@pytest.mark.parametrize("label,expected", [
    ("Contestaste Barón Nashor con buen setup (lo que hiciste) — fue lo correcto",
     "Contestaste Barón Nashor con buen setup (lo que hiciste)"),
    ("Acompañaste el objetivo controlado (2w/1sw) (lo que hiciste) — válido",
     "Acompañaste el objetivo controlado (2w/1sw) (lo que hiciste)"),
    ("Aprovechaste el tiempo fuera del pit: reset/farm/presión (lo que hiciste)"
     " — uso correcto del tiempo",
     "Aprovechaste el tiempo fuera del pit: reset/farm/presión (lo que hiciste)"),
    ("Contestaste Heraldo con setup y se perdió (lo que hiciste) — la decisión era correcta",
     "Contestaste Heraldo con setup y se perdió (lo que hiciste)"),
    ("Sin setup, ceder Heraldo o cambiarlo por otra jugada valía más que forzarlo",
     "Sin setup, ceder Heraldo o cambiarlo por otra jugada"),
    ("Retirarte / disengage", "Retirarte / disengage"),
])
def test_picker_label_drops_verdict_suffixes_only(label, expected):
    assert _run_js([label])[0]["label"] == expected
