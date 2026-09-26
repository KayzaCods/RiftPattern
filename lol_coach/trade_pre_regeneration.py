"""Selective, in-place regeneration of the historical PRE of trade_v1 decisions.

Why this module exists: 827bd07 anchored the trade_v1 PRE at the real opening of
the measured damage block (contract `trade_block_open_v1`). Rows persisted
before it were evaluated one frame too early. The normal pipeline
(`persist_decisions`) cannot fix them: it DELETEs and reinserts every decision of
a match, which changes ids and detaches human feedback. This tool instead
rewrites ONLY `context_json`/`options_json` of an explicit, hash-pinned allowlist
of rows, in one transaction, and proves that everything else is logically
identical.

Layout (read side first, write side last, no hidden commits anywhere):

  * canonical encodings and typed logical hashing (pure);
  * logical snapshot of a SQLite view: schema, every table, reference vectors;
  * frozen sources: capture into the bundle (prepare) and bundle-only loaders;
  * inventory + historical PRE adapter + overlay -> proposals (read-only);
  * bundle writing (prepare) and loading/verification (every other command);
  * the transactional engine: `apply_batch`, `rollback_batch`, `verify_state`.

The engine takes a connection owned by the caller and never opens another one.
Which file it may be pointed at is the CLI's policy (copy-only for now), not the
engine's.

Threat model of the copy-only policy: it defends against MISTAKES -- a wrong
path, an alias, a hard/soft link or junction, a substituted or unregistered
file. The trial registry, receipts and approval file are plain JSON with plain
hashes; a local user who deliberately forges a whole evidence directory (bundle
copy + registry + receipt around another DB) is NOT stopped by it. There is no
cryptographic signing by design; the external hashes passed on the command line
(manifest, bundle, trial registry) are what an approver pins.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import game_data as _game_data
from .context import build_world_state
from .decisions import trade as _trade
from .decisions.features import (
    ACTION_ATTRITION,
    ACTION_DISENGAGE,
    ACTION_FIGHT_ENTRY,
    build_state_features,
)
from .snapshots import load_snapshots

# --------------------------------------------------------------------- constants

PROCEDURE_VERSION = "trade_pre_selective_regeneration_v1"
CONTRACT_SHA = "827bd0749171158122a332737c2f59bbec71facb"
PRE_CONTRACT = "trade_block_open_v1"
DETECTOR_ID = _trade.DETECTOR_ID
# The audited cut: digest of the pinned keyset. Defaults/documentation for the real rehearsal;
# synthetic tests pass their own keyset digest.
AUDITED_KEYSET_SHA256 = "3b032d787a8b3583199921bb9bc4f1647a781d2d7641416bd64ac960e3c16262"
AUDIT_RESULTS_SHA256 = "7a16f06ab97f92dc13451a800704ef253ee9572c4b90b94928bc90d9743bef9f"

USER_COLUMNS = (
    "user_feedback", "user_feedback_note", "user_feedback_at_utc",
    "user_best_option", "user_feedback_fingerprint", "user_mark_type",
)
WRITABLE_COLUMNS = ("context_json", "options_json")
ACTION_IDS = (ACTION_DISENGAGE, ACTION_FIGHT_ENTRY, ACTION_ATTRITION)
BUSY_TIMEOUT_MS = 2000

# Reference-shaped columns this tool knows how to resolve. Anything else that
# looks like a decision reference aborts: silently ignoring it could leave a
# human signal unprotected.
KNOWN_REFERENCE_COLUMNS = {
    ("preferences", "decision_a_id"): "id",
    ("preferences", "decision_b_id"): "id",
    ("preferences", "a_key"): "key",
    ("preferences", "b_key"): "key",
    ("exposure_anchors", "decision_key"): "key",
    ("vision_proxy_labels", "decision_key"): "key",
    ("vision_sample", "decision_key"): "key",
}
_REFERENCE_SHAPED = re.compile(r"(^|_)decision(_[a-z0-9]+)?_id$|_key$|^decision_key$")
_TRIPLE = ("match_id", "detector_id", "game_time_ms")
TRIPLE_REFERENCE_TABLES = ("orphan_feedback",)

SOURCE_TIMELINES = "sources/timelines"
SOURCE_SNAPSHOTS = "sources/snapshots"
SOURCE_GAME_DATA = "sources/game_data"
GAME_DATA_FILES = ("champion_stats_meraki.json", "champions_summary.json")
_SAFE_MATCH_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

IMPLEMENTATION_FILES = (
    "lol_coach/decisions/trade.py",
    "lol_coach/trade_pre_regeneration.py",
    "scripts/regenerate_trade_pre.py",
)
CLI_SCRIPT = "scripts/regenerate_trade_pre.py"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------ errors

class RegenerationError(Exception):
    """Base error. `code` is stable and machine-readable; `details` is JSON-able."""

    def __init__(self, code: str, message: str = "", details=None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationAbort(RegenerationError):
    """Inputs, bundle, selection or eligibility do not hold. Nothing was written."""


class StateMismatch(RegenerationError):
    """The target is not in the state the operation requires (or is busy)."""


# ------------------------------------------------------------ canonical encoding

def canonical_json(obj) -> bytes:
    """Canonical bytes for manifest, proposals, receipts and member lists."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def file_sha256(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def _check_key(key) -> tuple:
    if (not isinstance(key, (list, tuple)) or len(key) != 3
            or not isinstance(key[0], str) or not key[0]
            or key[1] != DETECTOR_ID
            or type(key[2]) is not int):
        raise ValidationAbort("invalid_key", f"not a trade_v1 key: {key!r}")
    return (key[0], key[1], key[2])


def keyset_digest(keys: Iterable) -> str:
    """Keyset encoding: list of K arrays, no duplicates, sorted by (match, detector,
    int time), compact JSON, UTF-8, SHA-256."""
    checked = [_check_key(k) for k in keys]
    if len(set(checked)) != len(checked):
        raise ValidationAbort("duplicate_key_in_keyset")
    ordered = [list(k) for k in sorted(checked)]
    data = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    return sha256_hex(data)


def key_string(key) -> str:
    """The stable key string reference tables use: "match|detector|time"."""
    return f"{key[0]}|{key[1]}|{key[2]}"


def _check_hex(value: str, what: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise ValidationAbort("invalid_hash_argument", f"{what} must be 64 lowercase hex chars")
    return value


# ------------------------------------------------------------------ strict JSON

def _reject_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate JSON key {k!r}")
        out[k] = v
    return out


def _reject_constant(name):
    raise ValueError(f"non-finite JSON constant {name}")


def strict_loads(text, what: str = "json"):
    """json.loads that rejects duplicate keys and NaN/Infinity/-Infinity."""
    if not isinstance(text, str):
        raise ValidationAbort("invalid_json", f"{what} is not text")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates,
                          parse_constant=_reject_constant)
    except ValueError as exc:
        raise ValidationAbort("invalid_json", f"{what}: {exc}") from None


def json_normalize(obj):
    """What a value becomes once persisted as JSON (the builders' outputs are
    compared against stored JSON through this, never through lenient ==)."""
    return json.loads(json.dumps(obj, allow_nan=False))


