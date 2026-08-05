"""Etapa 1 del experimento de atencion PRE-REGISTRADO (#13).

Pre-registro sellado en git ANTES de correr esto: hipotesis, senal primaria,
ventana, alfa, estratos y regla de paro quedaron fijados de antemano y no se
tocan segun el resultado. Sin ese sello previo el pre-registro no vale.

Pregunta: ¿hay MENOS consultas de marcador (Tab) en los 30 s previos a las
muertes que el jugador atribuye a EXPOSICION, una vez controlado el estado de
pelea?

El confusor: las muertes "no" ocurren mayormente en pelea, donde la actividad es
alta por construccion. Se controla permutando la etiqueta SOLO DENTRO de cada
estrato de `fight_ongoing`, que se define con `CHAMPION_KILL` del timeline —
objetivo e INDEPENDIENTE de los input_events que forman la senal.

ASIMETRIA DECLARADA: estos son los mismos datos que generaron la hipotesis, asi
que esta etapa **solo puede REFUTAR**, nunca confirmar. Si sobrevive, no se
declara nada: habilita la Etapa 2 sobre anclas frescas.

Solo lee la DB. Escribe data/attention_experiment.md.

    .venv\\Scripts\\python.exe scripts\\attention_experiment.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach import anchor_eval
from lol_coach.config import load_config

# --- Todo lo de abajo esta PRE-REGISTRADO: no se toca segun el resultado ------
WINDOW_MS = 30_000        # ventana fija previa a la muerte
ALPHA = 0.05              # una cola, en la direccion pre-especificada
MIN_STRATUM = 8           # estrato menor a esto -> NO CONCLUYENTE, no "sobrevivio"
PRIMARY = "tab_rate"      # consultas de marcador por minuto (senal primaria UNICA)


def _anchors(conn) -> list:
    out = []
    for r in conn.execute("SELECT decision_key, label FROM exposure_anchors"):
        try:
            mid, _det, gt = r["decision_key"].split("|")
            gt = int(gt)
        except ValueError:
            continue
        if r["label"] in ("yes", "no"):
            out.append((mid, gt, r["label"]))
    return out


def _measure(conn, mid, death_ms) -> dict:
    """Senal + estrato de un ancla. `fight_ongoing` sale de CHAMPION_KILL (de
    CUALQUIER participante) en la ventana: objetivo y sin tocar input_events."""
    lo = death_ms - WINDOW_MS
    n_tab = conn.execute(
        "SELECT COUNT(*) FROM input_events WHERE match_id=? AND key='Tab' "
        "AND game_time_ms BETWEEN ? AND ?", (mid, lo, death_ms)).fetchone()[0]
    n_any = conn.execute(
        "SELECT COUNT(*) FROM input_events WHERE match_id=? "
        "AND game_time_ms BETWEEN ? AND ?", (mid, lo, death_ms)).fetchone()[0]
    # Estrictamente ANTES de la muerte anclada: el kill propio no cuenta como
    # "ya habia pelea" (seria circular: toda muerte tendria pelea).
    n_kills = conn.execute(
        "SELECT COUNT(*) FROM timeline_events WHERE match_id=? AND type='CHAMPION_KILL' "
        "AND timestamp_ms >= ? AND timestamp_ms < ?", (mid, lo, death_ms)).fetchone()[0]
    return {
        "has_input": n_any > 0,
        "tab_rate": n_tab / (WINDOW_MS / 60_000.0),   # por minuto
        "fight": "pelea" if n_kills > 0 else "sin_pelea",
    }


def build(conn) -> tuple[str, dict]:
    pos, neg, sp, sn = [], [], [], []
    skipped = 0
    for mid, gt, lab in _anchors(conn):
        m = _measure(conn, mid, gt)
        if not m["has_input"]:
            skipped += 1
            continue
        # Direccion PRE-ESPECIFICADA: menos Tab en "si" -> se invierte la senal
        # para que "mayor" signifique siempre "mas compatible con la hipotesis".
        v = -m["tab_rate"]
        (pos if lab == "yes" else neg).append(v)
        (sp if lab == "yes" else sn).append(m["fight"])

    strata_sizes: dict = {}
    for s in sp + sn:
        strata_sizes[s] = strata_sizes.get(s, 0) + 1

    auc_plain = anchor_eval.auc(pos, neg)
    p_plain = anchor_eval.permutation_p(pos, neg)
    p_strat = anchor_eval.permutation_p_stratified(pos, neg, sp, sn)

    small = min(strata_sizes.values()) if strata_sizes else 0
    if p_strat is None:
        verdict, detail = "SIN DATOS", "no hay ambos grupos"
    elif p_strat >= ALPHA:
        verdict = "REFUTADA"
        detail = (f"p estratificado {p_strat:.4f} >= {ALPHA}: la senal no sobrevive al "
                  f"control por estado de pelea. Por la regla de paro pre-registrada, "
                  f"la linea de atencion se CIERRA (no se prueba otra senal, otra "
                  f"ventana ni otro estrato).")
    elif small < MIN_STRATUM:
        verdict = "NO CONCLUYENTE"
        detail = (f"p estratificado {p_strat:.4f} < {ALPHA}, pero el estrato menor tiene "
                  f"{small} anclas (< {MIN_STRATUM}): potencia insuficiente para leerlo. "
                  f"No cuenta como sobrevivir.")
    else:
        verdict = "NO REFUTADA"
        detail = (f"p estratificado {p_strat:.4f} < {ALPHA}. NO es un hallazgo: estos son "
                  f"los mismos datos que generaron la hipotesis. Solo habilita la Etapa 2 "
                  f"sobre >= 25 anclas frescas.")

    out = ["# Experimento de atención — Etapa 1 (refutación)", ""]
    out.append("> Pre-registro (hipótesis, señal primaria, ventana, α, estratos y "
               "regla de paro) sellado en git ANTES de correr esto.")
    out.append("> Estos son los mismos datos que generaron la hipótesis: esta etapa "
               "**solo puede refutar**, nunca confirmar.")
    out.append("")
    out.append(f"## Veredicto: **{verdict}**")
    out.append("")
    out.append(detail)
    out.append("")
    out.append("## Números")
    out.append("")
    out.append(f"- Señal primaria pre-registrada: `{PRIMARY}` (Tab por minuto en los "
               f"{WINDOW_MS//1000}s previos), dirección: MENOR en las muertes \"sí\".")
    out.append(f"- n = {len(pos)} sí / {len(neg)} no" +
               (f" ({skipped} anclas sin input_events en la ventana, excluidas)" if skipped else ""))
    out.append(f"- Estratos (`fight_ongoing`): " +
               " · ".join(f"{k} {v}" for k, v in sorted(strata_sizes.items())))
    out.append(f"- AUC (sin control): {auc_plain:.3f}" if auc_plain is not None else "- AUC: —")
    out.append(f"- p SIN estratificar: {p_plain:.4f}" if p_plain is not None else "- p: —")
    out.append(f"- **p ESTRATIFICADO (el que decide): {p_strat:.4f}**" if p_strat is not None else "- p estratificado: —")
    out.append("")
    out.append("La diferencia entre el p sin estratificar y el estratificado es "
               "exactamente lo que el confusor explicaba.")
    return "\n".join(out), {"verdict": verdict, "p_strat": p_strat, "auc": auc_plain,
                            "n_pos": len(pos), "n_neg": len(neg), "strata": strata_sizes}


def main() -> int:
    ap = argparse.ArgumentParser(description="Etapa 1 del experimento de atencion")
    ap.add_argument("--db", help="ruta a la DB (por defecto, la de config.toml)")
    args = ap.parse_args()

    db = args.db or load_config()["paths"]["sqlite_db"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    text, res = build(conn)
    conn.close()

    out_path = Path(db).parent / "attention_experiment.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"VEREDICTO: {res['verdict']}")
    print(f"  AUC {res['auc']:.3f} | p estratificado {res['p_strat']:.4f} | "
          f"n {res['n_pos']}/{res['n_neg']} | estratos {res['strata']}")
    print(f"Escrito: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
