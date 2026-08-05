"""Valida exposure_v1 (#17) contra el anchor set etiquetado por el jugador.

Pregunta que responde: el detector encuentra cientos de episodios expuestos,
pero ¿encuentra los CORRECTOS? El anchor set son las muertes que el jugador
marco en desacuerdo, etiquetadas por el mismo como "el error real fue estar
EXPUESTO antes de morir" (yes) o no (no); las dudosas se excluyen.

Tres medidas:
  1. RECALL — de las muertes etiquetadas "yes", en cuantas hay un frame expuesto
     en la ventana previa. Si el detector no dispara donde el jugador dice que
     hubo exposicion, no sirve por bonito que sea el agregado.
  2. AUC — separabilidad entre las muertes "yes" y las "no". Ambos grupos son
     MUERTES, asi que controla por "es una muerte" y aisla la pregunta real.
  3. PERMUTACION — con n chico, un AUC alto puede ser azar. Se baraja la
     etiqueta y se mide que fraccion de barajadas iguala o supera lo observado.

Solo lee la DB. Escribe data/exposure_validation.md.

    .venv\\Scripts\\python.exe scripts\\exposure_validation.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach import anchor_eval, exposure
from lol_coach.config import load_config

# Ventana previa a la muerte en la que se busca un frame expuesto. Los frames de
# Match-V5 son POR MINUTO, asi que una ventana de 20s casi nunca contendria uno:
# 90s garantiza mirar el frame anterior. Se reporta la sensibilidad a esta
# eleccion (60/90/120/180) para que el resultado no dependa de un numero magico.
WINDOWS_S = (60, 90, 120, 180)
MAIN_WINDOW_S = 90


def _anchors(conn) -> list:
    """[(decision_id, match_id, game_time_ms, label)] resolviendo la clave estable."""
    out = []
    for r in conn.execute("SELECT decision_key, label FROM exposure_anchors"):
        try:
            mid, det, gt = r["decision_key"].split("|")
            gt = int(gt)
        except ValueError:
            continue
        d = conn.execute(
            "SELECT id FROM decisions WHERE match_id=? AND detector_id=? AND game_time_ms=?",
            (mid, det, gt)).fetchone()
        if d:
            out.append((d["id"], mid, gt, r["label"]))
    return out


def _signal(frames, death_ms, window_s) -> float:
    """Senal del detector antes de una muerte: la severidad MAXIMA de los frames
    expuestos en [death - window, death]. 0.0 = no disparo (queda al fondo del
    ranking, que es lo correcto)."""
    lo = death_ms - window_s * 1000
    sev = [exposure.severity(f.local_threat) for f in frames
           if lo <= f.game_time_ms <= death_ms]
    return max(sev) if sev else 0.0


def build(conn) -> tuple[str, dict]:
    anchors = _anchors(conn)
    by_match: dict = {}
    for _id, mid, _gt, _lab in anchors:
        by_match.setdefault(mid, None)
    for mid in by_match:                       # escaneo una vez por partida
        by_match[mid] = exposure.scan_match(conn, mid)

    counts = {"yes": 0, "no": 0, "unsure": 0}
    for _id, _mid, _gt, lab in anchors:
        counts[lab] = counts.get(lab, 0) + 1

    rows = []
    for W in WINDOWS_S:
        pos, neg = [], []
        for _id, mid, gt, lab in anchors:
            if lab == "unsure":
                continue
            s = _signal(by_match[mid], gt, W)
            (pos if lab == "yes" else neg).append(s)
        recall = sum(1 for s in pos if s > 0) / len(pos) if pos else None
        fp_rate = sum(1 for s in neg if s > 0) / len(neg) if neg else None
        a = anchor_eval.auc(pos, neg)
        p = anchor_eval.permutation_p(pos, neg)
        rows.append({"w": W, "recall": recall, "fired_on_no": fp_rate, "auc": a, "p": p,
                     "n_pos": len(pos), "n_neg": len(neg)})

    main = next(r for r in rows if r["w"] == MAIN_WINDOW_S)
    out = ["# Validación de exposure_v1 contra el anchor set", ""]
    out.append(
        f"> {len(anchors)} muertes etiquetadas por el jugador: **{counts.get('yes',0)} sí** "
        f"(fue exposición) · **{counts.get('no',0)} no** · {counts.get('unsure',0)} dudosas "
        f"(excluidas). La comparación es muerte-contra-muerte, así que controla por "
        f"\"es una muerte\" y aísla si el detector distingue lo que el jugador llama exposición."
    )
    out.append("")
    out.append(f"## Resultado (ventana principal: {MAIN_WINDOW_S}s antes de morir)")
    out.append("")
    out.append(f"- **Recall**: {_pct(main['recall'])} de las muertes \"sí\" tienen un frame "
               f"expuesto en la ventana previa.")
    out.append(f"- **Disparó en las \"no\"**: {_pct(main['fired_on_no'])} (cuanto más bajo, "
               f"mejor separa).")
    out.append(f"- **AUC**: {_num(main['auc'])} (0.5 = no distingue nada).")
    out.append(f"- **Permutación**: p = {_num(main['p'])} "
               f"({'significativo' if (main['p'] or 1) < 0.05 else 'NO significativo'} a 0.05).")
    out.append("")
    out.append("## Sensibilidad a la ventana")
    out.append("")
    out.append("| ventana | recall (sí) | disparó en (no) | AUC | p (permutación) |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        out.append(f"| {r['w']}s | {_pct(r['recall'])} | {_pct(r['fired_on_no'])} | "
                   f"{_num(r['auc'])} | {_num(r['p'])} |")
    out.append("")
    out.append(
        f"n = {main['n_pos']} sí / {main['n_neg']} no. Los frames de Match-V5 son por MINUTO: "
        f"una ventana menor a 60s casi nunca contiene un frame, por eso la principal es "
        f"{MAIN_WINDOW_S}s (mira el frame anterior)."
    )
    return "\n".join(out), {"rows": rows, "counts": counts, "main": main}


def _pct(x) -> str:
    return "—" if x is None else f"{100*x:.0f}%"


def _num(x) -> str:
    return "—" if x is None else f"{x:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Valida exposure_v1 contra el anchor set")
    ap.add_argument("--db", help="ruta a la DB (por defecto, la de config.toml)")
    args = ap.parse_args()

    db = args.db or load_config()["paths"]["sqlite_db"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    text, res = build(conn)
    conn.close()

    out_path = Path(db).parent / "exposure_validation.md"
    out_path.write_text(text, encoding="utf-8")
    m = res["main"]
    print(f"recall {_pct(m['recall'])} · disparo en 'no' {_pct(m['fired_on_no'])} · "
          f"AUC {_num(m['auc'])} · p {_num(m['p'])}")
    print(f"Escrito: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
