"""Automated Ascent ingestion — run by the LoLCoachIngest scheduled task.

Runs the sync_ascent pipeline for NEW Ascent matches only, appends a timestamped
record to data/ingest.log, and writes data/ingest_status.json so the dashboard
(and you) can see the last run at a glance — including a clear flag when the Riot
dev API key has expired, which is the usual reason ingestion silently does nothing.

Idempotent and cheap: if there is nothing new it does almost no work. Safe to run
on a timer. Designed to be invisible (launch with pythonw.exe).
"""
from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                 # project root
sys.path.insert(0, str(ROOT / "scripts"))     # sibling scripts (sync_ascent, etc.)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from lol_coach.config import load_config  # noqa: E402

DATA = ROOT / "data"
LOG = DATA / "ingest.log"
STATUS = DATA / "ingest_status.json"
LOG_MAX_BYTES = 512 * 1024  # keep the log small; trim oldest when it grows past this


def _key_alive(cfg) -> "bool | None":
    """True/False whether the Riot key can do what ingestion needs; None if
    undeterminable.

    OJO — esto fallo el 14-ago y hay que entender por que. La version anterior
    probaba match-v5 contra un `match_id` ya guardado, que es una llamada SIN
    PUUID: exactamente la unica capacidad que sobrevive a una rotacion de key.
    Devolvia 200 y escribia `key_alive: true` mientras la ingesta llevaba 9 dias
    sin traer nada, porque lo que se habia roto era la lectura POR PUUID (los
    PUUID guardados quedan indescifrables al rotar la key).

    Ahora se prueba `matches/by-puuid/.../ids`, que es la llamada de la que
    depende traer partidas nuevas. Se usa el PUUID CACHEADO a proposito: es el
    que la ingesta va a usar, asi que si esta caducado tiene que salir aqui.
    """
    riot = cfg.get("riot", {})
    key = riot.get("api_key")
    routing = riot.get("routing", "americas")
    if not key:
        return False
    puuid = None
    try:
        cache = json.loads(
            (DATA / "puuids.json").read_text(encoding="utf-8"))
        puuid = next(iter(cache.values()), None)
    except (OSError, ValueError, StopIteration):
        puuid = None
    if not puuid:
        return None      # sin PUUID cacheado no hay nada que probar todavia
    url = (f"https://{routing}.api.riotgames.com/lol/match/v5/matches/"
           f"by-puuid/{puuid}/ids?start=0&count=1")
    # Use requests, like the rest of the pipeline: the Riot API rejects the
    # default Python-urllib User-Agent with 403, so urllib gives false negatives.
    # Retry on 401/403 in case it's a transient throttle before declaring dead.
    for attempt in range(3):
        try:
            code = requests.get(url, headers={"X-Riot-Token": key}, timeout=10).status_code
        except Exception:
            return None
        # 400 = "Exception decrypting <puuid>": la key vale, pero el PUUID
        # cacheado es de otra epoca y la ingesta NO puede traer partidas. Antes
        # caia en el `not in (401, 403)` y se reportaba como viva; ese fue el
        # fallo del 14-ago. Cuenta como muerta porque, para lo que aqui importa,
        # lo esta: no entra ni una partida.
        if code in (401, 403, 400):
            if attempt < 2:
                time.sleep(2)
                continue
            return False
        return True  # 200 ok; 429/5xx -> key valida, solo throttled/caida
    return False


def _trim_log() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
            tail = LOG.read_text(encoding="utf-8", errors="replace")[-LOG_MAX_BYTES // 2:]
            LOG.write_text("...(recortado)...\n" + tail, encoding="utf-8")
    except Exception:
        pass


BACKUP_KEEP = 7  # daily backups retained


def _is_daily_backup(path) -> bool:
    """True solo para `lol_coach_YYYYMMDD.db` (la familia que rota por dia).

    Los `lol_coach_pre*.db` son puntos de control manuales escritos antes de
    trabajo riesgoso (migraciones, regeneraciones). Entraban a la ventana de
    rotacion porque el glob era `lol_coach_*.db` y ordenan despues de las
    fechas, asi que cada copia manual acortaba la historia diaria.
    """
    name = path.name
    stem, suffix = "lol_coach_", ".db"
    if not (name.startswith(stem) and name.endswith(suffix)):
        return False
    stamp = name[len(stem):-len(suffix)]
    return len(stamp) == 8 and stamp.isdigit()


def _daily_db_backup(cfg) -> str | None:
    """One consistent DB backup per day in data/backups/, keeping the last 7.

    Solo rotan los archivos DIARIOS (`lol_coach_YYYYMMDD.db`); los puntos de
    control manuales (`lol_coach_pre*.db`, `lol_coach.db.pre22-*.db`) no se
    tocan nunca.

    Uses sqlite's online backup API (WAL-safe), so it's valid even while the
    dashboard or an analysis is writing. Runs regardless of Riot key state —
    the user's feedback lives only in this DB. Returns the path written today,
    or None if today's backup already existed / on failure (logged by caller).
    """
    import sqlite3
    db_path = cfg["paths"]["sqlite_db"]
    bdir = DATA / "backups"
    bdir.mkdir(exist_ok=True)
    dest = bdir / f"lol_coach_{datetime.now():%Y%m%d}.db"
    if dest.exists():
        return None
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    daily = sorted(p for p in bdir.glob("lol_coach_*.db") if _is_daily_backup(p))
    for old in daily[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return str(dest)


def main() -> int:
    """Persist the run outcome; return 0 for the existing timer contract.

    Pipeline failures are reported by ``state``/``error`` in the status file.
    ``key_alive`` remains the independent precheck result, not a diagnosis of
    subsequent stage failures. Raw exception text is never copied to status.
    """
    DATA.mkdir(exist_ok=True)
    _trim_log()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = {"last_run": ts, "key_alive": None, "ingested": 0,
              "error": "ingestion_failed", "state": "failed", "errors": []}

    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n===== {ts} =====\n")
        buf = io.StringIO()
        stage = "config"
        try:
            cfg = load_config()
            stage = "precheck"
            alive = _key_alive(cfg)
            status["key_alive"] = alive
            try:
                bpath = _daily_db_backup(cfg)
                if bpath:
                    f.write(f"backup diario de la DB: {bpath}\n")
            except Exception as e:
                f.write(f"BACKUP FALLO: {e!r}\n")
            if alive is False:
                f.write("La comprobación previa de Riot falló; la ingesta no correrá. "
                        "Revisa la key y los PUUID cacheados.\n")
                status["error"] = "api_key_expired"  # legacy client compatibility
                status["errors"] = [{"stage": "precheck", "code": "riot_precheck_failed", "count": 1}]
            else:
                stage = "sync"
                import sync_ascent
                with redirect_stdout(buf):
                    result = sync_ascent.run_sync(all_flag=False)
                status.update(state=result["state"], ingested=result["ingested"],
                              errors=result["errors"])
                status["error"] = {"completed": None, "partial": "ingestion_partial",
                                   "failed": "ingestion_failed"}[result["state"]]
        except Exception as exc:  # never let a pipeline failure crash the timer
            status["state"] = "failed"
            status["error"] = "ingestion_failed"
            status["errors"].append({"stage": stage, "code": "stage_failed", "count": 1})
            f.write(f"ERROR en etapa {stage} ({type(exc).__name__}); consulta el estado de ingesta.\n")
        finally:
            f.write(buf.getvalue())

    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
