"""Re-analisis EN EL SITIO de un solo detector (#23 fases C y D).

`scripts/analyze.py` re-analiza TODOS los detectores de cada partida con borrar +
reinsertar (`persist_decisions`). Para cambiar el vocabulario de death u objective
eso tiene dos efectos que no se quieren:

- re-ejecuta trade_v1 con el codigo actual y aplicaria de paso el cambio de PRE de
  la regeneracion historica (840 decisiones, 54 marcas), que NO esta autorizado;
- cambia los ids de todas las decisiones y refresca `created_at_utc`, lo que dejaria
  caducadas por timestamp las marcas legacy sin fingerprint.

Esto hace lo minimo: re-ejecuta UN detector, exige que sus claves
`(match_id, detector_id, game_time_ms)` coincidan una a una con las guardadas, y
actualiza solo las columnas que el detector produce: moment, outcome, context_json,
options_json y argument. Ids, marcas (user_*), clips y created_at no se tocan. Las
claves de `context_json` que el detector no produce (el `reward`, que escribe
compute_reward despues) se conservan de la fila guardada.

La vigencia de las marcas la decide despues `features.mark_validity` como siempre:
si el fingerprint (action_ids + indice tomado) cambia, la marca pasa a historica;
el plan lo cuenta antes de escribir.

Solo death_v1 y objective_readiness_v1: trade_v1 tiene su propia herramienta
(`trade_pre_regeneration`) y tempo no esta en ninguna fase.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .decisions import death, objective_readiness
from .decisions.features import MARK_REVIVED, MARK_VALID, mark_validity

DETECTORS = {
    death.DETECTOR_ID: death.analyze_deaths,
    objective_readiness.DETECTOR_ID: objective_readiness.analyze_objective_readiness,
}
WRITABLE_COLUMNS = frozenset({"moment", "outcome", "context_json", "options_json", "argument"})
_VALID = (MARK_VALID, MARK_REVIVED)


class KeyMismatch(RuntimeError):
    """El detector no reemite exactamente las mismas claves: en el sitio no se puede."""


@dataclass
class Plan:
    detector_id: str
    matches: int = 0
    rows: int = 0
    changed: list = field(default_factory=list)     # (id, {col: new_value})
    carried_keys: Counter = field(default_factory=Counter)
    validity: Counter = field(default_factory=Counter)
    label_shift_valid: list = field(default_factory=list)  # marca vigente cuyo label elegido cambia
    mismatches: list = field(default_factory=list)
    rows_in_unanalyzed_matches: int = 0

    def summary(self) -> dict:
        return {
            "detector_id": self.detector_id, "matches": self.matches, "rows": self.rows,
            "rows_changed": len(self.changed),
            "columns_changed": dict(Counter(c for _, cols in self.changed for c in cols)),
            "context_keys_carried": dict(self.carried_keys),
            "mark_validity_transitions": {f"{a} -> {b}": n for (a, b), n in self.validity.items()},
            "valid_marks_whose_chosen_label_changes": len(self.label_shift_valid),
            "key_mismatches": self.mismatches[:10], "n_key_mismatches": len(self.mismatches),
            "rows_in_unanalyzed_matches_untouched": self.rows_in_unanalyzed_matches,
        }


def _dump(obj) -> str:
    # Misma serializacion que analysis.insert_decision: comparar texto con texto.
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build_plan(conn: sqlite3.Connection, detector_id: str) -> Plan:
    if detector_id not in DETECTORS:
        raise ValueError(f"detector no soportado en el sitio: {detector_id}")
    fn = DETECTORS[detector_id]
    plan = Plan(detector_id)
    stored: dict = {}
    for r in conn.execute("SELECT * FROM decisions WHERE detector_id = ?", (detector_id,)):
        stored.setdefault(r["match_id"], {})[r["game_time_ms"]] = r
    mids = [r["match_id"] for r in conn.execute(
        "SELECT match_id FROM matches WHERE analyzed_at_utc IS NOT NULL ORDER BY game_start_ms")]
    mid_set = set(mids)
    plan.rows_in_unanalyzed_matches = sum(
        len(v) for m, v in stored.items() if m not in mid_set)
    for mid in mids:
        old = stored.get(mid, {})
        new = {d.game_time_ms: d for d in fn(conn, mid)}
        if set(old) != set(new):
            plan.mismatches.append({"match_id": mid, "only_stored": sorted(set(old) - set(new)),
                                    "only_new": sorted(set(new) - set(old))})
            continue
        if not new:
            continue
        plan.matches += 1
        for t, d in new.items():
            r = old[t]
            plan.rows += 1
            octx = json.loads(r["context_json"] or "{}")
            ctx = dict(d.context)
            for k, v in octx.items():
                if k not in ctx:
                    ctx[k] = v
                    plan.carried_keys[k] += 1
            nopts = [o.__dict__ for o in d.options]
            values = {"moment": d.moment, "outcome": d.outcome, "context_json": _dump(ctx),
                      "options_json": _dump(nopts), "argument": d.argument}
            diff = {c: v for c, v in values.items() if r[c] != v}
            if diff:
                plan.changed.append((r["id"], diff))
            if r["user_best_option"] is None and not r["user_feedback"] \
                    and not r["user_feedback_note"] and not r["user_mark_type"]:
                continue
            oopts = json.loads(r["options_json"] or "[]")
            before = mark_validity(r["user_feedback_fingerprint"], detector_id, octx.get("action"),
                                   oopts, r["user_feedback_at_utc"], r["created_at_utc"])
            after = mark_validity(r["user_feedback_fingerprint"], detector_id, ctx.get("action"),
                                  nopts, r["user_feedback_at_utc"], r["created_at_utc"])
            plan.validity[(before, after)] += 1
            bo = r["user_best_option"]
            if (after in _VALID and isinstance(bo, int) and bo < len(oopts) and bo < len(nopts)
                    and oopts[bo].get("label") != nopts[bo].get("label")):
                plan.label_shift_valid.append(
                    {"id": r["id"], "old": oopts[bo].get("label"), "new": nopts[bo].get("label")})
    return plan


def _update_authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION,
                  sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if (action == sqlite3.SQLITE_UPDATE and dbname == "main" and arg1 == "decisions"
            and arg2 in WRITABLE_COLUMNS):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def apply_plan(conn: sqlite3.Connection, detector_id: str) -> Plan:
    """Planifica y escribe dentro de UNA transaccion BEGIN IMMEDIATE: nadie puede
    escribir entre la lectura y la escritura. Aborta sin escribir si alguna clave
    no casa. Devuelve el plan aplicado."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = build_plan(conn, detector_id)
        if plan.mismatches:
            raise KeyMismatch(f"{len(plan.mismatches)} partida(s) con claves distintas: "
                              f"{plan.mismatches[:3]}")
        conn.set_authorizer(_update_authorizer)
        try:
            for dec_id, cols in plan.changed:
                sets = ", ".join(f"{c} = ?" for c in cols)
                cur = conn.execute(
                    f"UPDATE decisions SET {sets} WHERE id = ? AND detector_id = ?",
                    (*cols.values(), dec_id, detector_id))
                if cur.rowcount != 1:
                    raise RuntimeError(f"UPDATE de {dec_id} afecto {cur.rowcount} filas")
        finally:
            conn.set_authorizer(None)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return plan


def backup(src_path: str, dst_path: str) -> None:
    """Copia consistente (API de backup de SQLite, incluye el WAL)."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = ["DETECTORS", "WRITABLE_COLUMNS", "KeyMismatch", "Plan", "build_plan",
           "apply_plan", "backup", "now_tag"]
