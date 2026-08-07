"""Local coaching dashboard for lol-coach.

Serves the decisions in lol_coach.db as a small web app: read the structured
context + options + EV + argument for each detected decision, watch the linked
clip, and mark feedback (agree / disagree / missing_context) that persists back
to the DB so the EV criteria can be refined over time.

Run:  python scripts/dashboard.py  [--port 8765] [--no-browser]
Then open http://127.0.0.1:8765 (opened automatically by default).

stdlib only. The frontend lives in dashboard.html next to this file.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import mimetypes
import random
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach.config import load_config  # noqa: E402
from lol_coach.db import connect as _connect_schema  # noqa: E402  (ensures all tables exist)
from lol_coach.decisions.features import (  # noqa: E402
    STALE_REASONS, _commit_fit, _info_term, action_fingerprint, action_label_es,
    fingerprint_action_ids, hef_label, hef_score,
    mark_validity, MARK_REVIVED, mmss, power_index, power_label, taken_index,
)
from lol_coach.preference import (  # noqa: E402
    DEFAULT_WEIGHTS, learn_weights, load_weights, save_weights,
)
from lol_coach import feedback  # noqa: E402  (vocabulario de tipo de marca, #12)
from lol_coach import game_data  # noqa: E402  (patch_version para el dashboard)
from lol_coach import progress  # noqa: E402  (criterio + matematica del #10)
from lol_coach import session  # noqa: E402  (seleccion pura de la Sesion de hoy, #16)
from lol_coach import trade_queue  # noqa: E402  (ranking por informatividad de la cola #5)
from lol_coach.snapshots import biggest_drop_start_s, hp_pct_series_windowed  # noqa: E402
from lol_coach import exposure  # noqa: E402  (#17 Fase 1: exposicion sin muerte, report-only)
from lol_coach.context import find_clip_for_event  # noqa: E402  (clip de un momento arbitrario)

CFG = load_config()
DB_PATH = CFG["paths"]["sqlite_db"]
# Active HEF weights: learned-and-applied weights override the defaults. Cached
# in memory; reloaded when the user applies new ones via /api/weights/apply.
_ACTIVE_WEIGHTS = load_weights(DB_PATH)

# /api/decisions is the heaviest read: it enriches every decision (JSON parse + HEF
# recompute) and serializes ~3 MB. The result changes only on a write (feedback,
# preference, applied weights), so cache it per (filter, active-weights) and clear
# on any POST. Single-user tool, but ThreadingHTTPServer -> guard with a lock.
_DECISIONS_CACHE: dict = {}
_DECISIONS_CACHE_LOCK = threading.Lock()


def _decisions_cached(conn, cohort, detector, match) -> list:
    key = (cohort, detector, match, tuple(sorted((_ACTIVE_WEIGHTS or {}).items())))
    with _DECISIONS_CACHE_LOCK:
        cached = _DECISIONS_CACHE.get(key)
    if cached is not None:
        return cached
    res = query_decisions(conn, cohort, detector, match)
    with _DECISIONS_CACHE_LOCK:
        _DECISIONS_CACHE[key] = res
    return res


def _invalidate_decisions_cache() -> None:
    with _DECISIONS_CACHE_LOCK:
        _DECISIONS_CACHE.clear()


def query_ingest_status(conn, status_path: Path | None = None) -> dict:
    """Pipeline health for the dashboard banner (backlog #11). Reads auto_ingest's
    ingest_status.json (key_alive / error) and adds days since the last ingested
    match. Motivation: the 2026-06-24 key death went unnoticed for 7 days because
    key_alive only lived in this JSON that nobody looks at."""
    path = Path(status_path) if status_path else Path(DB_PATH).parent / "ingest_status.json"
    st = {}
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass  # no status file yet (auto_ingest never ran) -> key_alive stays None
    row = conn.execute("SELECT MAX(ingested_at_utc) AS last FROM matches").fetchone()
    last = row["last"] if row else None
    days = None
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = max(0, (datetime.now(timezone.utc) - dt).days)
        except ValueError:
            pass
    return {
        "key_alive": st.get("key_alive"),   # None = unknown (sin status file)
        "last_run": st.get("last_run"),
        "error": st.get("error"),
        "last_ingested_utc": last,
        "days_since_ingest": days,
    }


HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "dashboard.html"
# Activos versionados de Data Dragon (descargados por download_champion_icons.py).
# Raiz de contencion de /assets/: nada fuera de aqui se sirve.
ASSETS_ROOT = (HERE / "assets").resolve()
DDRAGON_VERSION = "16.14.1"

# Every decision encodes exactly one option as the one the player actually took.
# Quien la localiza es `features.taken_index` (campo `taken` explicito, con
# fallback al marcador de texto para filas antiguas): el dashboard lo llama en vez
# de llevar su propia copia del regex, que era la duplicacion que ataba el marcado
# a la redaccion. ev_taken = score de esa opcion; ev_optimal = el mejor disponible.

DETECTORS = ["death_v1", "trade_v1", "tempo_v1", "objective_readiness_v1"]
FEEDBACK_VALUES = {"agree", "disagree", "equivalent", "missing_context"}
MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_date(ms) -> str:
    if not ms:
        return "—"
    try:
        d = datetime.fromtimestamp(ms / 1000)
        return f"{d.day} {MONTHS_ES[d.month - 1]}"
    except Exception:
        return "—"


def _pct(v) -> str:
    return f"{round(v * 100)}%" if isinstance(v, (int, float)) else "—"


def _u(v) -> str:
    return f"{int(round(v))}u" if isinstance(v, (int, float)) else "—"


def ev_pair(opts: list) -> tuple:
    """Return (ev_taken, ev_optimal) from a parsed options list."""
    evs = [o["ev_score"] for o in opts if isinstance(o.get("ev_score"), (int, float))]
    if not evs:
        return (None, None)
    ti = taken_index(opts)
    taken = opts[ti].get("ev_score") if ti is not None else None
    return (taken, max(evs))


def build_ctx(det: str, c: dict) -> dict:
    """Flatten a detector's context_json into a clean, human-readable map."""
    out: dict = {}
    if det == "death_v1":
        s = c.get("our_state") or {}
        out["Estado"] = f"Nv {s.get('level', '?')} · {s.get('current_gold', '?')}g disp · {s.get('cs', '?')} CS"
        out["Pelea efectiva"] = c.get("fight_ratio_apparent", "—")
        na = c.get("nearest_ally") or {}
        if na.get("champion"):
            d = na.get("distance_units")
            out["Aliado cercano"] = f"{na['champion']}" + (f" a {int(d)}u" if isinstance(d, (int, float)) else "")
        uns = c.get("non_combatant_enemies_unseen") or []
        if uns:
            out["Enemigos sin ver"] = ", ".join(f"{e.get('champion')} ({e.get('unseen_s')}s)" for e in uns)
        td = c.get("top_damager") or {}
        if td.get("champion"):
            out["Top daño"] = f"{td['champion']} ({td.get('damage', '?')})"
        out["Killer"] = c.get("killer", "—")
    elif det == "trade_v1":
        out["Ventana"] = c.get("minute_span", "—")
        if c.get("duo_damage_taken") is not None:
            out["Daño recibido (tu duo)"] = str(c["duo_damage_taken"])
            out["Daño recibido (duo enemigo)"] = str(c.get("enemy_duo_damage_taken", "?"))
        if c.get("our_hp_pct_end") is not None:
            out["Tu HP al cierre"] = _pct(c["our_hp_pct_end"])
        if c.get("our_mana_pct_end") is not None:
            out["Tu maná al cierre"] = _pct(c["our_mana_pct_end"])
        if c.get("consequence"):
            out["Consecuencia"] = c["consequence"]
        info = (c.get("state_features") or {}).get("info_risk") or {}
        vis = []
        if info.get("wards_nearby") is not None:
            vis.append(f"{info['wards_nearby']} wards cerca")
        if info.get("enemies_unseen") is not None:
            vis.append(f"{info['enemies_unseen']} enemigo(s) sin ver")
        jg = info.get("enemy_jg_unseen_s")
        if isinstance(jg, (int, float)) and jg > 0:
            vis.append(f"jungla enemiga sin ver {int(jg)}s")
        if vis:
            out["Visión"] = " · ".join(vis)
        sr = info.get("safety_ratio")
        if isinstance(sr, (int, float)):
            out["Posición"] = f"profundidad {sr:.2f} — {'tu mitad' if sr >= 0.5 else 'mitad enemiga'}"
    elif det == "tempo_v1":
        out["Resultado pelea"] = c.get("fight_outcome_for_us", "—")
        if c.get("distance_to_fight_units") is not None:
            out["Distancia"] = _u(c["distance_to_fight_units"])
        if c.get("estimated_travel_s") is not None:
            out["Travel"] = f"{c['estimated_travel_s']}s"
        if c.get("fight_duration_s") is not None:
            out["Duración pelea"] = f"{c['fight_duration_s']}s"
        out["¿Podías llegar?"] = "Sí" if c.get("could_arrive_in_time") else "No"
        act = c.get("your_activity") or {}
        if act.get("description"):
            out["Tu actividad"] = act["description"]
        out["Objetivo en juego"] = c.get("objective_at_stake") or "ninguno"
    elif det == "objective_readiness_v1":
        out["Objetivo"] = c.get("objective", "—")
        out["Tomado por"] = c.get("taken_by", "—")
        if c.get("distance_to_pit_units") is not None:
            out["Distancia al pit"] = _u(c["distance_to_pit_units"])
        out["Posición"] = "En pit" if c.get("at_pit") else ("Demasiado lejos" if c.get("too_far") else "Cerca")
        if c.get("your_wards_near_pit") is not None:
            out["Tu visión"] = f"{c['your_wards_near_pit']} wards / {c.get('your_sweeps_near_pit', 0)} sweeps cerca"
        vd = c.get("vision_diff")
        if vd is not None:
            out["Info en el fight"] = ("a favor" if vd > 0 else "en contra" if vd < 0 else "pareja") + f" ({vd:+d})"
        st = c.get("your_state_at_kill") or {}
        if st.get("hp_pct") is not None or st.get("mana_pct") is not None:
            out["HP / Mana"] = f"{_pct(st.get('hp_pct'))} / {_pct(st.get('mana_pct'))}"
        if st.get("level") is not None:
            out["Nivel · items"] = f"Nv {st['level']} · {st.get('items_completed', '?')} items"
        if st.get("current_gold") is not None:
            out["Oro disponible"] = f"{st['current_gold']}g"
    else:
        for k, v in c.items():
            if isinstance(v, (str, int, float, bool)):
                out[k] = str(v)
    wt = (c.get("state_features") or {}).get("wave_tempo") or {}
    ws = wt.get("wave_state")
    if ws and "Oleada" not in out:
        label = {"pushed_into_enemy": "empujada al enemigo", "frozen_near_tower": "cerca de tu torre",
                 "under_tower": "bajo torre", "neutral": "neutral"}.get(ws, ws)
        if wt.get("wave_source") == "ally":
            label += " (lane cercana)"
        out["Oleada"] = label
    return out


SQL_DEC = """
SELECT d.id, d.match_id, d.detector_id, d.game_time_ms, d.moment, d.outcome,
       d.context_json, d.options_json, d.argument, d.clip_path, d.clip_offset_s,
       d.user_feedback, d.user_feedback_note, d.user_best_option, d.user_mark_type,
       d.user_feedback_fingerprint, d.user_feedback_at_utc, d.created_at_utc,
       m.our_cohort AS cohort, m.game_start_ms,
       p.champion_name AS champion, p.team_position AS role
FROM decisions d
JOIN matches m ON m.match_id = d.match_id
LEFT JOIN participants p ON p.match_id = d.match_id AND p.puuid = m.our_puuid
{where}
ORDER BY m.game_start_ms DESC, d.game_time_ms ASC
"""


def _situation(detector: str, ctx: dict):
    """A per-detector sub-type so comparisons stay apples-to-apples (e.g. don't
    compare an even-numbers death near an objective with an outnumbered death
    while farming). Used as part of the comparability key in query_pair."""
    if detector == "death_v1":
        fr = (ctx.get("fight_ratio_apparent") or "").lower()
        if "v" in fr:
            try:
                a, b = (int(x) for x in fr.split("v", 1))
                return "par" if a == b else ("desventaja" if a < b else "ventaja")
            except ValueError:
                return None
        return None
    if detector == "tempo_v1":
        return "con_objetivo" if ctx.get("objective_at_stake") else "farmeo"
    if detector == "objective_readiness_v1":
        return ctx.get("objective_type")
    if detector == "trade_v1":
        # trade v2 context: our_hp_pct_end (the old v1 "entry" block no longer
        # exists, so the previous predicate always returned None). Won and lost
        # trades must SHARE a situation — pairing them against each other is the
        # whole point of persisting won trades for Calibrar.
        e = ctx.get("our_hp_pct_end")
        if isinstance(e, (int, float)):
            return "hp_alto" if e >= 0.6 else "hp_bajo"
        return None
    return None


def _historical_mark(row: sqlite3.Row, ctx: dict) -> dict | None:
    """El bloque "marca historica" de la card, o None si la marca sigue vigente.

    `user_best_option` es un INDICE contra el conjunto de opciones de ENTONCES.
    Cuando ese conjunto cambia, la marca se conserva (nota incluida) pero deja de
    entrenar al learner — y hasta ahora la card seguia pintandola como vigente,
    sin forma de que el usuario lo supiera. El veredicto sale de
    `features.mark_validity`, la MISMA funcion que usa _decision_feedback_pairs:
    card y learner no pueden discrepar.

    Solo aplica a marcas CON `user_best_option`: es el unico dato cuyo
    significado depende del orden de las opciones.
    """
    if row["user_best_option"] is None:
        return None
    verdict = mark_validity(
        row["user_feedback_fingerprint"], row["detector_id"], ctx.get("action") or {},
        json.loads(row["options_json"]), row["user_feedback_at_utc"], row["created_at_utc"])
    if verdict not in STALE_REASONS:
        return None
    old_ids = fingerprint_action_ids(row["user_feedback_fingerprint"])
    bo = row["user_best_option"]
    chosen = old_ids[bo] if 0 <= bo < len(old_ids) else None
    return {
        "reason": verdict,
        "marked_at": row["user_feedback_at_utc"],
        "feedback": row["user_feedback"],
        "mark_type": row["user_mark_type"],
        "note": row["user_feedback_note"],
        "chosen_action_id": chosen,
        # Mismo id, ya en espanol: el mapa vive en features.ACTION_LABELS_ES y no
        # se duplica en el front.
        "chosen_action_label": action_label_es(chosen) if chosen else None,
        "previous_action_ids": [action_label_es(a) for a in old_ids],
    }


def enrich(row: sqlite3.Row, notes_map: dict | None = None) -> dict:
    ctx = json.loads(row["context_json"])
    opts = json.loads(row["options_json"])
    evs = [o["ev_score"] for o in opts if isinstance(o.get("ev_score"), (int, float))]
    ev_optimal = max(evs) if evs else None
    taken_idx = taken_index(opts)
    ev_taken = opts[taken_idx]["ev_score"] if taken_idx is not None else None
    options = [
        {
            "label": o.get("label", ""),
            "consequence": o.get("predicted_consequence", ""),
            "ev": o.get("ev_score"),
            "taken": i == taken_idx,
            "optimal": o.get("ev_score") == ev_optimal,
        }
        for i, o in enumerate(opts)
    ]
    pidx = power_index((ctx.get("state_features") or {}).get("power"))
    action_id = (ctx.get("action") or {}).get("action_id")
    hef = hef_score(ctx.get("state_features"), action_id, weights=_ACTIVE_WEIGHTS)
    hef["label"] = hef_label(hef.get("score"))
    return {
        "id": row["id"],
        "detector": row["detector_id"],
        "match": row["match_id"],
        "champion": row["champion"] or "—",
        "role": row["role"],
        "cohort": row["cohort"] or "—",
        "date": fmt_date(row["game_start_ms"]),
        "game_time_ms": row["game_time_ms"],
        "time": ctx.get("time_mmss") or mmss(row["game_time_ms"]),
        "minute_span": ctx.get("minute_span"),   # trades: ventana medida ("min 8" / "min 7-8")
        "objective": ctx.get("objective"),
        "objective_type": ctx.get("objective_type"),
        "situation": _situation(row["detector_id"], ctx),
        "moment": row["moment"] or "",
        "outcome": row["outcome"] or "",
        "ev_taken": ev_taken,
        "ev_optimal": ev_optimal,
        "ev_gap": round(ev_optimal - ev_taken, 2) if (ev_taken is not None and ev_optimal is not None) else None,
        "options": options,
        "ctx": build_ctx(row["detector_id"], ctx),
        "argument": row["argument"] or "",
        "has_clip": bool(row["clip_path"]),
        "clip_offset_s": row["clip_offset_s"],
        "reward_score": (ctx.get("reward") or {}).get("reward_score"),
        "power_index": pidx,
        "power_label": power_label(pidx),
        "hef": hef,
        "feedback": row["user_feedback"],
        "feedback_note": row["user_feedback_note"],
        "best_option": row["user_best_option"],
        "mark_type": row["user_mark_type"],
        # Presente SOLO si la marca ya no cuenta porque el conjunto de opciones
        # cambio bajo ella (#25). La card lo pinta aparte del feedback vigente.
        "historical_mark": _historical_mark(row, ctx),
        # The player's own reasoning about this play, in their words, taken from
        # the pairwise judgments it appeared in. Lets the coach cite how THEY think.
        "user_notes": (notes_map or {}).get(row["id"], []),
    }


# Cohorts of REFERENCE players (e.g. the challenger Sona one-trick). Their matches
# are for comparison only: aggregates (stats, patterns) must never mix them with the
# user's own play unless that cohort is selected explicitly.
REFERENCE_COHORTS = ("challenger",)


def _cohort_filter(cohort):
    return cohort if cohort in ("master", "emerald") + REFERENCE_COHORTS else None


def _own_cohort_sql(col: str) -> str:
    """WHERE fragment limiting to the user's own accounts (excludes reference cohorts)."""
    quoted = ",".join(f"'{c}'" for c in REFERENCE_COHORTS)
    return f"({col} IS NULL OR {col} NOT IN ({quoted}))"


def _decision_key(conn, decision_id):
    """Stable key for a decision that survives re-analysis: 'match|detector|game_time_ms'.
    (decision ids change on DELETE+reinsert; match/detector/game_time do not.)"""
    r = conn.execute(
        "SELECT match_id, detector_id, game_time_ms FROM decisions WHERE id = ?",
        (decision_id,)).fetchone()
    if not r:
        return None
    return f"{r['match_id']}|{r['detector_id']}|{r['game_time_ms']}"


def _resolve_key(conn, key):
    """Current decision id for a stable key (None if it no longer exists)."""
    if not key:
        return None
    parts = key.split("|", 2)
    if len(parts) != 3:
        return None
    mid, det, gtms = parts
    try:
        gt = int(gtms)
    except ValueError:
        return None
    r = conn.execute(
        "SELECT id FROM decisions WHERE match_id=? AND detector_id=? AND game_time_ms=? ORDER BY id LIMIT 1",
        (mid, det, gt)).fetchone()
    return r["id"] if r else None


def _pref_ids(conn, row):
    """(a_id, b_id) for a preference row, resolved by stable key when present so
    re-analysis can't orphan the feedback; falls back to the stored id."""
    keys = row.keys()
    a = _resolve_key(conn, row["a_key"]) if "a_key" in keys else None
    b = _resolve_key(conn, row["b_key"]) if "b_key" in keys else None
    return (a if a is not None else row["decision_a_id"],
            b if b is not None else row["decision_b_id"])


def _backfill_pref_keys(conn) -> int:
    """Fill stable keys for preferences created before the columns existed, while
    their stored decision ids still resolve. Idempotent; returns rows updated."""
    n = 0
    for r in conn.execute("SELECT id, decision_a_id, decision_b_id, a_key, b_key FROM preferences"):
        upd = {}
        if not r["a_key"]:
            k = _decision_key(conn, r["decision_a_id"])
            if k:
                upd["a_key"] = k
        if not r["b_key"]:
            k = _decision_key(conn, r["decision_b_id"])
            if k:
                upd["b_key"] = k
        if upd:
            cols = ", ".join(f"{c} = ?" for c in upd)
            conn.execute(f"UPDATE preferences SET {cols} WHERE id = ?", (*upd.values(), r["id"]))
            n += 1
    conn.commit()
    return n


def _notes_map(conn) -> dict:
    """decision_id -> list of the player's own reasoning notes from the pairwise
    judgments that decision took part in. A note describes the whole A/B pair, so
    it is attached to both sides with `side` marking which one this decision was.
    This is the player's real reasoning in their words, tied to the play."""
    rows = conn.execute(
        "SELECT id, detector_id, decision_a_id, decision_b_id, winner, note, created_at_utc, a_key, b_key "
        "FROM preferences ORDER BY id"
    ).fetchall()
    if not rows:
        return {}
    resolved = {r["id"]: _pref_ids(conn, r) for r in rows}  # pref id -> current (a_id, b_id)
    ids = {i for ab in resolved.values() for i in ab if i is not None}
    moments = {}
    if ids:
        qmarks = ",".join("?" * len(ids))
        moments = {d["id"]: (d["moment"] or "")
                   for d in conn.execute(
                       f"SELECT id, moment FROM decisions WHERE id IN ({qmarks})", tuple(ids))}
    out = collections.defaultdict(list)
    for r in rows:
        note = (r["note"] or "").strip()
        if not note:
            continue
        a_id, b_id = resolved[r["id"]]
        for did, side, other in ((a_id, "a", b_id), (b_id, "b", a_id)):
            if did is None:
                continue
            out[did].append({
                "pref_id": r["id"],
                "detector": r["detector_id"],
                "side": side,            # whether this decision was A or B in the pair
                "winner": r["winner"],   # which side the player judged better (a/b/tie)
                "note": note,            # full note (refers to both A and B)
                "against_id": other,
                "against_moment": moments.get(other, ""),
                "created_at": r["created_at_utc"],
            })
    return dict(out)


# WON trades are kept for Calibrar pairing but hidden from the review list —
# the player must not have to re-judge what went well (visual noise).
_HIDDEN_POSITIVE = '%"positive_trade": true%'


def query_decisions(conn, cohort, detector, match=None, include_hidden=False) -> list:
    where, args = [], []
    cf = _cohort_filter(cohort)
    if cf:
        where.append("m.our_cohort = ?")
        args.append(cf)
    if detector in DETECTORS:
        where.append("d.detector_id = ?")
        args.append(detector)
    if match:
        where.append("d.match_id = ?")
        args.append(match)
    if not include_hidden:
        where.append("d.context_json NOT LIKE ?")
        args.append(_HIDDEN_POSITIVE)
    sql = SQL_DEC.format(where=("WHERE " + " AND ".join(where)) if where else "")
    nmap = _notes_map(conn)
    return [enrich(r, nmap) for r in conn.execute(sql, args)]


# Momento del trade via HP del recorder (1Hz): Riot solo da danio POR MINUTO,
# asi que el detector estampa el minuto y el clip abre en su inicio — habia que
# buscar el frame a mano (feedback 2026-07-18). La mayor caida de HP propia
# dentro de la ventana localiza el segundo del intercambio. Se computa PEREZOSO
# (una decision a la vez, al abrir su clip): los snapshots.jsonl pesan 50-100 MB,
# leer el de cada partida en la ruta masiva /api/decisions colgaba el dashboard.
# El lector por ventana corta apenas pasa el minuto; el resultado se cachea por
# decision. Sin session o sin caida >= umbral -> None (clip abre al inicio del min).
_TRADE_MOMENT_CACHE: dict = {}   # decision_id -> moment_ms | None


def _span_minutes_from_text(minute_span):
    """Fallback para decisiones viejas sin `span_minutes`: parsea "min A-B" -> B-A.

    Si no hay dos numeros separados por guion (formato inesperado o ausente),
    cae a 1 minuto en vez de fallar.
    """
    text = minute_span or ""
    m = re.search(r"(-?\d+)\s*-\s*(-?\d+)", text)
    if not m:
        return 1
    a, b = int(m.group(1)), int(m.group(2))
    n = b - a
    return n if n >= 1 else 1


def trade_moment_ms(conn, decision_id):
    if decision_id in _TRADE_MOMENT_CACHE:
        return _TRADE_MOMENT_CACHE[decision_id]
    row = conn.execute(
        "SELECT d.game_time_ms, d.context_json, s.session_dir "
        "FROM decisions d LEFT JOIN sessions s ON s.match_id = d.match_id "
        "WHERE d.id = ? AND d.detector_id = 'trade_v1'", (decision_id,)).fetchone()
    moment = None
    if row and row["session_dir"]:
        ctx = json.loads(row["context_json"])
        n_min = ctx.get("span_minutes")
        if not isinstance(n_min, int) or n_min < 1:
            n_min = _span_minutes_from_text(ctx.get("minute_span"))
        t0 = row["game_time_ms"] / 1000.0
        serie = hp_pct_series_windowed(Path(row["session_dir"]), t0, t0 + 60.0 * n_min)
        start_s = biggest_drop_start_s(serie)
        moment = int(start_s * 1000) if start_s is not None else None
    _TRADE_MOMENT_CACHE[decision_id] = moment
    return moment


@lru_cache(maxsize=1)
def _champion_icon_index() -> dict:
    """Nombre visible de campeon -> ruta local de su retrato.

    El nombre que guarda Riot no sirve como filename: 'Bel'Veth' es 'Belveth' y
    'Wukong' es 'MonkeyKing'. El manifiesto del descargador guarda ambos, asi que
    el mapeo se lee de ahi en vez de derivarlo. Si no hay manifiesto, se devuelve
    vacio y la UI cae al glifo local sin romper la fila.
    """
    manifest = ASSETS_ROOT / "ddragon" / DDRAGON_VERSION / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index = {}
    for entry in data.get("champions") or []:
        name, rel = entry.get("champion_name") or entry.get("name"), entry.get("file")
        if name and rel:
            index[name] = f"/assets/ddragon/{DDRAGON_VERSION}/{rel}"
    return index


def champion_icon_url(champion):
    return _champion_icon_index().get(champion)


# A) Decisions grouped by match. obtener_partidas(): matches + decision counts.
# Los conteos por detector comparten el JOIN, asi que heredan la exclusion de
# `positive_trade`: si se contaran aparte, su suma no cuadraria con
# decisions_count y el desajuste se veria en la fila al primer vistazo.
DETECTOR_COUNT_KEYS = {
    "death_v1": "death_count",
    "objective_readiness_v1": "objective_count",
    "trade_v1": "trade_count",
    "tempo_v1": "tempo_count",
}
_SQL_DETECTOR_COUNTS = "\n".join(
    f"       SUM(CASE WHEN d.detector_id = '{det}' THEN 1 ELSE 0 END) AS {col},"
    for det, col in DETECTOR_COUNT_KEYS.items()
)
SQL_MATCHES = f"""
SELECT m.match_id, m.game_start_ms, m.game_duration_s, m.our_cohort,
       p.champion_name AS champion, p.win, p.kills, p.deaths, p.assists,
       p.team_position AS role,
       COUNT(d.id) AS decisions_count,
{_SQL_DETECTOR_COUNTS}
       SUM(CASE WHEN d.user_feedback IS NOT NULL OR d.user_best_option IS NOT NULL
                OR d.user_feedback_note IS NOT NULL THEN 1 ELSE 0 END) AS reviewed_count
FROM matches m
LEFT JOIN decisions d ON d.match_id = m.match_id
     AND d.context_json NOT LIKE '%"positive_trade": true%'
LEFT JOIN participants p ON p.match_id = m.match_id AND p.puuid = m.our_puuid
{{where}}
GROUP BY m.match_id
ORDER BY m.game_start_ms DESC
"""


def query_matches(conn, cohort=None) -> list:
    cf = _cohort_filter(cohort)
    where = "WHERE m.our_cohort = ?" if cf else ""
    rows = conn.execute(SQL_MATCHES.format(where=where), ((cf,) if cf else ()))
    return [
        {
            "match_id": r["match_id"],
            "date": fmt_date(r["game_start_ms"]),
            # Crudo ademas de formateado: la antiguedad se calcula en el
            # frontend y una fecha ya formateada no sirve para restar.
            "game_start_ms": r["game_start_ms"],
            "duration_s": r["game_duration_s"],
            "cohort": r["our_cohort"] or "—",
            "champion": r["champion"] or "—",
            "champion_icon": champion_icon_url(r["champion"]),
            # Sale del participante propio, no de las decisiones: una partida
            # puede no tener ninguna y aun asi tiene rol.
            "role": r["role"] or "—",
            "win": bool(r["win"]) if r["win"] is not None else None,
            "kills": r["kills"],
            "deaths": r["deaths"],
            "assists": r["assists"],
            "decisions_count": r["decisions_count"],
            "detector_counts": {
                det: (r[col] or 0) for det, col in DETECTOR_COUNT_KEYS.items()
            },
            "reviewed_count": r["reviewed_count"] or 0,
        }
        for r in rows
    ]


MAP_MAX = 15000  # Summoner's Rift is 0..15000 in x and y


def _our_participant_id(conn, match):
    r = conn.execute(
        "SELECT p.participant_id FROM participants p "
        "JOIN matches m ON m.match_id = p.match_id AND m.our_puuid = p.puuid "
        "WHERE p.match_id = ?",
        (match,),
    ).fetchone()
    return r["participant_id"] if r else None


def _decision_world_pos(ctx, conn, match, us_id, game_time_ms):
    """Best world (x, y) for a decision: the player's own position at the moment.

    Prefer an explicit player position in the context; fall back to the closest
    timeline_frame of the player. Returns (x, y) or None.
    """
    for key in ("death_position", "your_position_at_fight_start", "your_position_at_kill_frame"):
        p = ctx.get(key)
        if isinstance(p, dict) and isinstance(p.get("x"), (int, float)):
            return (p["x"], p["y"])
    if us_id is not None:
        r = conn.execute(
            "SELECT position_x, position_y FROM timeline_frames "
            "WHERE match_id = ? AND participant_id = ? AND timestamp_ms <= ? "
            "ORDER BY timestamp_ms DESC LIMIT 1",
            (match, us_id, game_time_ms),
        ).fetchone()
        if r and r["position_x"] is not None:
            return (r["position_x"], r["position_y"])
    return None


def query_match_map(conn, match) -> dict:
    """Player route + decisions placed in world coordinates, for the map view."""
    us_id = _our_participant_id(conn, match)
    mrow = conn.execute("SELECT game_duration_s FROM matches WHERE match_id = ?", (match,)).fetchone()
    duration_s = mrow["game_duration_s"] if mrow else None
    path = []
    if us_id is not None:
        for r in conn.execute(
            "SELECT timestamp_ms, position_x, position_y FROM timeline_frames "
            "WHERE match_id = ? AND participant_id = ? AND position_x IS NOT NULL "
            "ORDER BY timestamp_ms",
            (match, us_id),
        ):
            path.append({"t_ms": r["timestamp_ms"], "x": r["position_x"], "y": r["position_y"]})

    decisions = []
    for r in conn.execute(
        "SELECT id, detector_id, game_time_ms, moment, context_json, options_json "
        "FROM decisions WHERE match_id = ? ORDER BY game_time_ms",
        (match,),
    ):
        ctx = json.loads(r["context_json"])
        pos = _decision_world_pos(ctx, conn, match, us_id, r["game_time_ms"])
        if pos is None:
            continue
        ev_taken, ev_opt = ev_pair(json.loads(r["options_json"]))
        gap = round(ev_opt - ev_taken, 2) if (ev_taken is not None and ev_opt is not None) else None
        reward = (ctx.get("reward") or {}).get("reward_score")
        decisions.append({
            "id": r["id"], "detector": r["detector_id"], "game_time_ms": r["game_time_ms"],
            "time": ctx.get("time_mmss") or mmss(r["game_time_ms"]),
            "moment": r["moment"] or "", "x": pos[0], "y": pos[1],
            "ev_taken": ev_taken, "ev_gap": gap, "reward_score": reward,
        })
    # Win-probability proxy (p188226 idea, no trained RNN): team gold-diff per
    # minute through a logistic. total_gold = accumulated team economy = the
    # right metric for advantage here (NOT current_gold, which is rule #7's
    # spendable gold). Objectives/towers are marked as the hitos that explain
    # the jumps.
    our_row = conn.execute(
        "SELECT team_id FROM participants WHERE match_id = ? AND participant_id = ?",
        (match, us_id),
    ).fetchone() if us_id is not None else None
    our_team = our_row["team_id"] if our_row else None
    winprob, key_events = [], []
    if our_team is not None:
        team_of = {r["participant_id"]: r["team_id"]
                   for r in conn.execute("SELECT participant_id, team_id FROM participants WHERE match_id = ?", (match,))}
        agg = {}  # frame_index -> [t_ms, our_gold, enemy_gold]
        for r in conn.execute(
            "SELECT frame_index, timestamp_ms, participant_id, total_gold FROM timeline_frames WHERE match_id = ?",
            (match,),
        ):
            a = agg.setdefault(r["frame_index"], [r["timestamp_ms"], 0, 0])
            a[0] = r["timestamp_ms"]
            g = r["total_gold"] or 0
            if team_of.get(r["participant_id"]) == our_team:
                a[1] += g
            else:
                a[2] += g
        for fi in sorted(agg):
            t, o, e = agg[fi]
            gd = o - e
            winprob.append({"t_ms": t, "gold_diff": gd, "prob": round(1 / (1 + math.exp(-gd / 4000.0)), 3)})

        for r in conn.execute(
            "SELECT type, payload_json FROM timeline_events WHERE match_id = ? "
            "AND type IN ('ELITE_MONSTER_KILL','BUILDING_KILL') ORDER BY timestamp_ms",
            (match,),
        ):
            ev = json.loads(r["payload_json"])
            if r["type"] == "ELITE_MONSTER_KILL":
                kind = (ev.get("monsterType") or "objetivo").lower()
                ours = ev.get("killerTeamId") == our_team
            else:
                bt = ev.get("buildingType") or ""
                kind = "inhibitor" if "INHIBITOR" in bt else "tower"
                ours = ev.get("teamId") != our_team   # we destroyed the enemy's building
            key_events.append({"t_ms": ev.get("timestamp"), "kind": kind, "ours": bool(ours)})

    return {"map_max": MAP_MAX, "duration_s": duration_s, "path": path,
            "decisions": decisions, "winprob": winprob, "events": key_events}


# #4 — Match similarity (Online-Players-Viz idea: cluster matches by behavior).
# A per-match behavior profile: fraction of decisions per detector + average
# ev/gap/reward/power. Normalized (z-score) across the cohort so dimensions are
# comparable, then nearest neighbors by Euclidean distance. Answers "which of my
# games does this one resemble — and are those wins or losses?".
SIM_DIM_LABELS = {
    "death_v1": "% muertes", "trade_v1": "% trades", "tempo_v1": "% tempo",
    "objective_readiness_v1": "% objetivos",
    "avg_ev": "EV medio", "avg_gap": "gap EV medio",
    "avg_reward": "reward medio", "avg_power": "fuerza media",
}
SIM_DIMS = DETECTORS + ["avg_ev", "avg_gap", "avg_reward", "avg_power"]


def _build_profiles(conn, cohort):
    meta = {}
    for r in conn.execute(
        "SELECT m.match_id, m.game_start_ms, p.champion_name AS champion, p.win "
        "FROM matches m LEFT JOIN participants p "
        "ON p.match_id = m.match_id AND p.puuid = m.our_puuid "
        "WHERE m.our_cohort = ?", (cohort,),
    ):
        meta[r["match_id"]] = {"champion": r["champion"] or "—",
                               "date": fmt_date(r["game_start_ms"]),
                               "win": bool(r["win"]) if r["win"] is not None else None}

    acc = {mid: {"n": 0, "det": {}, "ev": [], "gap": [], "reward": [], "power": []} for mid in meta}
    for r in conn.execute(
        "SELECT d.match_id, d.detector_id, d.options_json, d.context_json "
        "FROM decisions d JOIN matches m ON m.match_id = d.match_id "
        "WHERE m.our_cohort = ?", (cohort,),
    ):
        a = acc.get(r["match_id"])
        if a is None:
            continue
        a["n"] += 1
        a["det"][r["detector_id"]] = a["det"].get(r["detector_id"], 0) + 1
        t, o = ev_pair(json.loads(r["options_json"]))
        if t is not None:
            a["ev"].append(t)
        if t is not None and o is not None:
            a["gap"].append(o - t)
        ctx = json.loads(r["context_json"])
        rs = (ctx.get("reward") or {}).get("reward_score")
        if isinstance(rs, (int, float)):
            a["reward"].append(rs)
        pi = (ctx.get("state_features") or {}).get("power", {}).get("power_index")
        if isinstance(pi, (int, float)):
            a["power"].append(pi)

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    vectors = {}
    for mid, a in acc.items():
        if a["n"] == 0:
            continue
        vec = [a["det"].get(det, 0) / a["n"] for det in DETECTORS]
        vec += [_avg(a["ev"]), _avg(a["gap"]), _avg(a["reward"]), _avg(a["power"])]
        vectors[mid] = vec
    return meta, vectors


def query_match_similarity(conn, match, top_n=4) -> dict:
    row = conn.execute("SELECT our_cohort FROM matches WHERE match_id = ?", (match,)).fetchone()
    if not row:
        return {"match": match, "neighbors": [], "error": "match no encontrado"}
    cohort = row["our_cohort"]
    meta, vectors = _build_profiles(conn, cohort)
    if match not in vectors or len(vectors) < 2:
        return {"match": match, "cohort": cohort, "neighbors": [],
                "note": "no hay suficientes partidas comparables en este cohort"}

    # z-score per dimension across the cohort
    mids = list(vectors)
    means, stds = [], []
    for j in range(len(SIM_DIMS)):
        col = [vectors[m][j] for m in mids]
        mu = sum(col) / len(col)
        var = sum((x - mu) ** 2 for x in col) / len(col)
        means.append(mu)
        stds.append(var ** 0.5)

    def z(mid):
        return [((vectors[mid][j] - means[j]) / stds[j]) if stds[j] > 1e-9 else 0.0
                for j in range(len(SIM_DIMS))]

    zt = z(match)
    dists = []
    for m in mids:
        if m == match:
            continue
        zm = z(m)
        d = sum((zt[j] - zm[j]) ** 2 for j in range(len(SIM_DIMS))) ** 0.5
        dists.append((d, m, zm))
    dists.sort(key=lambda x: x[0])

    neighbors = []
    for d, m, zm in dists[:top_n]:
        # Shared traits: dims where both deviate notably in the same direction.
        shared = []
        for j in range(len(SIM_DIMS)):
            if abs(zt[j]) > 0.5 and abs(zm[j]) > 0.5 and (zt[j] * zm[j]) > 0:
                shared.append({"dim": SIM_DIM_LABELS[SIM_DIMS[j]],
                               "high": zt[j] > 0,
                               "you": round(vectors[match][j], 2),
                               "them": round(vectors[m][j], 2)})
        shared.sort(key=lambda s: -abs(s["you"] - 0))
        neighbors.append({
            "match_id": m, "champion": meta[m]["champion"], "date": meta[m]["date"],
            # El retrato es el ancla de reconocimiento: sin el, una partida
            # similar solo se distingue por victoria/derrota y una fecha.
            "champion_icon": champion_icon_url(meta[m]["champion"]),
            "win": meta[m]["win"], "distance": round(d, 2), "shared": shared[:3],
        })
    wins = [n["win"] for n in neighbors if n["win"] is not None]
    loss_rate = round(sum(1 for w in wins if not w) / len(wins), 2) if wins else None
    return {"match": match, "cohort": cohort, "neighbors": neighbors, "loss_rate_neighbors": loss_rate}


# ---- Preference learning: compare two decisions, learn the HEF weights ω. ----
def _phase(ms) -> str:
    m = (ms or 0) / 60000.0
    return "early" if m < 14 else ("mid" if m < 25 else "late")


def query_pair(conn, detector=None) -> dict:
    """Pick two *comparable* decisions that differ in how they were played.

    Comparable = same cohort, role, game phase and (for objective detectors)
    objective type — so the judgment isn't biased by role/time/objective. Among
    comparable groups, return a NOT-yet-judged pair with a meaningful HEF gap,
    chosen with variety (so you don't keep judging the same pair). Relaxes the
    comparability key (drop objective, then phase) only if nothing stricter is
    available.
    """
    judged = {frozenset(_pref_ids(conn, r))
              for r in conn.execute("SELECT decision_a_id, decision_b_id, a_key, b_key FROM preferences")}
    dets = [detector] if detector in DETECTORS else list(DETECTORS)
    decs_by_det = {det: [d for d in query_decisions(conn, None, det, None, include_hidden=True)
                         if d.get("hef") and d["hef"].get("score") is not None] for det in dets}
    keyfns = [
        lambda d: (d.get("cohort"), d.get("role"), _phase(d.get("game_time_ms")), d.get("situation")),
        lambda d: (d.get("cohort"), d.get("role"), _phase(d.get("game_time_ms")), None),
        lambda d: (d.get("cohort"), d.get("role"), None, None),
    ]
    for keyfn in keyfns:
        # All valid pairs at this comparability level, scored by #clips then ΔHEF.
        cands = []  # (clip_score, dhef, det, key, a, b)
        for det, decs in decs_by_det.items():
            groups = collections.defaultdict(list)
            for d in decs:
                groups[keyfn(d)].append(d)
            for k, g in groups.items():
                if len(g) < 2:
                    continue
                for i in range(len(g)):
                    for j in range(i + 1, len(g)):
                        a, b = g[i], g[j]
                        if frozenset((a["id"], b["id"])) in judged:
                            continue
                        dh = abs(a["hef"]["score"] - b["hef"]["score"])
                        if dh < 0.10:
                            continue
                        clip_score = (1 if a["has_clip"] else 0) + (1 if b["has_clip"] else 0)
                        cands.append((clip_score, dh, det, k, a, b))
        if not cands:
            continue
        # Prefer both-have-clip; within that tier pick among the most informative
        # (variety so you don't keep judging the same pair).
        max_clip = max(c[0] for c in cands)
        tier = sorted([c for c in cands if c[0] == max_clip], key=lambda c: -c[1])
        clip_score, dh, det, k, a, b = random.choice(tier[:max(3, len(tier) // 3)])
        pair = [a, b]
        random.shuffle(pair)
        return {"detector": det, "both_have_clip": max_clip == 2,
                "context": {"cohort": k[0], "role": k[1], "phase": k[2], "situation": k[3]},
                "pair": pair}
    return {"pair": None}


def _terms_of(conn, decision_id, cache) -> dict:
    if decision_id in cache:
        return cache[decision_id]
    r = conn.execute("SELECT context_json FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    terms = {}
    if r:
        ctx = json.loads(r["context_json"])
        aid = (ctx.get("action") or {}).get("action_id")
        terms = hef_score(ctx.get("state_features"), aid).get("terms") or {}
    cache[decision_id] = terms
    return terms


# A pair whose note says the call hinged on mechanics, lag, or had no context
# ("partida oculta") is decided by something the HEF can't see, so it must NOT
# train the weights (it only adds noise). The player asked for exactly this.
_NOISE_NOTE = re.compile(r"ejecu|mec[aá]nic|fall[eé]|combo|lag|oculta", re.I)


def _decision_feedback_pairs(conn) -> tuple[list, dict]:
    """Turn per-decision feedback into preference pairs for the weight learner.

    Only decisions where the user EXPLICITLY marked the adequate option
    (user_best_option) train the learner: the adequate ACTION should outscore the
    action actually taken, holding the state fixed (isolates action_fit).

    Hard gates (each one bit us in production):
    - STALENESS BY CONTENT: option sets get redesigned; persist_decisions
      preserves the user's best_option index across re-analysis, so an index
      chosen for the OLD options can point at a semantically different option
      today (it generated exactly-backwards pairs after the 06-09 redesign).
      A mark counts if the fingerprint of the option set it was made against
      (stored at mark time / backfilled from backups) still equals the current
      incarnation's fingerprint — i.e. the options didn't actually change. The
      old timestamp gate (feedback after created_at) remains as fallback for
      marks without a fingerprint; on 2026-06-10 that gate alone killed 192/192
      marks just because a full re-analysis refreshed every created_at.
    - NO 'agree' BACKFILL: deriving ground truth from the system's own top-EV
      option trains the learner to agree with the heuristic it's calibrating
      (circular). Only explicit user choices vote.

    Returns (pairs, stats) — pairs are (terms_best, terms_taken, "a", "dec");
    stats = {n_stale, n_revived, n_noisy, excluded_noisy: [...]}.
    """
    pairs = []
    stats = {"n_stale": 0, "n_revived": 0, "n_noisy": 0, "excluded_noisy": []}
    for r in conn.execute(
        "SELECT id, detector_id, context_json, options_json, user_feedback, "
        "user_feedback_note, user_best_option, user_feedback_at_utc, created_at_utc, "
        "user_feedback_fingerprint, user_mark_type "
        "FROM decisions WHERE user_best_option IS NOT NULL"
    ):
        note = r["user_feedback_note"] or ""
        if not feedback.trains_learner(r["user_mark_type"], note, _NOISE_NOTE):
            stats["n_noisy"] += 1
            stats["excluded_noisy"].append(
                {"source": "dec", "id": r["id"], "note": note[:160]})
            continue
        if r["user_feedback"] == "equivalent":
            continue  # a near-tie the user judged equivalent: no directional signal
        c = json.loads(r["context_json"])
        action = c.get("action") or {}
        # Un solo dueno de la regla de vigencia (features.mark_validity): la card
        # del dashboard llama a la MISMA funcion, asi que no pueden divergir.
        verdict = mark_validity(
            r["user_feedback_fingerprint"], r["detector_id"], action,
            json.loads(r["options_json"]), r["user_feedback_at_utc"], r["created_at_utc"])
        if verdict in STALE_REASONS:
            stats["n_stale"] += 1
            continue
        if verdict == MARK_REVIVED:
            stats["n_revived"] += 1  # timestamp said stale; content says valid
        sf = c.get("state_features")
        avail = action.get("available_actions") or []
        taken_aid = action.get("action_id")
        if not avail or taken_aid is None:
            continue
        bo = r["user_best_option"]
        best_aid = avail[bo].get("action_id") if 0 <= bo < len(avail) else None
        if not best_aid or best_aid == taken_aid:
            continue
        tb = hef_score(sf, best_aid).get("terms") or {}
        tt = hef_score(sf, taken_aid).get("terms") or {}
        if tb and tt:
            pairs.append((tb, tt, "a", "dec"))  # adequate action beats taken
    return pairs, stats


def query_weights(conn) -> dict:
    prefs = conn.execute(
        "SELECT id, decision_a_id, decision_b_id, winner, note, a_key, b_key FROM preferences"
    ).fetchall()
    cache = {}
    pairs = []
    n_noisy = 0
    n_orphan = 0
    excluded_noisy = []
    for p in prefs:
        if p["winner"] not in ("a", "b"):
            continue
        if _NOISE_NOTE.search(p["note"] or ""):
            n_noisy += 1                       # mechanics/lag/no-context -> exclude
            excluded_noisy.append(
                {"source": "pref", "id": p["id"], "note": (p["note"] or "")[:160]})
            continue
        a_id, b_id = _pref_ids(conn, p)
        ta, tb = _terms_of(conn, a_id, cache), _terms_of(conn, b_id, cache)
        if not ta or not tb:
            n_orphan += 1                      # key no longer resolves / empty terms
            continue
        pairs.append((ta, tb, p["winner"], "pref"))
    dec_pairs, dec_stats = _decision_feedback_pairs(conn)
    return {
        "current": _ACTIVE_WEIGHTS or DEFAULT_WEIGHTS,
        "is_custom": _ACTIVE_WEIGHTS is not None,
        "default": DEFAULT_WEIGHTS,
        "learned": learn_weights(pairs + dec_pairs),
        "n_preferences": len(prefs),
        "n_clean": len(pairs),
        "n_decision_pairs": len(dec_pairs),
        "n_excluded_noisy": n_noisy + dec_stats["n_noisy"],
        "n_excluded_orphan": n_orphan,
        "n_excluded_stale": dec_stats["n_stale"],
        # Marks the old timestamp gate would have killed but whose option set is
        # verifiably unchanged (fingerprint match) — i.e. feedback rescued.
        "n_revived": dec_stats["n_revived"],
        # Auditability: WHICH notes the noise regex ate, from both sources, so a
        # miscategorized judgment ("fallé en rotar" is decisional, not mechanic)
        # can be spotted instead of vanishing into a counter.
        "excluded_noisy": excluded_noisy + dec_stats["excluded_noisy"],
    }


def query_review_queue(conn) -> dict:
    """The triage queue, served live: decisions whose explicit best-option mark
    contradicts (or confirms) the current action_fit formula. Same validity
    gates as _decision_feedback_pairs; same cases scripts/triage_commit_fit.py
    reports — but clickable from the UI, so reviewing them doesn't require
    hunting decision ids by hand."""
    items = []
    for r in conn.execute(
        "SELECT d.id, d.match_id, d.detector_id, d.game_time_ms, d.moment, "
        "d.context_json, d.options_json, d.user_feedback, d.user_feedback_note, "
        "d.user_best_option, d.user_feedback_at_utc, d.created_at_utc, "
        "d.user_feedback_fingerprint, d.user_mark_type, "
        "m.game_start_ms, m.our_cohort AS cohort "
        "FROM decisions d JOIN matches m ON m.match_id = d.match_id "
        "WHERE d.user_best_option IS NOT NULL"
    ):
        note = (r["user_feedback_note"] or "").strip()
        if not feedback.trains_learner(r["user_mark_type"], note, _NOISE_NOTE) \
                or r["user_feedback"] == "equivalent":
            continue
        ctx = json.loads(r["context_json"])
        action = ctx.get("action") or {}
        if mark_validity(r["user_feedback_fingerprint"], r["detector_id"], action,
                         json.loads(r["options_json"]), r["user_feedback_at_utc"],
                         r["created_at_utc"]) in STALE_REASONS:
            continue      # misma regla que el learner y que la card
        avail = action.get("available_actions") or []
        taken = action.get("action_id")
        bo = r["user_best_option"]
        best = avail[bo].get("action_id") if (avail and 0 <= bo < len(avail)) else None
        if not best or not taken or best == taken:
            continue
        sf = ctx.get("state_features") or {}
        pi = power_index(sf.get("power") or {})
        it = _info_term(sf.get("info_risk") or {})
        objectives = sf.get("objectives") or {}
        f_taken = _commit_fit(taken, pi, it, objectives)
        f_best = _commit_fit(best, pi, it, objectives)
        agree = (f_taken is not None and f_best is not None and f_best > f_taken)
        items.append({
            "id": r["id"], "match": r["match_id"], "detector": r["detector_id"],
            "cohort": r["cohort"], "date": fmt_date(r["game_start_ms"]),
            "time": ctx.get("time_mmss") or mmss(r["game_time_ms"]),
            "moment": r["moment"] or "", "taken": taken, "best": best,
            "fit_taken": f_taken, "fit_best": f_best, "agree": agree,
            "note": note[:200],
        })
    items.sort(key=lambda x: (x["agree"], x["detector"], x["date"]))
    return {"items": items, "n": len(items),
            "n_disagree": sum(1 for x in items if not x["agree"])}


def query_player_model(conn) -> dict:
    """The player's self-model for the coach: the curated synthesis written to
    data/player_model.md (distilled from the player's own reasoning notes) plus
    the raw notes with the moment of each side, so the coach reasons with how
    THIS player actually thinks, not just the numeric HEF weights."""
    model_path = Path(DB_PATH).parent / "player_model.md"
    model = model_path.read_text(encoding="utf-8") if model_path.exists() else None
    rows = conn.execute(
        "SELECT id, detector_id, decision_a_id, decision_b_id, winner, note, created_at_utc, a_key, b_key "
        "FROM preferences ORDER BY id"
    ).fetchall()
    resolved = {r["id"]: _pref_ids(conn, r) for r in rows}  # pref id -> current (a_id, b_id)
    ids = {i for ab in resolved.values() for i in ab if i is not None}
    moments = {}
    if ids:
        qmarks = ",".join("?" * len(ids))
        moments = {d["id"]: (d["moment"] or "")
                   for d in conn.execute(
                       f"SELECT id, moment FROM decisions WHERE id IN ({qmarks})", tuple(ids))}
    prefs, n_note, n_dec, n_tie = [], 0, 0, 0
    for r in rows:
        note = (r["note"] or "").strip()
        if note:
            n_note += 1
        if r["winner"] in ("a", "b"):
            n_dec += 1
        elif r["winner"] == "tie":
            n_tie += 1
        prefs.append({
            "pref_id": r["id"],
            "detector": r["detector_id"],
            "winner": r["winner"],
            "note": note,
            "a": {"id": resolved[r["id"]][0], "moment": moments.get(resolved[r["id"]][0], "")},
            "b": {"id": resolved[r["id"]][1], "moment": moments.get(resolved[r["id"]][1], "")},
            "created_at": r["created_at_utc"],
        })
    return {
        "model": model,
        "preferences": prefs,
        "stats": {"n": len(rows), "with_note": n_note, "decisive": n_dec, "tie": n_tie},
    }


def save_preference(conn, payload) -> dict:
    a, b = int(payload["a"]), int(payload["b"])
    winner = payload.get("winner")
    if winner not in ("a", "b", "tie", "skip"):
        return {"ok": False, "error": "winner inválido"}
    conn.execute(
        "INSERT INTO preferences (detector_id, decision_a_id, decision_b_id, winner, note, created_at_utc, a_key, b_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (payload.get("detector"), a, b, winner, payload.get("note"),
         datetime.now(timezone.utc).isoformat(), _decision_key(conn, a), _decision_key(conn, b)),
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM preferences").fetchone()["c"]
    return {"ok": True, "n_preferences": n}


def apply_weights(conn) -> dict:
    res = query_weights(conn)
    learned = res.get("learned")
    if not learned:
        return {"ok": False, "error": "no hay preferencias suficientes"}
    if not learned.get("enough"):
        return {"ok": False, "error": f"solo {learned['n']} pares utilizables: insuficiente para confiar en los pesos"}
    # Guardrail: never apply weights that predict the user's own feedback WORSE
    # than the defaults — that would actively degrade the HEF with one click.
    # Trust the CROSS-VALIDATED accuracy when available: in-sample accuracy
    # flatters small samples (the fit saw every row it is graded on), while the
    # default weights' in-sample accuracy IS out-of-sample (no fitting).
    acc_cv = learned.get("accuracy_cv")
    acc_in = learned.get("accuracy")
    acc = acc_cv if acc_cv is not None else acc_in
    acc_def = learned.get("accuracy_default")
    if acc is not None and acc_def is not None and acc <= acc_def:
        label = "accuracy validada (CV)" if acc_cv is not None else "accuracy"
        return {"ok": False, "error": (
            f"rechazado: {label} {acc:.2f} <= default {acc_def:.2f}. "
            f"Los pesos aprendidos hoy no superan a los default fuera de muestra; "
            f"revisa el feedback antes de aplicar.")}
    global _ACTIVE_WEIGHTS
    save_weights(DB_PATH, learned["weights"])
    _ACTIVE_WEIGHTS = load_weights(DB_PATH)
    return {"ok": True, "weights": _ACTIVE_WEIGHTS,
            "accuracy": acc_in, "accuracy_cv": acc_cv}


def reset_weights(conn) -> dict:
    global _ACTIVE_WEIGHTS
    p = Path(DB_PATH).parent / "hef_weights.json"
    if p.exists():
        p.unlink()
    _ACTIVE_WEIGHTS = None
    return {"ok": True, "weights": DEFAULT_WEIGHTS}


def compute_stats(conn) -> dict:
    # by_cohort stays global (drives the cohort filter buttons); every other metric
    # is about the USER's play, so reference cohorts are excluded.
    by_cohort = {r["our_cohort"]: r["c"] for r in conn.execute(
        "SELECT our_cohort, COUNT(*) c FROM matches GROUP BY our_cohort")}
    own = _own_cohort_sql("m.our_cohort") + " AND d.context_json NOT LIKE '%\"positive_trade\": true%'"
    dec_total = conn.execute(
        f"SELECT COUNT(*) c FROM decisions d JOIN matches m ON m.match_id=d.match_id WHERE {own}"
    ).fetchone()["c"]
    ev_t, ev_o = [], []
    for r in conn.execute(
            f"SELECT d.options_json FROM decisions d JOIN matches m ON m.match_id=d.match_id WHERE {own}"):
        t, o = ev_pair(json.loads(r["options_json"]))
        if t is not None:
            ev_t.append(t)
        if o is not None:
            ev_o.append(o)
    clips_dec = conn.execute(
        f"SELECT COUNT(*) c FROM decisions d JOIN matches m ON m.match_id=d.match_id "
        f"WHERE d.clip_path IS NOT NULL AND d.clip_path<>'' AND {own}").fetchone()["c"]
    clips_matches = conn.execute(
        f"SELECT COUNT(DISTINCT d.match_id) c FROM decisions d JOIN matches m ON m.match_id=d.match_id "
        f"WHERE d.clip_path IS NOT NULL AND d.clip_path<>'' AND {own}").fetchone()["c"]
    fb = conn.execute(
        f"SELECT COUNT(*) c FROM decisions d JOIN matches m ON m.match_id=d.match_id "
        f"WHERE d.user_feedback IS NOT NULL AND {own}").fetchone()["c"]
    avg_t = round(sum(ev_t) / len(ev_t), 2) if ev_t else 0
    avg_o = round(sum(ev_o) / len(ev_o), 2) if ev_o else 0
    return {
        "matches": sum(v for k, v in by_cohort.items() if k not in REFERENCE_COHORTS),
        "by_cohort": by_cohort,
        "decisions": dec_total,
        "detectors": len(DETECTORS),
        "ev_taken": avg_t,
        "ev_optimal": avg_o,
        "ev_gap": round(avg_o - avg_t, 2),
        "clips_decisions": clips_dec,
        "clips_matches": clips_matches,
        "feedback": fb,
        "patch": game_data.patch_version(),
    }


# Patterns are computed on the fly: one predicate per detector over context_json.
PATTERN_DEFS = [
    {
        "id": "low_hp_entry", "detector": "trade_v1",
        "title": "Trade cerrado bajo 45% HP",
        # trade v2 context: our_hp_pct_end (v1's entry.hp_pct no longer exists,
        # so the old predicate could never fire again).
        "pred": lambda c: isinstance(c.get("our_hp_pct_end"), (int, float)) and c["our_hp_pct_end"] < 0.45,
        "desc": "Cerraste el intercambio con HP por debajo del umbral crítico (45%).",
    },
    {
        "id": "unseen_death", "detector": "death_v1",
        "title": "Muerte con enemigos sin visibilidad",
        "pred": lambda c: len(c.get("non_combatant_enemies_unseen") or []) > 0,
        "desc": "Moriste con uno o más enemigos sin aparecer en el mapa: información sin resolver.",
    },
    {
        "id": "obj_out_of_pos", "detector": "objective_readiness_v1",
        "title": "Llegada a objetivo fuera de estado",
        # hp_pct may be None when there's no Live Client session (e.g. Ascent-only
        # matches); guard with isinstance so None never reaches the comparison.
        "pred": lambda c: bool(c.get("too_far")) or (
            isinstance((c.get("your_state_at_kill") or {}).get("hp_pct"), (int, float))
            and c["your_state_at_kill"]["hp_pct"] < 0.5
        ),
        "desc": "Llegaste al objetivo demasiado lejos del pit o con HP por debajo del 50%.",
    },
    {
        "id": "vision_deficit", "detector": "objective_readiness_v1",
        "title": "Déficit de visión en objetivo mayor",
        "pred": lambda c: isinstance(c.get("vision_diff"), (int, float)) and c["vision_diff"] < 0,
        "desc": "El enemigo tenía más visión que tu equipo en la ventana previa al objetivo.",
    },
    {
        "id": "absent_reachable", "detector": "tempo_v1",
        "title": "Ausente en pelea alcanzable",
        "pred": lambda c: bool(c.get("could_arrive_in_time")),
        "desc": "Hubo una pelea a la que podías llegar a tiempo y no participaste.",
    },
]


def _trend(by_match_hit: dict, match_dates: dict) -> str:
    items = [(match_dates[m], hit) for m, hit in by_match_hit.items() if m in match_dates]
    if len(items) < 4:
        return "stable"
    items.sort()
    half = len(items) // 2
    early, late = items[:half], items[half:]
    er = sum(1 for _, h in early if h) / max(1, len(early))
    lr = sum(1 for _, h in late if h) / max(1, len(late))
    if lr > er * 1.2 and lr - er > 0.1:
        return "worsening"
    if lr < er * 0.8 and er - lr > 0.1:
        return "improving"
    return "stable"


def compute_patterns(conn, cohort) -> list:
    cf = _cohort_filter(cohort)
    # No explicit cohort = the user's own accounts only; reference players (the
    # challenger) must not contaminate "ocurrió en X de Y partidas".
    mwhere = " WHERE our_cohort = ?" if cf else " WHERE " + _own_cohort_sql("our_cohort")
    margs = (cf,) if cf else ()
    match_dates = {r["match_id"]: r["game_start_ms"]
                   for r in conn.execute("SELECT match_id, game_start_ms FROM matches" + mwhere, margs)}
    total_matches = len(match_dates)
    results = []
    for pdef in PATTERN_DEFS:
        sql = ("SELECT d.match_id, d.context_json, d.options_json, m.game_start_ms "
               "FROM decisions d JOIN matches m ON m.match_id = d.match_id "
               "WHERE d.detector_id = ?" + (" AND m.our_cohort = ?" if cf else ""))
        args = (pdef["detector"], cf) if cf else (pdef["detector"],)
        occ, gaps, last_ms = 0, [], 0
        by_match_hit: dict = {}
        for r in conn.execute(sql, args):
            c = json.loads(r["context_json"])
            hit = bool(pdef["pred"](c))
            by_match_hit[r["match_id"]] = by_match_hit.get(r["match_id"], False) or hit
            if hit:
                occ += 1
                t, o = ev_pair(json.loads(r["options_json"]))
                if t is not None and o is not None:
                    gaps.append(o - t)
                last_ms = max(last_ms, r["game_start_ms"] or 0)
        if occ == 0:
            continue
        n_matches = sum(1 for v in by_match_hit.values() if v)
        results.append({
            "id": pdef["id"],
            "detector": pdef["detector"],
            "title": pdef["title"],
            "occ": occ,
            "matches": n_matches,
            "total": total_matches,
            "ev_gap": round(sum(gaps) / len(gaps), 2) if gaps else 0,
            "trend": _trend(by_match_hit, match_dates),
            "last": fmt_date(last_ms) if last_ms else "—",
            "desc": f"{pdef['desc']} Ocurrió en {n_matches} de {total_matches} partidas.",
        })
    results.sort(key=lambda p: p["occ"], reverse=True)
    return results


def _overextension_per_match(conn, cohort) -> list:
    """Per own match, chronological: deaths analyzed by death_v1 and how many
    were overextension-with-lead (#10). Matches with zero deaths count as games
    (excluding them would bias the rate upward). The criterion (classes + power
    threshold) lives in lol_coach.progress — single source of truth."""
    cf = _cohort_filter(cohort)
    if cf in REFERENCE_COHORTS:
        cf = None   # progress is about the player, never the reference cohort
    mwhere = " WHERE our_cohort = ?" if cf else " WHERE " + _own_cohort_sql("our_cohort")
    margs = (cf,) if cf else ()
    out, idx = [], {}
    for r in conn.execute(
            "SELECT match_id, game_start_ms FROM matches" + mwhere +
            " ORDER BY game_start_ms", margs):
        idx[r["match_id"]] = len(out)
        out.append({"match_id": r["match_id"], "date": fmt_date(r["game_start_ms"]),
                    "deaths": 0, "incidents": 0})
    dwhere = " AND m.our_cohort = ?" if cf else " AND " + _own_cohort_sql("m.our_cohort")
    dargs = ("death_v1", cf) if cf else ("death_v1",)
    for r in conn.execute(
            "SELECT d.match_id, d.context_json FROM decisions d "
            "JOIN matches m ON m.match_id = d.match_id "
            "WHERE d.detector_id = ?" + dwhere, dargs):
        i = idx.get(r["match_id"])
        if i is None:
            continue
        ctx = json.loads(r["context_json"])
        out[i]["deaths"] += 1
        pi = ((ctx.get("state_features") or {}).get("power") or {}).get("power_index")
        if (ctx.get("inferred_action_class") in progress.OVEREXT_CLASSES
                and isinstance(pi, (int, float)) and pi > progress.LEAD_POWER_INDEX):
            out[i]["incidents"] += 1
    return out


def compute_progress(conn, cohort) -> dict | None:
    """The #10 progress block: rolling series with Wilson CIs, opportunity
    density, provisional goal and block verdict. None while the history is
    shorter than one window (the UI simply omits the card)."""
    pm = _overextension_per_match(conn, cohort)
    pairs = [(m["deaths"], m["incidents"]) for m in pm]
    series = progress.rolling_series(pairs)
    if not series:
        return None
    for pt in series:   # label each window with the date of its last match
        pt["date"] = pm[pt["end"] - 1]["date"]
    return {
        "window": progress.WINDOW,
        "goal_prop": progress.GOAL_PROP,
        "block": progress.BLOCK,
        "classes": list(progress.OVEREXT_CLASSES),
        "lead_power_index": progress.LEAD_POWER_INDEX,
        "note": "denominador = muertes analizadas (provisional hasta #17 exposure)",
        "games": len(pm),
        "series": series,
        "latest": series[-1],
        "blocks": progress.block_comparison(pairs),
    }


def _session_candidates(conn) -> list:
    """UNMARKED decisions from own cohorts as flat dicts for session.build_session.
    Same gates as the decision list: hidden positive trades out, reference out."""
    out = []
    for r in conn.execute(
            "SELECT d.id, d.match_id, d.detector_id, d.game_time_ms, d.moment, "
            "d.context_json, d.options_json, m.game_start_ms, m.our_cohort, "
            "(SELECT p.champion_name FROM participants p WHERE p.match_id = d.match_id "
            " AND p.puuid = m.our_puuid) AS champion "
            "FROM decisions d JOIN matches m ON m.match_id = d.match_id "
            "WHERE d.user_feedback IS NULL AND d.user_best_option IS NULL "
            "AND d.user_mark_type IS NULL "
            "AND d.context_json NOT LIKE ? AND " + _own_cohort_sql("m.our_cohort"),
            (_HIDDEN_POSITIVE,)):
        ctx = json.loads(r["context_json"])
        opts = json.loads(r["options_json"])
        evs = [o.get("ev_score") for o in opts if isinstance(o.get("ev_score"), (int, float))]
        ti = taken_index(opts)
        ev_taken = opts[ti].get("ev_score") if ti is not None else None
        ev_optimal = max(evs) if evs else None
        out.append({
            "id": r["id"], "match": r["match_id"], "cohort": r["our_cohort"],
            "detector": r["detector_id"], "game_time_ms": r["game_time_ms"],
            "game_start_ms": r["game_start_ms"] or 0,
            "date": fmt_date(r["game_start_ms"]),
            "time": ctx.get("time_mmss") or mmss(r["game_time_ms"]),
            "champion": r["champion"] or "—",
            "moment": r["moment"] or "",
            "ev_taken": ev_taken, "ev_optimal": ev_optimal,
            "ev_min": min(evs) if evs else None,
            "ev_gap": (round(ev_optimal - ev_taken, 2)
                       if ev_taken is not None and ev_optimal is not None else None),
            "action_class": ctx.get("inferred_action_class"),
            "power_index": ((ctx.get("state_features") or {}).get("power") or {}).get("power_index"),
        })
    return out


def query_session(conn, exclude=()) -> dict:
    """The 'Sesion de hoy' payload (#16): ~6 unmarked moments in the R4 mix plus
    the temporal clustering note computed over ALL dominant-pattern incidents
    (marked or not — the note describes the habit, not the queue).

    `exclude` = decision ids already on screen: 'Traer mas' appends the NEXT
    batch without repeating moments the player skipped but can still see."""
    cands = [c for c in _session_candidates(conn) if c["id"] not in exclude]
    moments = session.build_session(cands)
    minutes = []
    for r in conn.execute(
            "SELECT d.game_time_ms, d.context_json FROM decisions d "
            "JOIN matches m ON m.match_id = d.match_id "
            "WHERE d.detector_id = 'death_v1' AND " + _own_cohort_sql("m.our_cohort") +
            " ORDER BY m.game_start_ms, d.game_time_ms"):
        ctx = json.loads(r["context_json"])
        pi = ((ctx.get("state_features") or {}).get("power") or {}).get("power_index")
        if (ctx.get("inferred_action_class") in progress.OVEREXT_CLASSES
                and isinstance(pi, (int, float)) and pi > progress.LEAD_POWER_INDEX):
            minutes.append(r["game_time_ms"] // 60_000)
    counts: dict = {}
    for c in cands:
        b = session.classify(c)
        counts[b] = counts.get(b, 0) + 1
    return {
        "size": session.SESSION_SIZE,
        "moments": moments,
        "temporal": session.temporal_note(minutes),
        "candidates_by_bucket": counts,
    }


def _trade_marked_bucket_counts(conn) -> dict:
    """Coverage seed for the trade queue: buckets already covered by marks on
    lost trades, so the ranking steers scarce new marks to under-sampled ones."""
    counts: dict = {}
    for r in conn.execute(
            "SELECT d.id, d.game_time_ms, d.context_json, "
            "(SELECT p.champion_name FROM participants p WHERE p.match_id = d.match_id "
            " AND p.puuid = m.our_puuid) AS champion "
            "FROM decisions d JOIN matches m ON m.match_id = d.match_id "
            "WHERE d.detector_id = 'trade_v1' AND d.context_json NOT LIKE ? "
            "AND (d.user_feedback IS NOT NULL OR d.user_best_option IS NOT NULL) "
            "AND " + _own_cohort_sql("m.our_cohort"),
            (_HIDDEN_POSITIVE,)):
        item = trade_queue.item_from_context(
            r["id"], r["game_time_ms"], json.loads(r["context_json"]), champion=r["champion"])
        b = trade_queue.bucket_of(item)
        counts[b] = counts.get(b, 0) + 1
    return counts


def query_trade_queue(conn, exclude=()) -> dict:
    """Campana #5: cola de trades PERDIDOS sin marcar, enriquecidos igual que
    /api/decisions y ORDENADOS por informatividad (trade_queue: Fisher x
    cobertura). Cada item lleva `rank_why` (Fisher + bucket) para declarar por
    que esta arriba. `exclude` = ids ya en pantalla (para 'traer mas')."""
    weights = _ACTIVE_WEIGHTS or trade_queue.DEFAULT_WEIGHTS
    marked = _trade_marked_bucket_counts(conn)
    rows = conn.execute(
        SQL_DEC.format(where=(
            "WHERE d.detector_id = 'trade_v1' "
            "AND d.context_json NOT LIKE ? "
            "AND d.user_feedback IS NULL AND d.user_best_option IS NULL "
            "AND " + _own_cohort_sql("m.our_cohort"))),
        (_HIDDEN_POSITIVE,)).fetchall()
    nmap = _notes_map(conn)
    items, enriched = [], {}
    for r in rows:
        if r["id"] in exclude:
            continue
        ctx = json.loads(r["context_json"])
        items.append(
            trade_queue.item_from_context(r["id"], r["game_time_ms"], ctx, champion=r["champion"]))
        enriched[r["id"]] = enrich(r, nmap)
    moments = []
    for it in trade_queue.rank(items, weights, marked):
        d = enriched[it["id"]]
        phase, gold, champ = it["bucket"]
        d["rank_why"] = f"Fisher {it['fisher']:.3f} · {phase} / {gold} / {champ}"
        moments.append(d)
    return {"moments": moments, "total": len(moments)}


# El escaneo de exposicion recorre ~4600 frames y llama build_state_features en
# cada uno: es lo mas caro del dashboard. Depende solo de la DB de partidas (NO
# del feedback: /api/feedback, /api/preference y /api/weights/* no la tocan), asi
# que en vez de limpiarse en cada POST lleva un token de frescura barato (COUNT +
# MAX(analyzed_at_utc) sobre matches): una partida nueva ingerida/analizada cambia
# el token y refresca sola; una marca de feedback no. Cache structure: {cohort: (token, result)}.
# El token nuevo sobrescribe la entrada anterior del cohorte, manteniendo el dict acotado.
_EXPOSURE_CACHE: dict = {}
_EXPOSURE_CACHE_LOCK = threading.Lock()
EXPOSURE_MOMENTS_LIMIT = 40   # lista revisable: muestra suficiente para juzgar FP


def _exposure_freshness_token(conn) -> tuple:
    r = conn.execute("SELECT COUNT(*) AS n, MAX(analyzed_at_utc) AS m FROM matches").fetchone()
    return (r["n"], r["m"])


def _exposure_cached(conn, cohort) -> dict:
    token = _exposure_freshness_token(conn)
    with _EXPOSURE_CACHE_LOCK:
        entry = _EXPOSURE_CACHE.get(cohort)
    if entry is not None:
        cached_token, cached_result = entry
        if cached_token == token:
            return cached_result
    res = query_exposure(conn, cohort)
    with _EXPOSURE_CACHE_LOCK:
        _EXPOSURE_CACHE[cohort] = (token, res)
    return res


def query_exposure(conn, cohort=None) -> dict:
    """#17 Fase 1 (REPORT-ONLY): exposiciones sin muerte, agregadas + lista
    revisable para medir falsos positivos. No persiste nada ni pide feedback."""
    cf = _cohort_filter(cohort)
    where = "WHERE analyzed_at_utc IS NOT NULL AND " + _own_cohort_sql("our_cohort")
    args: list = []
    if cf and cf not in REFERENCE_COHORTS:
        where += " AND our_cohort = ?"
        args.append(cf)
    rows = conn.execute(
        f"SELECT match_id, game_start_ms, our_cohort FROM matches {where}", args).fetchall()
    meta = {r["match_id"]: r for r in rows}

    # Segmenta POR MATCH (los episodios son frames consecutivos dentro de una
    # partida) y agrega/lista sobre EPISODIOS, no frames: contar frames infla el
    # N por autocorrelacion y falsea cualquier cota de FP (scout 2026-07-19).
    episodes = []
    for mid in meta:
        episodes.extend(exposure.segment_episodes(exposure.scan_match(conn, mid)))

    agg = exposure.aggregate(episodes, n_matches=len(meta))
    top = sorted(episodes, key=lambda e: e.peak_severity, reverse=True)
    moments = []
    for e in top[:EXPOSURE_MOMENTS_LIMIT]:
        m = meta.get(e.match_id)
        clip_path, offset_s = find_clip_for_event(conn, e.match_id, e.start_ms)
        dur = int((e.end_ms - e.start_ms) / 1000)
        moments.append({
            "match": e.match_id,
            "cohort": m["our_cohort"] if m else None,
            "date": fmt_date(m["game_start_ms"]) if m else "?",
            "time": mmss(e.start_ms),
            "game_time_ms": e.start_ms,   # el salto a Decisiones busca este momento en el replay
            "duration_s": dur,            # 0 = episodio de un solo frame
            "n_frames": e.n_frames,
            "champion": e.champion or "?",
            "phase": exposure.phase_of(e.start_ms),
            "gold_band": e.gold_band,
            "safety_ratio": e.worst_safety_ratio,
            "enemies_unseen": e.max_enemies_unseen,
            "severity": e.peak_severity,
            "died": e.died,
            "has_clip": bool(clip_path),
            "clip_offset_s": offset_s,
        })
    return {"aggregate": agg, "moments": moments, "shown": len(moments)}


# --- Anchor set de exposicion (#17): la clave de respuestas -----------------
# Las muertes que el jugador marco en desacuerdo ya traen su nota diciendo si el
# error real fue la posicion/vision ANTES de morir. Etiquetarlas da el ground
# truth contra el que medir exposure_v1 (recall y separabilidad).
# A CIEGAS a proposito: este endpoint NO dice si exposure_v1 disparo en cada
# muerte. Mostrarlo anclaria el juicio en la respuesta del sistema (sesgo de
# confirmacion, el modo de fallo clasico de validar un detector contra su autor).
ANCHOR_LABELS = ("yes", "no", "unsure")


def query_anchor_set(conn) -> dict:
    """Muertes propias marcadas en desacuerdo, ENRIQUECIDAS (para ver el clip y
    el contexto al juzgar) + la nota del jugador y su etiqueta actual."""
    labels = {r["decision_key"]: r["label"]
              for r in conn.execute("SELECT decision_key, label FROM exposure_anchors")}
    nmap = _notes_map(conn)
    where = ("WHERE d.detector_id = 'death_v1' AND d.user_feedback = 'disagree' "
             "AND d.user_feedback_note IS NOT NULL AND TRIM(d.user_feedback_note) <> '' "
             "AND " + _own_cohort_sql("m.our_cohort"))
    items = []
    for r in conn.execute(SQL_DEC.format(where=where)):
        d = enrich(r, nmap)
        key = f"{r['match_id']}|{r['detector_id']}|{r['game_time_ms']}"
        d["anchor_key"] = key
        d["anchor_label"] = labels.get(key)
        items.append(d)
    n_marked = sum(1 for d in items if d["anchor_label"])
    return {"moments": items, "total": len(items), "n_marked": n_marked}


def set_anchor(conn, decision_id: int, label) -> dict:
    """Guarda (o borra, con label=None) la etiqueta del anchor set. Se guarda por
    clave ESTABLE, asi que sobrevive a un re-analisis."""
    key = _decision_key(conn, decision_id)
    if not key:
        return {"error": "decision no encontrada"}
    if label is None:
        conn.execute("DELETE FROM exposure_anchors WHERE decision_key = ?", (key,))
    elif label in ANCHOR_LABELS:
        conn.execute(
            "INSERT INTO exposure_anchors (decision_key, label, marked_at_utc) "
            "VALUES (?,?,?) ON CONFLICT(decision_key) DO UPDATE SET "
            "label=excluded.label, marked_at_utc=excluded.marked_at_utc",
            (key, label, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    else:
        return {"error": f"label invalida: {label}"}
    conn.commit()
    return {"ok": True, "key": key, "label": label}


def set_feedback(conn, decision_id: int, payload: dict) -> dict:
    """Partial update of a decision's feedback. Only fields present in the
    payload are written, so saving a written note / chosen option does not clear
    the agree/disagree flag and vice-versa. The note is saved even with no
    agree/disagree (the player can write context without judging the system)."""
    sets, args = [], []
    if "feedback" in payload:
        fb = payload["feedback"]
        sets.append("user_feedback=?"); args.append(fb if fb in FEEDBACK_VALUES else None)
    if "note" in payload:
        note = (payload.get("note") or "").strip() or None
        sets.append("user_feedback_note=?"); args.append(note)
    if "best_option" in payload:
        bo = payload.get("best_option")
        sets.append("user_best_option=?"); args.append(int(bo) if isinstance(bo, (int, float)) else None)
        # Fingerprint of the option set being judged, stored WITH the mark so
        # the learner later validates it by content (options unchanged) instead
        # of by timestamp. Written/cleared only alongside best_option: a
        # note-only edit must NOT refresh an old mark (that loophole let
        # pre-redesign marks slip past the staleness gate).
        fp = None
        if isinstance(bo, (int, float)):
            row = conn.execute(
                "SELECT detector_id, context_json, options_json FROM decisions WHERE id=?",
                (decision_id,)).fetchone()
            if row:
                ctx = json.loads(row["context_json"])
                fp = action_fingerprint(
                    row["detector_id"], ctx.get("action"), json.loads(row["options_json"]))
        sets.append("user_feedback_fingerprint=?"); args.append(fp)
    if "mark_type" in payload:
        mt = payload.get("mark_type")
        sets.append("user_mark_type=?")
        args.append(mt if mt in feedback.MARK_TYPES else None)
    if not sets:
        return {"ok": False, "error": "nada que guardar"}
    sets.append("user_feedback_at_utc=?"); args.append(datetime.now(timezone.utc).isoformat())
    args.append(decision_id)
    conn.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()
    r = conn.execute(
        "SELECT user_feedback, user_feedback_note, user_best_option, user_mark_type "
        "FROM decisions WHERE id=?", (decision_id,)).fetchone()
    return {"ok": True, "id": decision_id, "feedback": r["user_feedback"],
            "note": r["user_feedback_note"], "best_option": r["user_best_option"],
            "mark_type": r["user_mark_type"]}


def clip_for(conn, decision_id: int):
    r = conn.execute("SELECT clip_path FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if not r or not r["clip_path"]:
        return None
    p = Path(r["clip_path"])
    return p if p.exists() else None


def replay_for(conn, match_id):
    """Full-match replay video + its in-match start offset (s). Prefers Ascent
    (records the whole game from 0:00) over Replays.lol fragments."""
    if not match_id:
        return None
    r = conn.execute(
        "SELECT path, in_match_start_s FROM clips WHERE match_id=? AND "
        "source IN ('ascent_full','replays_lol_full','Replays.lol') "
        "ORDER BY (source='ascent_full') DESC LIMIT 1",
        (match_id,),
    ).fetchone()
    if not r:
        return None
    p = Path(r["path"])
    if not p.exists():
        return None
    return p, float(r["in_match_start_s"] or 0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        try:
            body = HTML_PATH.read_bytes()
        except FileNotFoundError:
            self._json({"error": f"dashboard.html no encontrado en {HTML_PATH}"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, rel):
        """Sirve los PNG versionados de Data Dragon bajo scripts/assets/.

        Solo lectura y solo PNG. La ruta pedida se resuelve y se comprueba que
        siga dentro de ASSETS_ROOT: sin eso, un '..' en la URL leeria cualquier
        archivo del disco. Se rechaza tambien lo que resuelva a un enlace, para
        que un symlink dentro de assets/ no saque la lectura de la carpeta.
        """
        rel = unquote(rel)
        if not rel.endswith(".png") or "\\" in rel:
            self._json({"error": "activo no permitido"}, 404)
            return
        try:
            target = (ASSETS_ROOT / rel).resolve()
            target.relative_to(ASSETS_ROOT)
            if target.is_symlink() or not target.is_file():
                raise OSError
            body = target.read_bytes()
        except (OSError, ValueError):
            self._json({"error": "activo no encontrado"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        # Versionados por ruta (ddragon/<version>/...): cachear es seguro.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def _serve_clip(self, decision_id):
        conn = get_conn()
        try:
            path = clip_for(conn, decision_id)
        finally:
            conn.close()
        self._serve_video_file(path)

    def _serve_replay(self, match_id):
        conn = get_conn()
        try:
            rep = replay_for(conn, match_id)
        finally:
            conn.close()
        self._serve_video_file(rep[0] if rep else None)

    def _serve_video_file(self, path):
        if path is None:
            self._json({"error": "clip no disponible"}, 404)
            return
        fsize = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, fsize - 1
        partial = False
        if rng and rng.startswith("bytes="):
            partial = True
            try:
                s, e = rng[6:].split("-")
                start = int(s) if s else 0
                end = int(e) if e else fsize - 1
            except ValueError:
                start, end = 0, fsize - 1
        end = min(end, fsize - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", ctype)
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # browser seeked / closed the stream

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            self._serve_html()
            return
        if u.path in ("/arch", "/architecture"):
            try:
                body = (HERE.parent / "architecture.html").read_bytes()
            except FileNotFoundError:
                self._json({"error": "architecture.html no encontrado"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path.startswith("/assets/"):
            self._serve_asset(u.path[len("/assets/"):])
            return
        if u.path == "/clip":
            try:
                self._serve_clip(int(qs.get("id", ["0"])[0]))
            except (ValueError, OSError):
                self._json({"error": "clip inválido"}, 400)
            return
        if u.path == "/replay":
            try:
                self._serve_replay(qs.get("match", [None])[0])
            except OSError:
                self._json({"error": "replay inválido"}, 400)
            return
        if u.path.startswith("/api/"):
            conn = get_conn()
            try:
                if u.path == "/api/stats":
                    self._json(compute_stats(conn))
                elif u.path == "/api/matches":
                    self._json(query_matches(conn, qs.get("cohort", [None])[0]))
                elif u.path == "/api/match_map":
                    self._json(query_match_map(conn, qs.get("match", [None])[0]))
                elif u.path == "/api/match_replay":
                    rep = replay_for(conn, qs.get("match", [None])[0])
                    self._json({"available": rep is not None,
                                "in_match_start_s": rep[1] if rep else None})
                elif u.path == "/api/match_similarity":
                    self._json(query_match_similarity(conn, qs.get("match", [None])[0]))
                elif u.path == "/api/decisions":
                    self._json(_decisions_cached(conn, qs.get("cohort", [None])[0],
                                                 qs.get("detector", [None])[0], qs.get("match", [None])[0]))
                elif u.path == "/api/patterns":
                    ch = qs.get("cohort", [None])[0]
                    self._json({"patterns": compute_patterns(conn, ch),
                                "progress": compute_progress(conn, ch)})
                elif u.path == "/api/pair":
                    self._json(query_pair(conn, qs.get("detector", [None])[0]))
                elif u.path == "/api/weights":
                    self._json(query_weights(conn))
                elif u.path == "/api/player_model":
                    self._json(query_player_model(conn))
                elif u.path == "/api/review_queue":
                    self._json(query_review_queue(conn))
                elif u.path == "/api/ingest_status":
                    self._json(query_ingest_status(conn))
                elif u.path == "/api/session":
                    exc = qs.get("exclude", [""])[0]
                    exclude = {int(x) for x in exc.split(",") if x.strip().isdigit()}
                    self._json(query_session(conn, exclude))
                elif u.path == "/api/trade_queue":
                    exc = qs.get("exclude", [""])[0]
                    exclude = {int(x) for x in exc.split(",") if x.strip().isdigit()}
                    self._json(query_trade_queue(conn, exclude))
                elif u.path == "/api/trade_moment":
                    tid = qs.get("id", [None])[0]
                    self._json({"moment_ms": trade_moment_ms(conn, int(tid)) if tid and tid.isdigit() else None})
                elif u.path == "/api/exposure":
                    self._json(_exposure_cached(conn, qs.get("cohort", [None])[0]))
                elif u.path == "/api/anchor_set":
                    self._json(query_anchor_set(conn))
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:  # surface errors as JSON instead of a blank 500
                self._json({"error": str(exc)}, 500)
            finally:
                conn.close()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "payload inválido"}, 400)
            return
        conn = get_conn()
        try:
            if u.path == "/api/feedback":
                self._json(set_feedback(conn, int(payload["id"]), payload))
            elif u.path == "/api/preference":
                self._json(save_preference(conn, payload))
            elif u.path == "/api/anchor":
                self._json(set_anchor(conn, int(payload["id"]), payload.get("label")))
            elif u.path == "/api/weights/apply":
                self._json(apply_weights(conn))
            elif u.path == "/api/weights/reset":
                self._json(reset_weights(conn))
            else:
                self._json({"error": "not found"}, 404)
            _invalidate_decisions_cache()   # any write can change the decisions list
        except (KeyError, ValueError) as e:
            self._json({"error": f"payload inválido: {e}"}, 400)
        except Exception as e:  # surface errors as JSON
            self._json({"error": str(e)}, 500)
        finally:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="lol-coach dashboard")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not Path(DB_PATH).exists():
        print(f"DB no encontrada en {DB_PATH}. Corre el pipeline primero.")
        return 1
    _connect_schema(DB_PATH).close()  # ensure all tables exist + run migrations
    _bf = get_conn()
    try:
        filled = _backfill_pref_keys(_bf)
        if filled:
            print(f"blindadas {filled} preferencia(s) con clave estable")
    finally:
        _bf.close()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"lol-coach dashboard en {url}  (Ctrl+C para detener)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