def typed_equal(a, b) -> bool:
    """Deep equality that distinguishes bool/int/float and dict/list types."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(typed_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(typed_equal(x, y) for x, y in zip(a, b))
    return a == b


def json_diff_paths(old, new, path=()) -> list[tuple]:
    """Every path whose value (or type) differs between two parsed JSON trees."""
    if type(old) is not type(new):
        return [path]
    if isinstance(old, dict):
        out = []
        for k in list(old.keys()) + [k for k in new.keys() if k not in old]:
            if k not in old or k not in new:
                out.append(path + (k,))
            else:
                out.extend(json_diff_paths(old[k], new[k], path + (k,)))
        return out
    if isinstance(old, list):
        if len(old) != len(new):
            return [path]
        out = []
        for i, (x, y) in enumerate(zip(old, new)):
            out.extend(json_diff_paths(x, y, path + (i,)))
        return out
    return [] if old == new else [path]


# ------------------------------------------------------- typed logical hashing

def value_tag(v) -> str:
    """Unambiguous tag per SQLite storage class, so 1, 1.0, '1' and b'1' never collide."""
    if v is None:
        return "N"
    if type(v) is int:
        return f"I:{v}"
    if type(v) is float:
        return "R:" + v.hex()
    if type(v) is str:
        return f"T:{len(v)}:{v}"
    if type(v) is bytes:
        return "B:" + v.hex()
    raise ValidationAbort("unsupported_sqlite_value", repr(type(v)))


def row_serialization(values) -> str:
    return json.dumps([value_tag(v) for v in values], ensure_ascii=False,
                      separators=(",", ":"))


def row_digest(values) -> str:
    return sha256_text(row_serialization(values))


def rows_digest(row_digests: Iterable[str]) -> tuple[int, str]:
    """Multiset digest: SHA-256 over the SORTED list of per-row digests (duplicates
    kept, never a set). Sorting fixed-size row digests instead of the full row
    serializations is equivalent for identity and keeps memory bounded on
    tables with ~10^6 rows."""
    ordered = sorted(row_digests)
    return len(ordered), sha256_hex(canonical_json(ordered))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def list_tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def table_columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})")]


def schema_description(conn) -> dict:
    master = sorted(
        [[value_tag(v) for v in r] for r in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master")])
    info = {t: [[value_tag(v) for v in r] for r in conn.execute(f"PRAGMA table_info({_q(t)})")]
            for t in list_tables(conn)}
    return {"sqlite_master": master, "table_info": info}


def schema_fingerprint(conn) -> str:
    return sha256_hex(canonical_json(schema_description(conn)))


def table_digest(conn, table: str) -> tuple[int, str]:
    return rows_digest(row_digest(tuple(r)) for r in conn.execute(f"SELECT * FROM {_q(table)}"))


def decision_rows(conn) -> tuple[list[str], list[tuple]]:
    cols = table_columns(conn, "decisions")
    rows = [tuple(r) for r in conn.execute("SELECT * FROM decisions")]
    return cols, rows


# ------------------------------------------------------------------ references

def reference_columns(conn) -> dict:
    """Introspect every reference-shaped column. Unknown ones abort."""
    found = {}
    unknown = []
    for t in list_tables(conn):
        cols = table_columns(conn, t)
        for c in cols:
            if t == "decisions":
                continue
            if _REFERENCE_SHAPED.search(c.lower()):
                kind = KNOWN_REFERENCE_COLUMNS.get((t, c))
                if kind is None:
                    unknown.append([t, c])
                else:
                    found[(t, c)] = kind
        if t != "decisions" and all(x in cols for x in _TRIPLE) and t not in TRIPLE_REFERENCE_TABLES:
            unknown.append([t, "+".join(_TRIPLE)])
    if unknown:
        raise ValidationAbort("unknown_reference_column",
                              "reference-shaped columns this tool cannot resolve",
                              sorted(unknown))
    return found


@dataclass
class ReferenceIndex:
    """Every human/label reference that could point at a decision, by id and key."""
    by_id: Counter
    by_key: Counter
    by_triple: Counter
    vectors: dict

    def refs_for(self, row_id: int, key) -> list[str]:
        out = []
        if self.by_id.get(row_id):
            out.append("id_reference")
        if self.by_key.get(key_string(key)):
            out.append("key_reference")
        if self.by_triple.get(tuple(key)):
            out.append("orphan_feedback_triple")
        return out


def build_reference_index(conn) -> ReferenceIndex:
    ref_cols = reference_columns(conn)
    ids = Counter()
    keys = Counter()
    for r in conn.execute("SELECT id, match_id, detector_id, game_time_ms FROM decisions"):
        ids[r[0]] += 1
        keys[f"{r[1]}|{r[2]}|{r[3]}"] += 1
    by_id, by_key, by_triple = Counter(), Counter(), Counter()
    vectors = {}
    tables = {t for t, _ in ref_cols}
    for t in sorted(tables):
        cols = [c for (tt, c) in ref_cols if tt == t]
        cols.sort()
        vec = []
        for r in conn.execute(f"SELECT rowid, {', '.join(_q(c) for c in cols)} FROM {_q(t)} "
                              "ORDER BY rowid"):
            entry = [value_tag(r[0])]
            for c, v in zip(cols, list(r)[1:]):
                if ref_cols[(t, c)] == "id":
                    if v is not None:
                        by_id[v] += 1
                    entry.append([c, value_tag(v), ids.get(v, 0) if v is not None else 0])
                else:
                    if v is not None:
                        by_key[v] += 1
                    entry.append([c, value_tag(v), keys.get(v, 0) if isinstance(v, str) else 0])
            vec.append(entry)
        vectors[t] = vec
    for t in TRIPLE_REFERENCE_TABLES:
        if t not in set(list_tables(conn)):
            continue
        vec = []
        for r in conn.execute(f"SELECT rowid, match_id, detector_id, game_time_ms FROM {_q(t)} "
                              "ORDER BY rowid"):
            triple = (r[1], r[2], r[3])
            by_triple[triple] += 1
            vec.append([value_tag(r[0]), keys.get(f"{r[1]}|{r[2]}|{r[3]}", 0)])
        vectors[t + ":triple"] = vec
    return ReferenceIndex(by_id, by_key, by_triple, vectors)


# ------------------------------------------------------------- logical state

def logical_state(conn) -> dict:
    """Schema, every table (multiset of typed rows) and reference vectors.

    sqlite_sequence and any table found by introspection are ordinary members.
    Physical pages do not matter; logical equality does.
    """
    tables = {}
    for t in list_tables(conn):
        n, h = table_digest(conn, t)
        tables[t] = [n, h]
    refs = build_reference_index(conn)
    return {
        "schema_sha256": schema_fingerprint(conn),
        "tables": tables,
        "references_sha256": sha256_hex(canonical_json(refs.vectors)),
    }


def state_digest(state: dict) -> str:
    return sha256_hex(canonical_json(state))


# ------------------------------------------------------------------ connections

def _file_uri(path: Path) -> str:
    return Path(os.path.abspath(path)).as_uri()


def connect_ro(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Read-only connection. `immutable` only for frozen bundle members."""
    uri = _file_uri(path) + "?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def connect_rw_existing(path: Path) -> sqlite3.Connection:
    """Write connection to an EXISTING file (mode=rw never creates one)."""
    uri = _file_uri(path) + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


# -------------------------------------------------------------- durable writes

