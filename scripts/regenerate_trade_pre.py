"""Mantenimiento: regeneracion selectiva del PRE historico de trade_v1 (SOLO COPIA).

Reescribe in-place `context_json`/`options_json` de una allowlist cerrada y
fijada por hash de decisiones trade_v1, sobre una copia de ensayo que la propia
herramienta crea. No existe comando para la DB real: esa habilitacion exige otra
autorizacion expresa.

    prepare        --source SNAPSHOT (--audit-results PATH | --no-audit-results)
                   --expected-keyset-sha256 HEX --game-data-dir DIR --output NEWDIR
    dry-run        --db TRIAL --bundle DIR --manifest-sha256 HEX --bundle-sha256 HEX
                   --trial-registry-sha256 HEX
    apply-copy     (mismos argumentos que dry-run)
    verify         (mismos argumentos que dry-run) --expect before|after
    rollback-copy  (mismos argumentos que dry-run)

Los tres hashes estan en approval.txt tras prepare. La politica solo-copia
protege contra ERRORES (ruta equivocada, alias, enlaces, sustitucion); no
contra alguien que falsifique a proposito un directorio de evidencia completo.

Salida: 0 exito / ya aplicado, 2 validacion abortada, 3 estado distinto al
esperado (o destino ocupado), 1 error inesperado.

Deliberadamente NO importa scripts/analyze.py, lol_coach.config ni lol_coach.db:
nada de config real, ni migraciones, ni conexiones que escriban por su cuenta.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import stat
import sys
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach import trade_pre_regeneration as trp  # noqa: E402

EXIT_OK, EXIT_UNEXPECTED, EXIT_VALIDATION, EXIT_STATE = 0, 1, 2, 3


# ------------------------------------------------------------------ network

@contextlib.contextmanager
def network_blocked():
    """The tool never needs the network; any attempt is a bug and must fail."""
    def refuse(*args, **kwargs):
        raise OSError("network access is blocked in regenerate_trade_pre")
    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = saved


# ---------------------------------------------------------- copy-only policy

def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(
        attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _load_registry(evidence_dir: Path) -> dict:
    path = evidence_dir / "trial-registry.json"
    if not path.is_file() or _is_link(path):
        raise trp.ValidationAbort("trial_not_registered", str(path))
    reg = trp.strict_loads(path.read_bytes().decode("utf-8"), "trial registry")
    if not isinstance(reg, dict):
        raise trp.ValidationAbort("trial_not_registered", "registry shape")
    return reg


def check_trial_identity(db: Path, bundle: trp.Bundle, registry_sha256: str) -> dict:
    """The destination must be exactly the trial prepare created next to this
    bundle, still the same physical file, with no alias of any kind.

    Compares the ABSOLUTE path text first, so a foreign path (e.g. the real DB)
    is rejected before anything touches that file.
    """
    evidence_dir = Path(os.path.abspath(bundle.dir)).parent
    expected = evidence_dir / "trial.sqlite"
    if os.path.normcase(os.path.abspath(db)) != os.path.normcase(str(expected)):
        raise trp.ValidationAbort("destination_not_registered_trial",
                                  "--db must be <evidenceDir>/trial.sqlite of this bundle")
    trp._check_hex(registry_sha256, "--trial-registry-sha256")
    reg_path = evidence_dir / "trial-registry.json"
    if not reg_path.is_file() or _is_link(reg_path):
        raise trp.ValidationAbort("trial_not_registered", str(reg_path))
    if trp.sha256_hex(reg_path.read_bytes()) != registry_sha256:
        raise trp.ValidationAbort("trial_registry_hash_mismatch")
    reg = _load_registry(evidence_dir)
    if (reg.get("manifest_sha256") != bundle.manifest_sha256
            or reg.get("bundle_sha256") != bundle.bundle_sha256):
        raise trp.ValidationAbort("trial_registered_for_other_bundle")
    p = Path(os.path.abspath(db))
    for node in [p, *p.parents]:
        if _is_link(node):
            raise trp.ValidationAbort("destination_alias_rejected", f"link or reparse point: {node}")
    if not p.is_file():
        raise trp.ValidationAbort("trial_missing", str(p))
    if os.path.normcase(os.path.realpath(p)) != reg.get("path"):
        raise trp.ValidationAbort("destination_alias_rejected", "resolved path differs from registry")
    _check_physical(p, reg)
    source = bundle.manifest["source_db"]["path"]
    if os.path.exists(source) and os.path.samefile(p, source):
        raise trp.ValidationAbort("destination_is_source")
    return reg


def _check_physical(p: Path, reg: dict) -> None:
    st = os.stat(p)
    if (st.st_dev, st.st_ino) != (reg.get("st_dev"), reg.get("st_ino")):
        raise trp.ValidationAbort("trial_substituted", "file identity differs from registry")
    if st.st_nlink != 1:
        raise trp.ValidationAbort("destination_alias_rejected", f"st_nlink={st.st_nlink}")


# ---------------------------------------------------------------- commands

def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _receipt(evidence_dir: Path, kind: str, body: dict) -> Path:
    receipts = evidence_dir / "receipts"
    receipts.mkdir(exist_ok=True)
    stamp = trp.utc_now().replace(":", "").replace("+", "Z").replace(".", "")
    path = receipts / f"{kind}-{stamp}-{os.getpid()}.json"
    write_receipt(path, trp.canonical_json({"kind": kind, "at_utc": trp.utc_now(), **body}))
    return path


def write_receipt(path: Path, data: bytes) -> None:
    trp.write_durable(path, data)


def cmd_prepare(args) -> int:
    if args.audit_results is None and not args.no_audit_results:
        raise trp.ValidationAbort("selection_route_required",
                                  "--audit-results PATH or --no-audit-results")
    res = trp.prepare(args.source, args.output,
                      expected_keyset_sha256=args.expected_keyset_sha256,
                      game_data_dir=args.game_data_dir, audit_results=args.audit_results)
    _emit({"result": "prepared", "evidence_dir": str(res.evidence_dir),
           "bundle": str(res.bundle_dir), "trial": str(res.trial_path), **res.summary})
    return EXIT_OK


def _open_target(args):
    bundle = trp.load_bundle(args.bundle, args.manifest_sha256, args.bundle_sha256)
    reg = check_trial_identity(Path(args.db), bundle, args.trial_registry_sha256)
    return bundle, reg, Path(os.path.abspath(args.bundle)).parent


def cmd_verify(args) -> int:
    """Verification only: classifies before/after/mixed and never re-applies.

    A crash inside a transaction leaves a hot rollback journal that a read-only
    connection cannot open; SQLite's own recovery (restoring the pre-transaction
    pages) runs first, only on the registered trial, and is reported."""
    bundle, reg, evidence_dir = _open_target(args)
    db = Path(os.path.abspath(args.db))
    recovered = trp.recover_hot_journal(db)
    conn = trp.connect_ro(db)
    try:
        state, details = trp.verify_state(conn, bundle)
    finally:
        conn.close()
    out = {"result": state, "expected": args.expect, "details": details,
           "hot_journal_recovered": recovered}
    if recovered:
        out["receipt"] = str(_receipt(evidence_dir, "verify-recovery", {
            "hot_journal_recovered": True, "state": state, "db": str(db),
            "manifest_sha256": bundle.manifest_sha256, "bundle_sha256": bundle.bundle_sha256}))
    _emit(out)
    return EXIT_OK if state == args.expect else EXIT_STATE


def cmd_dry_run(args) -> int:
    bundle, reg, evidence_dir = _open_target(args)
    if trp.hot_journal(Path(os.path.abspath(args.db))) is not None:
        # dry-run promises zero writes; journal recovery is a write. verify does it.
        raise trp.StateMismatch("hot_journal_present", "run verify first to recover the trial")
    before_mtime = os.stat(args.db).st_mtime_ns
    conn = trp.connect_ro(Path(args.db))
    try:
        state, details = trp.verify_state(conn, bundle)
        if state != "before":
            _emit({"result": "not_before", "state": state, "details": details})
            return EXIT_STATE
        expected_ids = {tuple(e["key"]): e["id"] for e in bundle.entries}
        plan = trp.compute_plan(conn, bundle.sources(), list(expected_ids),
                                expected_ids=expected_ids)
        excluded = trp.excluded_snapshot(conn, plan.entries)
    finally:
        conn.close()
    proposals = trp.canonical_json({"keyset_sha256": bundle.manifest["keyset_sha256"],
                                    "proposals": plan.entries})
    analysis = trp.canonical_json(plan.analysis)
    report = {
        "result": "dry_run",
        "target_state": state,
        "proposals_byte_equal": proposals == (bundle.dir / "proposals.json").read_bytes(),
        "analysis_byte_equal": analysis == (bundle.dir / "analysis.json").read_bytes(),
        "inventory_equal": trp.typed_equal(trp.json_normalize(plan.inventory),
                                           bundle.manifest["inventory"]),
        "excluded_equal": trp.typed_equal(trp.json_normalize(excluded),
                                          bundle.manifest["excluded"]),
        "summary": {k: v for k, v in plan.analysis.items() if k != "per_key"},
        "counts": excluded["counts"],
        "manifest_sha256": bundle.manifest_sha256, "bundle_sha256": bundle.bundle_sha256,
    }
    if os.stat(args.db).st_mtime_ns != before_mtime:
        raise trp.StateMismatch("dry_run_touched_target")
    reports = evidence_dir / "reports"
    reports.mkdir(exist_ok=True)
    trp.write_durable(reports / "dry-run.json", trp.canonical_json(report), exclusive=False)
    _emit(report)
    ok = all(report[k] for k in ("proposals_byte_equal", "analysis_byte_equal",
                                 "inventory_equal", "excluded_equal"))
    if not ok:
        raise trp.ValidationAbort("dry_run_differs_from_bundle", "", report)
    return EXIT_OK


def _write_command(args, engine, kind: str, expected_state: str) -> int:
    bundle, reg, evidence_dir = _open_target(args)
    trp.verify_prepared_receipt(evidence_dir, bundle)
    db = Path(os.path.abspath(args.db))
    recovered = trp.recover_hot_journal(db)
    conn = trp.connect_rw_existing(db)
    try:
        _check_physical(db, reg)                     # the file we just opened is still the trial
        result = engine(conn, bundle, pre_commit_check=lambda: _check_physical(db, reg))
    finally:
        conn.close()
    check = trp.connect_ro(db)
    try:
        state, details = trp.verify_state(check, bundle)
    finally:
        check.close()
    if state != expected_state:
        raise trp.StateMismatch("post_commit_state", f"found {state}", details)
    path = _receipt(evidence_dir, kind, {
        "result": result, "state": state, "manifest_sha256": bundle.manifest_sha256,
        "bundle_sha256": bundle.bundle_sha256, "db": str(db),
        "st_dev": reg["st_dev"], "st_ino": reg["st_ino"],
        "state_sha256": bundle.after_digest if state == "after" else bundle.before_digest,
        "keys": len(bundle.entries), "hot_journal_recovered": recovered,
    })
    _emit({"result": result, "state": state, "receipt": str(path),
           "hot_journal_recovered": recovered})
    return EXIT_OK


def cmd_apply_copy(args) -> int:
    return _write_command(args, trp.apply_batch, "apply", "after")


def cmd_rollback_copy(args) -> int:
    return _write_command(args, trp.rollback_batch, "rollback", "before")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="regenerate_trade_pre", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("prepare")
    pr.add_argument("--source", required=True)
    route = pr.add_mutually_exclusive_group()
    route.add_argument("--audit-results")
    route.add_argument("--no-audit-results", action="store_true")
    pr.add_argument("--expected-keyset-sha256", required=True)
    pr.add_argument("--game-data-dir", required=True)
    pr.add_argument("--output", required=True)
    pr.set_defaults(func=cmd_prepare)

    for name, func in (("dry-run", cmd_dry_run), ("apply-copy", cmd_apply_copy),
                       ("verify", cmd_verify), ("rollback-copy", cmd_rollback_copy)):
        c = sub.add_parser(name)
        c.add_argument("--db", required=True)
        c.add_argument("--bundle", required=True)
        c.add_argument("--manifest-sha256", required=True)
        c.add_argument("--bundle-sha256", required=True)
        c.add_argument("--trial-registry-sha256", required=True)
        if name == "verify":
            c.add_argument("--expect", required=True, choices=("before", "after"))
        c.set_defaults(func=func)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    with network_blocked():
        try:
            return args.func(args)
        except trp.StateMismatch as exc:
            _emit({"result": "state_mismatch", **exc.as_dict()})
            return EXIT_STATE
        except trp.ValidationAbort as exc:
            _emit({"result": "aborted", **exc.as_dict()})
            return EXIT_VALIDATION
        except Exception:
            traceback.print_exc()
            return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
