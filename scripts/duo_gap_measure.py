#!/usr/bin/env python3
"""Parada de medición #22 — co-presencia del duo en trade_v1 (READ-ONLY).

Mide cuántas decisiones trade_v1 y cuántas marcas históricas de trade caen en
el conjunto que el guard de co-presencia del duo EXCLUIRÍA. Es la medición previa
obligatoria al fix: un fix de detector puede invalidar marcas limpias vía
orphan_feedback, así que se mide antes de tocar el detector.

DEFINICIÓN OPERATIVA (la que usa el fix, verificada 19-ago-2026):
- Para cada decisión trade_v1, el run cubre los minutos m_start..m_end
  (m_start = game_time_ms//60_000 + 1; m_end desde context_json.span_minutes).
- Por cada minuto del run, se muestrea la posición del jugador (us_id) y del
  otro miembro de my_duo (el ADC) en el MIDPOINT (m-0.5)*60_000 — el mismo
  punto donde el guard existente muestrea al jugador (lol_coach/decisions/
  trade.py ~210-221).
- El guard excluye un minuto si el ADC tiene posición completa y no está en bot
  (bot lane o cuadrante bot de nuestro lado), o si ambos tienen posición completa
  y su distancia euclidiana supera 2500u. Coordenadas faltantes son desconocidas:
  no excluyen ni rompen la medición.

RECONCILIACIÓN 17 vs 8 (19-ago-2026): una estimación previa daba
"17 de las 60 marcas viven en decisiones con duo separado", pero con la
definición operativa de arriba son 8 (verificada contra la DB, ambas
variantes). Aquella definición no quedó documentada; este script es la
fuente canónica de la medición.

Uso: python3 scripts/duo_gap_measure.py [--db data/lol_coach.db] [--threshold 2500]
Conecta con URI mode=ro: NO escribe nada.
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from lol_coach.context import map_lane, map_side

DEFAULT_DB = "data/lol_coach.db"
DEFAULT_THRESHOLD = 2500.0

# trade_v1: order congelado [disengage, fight_entry, attrition]; taken siempre
# fight_entry (índice 1). user_best_option es un ÍNDICE posicional.
TAKEN_IDX = 1


def get_pos(conn, match_id, pid, t_ms):
    r = conn.execute(
        "SELECT position_x x, position_y y FROM timeline_frames "
        "WHERE match_id=? AND participant_id=? "
        "ORDER BY ABS(timestamp_ms-?), timestamp_ms LIMIT 1",
        (match_id, pid, int(t_ms)),
    ).fetchone()
    return None if r is None or r["x"] is None or r["y"] is None else (r["x"], r["y"])


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def has_complete_pos(pos):
    return pos is not None and pos[0] is not None and pos[1] is not None


def is_bot_presence(pos, our_team):
    """Match trade_v1's bot-presence predicate for a complete coordinate."""
    x, y = pos
    return (
        map_lane(x, y) == "bot lane"
        or (x > 7500 and y < 7500 and map_side(x, y, our_team) == "nuestro lado")
    )