def write_durable(path: Path, data: bytes, *, exclusive: bool = True) -> None:
    """tmp + fsync + os.replace. `exclusive` refuses to replace an existing file:
    evidence is never overwritten."""
    path = Path(path)
    if exclusive and path.exists():
        raise ValidationAbort("refuse_overwrite", str(path))
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if exclusive and path.exists():
        tmp.unlink()
        raise ValidationAbort("refuse_overwrite", str(path))
    os.replace(tmp, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =================================================================== sources

def _check_match_id(match_id) -> str:
    if not isinstance(match_id, str) or not _SAFE_MATCH_ID.match(match_id):
        raise ValidationAbort("unsafe_match_id", repr(match_id))
    return match_id


def _source_session_dir(conn, match_id):
    # Same query as the detector: first session row of the match.
    row = conn.execute("SELECT session_dir FROM sessions WHERE match_id = ?", (match_id,)).fetchone()
    return row["session_dir"] if row else None


def _project_snapshot_line(line: str) -> Optional[str]:
    """One snapshots.jsonl line -> the projection `_our_stats_at` needs, or None if
    load_snapshots would drop it. Aborts wherever load_snapshots or _our_stats_at
    could raise on the original: a projection must never hide a crash."""
    try:
        obj = json.loads(line)
    except ValueError:
        raise ValidationAbort("snapshot_unparseable",
                              "a snapshots.jsonl line would crash load_snapshots") from None
    if not isinstance(obj, dict):
        raise ValidationAbort("snapshot_unexpected_shape", "line is not an object")
    data = obj.get("data") or {}
    if not isinstance(data, dict):
        raise ValidationAbort("snapshot_unexpected_shape", "data is not an object")
    game = data.get("gameData") or {}
    if not isinstance(game, dict):
        raise ValidationAbort("snapshot_unexpected_shape", "gameData is not an object")
    gt = game.get("gameTime")
    if not isinstance(gt, (int, float)):          # the SAME test load_snapshots applies
        return None
    projected = {"data": {"gameData": {"gameTime": gt}}}
    player = data.get("activePlayer") or {}
    if not isinstance(player, dict):
        raise ValidationAbort("snapshot_unexpected_shape", "activePlayer is not an object")
    stats = player.get("championStats") or {}
    if not isinstance(stats, dict):
        raise ValidationAbort("snapshot_unexpected_shape", "championStats is not an object")
    kept = {k: stats[k] for k in ("currentHealth", "maxHealth", "resourceValue", "resourceMax")
            if k in stats}
    if kept:
        projected["data"]["activePlayer"] = {"championStats": kept}
    return json.dumps(projected, ensure_ascii=False)


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _source_plan(conn, match_ids, game_data_dir: Path) -> list:
    """Every original path the trade scan of `match_ids` can read, with the kind
    of capture it needs: [(section, name, path_or_None, extra)]."""
    plan = []
    for mid in sorted(set(match_ids)):
        _check_match_id(mid)
        row = conn.execute("SELECT timeline_json_path FROM matches WHERE match_id=?",
                           (mid,)).fetchone()
        stored = row["timeline_json_path"] if row else None
        plan.append(("timelines", mid, Path(stored) if stored else None, {"stored_path": stored}))
        session_dir = _source_session_dir(conn, mid)
        plan.append(("snapshots", mid,
                     Path(session_dir) / "snapshots.jsonl" if session_dir else None,
                     {"session_dir": session_dir}))
    for name in GAME_DATA_FILES:
        plan.append(("game_data", name, Path(game_data_dir) / name, {}))
    return plan


def _presence(path: Optional[Path]):
    if path is None:
        return None
    if path.is_file():
        return file_sha256(path)
    if path.exists():
        raise ValidationAbort("source_unexpected_type", str(path))
    return "absent"


def capture_sources(conn, match_ids: Iterable[str], bundle_dir: Path,
                    game_data_dir: Path) -> dict:
    """Copy every non-SQLite input of the trade scan into the bundle.

    The presence and hash of EVERY source is inventoried before anything is
    copied and verified again after everything is copied: a file that changes,
    disappears, or appears where it was absent invalidates the prepare. Loaders
    used afterwards read only the bundle, never these original paths again.
    """
    plan = _source_plan(conn, match_ids, game_data_dir)
    initial = [_presence(p) for _, _, p, _ in plan]
    out = {"timelines": {}, "snapshots": {}, "game_data": {"dir": os.path.abspath(game_data_dir),
                                                          "files": {}}}
    for (section, name, path, extra), seen in zip(plan, initial):
        entry = dict(extra)
        if path is not None:
            entry["resolved_path"] = os.path.abspath(path)
        if seen is None:
            entry["status"] = "no_path" if section == "timelines" else "no_session"
        elif seen == "absent":
            entry["status"] = "absent"
        elif section == "snapshots":
            lines = []
            with open(path, encoding="utf-8") as f:     # same text-mode read as load_snapshots
                for line in f:
                    p = _project_snapshot_line(line)
                    if p is not None:
                        lines.append(p)
            member = f"{SOURCE_SNAPSHOTS}/{name}/snapshots.jsonl"
            data = "".join(x + "\n" for x in lines).encode("utf-8")
            _write_new(bundle_dir / member, data)
            entry.update(status="projected", original_size=seen[0], original_sha256=seen[1],
                         member=member, projected_lines=len(lines),
                         projected_sha256=sha256_hex(data))
        else:
            data = path.read_bytes()
            if (len(data), sha256_hex(data)) != seen:
                raise ValidationAbort("source_changed_during_capture", str(path))
            member = (f"{SOURCE_TIMELINES}/{name}.json" if section == "timelines"
                      else f"{SOURCE_GAME_DATA}/{name}")
            _write_new(bundle_dir / member, data)
            entry.update(status="present", size=len(data), sha256=sha256_hex(data), member=member)
        if section == "game_data":
            out["game_data"]["files"][name] = entry
        else:
            out[section][name] = entry
    for (_, _, path, _), seen in zip(plan, initial):
        now = _presence(path)
        if now != seen:
            code = ("source_appeared_during_capture" if seen == "absent"
                    else "source_changed_during_capture")
            raise ValidationAbort(code, str(path))
    return out


def source_members(sources) -> list:
    """Every bundle member a source entry points at."""
    try:
        entries = (list(sources["timelines"].values()) + list(sources["snapshots"].values())
                   + list(sources["game_data"]["files"].values()))
    except (KeyError, TypeError, AttributeError):
        raise ValidationAbort("manifest_sources_invalid") from None
    out = []
    for e in entries:
        if not isinstance(e, dict):
            raise ValidationAbort("manifest_sources_invalid")
        if "member" in e:
            out.append(e["member"])
    return out


class BundleSources:
    """Loaders that answer the trade scan exclusively from the frozen bundle.

    A path the scan asks for that differs from the recorded one is an escape
    from the inventory and aborts; nothing falls back to the checkout or to the
    original files.
    """

    def __init__(self, bundle_dir: Path, sources: dict, members: Optional[set] = None):
        self.dir = Path(bundle_dir)
        self.sources = sources
        for m in source_members(sources):
            _check_member_path(m)
            if members is not None and m not in members:
                raise ValidationAbort("bundle_member_mismatch", f"source member not declared: {m}")

    def loaders_for(self, match_id: str):
        tl = self.sources["timelines"].get(match_id)
        sn = self.sources["snapshots"].get(match_id)
        if tl is None or sn is None:
            raise ValidationAbort("source_not_in_bundle", match_id)

        def timeline_loader(path_str):
            if path_str != tl["stored_path"]:
                raise ValidationAbort("unexpected_source_path", f"timeline {path_str!r}")
            if tl["status"] != "present":
                raise FileNotFoundError(path_str)   # what the detector saw at capture
            return json.loads((self.dir / tl["member"]).read_text(encoding="utf-8"))

        def snapshot_loader(session_dir):
            if sn["status"] == "no_session" or session_dir != sn["session_dir"]:
                raise ValidationAbort("unexpected_source_path", f"session {session_dir!r}")
            if sn["status"] == "absent":
                return []
            return load_snapshots(self.dir / Path(sn["member"]).parent)

        return timeline_loader, snapshot_loader

    @contextlib.contextmanager
    def game_data(self):
        """Point lol_coach.game_data at the bundle copy with empty caches, restoring
        the module afterwards. Absent files stay absent (the module then behaves as
        it did when they were absent at capture)."""
        gd = _game_data
        saved = (gd._GAME_DATA_DIR, gd._meraki, gd._items, gd._runes, gd._champ_summary)
        gd._GAME_DATA_DIR = self.dir / SOURCE_GAME_DATA
        gd._meraki = gd._items = gd._runes = gd._champ_summary = None
        try:
            yield gd.has_meaningful_resource
        finally:
            (gd._GAME_DATA_DIR, gd._meraki, gd._items, gd._runes, gd._champ_summary) = saved


# ======================================================== inventory and plan

_SPAN = re.compile(r"^min (\d+)-(\d+)$")


def _row_key(row) -> tuple:
    return (row["match_id"], row["detector_id"], row["game_time_ms"])


def _candidate_key(match_id, c) -> tuple:
    return (match_id, DETECTOR_ID, (c.m_start - 1) * 60_000)


def _descriptor(c) -> dict:
    return {"m_start": c.m_start, "m_end": c.m_end, "kind": c.kind,
            "d_us": int(c.d_us), "d_en": int(c.d_en), "d_us_raw": c.d_us, "d_en_raw": c.d_en,
            "hp_pct": c.stats["hp_pct"], "mana_pct": c.stats["mana_pct"]}


def _stored_descriptor_mismatches(ctx: dict, c) -> list[str]:
    bad = []
    m = _SPAN.match(ctx.get("minute_span") if isinstance(ctx.get("minute_span"), str) else "")
    if not m or (int(m.group(1)) + 1, int(m.group(2))) != (c.m_start, c.m_end):
        bad.append("minute_span")
    if not typed_equal(ctx.get("span_minutes"), c.m_end - (c.m_start - 1)):
        bad.append("span_minutes")
    if not typed_equal(ctx.get("positive_trade"), c.kind == "good"):
        bad.append("positive_trade")
    if not typed_equal(ctx.get("duo_damage_taken"), int(c.d_us)):
        bad.append("duo_damage_taken")
    if not typed_equal(ctx.get("enemy_duo_damage_taken"), int(c.d_en)):
        bad.append("enemy_duo_damage_taken")
    for field_name, stat_name in (("our_hp_pct_end", "hp_pct"), ("our_mana_pct_end", "mana_pct")):
        if field_name not in ctx or not typed_equal(ctx[field_name], json_normalize(c.stats[stat_name])):
            bad.append(field_name)
    return bad


@dataclass
class MatchInventory:
    inv: object                       # trade.TradeInventory
    candidates: dict                  # key -> TradeCandidate (verified unique)
    rows: dict                        # key -> sqlite Row (verified unique)


def inventory_match(conn, sources: BundleSources, match_id: str, has_resource) -> MatchInventory:
    """Scan one match with the detector's own collector and prove the persisted
    trade universe is exactly what it would emit: no row is regenerated from a
    candidate the detector would not produce today."""
    tl, sn = sources.loaders_for(match_id)
    inv = _trade._collect_trade_candidates(conn, match_id, timeline_loader=tl,
                                           snapshot_loader=sn, has_resource=has_resource)
    rows = conn.execute(
        "SELECT * FROM decisions WHERE match_id=? AND detector_id=? ORDER BY id",
        (match_id, DETECTOR_ID)).fetchall()
    row_keys = [_row_key(r) for r in rows]
    dup_rows = sorted(k for k, n in Counter(row_keys).items() if n > 1)
    if dup_rows:
        raise ValidationAbort("duplicate_decision_key", match_id, [list(k) for k in dup_rows])
    if inv is None:
        raise ValidationAbort("inventory_unavailable",
                              f"the trade scan gives up on {match_id}",
                              {"persisted": [list(k) for k in row_keys]})
    cand_keys = [_candidate_key(match_id, c) for c in inv.candidates]
    dup_cands = sorted(k for k, n in Counter(cand_keys).items() if n > 1)
    if dup_cands:
        raise ValidationAbort("duplicate_candidate_key", match_id, [list(k) for k in dup_cands])
    missing = sorted(set(row_keys) - set(cand_keys))
    new = sorted(set(cand_keys) - set(row_keys))
    if missing:
        raise ValidationAbort("candidate_missing", match_id, [list(k) for k in missing])
    if new:
        raise ValidationAbort("candidate_new", match_id, [list(k) for k in new])
    candidates = dict(zip(cand_keys, inv.candidates))
    by_key = dict(zip(row_keys, rows))
    changed = {}
    for k, row in by_key.items():
        ctx = strict_loads(row["context_json"], f"context_json of {list(k)}")
        if not isinstance(ctx, dict):
            raise ValidationAbort("unexpected_context_shape", str(list(k)))
        bad = _stored_descriptor_mismatches(ctx, candidates[k])
        if bad:
            changed[key_string(k)] = bad
    if changed:
        raise ValidationAbort("descriptor_changed", match_id, changed)
    return MatchInventory(inv, candidates, by_key)


def _sf_keytree(sf) -> Optional[dict]:
    if not isinstance(sf, dict):
        return None
    return {k: sorted(v.keys()) if isinstance(v, dict) else None for k, v in sf.items()}


def _argmax(evs):
    top = max(evs)
    return [i for i, v in enumerate(evs) if v == top]


def _ranking(evs):
    return sorted(range(len(evs)), key=lambda i: (-evs[i], i))


def _ev_report(evs, taken):
    rank = _ranking(evs)
    return {
        "ev": evs,
        "argmax": _argmax(evs),
        "ranking": rank,
        "margin_top1_top2": round(evs[rank[0]] - evs[rank[1]], 6),
        "delta_vs_taken": (round(evs[rank[0]] - evs[taken], 6) if taken is not None else None),
    }


def _feature_changes(old, new, prefix=()) -> list:
    out = []
    for p in json_diff_paths(old, new):
        o, n = old, new
        for part in p:
            o = o.get(part) if isinstance(o, dict) else None
            n = n.get(part) if isinstance(n, dict) else None
        out.append({"path": ".".join(str(x) for x in prefix + p), "old": o, "new": n})
    return out


ALLOWED_CONTEXT_PATHS = (
    ("state_features",), ("pre_temporal",), ("next_play_available",),
    ("action", "available_actions", 0, "ev_model"),
    ("action", "available_actions", 1, "ev_model"),
    ("action", "available_actions", 2, "ev_model"),
)
ALLOWED_OPTION_PATHS = ((0, "ev_score"), (1, "ev_score"), (2, "ev_score"),
                        (2, "predicted_consequence"))


def _path_allowed(path: tuple, allowed) -> bool:
    return any(path[:len(a)] == a for a in allowed)


def check_allowed_paths(old_ctx, new_ctx, old_opts, new_opts, key=None) -> None:
    """Only the PRE paths in the allowlist may change. Type changes count as changes."""
    bad = [["context", *p] for p in json_diff_paths(old_ctx, new_ctx)
           if not _path_allowed(p, ALLOWED_CONTEXT_PATHS)]
    bad += [["options", *p] for p in json_diff_paths(old_opts, new_opts)
            if not _path_allowed(p, ALLOWED_OPTION_PATHS)]
    if bad:
        raise ValidationAbort("path_not_allowed", key_string(key) if key else "", bad)


def _decision_json(d) -> dict:
    return json_normalize({
        "context": d.context, "options": [o.__dict__ for o in d.options],
        "moment": d.moment, "outcome": d.outcome, "argument": d.argument,
        "detector_id": d.detector_id, "game_time_ms": d.game_time_ms, "match_id": d.match_id,
    })


def _non_pre_outputs(dj: dict) -> dict:
    """Everything `_evaluate` produces that the PRE must not influence."""
    ctx = copy.deepcopy(dj["context"])
    for k in ("state_features", "pre_temporal", "next_play_available"):
        ctx.pop(k, None)
    for a in ctx.get("action", {}).get("available_actions", []):
        a.pop("ev_model", None)
    opts = copy.deepcopy(dj["options"])
    for i, o in enumerate(opts):
        o.pop("ev_score", None)
        if i == 2:
            o.pop("predicted_consequence", None)
    return {"context": ctx, "options": opts,
            **{k: dj[k] for k in ("moment", "outcome", "argument", "detector_id",
                                  "game_time_ms", "match_id")}}


def _baseline_mismatches(stored_ctx, stored_opts, legacy: dict) -> list[str]:
    bad = []
    lctx, lopts = legacy["context"], legacy["options"]
    if not typed_equal(stored_ctx.get("next_play_available"), lctx["next_play_available"]) \
            or "next_play_available" not in stored_ctx:
        bad.append("next_play_available")
    if not typed_equal(stored_ctx.get("action"), lctx["action"]):
        bad.append("action")
    if not isinstance(stored_opts, list) or len(stored_opts) != len(lopts):
        bad.append("options.length")
        return bad
    for i, (so, lo) in enumerate(zip(stored_opts, lopts)):
        if not isinstance(so, dict):
            bad.append(f"options.{i}")
            continue
        for f in ("label", "taken", "ev_score"):
            if f not in so or not typed_equal(so[f], lo[f]):
                bad.append(f"options.{i}.{f}")
        if i == 2 and ("predicted_consequence" not in so
                       or not typed_equal(so["predicted_consequence"], lo["predicted_consequence"])):
            bad.append("options.2.predicted_consequence")
    return bad


def _overlay(key, stored_ctx: dict, stored_opts: list, new: dict) -> tuple[dict, list]:
    """Build the new row payload ON TOP of the historical object: only the
    allowlisted PRE paths change; reward, POST facts and any extension survive untouched."""
    if "pre_temporal" in stored_ctx:
        raise ValidationAbort("already_versioned", key_string(key))
    if "state_features" not in stored_ctx or "next_play_available" not in stored_ctx \
            or "action" not in stored_ctx:
        raise ValidationAbort("missing_overlay_path", key_string(key))
    nctx = new["context"]
    out = {}
    for k, v in stored_ctx.items():
        if k == "state_features":
            out[k] = copy.deepcopy(nctx["state_features"])
            out["pre_temporal"] = copy.deepcopy(nctx["pre_temporal"])
        elif k == "next_play_available":
            out[k] = nctx["next_play_available"]
        elif k == "action":
            action = copy.deepcopy(v)
            avail = action.get("available_actions") if isinstance(action, dict) else None
            navail = nctx["action"]["available_actions"]
            if (not isinstance(avail, list) or len(avail) != len(ACTION_IDS)
                    or action.get("action_id") != nctx["action"]["action_id"]):
                raise ValidationAbort("action_identity_changed", key_string(key))
            for i, aid in enumerate(ACTION_IDS):
                if (not isinstance(avail[i], dict) or avail[i].get("action_id") != aid
                        or navail[i]["action_id"] != aid or "ev_model" not in avail[i]):
                    raise ValidationAbort("action_identity_changed", key_string(key))
                avail[i]["ev_model"] = navail[i]["ev_model"]
            out[k] = action
        else:
            out[k] = copy.deepcopy(v)
    opts = copy.deepcopy(stored_opts)
    for i in range(3):
        opts[i]["ev_score"] = new["options"][i]["ev_score"]
    opts[2]["predicted_consequence"] = new["options"][2]["predicted_consequence"]
    return out, opts


def serialize_payload(obj) -> str:
    # Exactly how insert_decision serializes both columns.
    return json.dumps(obj, ensure_ascii=False, indent=2)


@dataclass
class AnchorInfo:
    key: tuple
    row: object
    candidate: object
    pre: object                       # trade.TradePre (available)
    ws_old: object
    anchor_different: bool


@dataclass
class Plan:
    entries: list                     # sorted by key: proposal dicts
    analysis: dict
    inventory: dict


def _eligibility_problems(row, key, refs: ReferenceIndex) -> list[str]:
    bad = [c for c in USER_COLUMNS if row[c] is not None]
    bad += refs.refs_for(row["id"], key)
    return bad


def _resolve_anchors(conn, sources, keys, has_resource) -> tuple[dict, dict]:
    """Inventory every match of `keys`, then resolve PRE for every key BEFORE any
    evaluation happens (an abstention aborts the whole batch)."""
    inventories = {}
    for mid in sorted({k[0] for k in keys}):
        inventories[mid] = inventory_match(conn, sources, mid, has_resource)
    anchors = {}
    unavailable = {}
    for k in keys:
        mi = inventories[k[0]]
        if k not in mi.rows:
            raise ValidationAbort("authorized_key_missing", key_string(k))
        c = mi.candidates[k]
        pre = _trade._resolve_trade_pre(conn, k[0], mi.inv.frames, c.m_start, c.m_end)
        if pre.status != "available":
            unavailable[key_string(k)] = pre.reason
            continue
        n = (c.m_start - 1) * 60_000
        ws_old = build_world_state(conn, k[0], n)
        b = pre.temporal["target_ms"]
        different = any(p.pos_at_ms != b for p in ws_old.players.values())
        anchors[k] = AnchorInfo(k, mi.rows[k], c, pre, ws_old, different)
    if unavailable:
        raise ValidationAbort("pre_unavailable", "PRE abstains for authorized keys", unavailable)
    return inventories, anchors


def compute_plan(conn, sources: BundleSources, keys, *, expected_ids: Optional[dict] = None) -> Plan:
    """Proposals for exactly `keys` (read-only). Every failure aborts the batch.

    `_evaluate` is only ever called with the facts of a key in `keys`; the
    descriptors of the other decisions only prove the universe did not change.
    """
    keys = sorted(_check_key(k) for k in keys)
    if len(set(keys)) != len(keys):
        raise ValidationAbort("duplicate_key_in_keyset")
    # Cardinality and eligibility straight from the table (no LIMIT 1, no dicts first).
    refs = build_reference_index(conn)
    problems = {}
    for k in keys:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE match_id=? AND detector_id=? AND game_time_ms=?",
            k).fetchall()
        if len(rows) == 0:
            raise ValidationAbort("authorized_key_missing", key_string(k))
        if len(rows) > 1:
            raise ValidationAbort("duplicate_decision_key", key_string(k),
                                  [r["id"] for r in rows])
        if expected_ids is not None and expected_ids.get(k) != rows[0]["id"]:
            raise ValidationAbort("id_changed", key_string(k),
                                  {"expected": expected_ids.get(k), "found": rows[0]["id"]})
        bad = _eligibility_problems(rows[0], k, refs)
        if bad:
            problems[key_string(k)] = bad
    if problems:
        raise ValidationAbort("human_signal_or_reference", "authorized rows are no longer eligible",
                              problems)

    with sources.game_data() as has_resource:
        inventories, anchors = _resolve_anchors(conn, sources, keys, has_resource)
    same = [key_string(k) for k in keys if not anchors[k].anchor_different]
    if same:
        raise ValidationAbort("pre_not_different", "legacy anchor already equals B", same)

    entries, reports, baseline_bad = [], [], {}
    for k in keys:
        a = anchors[k]
        mi = inventories[k[0]]
        us_id, our_team = mi.inv.us_id, mi.inv.our_team
        row = a.row
        stored_ctx = strict_loads(row["context_json"], f"context_json of {list(k)}")
        stored_opts = strict_loads(row["options_json"], f"options_json of {list(k)}")
        if not isinstance(stored_ctx, dict) or not isinstance(stored_opts, list) \
                or len(stored_opts) != 3 or not all(isinstance(o, dict) for o in stored_opts):
            raise ValidationAbort("unexpected_payload_shape", key_string(k))
        if "pre_temporal" in stored_ctx:
            raise ValidationAbort("already_versioned", key_string(k))
        sf_old = json_normalize(build_state_features(conn, k[0], a.ws_old, us_id, our_team))
        if _sf_keytree(stored_ctx.get("state_features")) != _sf_keytree(sf_old):
            raise ValidationAbort("unknown_state_features_extension", key_string(k),
                                  _sf_keytree(stored_ctx.get("state_features")))
        if not typed_equal(sf_old, stored_ctx["state_features"]):
            baseline_bad[key_string(k)] = ["state_features"]
            continue
        c = a.candidate
        # POST facts come from the STORED row, never recomputed: only the PRE moves.
        post = dict(
            match_id=k[0], t_ms=k[2], m_start=c.m_start, m_end=c.m_end,
            positive=stored_ctx["positive_trade"], d_us=stored_ctx["duo_damage_taken"],
            d_en=stored_ctx["enemy_duo_damage_taken"], hp_pct=stored_ctx["our_hp_pct_end"],
            mana_pct=stored_ctx["our_mana_pct_end"], consequence=stored_ctx.get("consequence"),
        )
        if "consequence" not in stored_ctx:
            raise ValidationAbort("missing_post_fact", key_string(k), "consequence")
        legacy = _decision_json(_trade._evaluate(_trade.TradeFacts(
            **post, state_features=sf_old, pre_temporal=None)))
        bad = _baseline_mismatches(stored_ctx, stored_opts, legacy)
        if bad:
            baseline_bad[key_string(k)] = bad
            continue
        sf_new = build_state_features(conn, k[0], a.pre.state, us_id, our_team)
        new = _decision_json(_trade._evaluate(_trade.TradeFacts(
            **post, state_features=sf_new, pre_temporal=a.pre.temporal)))
        if not typed_equal(_non_pre_outputs(new), _non_pre_outputs(legacy)):
            raise ValidationAbort("non_pre_output_changed", key_string(k))
        if new["context"]["pre_temporal"].get("contract") != PRE_CONTRACT:
            raise ValidationAbort("unexpected_pre_contract", key_string(k))
        new_ctx, new_opts = _overlay(k, stored_ctx, stored_opts, new)
        ctx_text, opts_text = serialize_payload(new_ctx), serialize_payload(new_opts)
        parsed_ctx = strict_loads(ctx_text)
        parsed_opts = strict_loads(opts_text)
        check_allowed_paths(stored_ctx, parsed_ctx, stored_opts, parsed_opts, k)
        if [x for x in parsed_ctx if x != "pre_temporal"] != list(stored_ctx):
            raise ValidationAbort("key_order_changed", key_string(k))
        entries.append({
            "id": row["id"], "key": list(k),
            "before": {"context_json": row["context_json"], "options_json": row["options_json"]},
            "after": {"context_json": ctx_text, "options_json": opts_text},
        })
        taken = next((i for i, o in enumerate(stored_opts) if o.get("taken") is True), None)
        ev_old = [o["ev_score"] for o in stored_opts]
        ev_new = [o["ev_score"] for o in new_opts]
        old_r, new_r = _ev_report(ev_old, taken), _ev_report(ev_new, taken)
        reports.append({
            "key": list(k), "id": row["id"],
            "old_pre": {"target_ms": k[2],
                        "source_frame_ms": sorted({p.pos_at_ms for p in a.ws_old.players.values()
                                                   if p.pos_at_ms is not None})},
            "new_pre": {"target_ms": a.pre.temporal["target_ms"],
                        "opening_frame_index": a.pre.temporal["opening_frame_index"]},
            "feature_changes": _feature_changes(stored_ctx["state_features"],
                                                parsed_ctx["state_features"], ("state_features",)),
            "taken_index": taken, "old": old_r, "new": new_r,
            "recommendation_changed": old_r["argmax"] != new_r["argmax"],
        })
    if baseline_bad:
        raise ValidationAbort("baseline_not_reproducible",
                              "stored features/EV no longer reproduce (scoring or data evolution)",
                              baseline_bad)
    changed = sum(1 for r in reports if r["recommendation_changed"])
    analysis = {
        "keys": len(reports),
        "pre_different": sum(1 for k in keys if anchors[k].anchor_different),
        "recommendation_changed": changed,
        "recommendation_same": len(reports) - changed,
        "per_key": reports,
    }
    inventory = {mid: [dict(key=list(k), **_descriptor(c)) for k, c in mi.candidates.items()]
                 for mid, mi in sorted(inventories.items())}
    return Plan(entries, analysis, inventory)


