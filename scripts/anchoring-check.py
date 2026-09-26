"""¿La interfaz le está dictando la respuesta al jugador? (read-only)

Mide con qué frecuencia la opción que el jugador marcó como adecuada
(`user_best_option`) coincide con la que el sistema puntúa más alto (máximo EV).

El razonamiento: si el jugador juzgara de forma independiente, su elección
coincidiría con la del sistema aproximadamente tan a menudo como el sistema
acierta — es decir, alrededor del 52%, que es la exactitud medida del HEF. Una
coincidencia MUY superior a eso no significa que el sistema sea bueno: significa
que el jugador está viendo la recomendación mientras elige y la está copiando.
Cuando eso pasa, los pares `dec` del learner entrenan contra una copia del propio
HEF y el aprendizaje es circular.

La medición de agosto dio 273/323 (85%) con el picker que mostraba `optimal`, EV
y barra. Tras neutralizarlo, el umbral de sospecha acordado es 60%: por debajo,
las marcas vuelven a ser señal independiente.

IMPORTANTE — sólo se cuentan marcas VIGENTES. `user_best_option` es un ÍNDICE
dentro de la lista de opciones, así que una marca sólo significa algo mientras
esa lista conserve la misma semántica. Se reutiliza `mark_validity`, que es la
misma función que usan el learner, la cola de revisión y la card; reimplementar
el criterio aquí lo dejaría libre de discrepar.

No escribe nada: abre la base de datos en modo lectura.

Uso:
    python scripts/anchoring-check.py
    python scripts/anchoring-check.py --since 2026-08-06
    python scripts/anchoring-check.py --since 2026-08-06 --detector trade_v1
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach.decisions.features import MARK_REVIVED, MARK_VALID, mark_validity

DB = Path(__file__).resolve().parent.parent / "data" / "lol_coach.db"

# Por debajo de esto las marcas vuelven a considerarse independientes.
SUSPICION_THRESHOLD = 0.60
# Exactitud medida del HEF: el suelo con el que se compara la coincidencia.
HEF_ACCURACY = 0.52

COUNTS_AS_VALID = (MARK_VALID, MARK_REVIVED)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """IC de Wilson. Con n de dos dígitos el intervalo normal se sale de [0,1] y
    da una falsa sensación de precisión; el proyecto ya usa Wilson en #10."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def max_ev_indices(options: list) -> set[int]:
    """Índices con el EV máximo. Devuelve un CONJUNTO a propósito: cuando dos
    opciones empatan, elegir cualquiera de ellas es coincidir con el sistema, y
    contar sólo la primera inflaría artificialmente el desacuerdo."""
    # En la DB el campo es `ev_score`; `ev` es el nombre que le pone el dashboard
    # al servirlo. Se aceptan ambos para que el script sirva contra las dos formas.
    evs = [(o or {}).get("ev_score", (o or {}).get("ev")) for o in options]
    real = [e for e in evs if isinstance(e, (int, float))]
    if not real:
        return set()
    top = max(real)
    return {i for i, e in enumerate(evs)
            if isinstance(e, (int, float)) and abs(e - top) < 1e-9}


def collect(conn, since: str | None, detector: str | None) -> tuple[list, dict]:
    sql = ("SELECT id, detector_id, options_json, context_json, user_best_option, "
           "user_feedback_fingerprint, user_feedback_at_utc, created_at_utc "
           "FROM decisions WHERE user_best_option IS NOT NULL")
    params: list = []
    if detector:
        sql += " AND detector_id = ?"
        params.append(detector)
    sql += " ORDER BY user_feedback_at_utc"

    rows, skipped = [], {"stale": 0, "sin_ev": 0, "indice_fuera": 0}
    for r in conn.execute(sql, params):
        try:
            options = json.loads(r["options_json"] or "[]")
            ctx = json.loads(r["context_json"] or "{}")
        except ValueError:
            skipped["stale"] += 1
            continue

        verdict = mark_validity(r["user_feedback_fingerprint"], r["detector_id"],
                                ctx.get("action"), options,
                                r["user_feedback_at_utc"], r["created_at_utc"])
        if verdict not in COUNTS_AS_VALID:
            skipped["stale"] += 1
            continue

        best = max_ev_indices(options)
        if not best:
            skipped["sin_ev"] += 1
            continue
        chosen = r["user_best_option"]
        if not isinstance(chosen, int) or not 0 <= chosen < len(options):
            skipped["indice_fuera"] += 1
            continue

        at = r["user_feedback_at_utc"] or ""
        rows.append({"id": r["id"], "detector": r["detector_id"], "at": at,
                     "match": chosen in best, "n_options": len(options),
                     "nueva": bool(since and at and at >= since)})
    return rows, skipped


