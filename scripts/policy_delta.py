"""Policy delta en setup de objetivo: mis cuentas vs el jugador de referencia (#14 redirigido).

¿En que DECISION divergimos cuando hay un objetivo (dragon/heraldo) en juego?
No en profundidad posicional (exposure_v1 ya la falsifico), sino en la accion
elegida: pelear el objetivo, poner vision, o ignorarlo. El estudio de 560k
partidas dice que ese eje —vision y conducta forward-looking— es el que separa
Master de Challenger; el combate individual es plano.

Read-only: usa las decisiones de objective_readiness_v1 que ya estan en la DB
(traen team_gold_diff y el action block). NO escribe nada, NO es detector nuevo,
NO pide feedback. Fuente de HIPOTESIS con n declarado, no de verdad.

    .venv\\Scripts\\python.exe scripts\\policy_delta.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach import policy_delta as PD
from lol_coach.config import load_config

OWN = ("master", "emerald")     # mis cuentas
REF = "challenger"              # jugador de referencia (one-trick), solo comparacion
MIN_N = 8                       # bucket con menos que esto en una cohorte = solo indicativo
ACTION_ES = {                   # etiquetas legibles del vocabulario de objective_readiness
    "fight_entry": "pelear objetivo",
    "vision_setup": "poner visión",
    "ignore_objective": "ignorar/ceder",
}


def _rows(conn) -> list:
    rows = []
    for r in conn.execute(
        "SELECT d.game_time_ms, d.context_json, m.our_cohort "
        "FROM decisions d JOIN matches m ON m.match_id = d.match_id "
        "WHERE d.detector_id = 'objective_readiness_v1'"
    ):
        cohort = r["our_cohort"]
        side = "own" if cohort in OWN else (REF if cohort == REF else None)
        if side is None:
            continue
        ctx = json.loads(r["context_json"])
        aid = (ctx.get("action") or {}).get("action_id")
        if not aid:
            continue
        rows.append((side, PD.bucket_of(r["game_time_ms"], ctx.get("team_gold_diff")), aid))
    return rows


def _fmt_dist(dist, indicative: bool) -> list:
    total = sum(v[0] for v in dist.values())
    tag = "  *(n<8: solo indicativo)*" if indicative else ""
    lines = [f"  n = {total}{tag}"]
    for aid, (cnt, prop, lo, hi) in dist.items():
        name = ACTION_ES.get(aid, aid)
        lines.append(f"    - {name}: {prop*100:.0f}% ({cnt}) · IC [{lo*100:.0f}–{hi*100:.0f}%]")
    return lines


def build(conn) -> tuple[str, dict]:
    rows = _rows(conn)
    buckets: dict = {}
    for side, b, aid in rows:
        buckets.setdefault(b, {"own": [], REF: []})[side].append(aid)

    out = ["# Policy delta en setup de objetivo — mis cuentas vs el jugador de referencia", ""]
    out.append(
        "> ¿QUÉ acción elijo yo vs el jugador de referencia cuando hay dragón/heraldo en juego? "
        "Pelear el objetivo / poner visión / ignorar. Este es el eje que separa "
        "Master de Challenger (estudio 560k partidas: el combate es plano; la visión "
        "y la conducta forward-looking, no). **Hipótesis, no veredicto** — la referencia es "
        "one-trick y su n es chico. Nada de esto se escribe en la DB."
    )
    out.append("")
    n_own = sum(1 for s, _, _ in rows if s == "own")
    n_ref = sum(1 for s, _, _ in rows if s == REF)
    out.append(f"Decisiones de objetivo: **{n_own}** mías (master+emerald) · **{n_ref}** del jugador de referencia.")
    out.append("")

    diverges = []
    order = sorted(buckets, key=lambda b: (b[0], {"behind": 0, "even": 1, "ahead": 2}[b[1]]))
    for b in order:
        own_ids, ref_ids = buckets[b]["own"], buckets[b][REF]
        do, dr = PD.action_distribution(own_ids), PD.action_distribution(ref_ids)
        thin = len(own_ids) < MIN_N or len(ref_ids) < MIN_N
        out.append(f"## {b[0]} · oro {b[1]}")
        out.append("")
        out.append("**Yo:**")
        out += _fmt_dist(do, len(own_ids) < MIN_N) if do else ["  (sin decisiones)"]
        out.append("")
        out.append("**la referencia:**")
        out += _fmt_dist(dr, len(ref_ids) < MIN_N) if dr else ["  (sin decisiones)"]
        out.append("")
        # Divergencia clara: alguna accion cuyos IC de Jeffreys NO se solapan, con
        # n suficiente. Una accion ausente en una cohorte es count 0 sobre SU
        # total (prop_ci), no un IC degenerado [0,0] (eso fabricaba falsos).
        if not thin:
            for aid in set(do) | set(dr):
                po, lo_o, hi_o = PD.prop_ci(do, len(own_ids), aid)
                pr, lo_r, hi_r = PD.prop_ci(dr, len(ref_ids), aid)
                if hi_o < lo_r or hi_r < lo_o:
                    diverges.append((b, aid, po, pr))

    out.append("---")
    out.append("")
    if diverges:
        out.append("## Divergencias con n suficiente (IC sin solapar)")
        out.append("")
        for b, aid, po, pr in diverges:
            out.append(f"- **{b[0]} · oro {b[1]}** — {ACTION_ES.get(aid, aid)}: "
                       f"yo {po*100:.0f}% vs el jugador de referencia {pr*100:.0f}%")
    else:
        out.append("## Sin divergencias con n suficiente")
        out.append("")
        out.append("Ningún bucket con n>=8 en ambas cohortes tiene una acción cuyos IC de "
                   "Jeffreys no se solapen. Con este n el estudio no puede afirmar una "
                   "diferencia de política; es límite de datos (la referencia = 20 partidas), no "
                   "evidencia de que juegues igual.")

    return "\n".join(out), {"diverges": diverges, "n_own": n_own, "n_ref": n_ref,
                            "buckets": len(buckets)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Policy delta vs el jugador de referencia en setup de objetivo")
    ap.add_argument("--db", help="ruta a la DB (por defecto, la de config.toml)")
    args = ap.parse_args()

    db = args.db or load_config()["paths"]["sqlite_db"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    text, res = build(conn)
    conn.close()

    out_path = Path(db).parent / "policy_delta.md"
    out_path.write_text(text, encoding="utf-8")
    d = res["diverges"]
    if d:
        print(f"{len(d)} divergencia(s) con n suficiente:")
        for b, aid, *_ in d:
            print(f"  {b[0]}/{b[1]}: {aid}")
    else:
        print("Sin divergencias con n>=8 en ambas cohortes (límite de datos del jugador de referencia).")
    print(f"mías {res['n_own']} · la referencia {res['n_ref']} · {res['buckets']} buckets")
    print(f"Escrito: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