# ================================================================ selection

def reconstruct_selection(conn, sources_factory) -> tuple[list, dict]:
    """Rebuild the audited selection read-only (second route, no audit artifact).

    S = trade_v1 rows that are legacy (no pre_temporal), carry no human signal
    or reference, and whose legacy anchor differs from B. Baseline
    reproducibility is then enforced by compute_plan (a failure aborts rather
    than silently shrinking S). `sources_factory(match_ids)` captures and
    returns the BundleSources for the matches that need a scan.
    """
    refs = build_reference_index(conn)
    counts = Counter()
    pending = []
    rows = conn.execute("SELECT * FROM decisions WHERE detector_id=? ORDER BY id",
                        (DETECTOR_ID,)).fetchall()
    for row in rows:
        k = _row_key(row)
        counts["trade_rows"] += 1
        try:
            ctx = strict_loads(row["context_json"])
        except ValidationAbort:
            counts["excluded_unparseable_context"] += 1
            continue
        if not isinstance(ctx, dict) or "pre_temporal" in ctx:
            counts["excluded_versioned"] += 1
            continue
        if any(row[c] is not None for c in USER_COLUMNS):
            counts["excluded_human_signal"] += 1
            continue
        if refs.refs_for(row["id"], k):
            counts["excluded_reference"] += 1
            continue
        pending.append(k)
    if len(set(pending)) != len(pending):
        raise ValidationAbort("duplicate_decision_key", "",
                              sorted(key_string(k) for k, n in Counter(pending).items() if n > 1))
    sources = sources_factory({k[0] for k in pending})
    with sources.game_data() as has_resource:
        _, anchors = _resolve_anchors(conn, sources, pending, has_resource)
    selected = []
    for k in pending:
        if anchors[k].anchor_different:
            selected.append(k)
        else:
            counts["excluded_anchor_same"] += 1
    counts["selected"] = len(selected)
    return sorted(selected), dict(sorted(counts.items()))