def report(label: str, rows: list) -> float | None:
    n = len(rows)
    if n == 0:
        print(f"  {label:<22} sin marcas")
        return None
    k = sum(1 for r in rows if r["match"])
    p = k / n
    lo, hi = wilson(k, n)
    print(f"  {label:<22} {k:>4}/{n:<4} = {p*100:5.1f}%   IC95 [{lo*100:4.1f}–{hi*100:4.1f}]")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", metavar="ISO",
                    help="fecha desde la que una marca cuenta como NUEVA "
                         "(p. ej. 2026-08-06, tras neutralizar el picker)")
    ap.add_argument("--detector", help="limitar a un detector (trade_v1, death_v1, …)")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"No encuentro la base de datos: {args.db}", file=sys.stderr)
        return 2

    # Sólo lectura: esta medición nunca debe poder alterar las marcas que mide.
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows, skipped = collect(conn, args.since, args.detector)
    finally:
        conn.close()

    print("Coincidencia entre la opción que marcaste y la de máximo EV")
    print("=" * 66)
    if args.detector:
        print(f"detector: {args.detector}")
    print(f"marcas vigentes analizadas: {len(rows)}")
    desc = {"stale": "marcas cuyas opciones ya cambiaron (no cuentan)",
            "sin_ev": "decisiones sin EV en sus opciones",
            "indice_fuera": "índice de elección fuera de rango"}
    for k, v in skipped.items():
        if v:
            print(f"  descartadas: {v:>4}  {desc[k]}")
    print()

    overall = report("todas", rows)

    veredicto_rows = rows
    if args.since:
        viejas = [r for r in rows if not r["nueva"]]
        nuevas = [r for r in rows if r["nueva"]]
        print()
        print(f"  partidas por fecha de marca (corte {args.since}):")
        report(f"antes del corte", viejas)
        report(f"desde el corte", nuevas)
        veredicto_rows = nuevas

    by_det: dict[str, list] = {}
    for r in rows:
        by_det.setdefault(r["detector"], []).append(r)
    if len(by_det) > 1:
        print()
        print("  por detector (todas las marcas):")
        for det in sorted(by_det, key=lambda d: -len(by_det[d])):
            report(det, by_det[det])

    print()
    print("-" * 66)
    n = len(veredicto_rows)
    if n == 0:
        print("Sin marcas en el tramo a juzgar: no hay veredicto.")
        return 0

    p = sum(1 for r in veredicto_rows if r["match"]) / n
    lo, hi = wilson(sum(1 for r in veredicto_rows if r["match"]), n)
    tramo = "las marcas nuevas" if args.since else "todas las marcas"
    print(f"Veredicto sobre {tramo} (n={n}, coincidencia {p*100:.1f}%):")

    if p < SUSPICION_THRESHOLD:
        print(f"  Por debajo del umbral de sospecha ({SUSPICION_THRESHOLD*100:.0f}%).")
        print("  Las marcas se comportan como juicio independiente.")
    else:
        print(f"  En o por encima del umbral de sospecha ({SUSPICION_THRESHOLD*100:.0f}%).")
        print("  Compatible con que la interfaz esté mostrando la recomendación")
        print("  mientras el jugador elige. Revisar que el picker siga neutro.")

    if hi < HEF_ACCURACY or lo > HEF_ACCURACY:
        rel = "por debajo" if hi < HEF_ACCURACY else "por encima"
        print(f"  El IC95 queda {rel} de la exactitud del HEF ({HEF_ACCURACY*100:.0f}%).")
    else:
        print(f"  El IC95 incluye la exactitud del HEF ({HEF_ACCURACY*100:.0f}%): "
              "indistinguible de juicio independiente.")

    if n < 25:
        print(f"  AVISO: n={n} es pequeño. El intervalo es ancho y un veredicto")
        print("  a un lado u otro del umbral puede ser ruido. Marcar más antes")
        print("  de dar la corrección por confirmada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