def duo_guard_excludes(us_pos, adc_pos, our_team, threshold):
    """Whether the #22 ADC co-presence guard excludes this sampled minute."""
    if not has_complete_pos(adc_pos):
        return False
    if not is_bot_presence(adc_pos, our_team):
        return True
    return has_complete_pos(us_pos) and dist(us_pos, adc_pos) > threshold


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    T = args.threshold

    rows = conn.execute(
        "SELECT id, match_id, game_time_ms, context_json, user_feedback, "
        "user_best_option FROM decisions WHERE detector_id='trade_v1'"
    ).fetchall()
    total = len(rows)
    marked = [r for r in rows if r["user_feedback"] is not None]

    parts = {}
    for r in conn.execute(
        "SELECT match_id, participant_id, team_id, team_position, champion_name "
        "FROM participants"
    ):
        parts.setdefault(r["match_id"], {})[r["participant_id"]] = dict(r)

    us_cache = {}
    for r in conn.execute(
        "SELECT participants.match_id, participants.participant_id, "
        "participants.team_id FROM participants "
        "JOIN matches ON matches.match_id = participants.match_id "
        "WHERE participants.puuid = matches.our_puuid"
    ):
        us_cache.setdefault(r["match_id"], (r["participant_id"], r["team_id"]))

    def duo_of(match_id, our_team):
        return [pid for pid, p in parts.get(match_id, {}).items()
                if p["team_id"] == our_team
                and p["team_position"] in ("UTILITY", "BOTTOM")]

    distance_only_affected = []
    outside_bot_affected = []
    combined_affected = []
    dmin_all = None
    dmax_all = 0.0           # distancias >= 0: semilla segura para max()
    n_computed = 0

    for row in rows:
        mid = row["match_id"]
        uc = us_cache.get(mid)
        if not uc:
            continue
        us_id, our_team = uc
        duo = duo_of(mid, our_team)
        if len(duo) < 2:
            continue
        adc = [pid for pid in duo if pid != us_id][0]
        try:
            span = json.loads(row["context_json"] or "{}").get("span_minutes", 1)
        except ValueError:
            span = 1
        m_start = row["game_time_ms"] // 60_000 + 1
        m_end = m_start + span - 1
        ds = []
        excluded_by_distance = False
        excluded_outside_bot = False
        excluded_combined = False
        for m in range(m_start, m_end + 1):
            t = (m - 0.5) * 60_000
            a = get_pos(conn, mid, us_id, t)
            b = get_pos(conn, mid, adc, t)
            if a and b:
                ds.append(dist(a, b))
                excluded_by_distance |= dist(a, b) > T
            excluded_outside_bot |= b is not None and not is_bot_presence(b, our_team)
            excluded_combined |= duo_guard_excludes(a, b, our_team, T)
        if ds:
            n_computed += 1
            dmin_all = min(dmin_all, min(ds)) if dmin_all is not None else min(ds)
            dmax_all = max(dmax_all, max(ds))
        if excluded_by_distance:
            distance_only_affected.append(row)
        if excluded_outside_bot:
            outside_bot_affected.append(row)
        if excluded_combined:
            combined_affected.append((row, ds))

    # --- Reporte ---
    print(f"DB: {args.db} (read-only)")
    print(f"Decisiones trade_v1: {total}  (con posiciones computables: {n_computed})")
    if dmin_all is not None:
        print(f"Distancia jugador<->ADC: min {dmin_all:.0f}u / max {dmax_all:.0f}u")
    def marked_count(rows):
        return sum(row["user_feedback"] is not None for row in rows)

    print(f"\nSolo distancia (algún minuto >{T:.0f}u): "
          f"{len(distance_only_affected)}/{total} decisiones; "
          f"{marked_count(distance_only_affected)} marcas afectadas")
    print(f"ADC fuera de bot (posición completa, algún minuto): "
          f"{len(outside_bot_affected)}/{total} decisiones; "
          f"{marked_count(outside_bot_affected)} marcas afectadas")
    print(f"Guard #22 combinado (distancia O ADC fuera de bot): "
          f"{len(combined_affected)}/{total} decisiones; "
          f"{marked_count([row for row, _ in combined_affected])} marcas afectadas")
    print(f"Marcas trade_v1 totales: {len(marked)}")

    print("\n--- Detalle: marcas afectadas por el guard #22 combinado ---")
    for row, ds in combined_affected:
        if row["user_feedback"] is None:
            continue
        mid = row["match_id"]
        uc = us_cache[mid]
        us_id, our_team = uc
        duo = duo_of(mid, our_team)
        adc = [pid for pid in duo if pid != us_id][0]
        champ = parts.get(mid, {}).get(adc, {}).get("champion_name", "?")
        best = row["user_best_option"]
        gen_par = best is not None and best != TAKEN_IDX
        mmss = f"{row['game_time_ms']//60000}:{(row['game_time_ms']//1000)%60:02d}"
        distance_text = (f"dmin={min(ds):.0f}u dmax={max(ds):.0f}u" if ds
                         else "distancia=desconocida")
        print(f"  id={row['id']} match={mid} t={mmss} {distance_text} adc={champ} "
              f"fb={row['user_feedback']!r} best={best!r} genera_par_dec={gen_par}")

    conn.close()
    print("\nOK — medición read-only terminada")


if __name__ == "__main__":
    main()