def keys_from_audit_results(path: Path, expected_sha256: str) -> list:
    """Extract S from the audit artifact (first route).

    The artifact is pinned by the hash of its bytes. Its record layout was not
    available to this implementation, so the parser accepts the documented
    fields in two shapes (a `key` triple or match_id/detector_id/game_time_ms)
    and aborts on anything else; the keyset digest check then proves the result.
    """
    data = Path(path).read_bytes()
    if sha256_hex(data) != expected_sha256:
        raise ValidationAbort("audit_results_hash_mismatch", str(path))
    obj = strict_loads(data.decode("utf-8"), "audit results")
    records = obj if isinstance(obj, list) else None
    if isinstance(obj, dict):
        for name in ("decisions", "results", "rows", "items"):
            if isinstance(obj.get(name), list):
                records = obj[name]
                break
    if records is None:
        raise ValidationAbort("audit_results_unrecognized_format", "no record list")
    keys = []
    for r in records:
        if not isinstance(r, dict) or not {"provenance", "feedback", "anchor",
                                           "baseline_verified"} <= r.keys():
            raise ValidationAbort("audit_results_unrecognized_format", "record fields")
        if "key" in r:
            k = r["key"]
        elif {"match_id", "game_time_ms"} <= r.keys():
            k = [r["match_id"], r.get("detector_id", DETECTOR_ID), r["game_time_ms"]]
        else:
            raise ValidationAbort("audit_results_unrecognized_format", "record key")
        if (r["provenance"] == "legacy_reconstructed" and r["feedback"] is False
                and r["anchor"] == "different" and r["baseline_verified"] is True):
            keys.append(_check_key(k))
    return keys


# ============================================================ excluded snapshot

def excluded_snapshot(conn, entries: list) -> dict:
    """E = every decision outside S, all columns; plus the marked-trade inventory
    and the shared-matches matrix."""
    cols, rows = decision_rows(conn)
    idx = {c: i for i, c in enumerate(cols)}
    s_ids = {e["id"] for e in entries}
    s_matches = {e["key"][0] for e in entries}
    refs = build_reference_index(conn)
    e_digests, marked, flagged_matches, trade_marked_matches = [], [], set(), set()
    for r in rows:
        rid = r[idx["id"]]
        k = (r[idx["match_id"]], r[idx["detector_id"]], r[idx["game_time_ms"]])
        if rid in s_ids:
            continue
        d = row_digest(r)
        e_digests.append(d)
        signal = any(r[idx[c]] is not None for c in USER_COLUMNS)
        referenced = bool(refs.refs_for(rid, k))
        if k[1] == DETECTOR_ID and signal:
            marked.append({"id": rid, "key": list(k), "row_sha256": d})
            if k[0] in s_matches:
                trade_marked_matches.add(k[0])
        if (signal or referenced) and k[0] in s_matches:
            flagged_matches.add(k[0])
    shared = []
    for mid in sorted(flagged_matches):
        auth = sorted([e["id"], e["key"]] for e in entries if e["key"][0] == mid)
        excl = sorted([r[idx["id"]], [r[idx["match_id"]], r[idx["detector_id"]], r[idx["game_time_ms"]]]]
                      for r in rows if r[idx["match_id"]] == mid and r[idx["id"]] not in s_ids)
        shared.append({"match_id": mid, "authorized": auth, "excluded": excl})
    n_e, e_sha = rows_digest(e_digests)
    tables = set(list_tables(conn))
    orphan_trade = (conn.execute("SELECT count(*) FROM orphan_feedback WHERE detector_id=?",
                                 (DETECTOR_ID,)).fetchone()[0] if "orphan_feedback" in tables else 0)
    prefs_trade = (conn.execute("SELECT count(*) FROM preferences WHERE detector_id=?",
                                (DETECTOR_ID,)).fetchone()[0] if "preferences" in tables else 0)
    return {
        "decision_columns": cols,
        "e_rows": n_e, "e_sha256": e_sha,
        "marked_trade": sorted(marked, key=lambda m: m["id"]),
        "shared_matches": shared,
        "counts": {
            "decisions_total": len(rows), "authorized": len(entries), "excluded": n_e,
            "matches_of_authorized": len(s_matches),
            "marked_trade": len(marked), "orphan_feedback_trade": orphan_trade,
            "preferences_trade": prefs_trade,
            # Any human signal or reference, any detector: the matrix revalidated at apply.
            "shared_matches": len(shared),
            "authorized_in_shared_matches": sum(len(s["authorized"]) for s in shared),
            # Only marked trade_v1 rows: the definition comparable with the audited 99/38.
            "matches_shared_with_trade_marks": len(trade_marked_matches),
            "authorized_in_matches_with_trade_marks": sum(
                1 for e in entries if e["key"][0] in trade_marked_matches),
        },
    }


def _expected_state_after(conn, before_state: dict, entries: list) -> dict:
    """The full logical state after applying `entries`, computed without writing."""
    cols, rows = decision_rows(conn)
    idx = {c: i for i, c in enumerate(cols)}
    by_id = {e["id"]: e for e in entries}
    digests = []
    for r in rows:
        e = by_id.get(r[idx["id"]])
        if e is not None:
            r = list(r)
            r[idx["context_json"]] = e["after"]["context_json"]
            r[idx["options_json"]] = e["after"]["options_json"]
        digests.append(row_digest(r))
    after = copy.deepcopy(before_state)
    after["tables"]["decisions"] = list(rows_digest(digests))
    return after


def _row_images(conn, entries, side: str) -> list:
    """[id, row_sha256] of each authorized row as it would read on `side`."""
    cols = table_columns(conn, "decisions")
    idx = {c: i for i, c in enumerate(cols)}
    out = []
    for e in entries:
        r = list(conn.execute("SELECT * FROM decisions WHERE id=?", (e["id"],)).fetchone())
        r[idx["context_json"]] = e[side]["context_json"]
        r[idx["options_json"]] = e[side]["options_json"]
        out.append(row_digest(r))
    return out


# ==================================================================== bundle

def _preflight_schema(conn) -> None:
    triggers = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
    if triggers:
        raise ValidationAbort("trigger_present", "triggers could cause indirect writes", triggers)
    cols = table_columns(conn, "decisions")
    user = sorted(c for c in cols if c.startswith("user_"))
    if user != sorted(USER_COLUMNS):
        raise ValidationAbort("unknown_user_columns", "", user)
    for c in ("id", "match_id", "detector_id", "game_time_ms") + WRITABLE_COLUMNS:
        if c not in cols:
            raise ValidationAbort("missing_decisions_column", c)


def loaded_implementation_files(repo_root: Path = REPO_ROOT) -> list:
    """Repo-relative paths of every lol_coach module loaded in this process (any
    of them can shape the proposals) plus the CLI script, sorted."""
    pkg = Path(repo_root).resolve() / "lol_coach"
    files = {CLI_SCRIPT, *IMPLEMENTATION_FILES}
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            rel = Path(f).resolve().relative_to(pkg)
        except ValueError:
            continue
        if rel.suffix == ".py":
            files.add(("lol_coach" / rel).as_posix())
    return sorted(files)


def implementation_identity(repo_root: Path = REPO_ROOT, files: Optional[list] = None) -> dict:
    """sha256 + git state of the code that produced a manifest.

    `implementation_committed` is true only when EVERY file is tracked at HEAD
    and clean: a dirty helper (context, features, game_data...) changes the
    proposals as surely as a dirty trade.py does.
    """
    files = sorted(files) if files is not None else loaded_implementation_files(repo_root)
    shas = {}
    for rel in files:
        p = Path(repo_root) / rel
        shas[rel] = file_sha256(p)[1] if p.is_file() else None
    head = porcelain = tracked = None
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
                              text=True, check=True).stdout.strip()
        porcelain = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all",
                                    "--", *files], cwd=repo_root, capture_output=True,
                                   text=True, check=True).stdout
        tracked = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD", "--", *files],
                                     cwd=repo_root, capture_output=True, text=True,
                                     check=True).stdout.split())
    except (OSError, subprocess.CalledProcessError):
        pass
    not_at_head = sorted(set(files) - tracked) if tracked is not None else None
    return {
        "git_head": head,
        "status_porcelain_sha256": sha256_text(porcelain) if porcelain is not None else None,
        "status_porcelain": porcelain,
        "not_at_head": not_at_head,
        "files_sha256": shas,
        # A dirty or untracked implementation has no reviewable SHA yet: the
        # rehearsal cannot claim a final PASS while this is false.
        "implementation_committed": (bool(head) and porcelain == "" and not_at_head == []
                                     and all(v is not None for v in shas.values())),
    }


def _bundle_members(bundle_dir: Path) -> list:
    """[[relpath, size, sha256]] of every regular file; rejects links, special
    files and directories that hold no member (nothing unlisted may hide there)."""
    out = []
    dirs = []

    def walk(d: Path, prefix: str):
        with os.scandir(d) as it:
            for entry in sorted(it, key=lambda e: e.name):
                rel = f"{prefix}{entry.name}"
                st = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse(st):
                    raise ValidationAbort("bundle_link_rejected", rel)
                if stat.S_ISDIR(st.st_mode):
                    dirs.append(rel + "/")
                    walk(Path(entry.path), rel + "/")
                elif stat.S_ISREG(st.st_mode):
                    # scandir does not fill st_nlink on Windows: ask lstat.
                    if os.lstat(entry.path).st_nlink != 1:
                        raise ValidationAbort("bundle_link_rejected", f"{rel} is hard-linked")
                    size, sha = file_sha256(Path(entry.path))
                    out.append([rel, size, sha])
                else:
                    raise ValidationAbort("bundle_member_type_rejected", rel)
    walk(bundle_dir, "")
    empty = [d for d in dirs if not any(m[0].startswith(d) for m in out)]
    if empty:
        raise ValidationAbort("bundle_member_mismatch", "directories without members", empty)
    return out


def _check_member_path(p) -> str:
    if (not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or ":" in p
            or any(seg in ("", ".", "..") for seg in p.split("/"))):
        raise ValidationAbort("bundle_member_path_rejected", repr(p))
    return p


def bundle_digest(members_with_manifest: list) -> str:
    return sha256_hex(canonical_json(sorted(members_with_manifest)))


def _is_reparse(st) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) \
        or stat.S_ISLNK(st.st_mode)


@dataclass
class Bundle:
    dir: Path
    manifest: dict
    manifest_sha256: str
    bundle_sha256: str
    entries: list                              # proposals, sorted by key, validated

    @property
    def before_path(self) -> Path:
        return self.dir / "before.sqlite"

    @property
    def before_digest(self) -> str:
        return self.manifest["before_state_sha256"]

    @property
    def after_digest(self) -> str:
        return self.manifest["after_state_sha256"]

    def sources(self) -> BundleSources:
        names = {m[0] for m in self.manifest["members"]}
        return BundleSources(self.dir, self.manifest["sources"], names)


def load_bundle(bundle_dir, manifest_sha256: str, bundle_sha256: str) -> Bundle:
    """Verify every byte of the bundle against the two EXTERNAL hashes."""
    _check_hex(manifest_sha256, "--manifest-sha256")
    _check_hex(bundle_sha256, "--bundle-sha256")
    bundle_dir = Path(bundle_dir)
    st = os.lstat(bundle_dir)
    if _is_reparse(st) or not stat.S_ISDIR(st.st_mode):
        raise ValidationAbort("bundle_link_rejected", str(bundle_dir))
    actual = _bundle_members(bundle_dir)
    by_path = {m[0]: m for m in actual}
    if "manifest.json" not in by_path:
        raise ValidationAbort("bundle_manifest_missing", str(bundle_dir))
    manifest_bytes = (bundle_dir / "manifest.json").read_bytes()
    if sha256_hex(manifest_bytes) != manifest_sha256:
        raise ValidationAbort("manifest_hash_mismatch")
    manifest = strict_loads(manifest_bytes.decode("utf-8"), "manifest")
    if not isinstance(manifest, dict) or manifest.get("procedure_version") != PROCEDURE_VERSION:
        raise ValidationAbort("manifest_version_mismatch")
    declared = manifest.get("members")
    if not isinstance(declared, list):
        raise ValidationAbort("manifest_members_invalid")
    names = []
    for m in declared:
        if m == ["manifest.json"]:
            names.append("manifest.json")
            continue
        if not (isinstance(m, list) and len(m) == 3):
            raise ValidationAbort("manifest_members_invalid", repr(m))
        names.append(_check_member_path(m[0]))
        if by_path.get(m[0]) != m:
            raise ValidationAbort("bundle_member_mismatch", m[0])
    if not {"manifest.json", "before.sqlite", "proposals.json", "analysis.json"} <= set(names):
        raise ValidationAbort("bundle_member_mismatch", "mandatory member missing")
    if sorted(names) != sorted(by_path) or len(set(names)) != len(names):
        raise ValidationAbort("bundle_member_mismatch", "extra or missing files",
                              {"extra": sorted(set(by_path) - set(names)),
                               "missing": sorted(set(names) - set(by_path))})
    if bundle_digest(actual) != bundle_sha256:
        raise ValidationAbort("bundle_hash_mismatch")
    proposals = strict_loads((bundle_dir / "proposals.json").read_bytes().decode("utf-8"),
                             "proposals")
    entries = _validate_proposals(manifest, proposals)
    BundleSources(bundle_dir, manifest.get("sources"), set(names))   # traversal/declaration check
    return Bundle(bundle_dir, manifest, manifest_sha256, bundle_sha256, entries)


def _validate_proposals(manifest: dict, proposals) -> list:
    if not isinstance(proposals, dict) or not isinstance(proposals.get("proposals"), list):
        raise ValidationAbort("proposals_invalid")
    entries = proposals["proposals"]
    m_entries = manifest.get("entries")
    if not isinstance(m_entries, list) or len(m_entries) != len(entries):
        raise ValidationAbort("proposals_mismatch", "count")
    keys = []
    for p, m in zip(entries, m_entries):
        k = _check_key(p.get("key"))
        keys.append(k)
        if (m.get("key") != list(k) or m.get("id") != p.get("id")
                or type(p.get("id")) is not int):
            raise ValidationAbort("proposals_mismatch", key_string(k))
        for side in ("before", "after"):
            for col in WRITABLE_COLUMNS:
                text = p[side][col]
                if not isinstance(text, str) or sha256_text(text) != m[side][col + "_sha256"]:
                    raise ValidationAbort("proposals_mismatch", f"{key_string(k)} {side}.{col}")
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ValidationAbort("proposals_mismatch", "order or duplicates")
    if keyset_digest(keys) != manifest.get("keyset_sha256"):
        raise ValidationAbort("proposals_mismatch", "keyset digest")
    if proposals.get("keyset_sha256") != manifest.get("keyset_sha256"):
        raise ValidationAbort("proposals_mismatch", "proposals keyset digest")
    return entries


@dataclass
class PrepareResult:
    evidence_dir: Path
    bundle_dir: Path
    trial_path: Path
    manifest_sha256: str
    bundle_sha256: str
    summary: dict
    trial_registry_sha256: str = ""


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def prepare(source, output, *, expected_keyset_sha256: str, game_data_dir,
            audit_results=None, audit_results_sha256: str = AUDIT_RESULTS_SHA256,
            repo_root: Path = REPO_ROOT) -> PrepareResult:
    """Snapshot `source` read-only, build the immutable bundle, create the trial.

    Never writes `source`; everything after the snapshot reads before.sqlite.
    """
    _check_hex(expected_keyset_sha256, "--expected-keyset-sha256")
    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise ValidationAbort("source_not_a_file", str(source))
    if os.path.lexists(output):
        raise ValidationAbort("output_exists", str(output))
    real_src = Path(os.path.realpath(source))
    real_out = Path(os.path.realpath(output.parent)) / output.name
    if _inside(real_src, real_out):
        raise ValidationAbort("source_inside_output", str(source))
    output.mkdir(parents=True)
    bundle_dir = output / "bundle"
    bundle_dir.mkdir()
    before_path = bundle_dir / "before.sqlite"

    # 1. One read transaction on the source pins the view that is hashed AND backed up.
    src = connect_ro(source)
    try:
        src.execute("BEGIN")
        src.execute("SELECT count(*) FROM sqlite_master").fetchone()
        source_state = logical_state(src)
        dst = sqlite3.connect(str(before_path))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")   # no WAL sidecars for a frozen member
        finally:
            dst.close()
        src.execute("COMMIT")
    finally:
        src.close()
    source_digest = state_digest(source_state)
    source_stat = source.stat()

    before = connect_ro(before_path, immutable=True)
    try:
        before_state = logical_state(before)
        if state_digest(before_state) != source_digest:
            raise ValidationAbort("backup_not_consistent",
                                  "before.sqlite is not the inventoried view")
        _preflight_schema(before)

        captured = {}

        def sources_for(match_ids):
            if not captured:
                captured.update(capture_sources(before, match_ids, bundle_dir, game_data_dir))
            elif not set(match_ids) <= set(captured["timelines"]):
                raise ValidationAbort("source_scope_grew")
            return BundleSources(bundle_dir, captured)

        if audit_results is not None:
            mode = "audit_results"
            keys = keys_from_audit_results(Path(audit_results), audit_results_sha256)
            criteria = {"selected": len(keys)}
        else:
            mode = "reconstructed"
            keys, criteria = reconstruct_selection(before, sources_for)
        digest = keyset_digest(keys)
        if digest != expected_keyset_sha256:
            write_durable(output / "rejection.json", canonical_json({
                "error": "keyset_mismatch", "selection_mode": mode,
                "expected_keyset_sha256": expected_keyset_sha256, "found_keyset_sha256": digest,
                "found_count": len(keys), "criteria_counts": criteria,
                "found_keys": sorted(list(k) for k in keys),
            }))
            raise ValidationAbort("keyset_mismatch",
                                  "the selected set is not the approved one; see rejection.json",
                                  {"expected": expected_keyset_sha256, "found": digest,
                                   "found_count": len(keys)})
        sources = sources_for({k[0] for k in keys})
        plan = compute_plan(before, sources, keys)
        entries = plan.entries
        excluded = excluded_snapshot(before, entries)
        after_state = _expected_state_after(before, before_state, entries)
        row_before = _row_images(before, entries, "before")
        row_after = _row_images(before, entries, "after")
    finally:
        before.close()

    proposals_bytes = canonical_json({"keyset_sha256": digest, "proposals": entries})
    analysis_bytes = canonical_json(plan.analysis)
    _write_new(bundle_dir / "proposals.json", proposals_bytes)
    _write_new(bundle_dir / "analysis.json", analysis_bytes)
    members = _bundle_members(bundle_dir)          # manifest not written yet
    manifest = {
        "procedure_version": PROCEDURE_VERSION,
        "contract_sha": CONTRACT_SHA,
        "pre_contract": PRE_CONTRACT,
        "implementation": implementation_identity(repo_root),
        "runtime": {"python": sys.version, "sqlite": sqlite3.sqlite_version},
        "selection": {"mode": mode, "audit_results_sha256":
                      audit_results_sha256 if audit_results is not None else None,
                      "criteria_counts": criteria},
        "keyset_sha256": digest,
        "entries": [{
            "id": e["id"], "key": e["key"],
            "before": {"context_json_sha256": sha256_text(e["before"]["context_json"]),
                       "options_json_sha256": sha256_text(e["before"]["options_json"]),
                       "row_sha256": rb},
            "after": {"context_json_sha256": sha256_text(e["after"]["context_json"]),
                      "options_json_sha256": sha256_text(e["after"]["options_json"]),
                      "row_sha256": ra},
        } for e, rb, ra in zip(entries, row_before, row_after)],
        # Path is informational, never identity: identity is the logical hash.
        "source_db": {"path": str(source), "size": source_stat.st_size,
                      "st_dev": source_stat.st_dev, "st_ino": source_stat.st_ino,
                      "logical_sha256": source_digest},
        "schema_sha256": before_state["schema_sha256"],
        "before_state": before_state, "before_state_sha256": state_digest(before_state),
        "after_state": after_state, "after_state_sha256": state_digest(after_state),
        "excluded": excluded,
        "inventory": plan.inventory,
        "sources": captured,
        "analysis_summary": {k: v for k, v in plan.analysis.items() if k != "per_key"},
        "proposals": {"member": "proposals.json", "sha256": sha256_hex(proposals_bytes)},
        "members": sorted(members + [["manifest.json"]], key=lambda m: m[0]),
    }
    manifest_bytes = canonical_json(manifest)
    _write_new(bundle_dir / "manifest.json", manifest_bytes)
    manifest_sha = sha256_hex(manifest_bytes)
    bundle_sha = bundle_digest(_bundle_members(bundle_dir))
    bundle = load_bundle(bundle_dir, manifest_sha, bundle_sha)   # prove it loads as written

    trial = create_trial(bundle, output)
    (output / "receipts").mkdir()
    write_durable(output / "receipts" / "prepared.json", canonical_json(
        prepared_receipt_body(bundle)))
    registry_sha = sha256_hex((output / "trial-registry.json").read_bytes())
    summary = {
        "manifest_sha256": manifest_sha, "bundle_sha256": bundle_sha,
        "trial_registry_sha256": registry_sha,
        "keyset_sha256": digest, "selection_mode": mode, "criteria_counts": criteria,
        "counts": excluded["counts"], "analysis": manifest["analysis_summary"],
        "implementation_committed": manifest["implementation"]["implementation_committed"],
    }
    write_durable(output / "approval.txt", (
        f"manifest_sha256={manifest_sha}\nbundle_sha256={bundle_sha}\n"
        f"trial_registry_sha256={registry_sha}\n"
        "informational only: every command still takes the three hashes as arguments\n"
    ).encode("utf-8"))
    return PrepareResult(output, bundle_dir, trial, manifest_sha, bundle_sha, summary,
                         registry_sha)


def prepared_receipt_body(bundle: Bundle) -> dict:
    return {
        "kind": "prepared", "manifest_sha256": bundle.manifest_sha256,
        "bundle_sha256": bundle.bundle_sha256, "keyset_sha256": bundle.manifest["keyset_sha256"],
        "before_state_sha256": bundle.before_digest, "after_state_sha256": bundle.after_digest,
        "entries": [{"id": e["id"], "key": e["key"], "before": e["before"], "after": e["after"]}
                    for e in bundle.entries],
    }


def verify_prepared_receipt(evidence_dir: Path, bundle: Bundle) -> None:
    path = Path(evidence_dir) / "receipts" / "prepared.json"
    if not path.is_file():
        raise ValidationAbort("prepared_receipt_missing", str(path))
    body = strict_loads(path.read_bytes().decode("utf-8"), "prepared receipt")
    if not typed_equal(body, json_normalize(prepared_receipt_body(bundle))):
        raise ValidationAbort("prepared_receipt_mismatch", str(path))


def create_trial(bundle: Bundle, evidence_dir: Path) -> Path:
    """The ONLY destination write commands accept: a fresh copy of before.sqlite
    in the tool's own evidence dir, registered by physical identity."""
    evidence_dir = Path(evidence_dir)
    trial = evidence_dir / "trial.sqlite"
    registry = evidence_dir / "trial-registry.json"
    if os.path.lexists(trial) or os.path.lexists(registry):
        raise ValidationAbort("refuse_overwrite", str(trial))
    src = connect_ro(bundle.before_path, immutable=True)
    try:
        dst = sqlite3.connect(str(trial))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    check = connect_ro(trial)
    try:
        digest = state_digest(logical_state(check))
    finally:
        check.close()
    if digest != bundle.before_digest:
        raise ValidationAbort("trial_not_consistent")
    st = os.stat(trial)
    if st.st_nlink != 1:
        raise ValidationAbort("trial_link_count", str(st.st_nlink))
    write_durable(registry, canonical_json({
        "path": os.path.normcase(os.path.realpath(trial)),
        "st_dev": st.st_dev, "st_ino": st.st_ino, "st_nlink": st.st_nlink,
        "size": st.st_size, "logical_sha256": digest,
        "manifest_sha256": bundle.manifest_sha256, "bundle_sha256": bundle.bundle_sha256,
        "created_at_utc": utc_now(),
    }))
    return trial


# ===================================================================== engine
#
# Write side. Same code for the copy rehearsal and any future authorized real
# application: the caller owns the connection and the destination policy.

def _classify(conn, bundle: Bundle) -> tuple[str, dict]:
    state = logical_state(conn)
    digest = state_digest(state)
    if digest == bundle.before_digest:
        return "before", {}
    if digest == bundle.after_digest:
        return "after", {}
    before, after = bundle.manifest["before_state"], bundle.manifest["after_state"]
    details = {
        "schema_changed": state["schema_sha256"] != before["schema_sha256"],
        "references_changed": state["references_sha256"] != before["references_sha256"],
        "tables_differing_from_before": sorted(
            t for t in set(state["tables"]) | set(before["tables"])
            if state["tables"].get(t) != before["tables"].get(t)),
        "tables_differing_from_after": sorted(
            t for t in set(state["tables"]) | set(after["tables"])
            if state["tables"].get(t) != after["tables"].get(t)),
    }
    if "decisions" in list_tables(conn):
        at_before, at_after, neither = 0, 0, []
        for e in bundle.entries:
            rows = conn.execute("SELECT context_json, options_json FROM decisions WHERE id=?",
                                (e["id"],)).fetchall()
            pair = tuple(rows[0]) if len(rows) == 1 else None
            if pair == (e["before"]["context_json"], e["before"]["options_json"]):
                at_before += 1
            elif pair == (e["after"]["context_json"], e["after"]["options_json"]):
                at_after += 1
            else:
                neither.append(e["id"])
        details.update(authorized_at_before=at_before, authorized_at_after=at_after,
                       authorized_neither=neither)
    return "mixed", details


def verify_state(conn, bundle: Bundle) -> tuple[str, dict]:
    """"before" | "after" | "mixed" (anything else), with diagnostics."""
    return _classify(conn, bundle)


def _allowed_authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION,
                  sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in ("integrity_check", "foreign_key_check",
                                                    "table_info"):
        return sqlite3.SQLITE_OK
    if (action == sqlite3.SQLITE_UPDATE and dbname == "main" and source is None
            and arg1 == "decisions" and arg2 in WRITABLE_COLUMNS):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _shared_matrix(conn, entries) -> list:
    return excluded_snapshot(conn, entries)["shared_matches"]


def _revalidate(conn, bundle: Bundle, side: str) -> None:
    """Explicit re-checks under the lock (the full-state match already implies
    them; they exist so a hash bug cannot silently let a human signal through)."""
    _preflight_schema(conn)
    if table_columns(conn, "decisions") != bundle.manifest["excluded"]["decision_columns"]:
        raise StateMismatch("schema_changed", "decisions columns")
    refs = build_reference_index(conn)
    for e in bundle.entries:
        k = tuple(e["key"])
        rows = conn.execute(
            "SELECT * FROM decisions WHERE match_id=? AND detector_id=? AND game_time_ms=?",
            k).fetchall()
        if len(rows) != 1 or rows[0]["id"] != e["id"]:
            raise StateMismatch("authorized_row_changed", key_string(k))
        bad = _eligibility_problems(rows[0], k, refs)
        if bad:
            raise StateMismatch("human_signal_or_reference", key_string(k), bad)
        if (rows[0]["context_json"], rows[0]["options_json"]) != (
                e[side]["context_json"], e[side]["options_json"]):
            raise StateMismatch("authorized_row_changed", key_string(k))
    if not typed_equal(json_normalize(_shared_matrix(conn, bundle.entries)),
                       bundle.manifest["excluded"]["shared_matches"]):
        raise StateMismatch("shared_matrix_changed")


def _verify_written(conn, bundle: Bundle, side: str) -> None:
    for e in bundle.entries:
        pair = tuple(conn.execute("SELECT context_json, options_json FROM decisions WHERE id=?",
                                  (e["id"],)).fetchone())
        if pair != (e[side]["context_json"], e[side]["options_json"]):
            raise StateMismatch("written_row_mismatch", key_string(e["key"]))
    expected = bundle.after_digest if side == "after" else bundle.before_digest
    if state_digest(logical_state(conn)) != expected:
        raise StateMismatch("post_state_mismatch", side)
    if [tuple(r) for r in conn.execute("PRAGMA integrity_check")] != [("ok",)]:
        raise StateMismatch("integrity_check_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise StateMismatch("foreign_key_check_failed")


def _run_batch(conn, bundle: Bundle, *, frm: str, to: str, done_state: str, done_result: str,
               fault, pre_commit_check) -> str:
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise StateMismatch("target_busy", str(exc)) from None
    try:
        state, details = _classify(conn, bundle)
        if state == done_state:
            conn.execute("ROLLBACK")
            return done_result
        if state != frm:
            raise StateMismatch("state_mismatch", f"target is {state}, expected {frm}", details)
        _revalidate(conn, bundle, frm)
        user_cols = [c for c in table_columns(conn, "decisions") if c.startswith("user_")]
        sql = ("UPDATE decisions SET context_json=?, options_json=? WHERE id=? AND match_id=? "
               "AND detector_id=? AND game_time_ms=? AND context_json IS ? AND options_json IS ? "
               + "".join(f"AND {_q(c)} IS NULL " for c in user_cols))
        conn.set_authorizer(_allowed_authorizer)
        start = conn.total_changes
        for i, e in enumerate(bundle.entries):
            cur = conn.execute(sql, (e[to]["context_json"], e[to]["options_json"], e["id"],
                                     *e["key"], e[frm]["context_json"], e[frm]["options_json"]))
            if cur.rowcount != 1:
                raise StateMismatch("row_predicate_failed", key_string(e["key"]))
            if fault:
                fault("after_update", i)
        if conn.total_changes - start != len(bundle.entries):
            raise StateMismatch("unexpected_change_count")
        _verify_written(conn, bundle, to)
        if fault:
            fault("before_commit", None)
        if pre_commit_check:
            pre_commit_check()
        conn.execute("COMMIT")
    except BaseException:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.set_authorizer(None)
        raise
    conn.set_authorizer(None)
    return "applied" if to == "after" else "rolled_back"


def _read_only_authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def hot_journal(path: Path) -> Optional[Path]:
    j = Path(str(path) + "-journal")
    return j if j.is_file() and j.stat().st_size > 0 else None


def recover_hot_journal(path: Path) -> bool:
    """After a crash mid-transaction a rollback-journal DB carries a hot journal
    that read-only connections cannot roll back ("attempt to write a readonly
    database"). Let SQLite perform its standard recovery through a write-capable
    connection that is query_only and whose authorizer denies every statement
    but reads: the only effect possible is restoring the pre-transaction pages.
    Returns whether a hot journal was recovered.

    Only ever call this on the registered trial, after its identity checks --
    never on a source (sources are opened read-only everywhere).
    """
    if hot_journal(path) is None:
        return False
    conn = connect_rw_existing(path)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_read_only_authorizer)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        conn.close()
    if hot_journal(path) is not None:
        raise StateMismatch("hot_journal_not_recovered", str(path))
    return True


def apply_batch(conn, bundle: Bundle, *, fault: Optional[Callable] = None,
                pre_commit_check: Optional[Callable] = None) -> str:
    """One transaction: before -> after. "already_applied" when the full state is
    already after (no writes); any other state raises StateMismatch."""
    return _run_batch(conn, bundle, frm="before", to="after", done_state="after",
                      done_result="already_applied", fault=fault,
                      pre_commit_check=pre_commit_check)


def rollback_batch(conn, bundle: Bundle, *, fault: Optional[Callable] = None,
                   pre_commit_check: Optional[Callable] = None) -> str:
    """Inverse in-place transaction: after -> before, only when the full state is
    exactly after (a new mark or any concurrent change blocks it)."""
    return _run_batch(conn, bundle, frm="after", to="before", done_state="before",
                      done_result="already_before", fault=fault,
                      pre_commit_check=pre_commit_check)
