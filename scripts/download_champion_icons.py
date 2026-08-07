"""Cache verified, versioned Data Dragon icons used by lol-coach.

The local game-data catalog is authoritative for what is fetched.  The default
command caches the account/support champion union, current Summoner's Rift
store items, and every rune in the five standard trees.  It never derives a
remote URL or output path from unvalidated display text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import tomllib
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
DDRAGON_BASE = "https://ddragon.leagueoflegends.com"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHAMPION_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
ITEM_ID_RE = re.compile(r"^[0-9]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
RUNE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ChampionIconError(RuntimeError):
    """An icon catalog, cache location, or downloaded asset is unsafe/invalid."""


def validate_version(version: str) -> str:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ChampionIconError(f"Versi\u00f3n de Data Dragon inv\u00e1lida: {version!r}")
    return version


def validate_champion_id(champion_id: str) -> str:
    if not isinstance(champion_id, str) or not CHAMPION_ID_RE.fullmatch(champion_id):
        raise ChampionIconError(
            f"ID de Data Dragon inv\u00e1lido: {champion_id!r}; solo se permiten letras y n\u00fameros ASCII"
        )
    return champion_id


def validate_item_id(item_id: str) -> str:
    if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id):
        raise ChampionIconError(f"ID de objeto inv\u00e1lido: {item_id!r}; solo se permiten d\u00edgitos")
    return item_id


def validate_rune_id(rune_id: object) -> int:
    # bool is an int subclass but is not an acceptable catalog ID.
    if isinstance(rune_id, bool) or not isinstance(rune_id, int) or rune_id <= 0:
        raise ChampionIconError(f"ID de runa inv\u00e1lido: {rune_id!r}")
    return rune_id


def validate_rune_icon_path(icon_path: object) -> str:
    """Allow only a normal relative Data Dragon image path, segment by segment."""
    if not isinstance(icon_path, str) or not icon_path or "\\" in icon_path:
        raise ChampionIconError(f"Ruta de icono de runa inv\u00e1lida: {icon_path!r}")
    if icon_path.startswith("/") or "?" in icon_path or "#" in icon_path:
        raise ChampionIconError(f"Ruta de icono de runa inv\u00e1lida: {icon_path!r}")
    segments = icon_path.split("/")
    if any(segment in {"", ".", ".."} or not RUNE_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ChampionIconError(f"Ruta de icono de runa inv\u00e1lida: {icon_path!r}")
    return "/".join(segments)


def is_reparse_or_symlink(path: Path) -> bool:
    """Return whether an existing path is a link/reparse point without following it."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ChampionIconError(f"No pude inspeccionar enlaces en {path}: {exc}") from exc
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse_point)


def safe_output_directory(output_dir: Path, *, create: bool) -> Path:
    """Create/use a directory only when no component is a link or reparse point."""
    absolute = Path(os.path.abspath(os.fspath(output_dir)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if is_reparse_or_symlink(current):
            raise ChampionIconError(f"El directorio de salida no puede atravesar enlaces: {current}")
    try:
        if create:
            absolute.mkdir(parents=True, exist_ok=True)
        if not absolute.exists() or not absolute.is_dir():
            raise ChampionIconError(f"El directorio de salida no es un directorio: {absolute}")
        # Recheck after mkdir; this also catches a concurrent replacement.
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            if is_reparse_or_symlink(current):
                raise ChampionIconError(f"El directorio de salida no puede atravesar enlaces: {current}")
        return absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ChampionIconError(f"No pude preparar el directorio de salida {absolute}: {exc}") from exc


def safe_destination(output_dir: Path, filename: str) -> Path:
    """Return a regular cache-file destination that remains under output_dir."""
    if Path(filename).name != filename:
        raise ChampionIconError(f"Nombre de archivo no seguro: {filename!r}")
    destination = output_dir / filename
    try:
        destination.resolve(strict=False).relative_to(output_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ChampionIconError(f"El destino sale del directorio de iconos: {destination}") from exc
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise ChampionIconError(f"No pude inspeccionar el destino {destination}: {exc}") from exc
    if is_reparse_or_symlink(destination):
        raise ChampionIconError(f"El destino existente no puede ser un enlace: {destination}")
    if not stat.S_ISREG(info.st_mode):
        raise ChampionIconError(f"El destino existente no es un archivo regular: {destination}")
    return destination


def normalize_champion_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in decomposed if not unicodedata.combining(char) and char.isalnum())


def load_configured_db_path(config_path: Path) -> Path:
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    data = config.get("data", {})
    paths = config.get("paths", {})
    db_path = data.get("db_path") or paths.get("sqlite_db")
    if not db_path:
        raise ChampionIconError(f"No encuentro data.db_path ni paths.sqlite_db en {config_path}")
    return Path(db_path)


def load_version_and_catalog(game_data_dir: Path) -> tuple[str, dict[str, dict]]:
    try:
        index = json.loads((game_data_dir / "_index.json").read_text(encoding="utf-8"))
        version = validate_version(index["ddragon_version"])
        catalog = json.loads((game_data_dir / "champions_summary.json").read_text(encoding="utf-8"))["champions"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ChampionIconError(f"No pude cargar el cat\u00e1logo local de campeones: {exc}") from exc
    if not isinstance(catalog, dict):
        raise ChampionIconError("El cat\u00e1logo local de campeones tiene una estructura inv\u00e1lida")
    return version, catalog


def load_asset_catalogs(game_data_dir: Path) -> tuple[str, dict[str, dict], dict[str, dict], dict[str, dict], list[dict]]:
    version, champions = load_version_and_catalog(game_data_dir)
    try:
        items = json.loads((game_data_dir / "items.json").read_text(encoding="utf-8"))["items"]
        cdragon_items = json.loads((game_data_dir / "items_cdragon.json").read_text(encoding="utf-8"))["items"]
        rune_paths = json.loads((game_data_dir / "runes.json").read_text(encoding="utf-8"))["rune_paths"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ChampionIconError(f"No pude cargar los cat\u00e1logos locales de iconos: {exc}") from exc
    if not isinstance(items, dict) or not isinstance(cdragon_items, dict) or not isinstance(rune_paths, list):
        raise ChampionIconError("Los cat\u00e1logos locales de iconos tienen una estructura inv\u00e1lida")
    return version, champions, items, cdragon_items, rune_paths


def own_champion_names(db_path: Path) -> list[str]:
    if not db_path.exists():
        raise ChampionIconError(f"No existe la base de datos: {db_path}")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            rows = connection.execute("""
                SELECT DISTINCT p.champion_name
                FROM participants AS p JOIN matches AS m
                  ON m.match_id = p.match_id AND m.our_puuid = p.puuid
                WHERE p.champion_name IS NOT NULL AND trim(p.champion_name) <> ''
                ORDER BY p.champion_name COLLATE NOCASE
            """).fetchall()
    except sqlite3.Error as exc:
        raise ChampionIconError(f"No pude consultar campeones propios en {db_path}: {exc}") from exc
    return [row[0] for row in rows]


def catalog_name_index(catalog: dict[str, dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for key, entry in catalog.items():
        if not isinstance(entry, dict):
            continue
        champion_id = entry.get("id")
        if not isinstance(champion_id, str):
            continue
        validate_champion_id(champion_id)
        for candidate in (key, entry.get("name"), champion_id):
            if not isinstance(candidate, str):
                continue
            normalized = normalize_champion_name(candidate)
            old = index.get(normalized)
            if old and old["id"] != champion_id:
                raise ChampionIconError(f"El cat\u00e1logo contiene nombres ambiguos: {candidate!r}")
            index[normalized] = {"id": champion_id, "name": entry.get("name", champion_id)}
    return index


def resolve_champions(champion_names: list[str], catalog: dict[str, dict]) -> list[dict]:
    index = catalog_name_index(catalog)
    resolved, unresolved, seen_ids = [], [], set()
    for champion_name in champion_names:
        entry = index.get(normalize_champion_name(champion_name))
        if entry is None:
            unresolved.append(champion_name)
        elif entry["id"] not in seen_ids:
            resolved.append({"champion_name": champion_name, **entry})
            seen_ids.add(entry["id"])
    if unresolved:
        raise ChampionIconError("No pude resolver: " + ", ".join(unresolved))
    return resolved


def resolve_extended_champions(own_names: list[str], catalog: dict[str, dict]) -> list[dict]:
    """Return canonical champion entries tagged own/support, once per ID."""
    result: dict[str, dict] = {}
    for entry in resolve_champions(own_names, catalog):
        result[entry["id"]] = {"name": entry["name"], "champion_name": entry["champion_name"], "id": entry["id"], "groups": {"own"}}
    for key, entry in catalog.items():
        if not isinstance(entry, dict) or "Support" not in entry.get("tags", []):
            continue
        champion_id = validate_champion_id(entry.get("id"))
        record = result.setdefault(champion_id, {"name": entry.get("name", key), "champion_name": entry.get("name", key), "id": champion_id, "groups": set()})
        record["groups"].add("support")
    return [{**entry, "groups": sorted(entry["groups"])} for _, entry in sorted(result.items())]


def resolve_store_items(items: dict[str, dict], cdragon_items: dict[str, dict]) -> list[dict]:
    """Keep only purchasable, current SR items confirmed in the CDragon store."""
    result = []
    for item_id, entry in items.items():
        item_id = validate_item_id(item_id)
        maps = entry.get("maps", {}) if isinstance(entry, dict) else {}
        gold = entry.get("gold", {}) if isinstance(entry, dict) else {}
        cdragon = cdragon_items.get(item_id, {})
        if maps.get("11") is True and gold.get("purchasable") is True and isinstance(cdragon, dict) and cdragon.get("inStore") is True:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ChampionIconError(f"Nombre de objeto inv\u00e1lido: {item_id}")
            result.append({"id": item_id, "name": name, "group": "summoners-rift"})
    return sorted(result, key=lambda entry: int(entry["id"]))


def resolve_runes(rune_paths: list[dict]) -> list[dict]:
    """Flatten all five standard trees; first slot is a keystone, rest secondary."""
    if len(rune_paths) != 5:
        raise ChampionIconError(f"Esperaba cinco \u00e1rboles de runas, recib\u00ed {len(rune_paths)}")
    result, seen_ids = [], set()
    for tree in rune_paths:
        if not isinstance(tree, dict) or not isinstance(tree.get("key"), str) or not isinstance(tree.get("slots"), list):
            raise ChampionIconError("\u00c1rbol de runas inv\u00e1lido")
        for slot_index, slot in enumerate(tree["slots"]):
            for rune in slot.get("runes", []) if isinstance(slot, dict) else []:
                rune_id = validate_rune_id(rune.get("id") if isinstance(rune, dict) else None)
                if rune_id in seen_ids:
                    raise ChampionIconError(f"ID de runa duplicado: {rune_id}")
                name = rune.get("name") if isinstance(rune, dict) else None
                if not isinstance(name, str) or not name:
                    raise ChampionIconError(f"Nombre de runa inv\u00e1lido: {rune_id}")
                result.append({"id": rune_id, "name": name, "icon_path": validate_rune_icon_path(rune.get("icon")), "tree": tree["key"], "group": "keystone" if slot_index == 0 else "secondary"})
                seen_ids.add(rune_id)
    return result


def validate_png(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or not payload.startswith(PNG_SIGNATURE):
        raise ChampionIconError("la respuesta no tiene firma PNG v\u00e1lida")
    if payload[12:16] != b"IHDR":
        raise ChampionIconError("la respuesta PNG no contiene IHDR")
    width, height = struct.unpack(">II", payload[16:24])
    if width <= 0 or height <= 0:
        raise ChampionIconError("la imagen PNG no tiene dimensiones v\u00e1lidas")
    return width, height


def fetch_png(url: str, timeout: float = 30) -> tuple[bytes, str | None]:
    request = Request(url, headers={"User-Agent": "lol-coach/1.0 icon-cache"})
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310: URL is constructed from validated official constants
            status = getattr(response, "status", response.getcode())
            if status != 200:
                raise ChampionIconError(f"HTTP {status}")
            return response.read(), response.headers.get_content_type()
    except (HTTPError, URLError, OSError) as exc:
        raise ChampionIconError(f"fall\u00f3 la descarga: {exc}") from exc


def atomic_write(path: Path, payload: bytes, *, output_dir: Path) -> None:
    if path.parent != output_dir:
        raise ChampionIconError(f"El destino no pertenece al directorio validado: {path}")
    safe_destination(output_dir, path.name)
    fd, temporary_name = tempfile.mkstemp(dir=output_dir, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        safe_destination(output_dir, path.name)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def inspected_png(path: Path) -> tuple[int, str, int, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ChampionIconError(f"No pude abrir el icono cacheado: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ChampionIconError("El icono cacheado no es un archivo regular")
        with os.fdopen(fd, "rb") as icon_file:
            fd = -1
            payload = icon_file.read()
    except OSError as exc:
        raise ChampionIconError(f"No pude leer el icono cacheado: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
    width, height = validate_png(payload)
    return len(payload), hashlib.sha256(payload).hexdigest(), width, height


Fetcher = Callable[[str], tuple[bytes, str | None]]


def cache_status(output_dir: Path, filename: str) -> str:
    """Inspect a cache entry without creating files or directories.

    ``missing`` is expected during an offline inventory and is not an error:
    the normal command can fetch it later.  Existing entries must be complete
    PNGs; a corrupt entry is an error because offline mode cannot repair it.
    """
    absolute = Path(os.path.abspath(os.fspath(output_dir)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if is_reparse_or_symlink(current):
            raise ChampionIconError(f"El directorio de salida no puede atravesar enlaces: {current}")
    if not absolute.exists():
        return "missing"
    icon_dir = safe_output_directory(absolute, create=False)
    destination = safe_destination(icon_dir, filename)
    if not destination.exists():
        return "missing"
    try:
        inspected_png(destination)
    except ChampionIconError:
        return "corrupt"
    return "valid"


def cache_entries(entries: Iterable[dict], output_dir: Path, *, fetcher: Fetcher = fetch_png) -> tuple[list[dict], int, int]:
    """Validate/reuse every cached PNG, downloading only missing or corrupt files."""
    icon_dir = safe_output_directory(output_dir, create=True)
    cached, reused, downloaded, errors = [], 0, 0, []
    for entry in entries:
        filename = entry["filename"]
        destination = safe_destination(icon_dir, filename)
        try:
            if destination.exists():
                try:
                    size, digest, width, height = inspected_png(destination)
                    reused += 1
                except ChampionIconError:
                    payload, content_type = fetcher(entry["url"])
                    if content_type and content_type.lower() != "image/png":
                        raise ChampionIconError(f"Content-Type inesperado: {content_type}")
                    width, height = validate_png(payload)
                    atomic_write(destination, payload, output_dir=icon_dir)
                    size, digest, downloaded = len(payload), hashlib.sha256(payload).hexdigest(), downloaded + 1
            else:
                payload, content_type = fetcher(entry["url"])
                if content_type and content_type.lower() != "image/png":
                    raise ChampionIconError(f"Content-Type inesperado: {content_type}")
                width, height = validate_png(payload)
                atomic_write(destination, payload, output_dir=icon_dir)
                size, digest, downloaded = len(payload), hashlib.sha256(payload).hexdigest(), downloaded + 1
            cached.append({key: value for key, value in entry.items() if key != "filename"} | {"bytes": size, "hash": digest, "sha256": digest, "width": width, "height": height})
        except (ChampionIconError, OSError) as exc:
            errors.append(f"{entry['name']} ({entry['id']}): {exc}")
    if errors:
        raise ChampionIconError("\n".join(errors))
    return cached, reused, downloaded


def write_manifest(manifest: dict, assets_dir: Path) -> None:
    root = safe_output_directory(assets_dir, create=True)
    destination = safe_destination(root, "manifest.json")
    atomic_write(destination, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), output_dir=root)


def extended_asset_entries(own_names: list[str], champions: dict[str, dict], items: dict[str, dict], cdragon_items: dict[str, dict], rune_paths: list[dict], version: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Resolve all scoped catalogs into validated, official URL/file entries."""
    version = validate_version(version)
    champions_resolved = resolve_extended_champions(own_names, champions)
    item_resolved = resolve_store_items(items, cdragon_items)
    runes_resolved = resolve_runes(rune_paths)
    champion_entries = [{**entry, "file": f"champion/{entry['id']}.png", "filename": f"{entry['id']}.png", "url": f"{DDRAGON_BASE}/cdn/{version}/img/champion/{entry['id']}.png"} for entry in champions_resolved]
    item_entries = [{**entry, "file": f"item/{entry['id']}.png", "filename": f"{entry['id']}.png", "url": f"{DDRAGON_BASE}/cdn/{version}/img/item/{entry['id']}.png"} for entry in item_resolved]
    rune_entries = [{**entry, "file": f"rune/{entry['id']}.png", "filename": f"{entry['id']}.png", "url": f"{DDRAGON_BASE}/cdn/img/{entry['icon_path']}"} for entry in runes_resolved]
    return champion_entries, item_entries, rune_entries


def dry_run_extended_icon_manifest(own_names: list[str], champions: dict[str, dict], items: dict[str, dict], cdragon_items: dict[str, dict], rune_paths: list[dict], version: str, assets_dir: Path) -> dict:
    """Resolve inventory and inspect existing cache only; never fetch or write.

    Missing entries are reported as planned downloads and intentionally return
    success when all catalogs resolve and existing cache entries are valid.
    """
    # Resolve and validate all URLs before querying the filesystem.
    champion_entries, item_entries, rune_entries = extended_asset_entries(own_names, champions, items, cdragon_items, rune_paths, version)
    sections = {"champions": (champion_entries, assets_dir / "champion"), "items": (item_entries, assets_dir / "item"), "runes": (rune_entries, assets_dir / "rune")}
    result: dict[str, dict[str, int]] = {}
    corrupt: list[str] = []
    for section, (entries, output_dir) in sections.items():
        counts = {"total": len(entries), "valid": 0, "missing": 0, "corrupt": 0}
        for entry in entries:
            status = cache_status(output_dir, entry["filename"])
            counts[status] += 1
            if status == "corrupt":
                corrupt.append(f"{section}/{entry['filename']}")
        result[section] = counts
    if corrupt:
        raise ChampionIconError("Cach\u00e9 PNG corrupta (dry-run no escribe ni descarga): " + ", ".join(corrupt))
    return result


def build_icon_manifest(champion_names: list[str], catalog: dict[str, dict], version: str, output_dir: Path, *, fetcher: Fetcher = fetch_png, dry_run: bool = False, generated_at: str | None = None) -> tuple[dict, int, int]:
    """Backward-compatible champion-only helper used by the original CLI/tests."""
    version = validate_version(version)
    resolved = resolve_champions(champion_names, catalog)
    champion_entries = [{"name": champion["name"], "champion_name": champion["champion_name"], "id": validate_champion_id(champion["id"]), "file": f"champion/{champion['id']}.png", "filename": f"{champion['id']}.png", "url": f"{DDRAGON_BASE}/cdn/{version}/img/champion/{champion['id']}.png"} for champion in resolved]
    if dry_run:
        for entry in champion_entries:
            print(f"resolver: {entry['name']} -> {entry['url']}")
        return {"version": version, "champions": []}, 0, 0
    entries, reused, downloaded = cache_entries(champion_entries, output_dir, fetcher=fetcher)
    manifest = {"version": version, "source_base": f"{DDRAGON_BASE}/cdn/{version}/img/champion/", "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "champions": entries}
    write_manifest(manifest, output_dir.parent)
    return manifest, reused, downloaded


def build_extended_icon_manifest(own_names: list[str], champions: dict[str, dict], items: dict[str, dict], cdragon_items: dict[str, dict], rune_paths: list[dict], version: str, assets_dir: Path, *, fetcher: Fetcher = fetch_png, generated_at: str | None = None) -> tuple[dict, int, int]:
    """Build the combined versioned manifest for champions, SR items, and runes."""
    version = validate_version(version)
    champion_dir = safe_output_directory(assets_dir / "champion", create=True)
    item_dir = safe_output_directory(assets_dir / "item", create=True)
    rune_dir = safe_output_directory(assets_dir / "rune", create=True)
    champion_entries, item_entries, rune_entries = extended_asset_entries(own_names, champions, items, cdragon_items, rune_paths, version)
    champion_cached, champion_reused, champion_downloaded = cache_entries(champion_entries, champion_dir, fetcher=fetcher)
    item_cached, item_reused, item_downloaded = cache_entries(item_entries, item_dir, fetcher=fetcher)
    rune_cached, rune_reused, rune_downloaded = cache_entries(rune_entries, rune_dir, fetcher=fetcher)
    champion_source = f"{DDRAGON_BASE}/cdn/{version}/img/champion/"
    manifest = {"version": version, "source_base": champion_source, "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "source_bases": {"champion": champion_source, "item": f"{DDRAGON_BASE}/cdn/{version}/img/item/", "rune": f"{DDRAGON_BASE}/cdn/img/"}, "champions": champion_cached, "items": item_cached, "runes": rune_cached}
    write_manifest(manifest, assets_dir)
    return manifest, champion_reused + item_reused + rune_reused, champion_downloaded + item_downloaded + rune_downloaded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--db", type=Path, help="Sobrescribe la DB configurada")
    parser.add_argument("--game-data-dir", type=Path, default=ROOT / "data" / "game_data")
    parser.add_argument("--assets-dir", type=Path, help="Destino versionado con champion/, item/, rune/ y manifest.json")
    parser.add_argument("--dry-run", action="store_true", help="Resuelve inventarios e inspecciona la cach\u00e9 sin red ni escrituras; los faltantes se reportan y no fallan")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        version, champions, items, cdragon_items, rune_paths = load_asset_catalogs(args.game_data_dir)
        own_names = own_champion_names(args.db or load_configured_db_path(args.config))
        if not own_names:
            raise ChampionIconError("No hay campeones propios en la base de datos")
        assets_dir = args.assets_dir or ROOT / "scripts" / "assets" / "ddragon" / version
        if args.dry_run:
            status = dry_run_extended_icon_manifest(own_names, champions, items, cdragon_items, rune_paths, version, assets_dir)
            counts = {section: value["total"] for section, value in status.items()}
            valid = sum(value["valid"] for value in status.values())
            missing = sum(value["missing"] for value in status.values())
            print(f"Dry run: {counts['champions']} campeones, {counts['items']} objetos, {counts['runes']} runas; {valid} PNG v\u00e1lidos en cach\u00e9, {missing} faltantes (no se descargan).")
            return 0
        manifest, reused, downloaded = build_extended_icon_manifest(own_names, champions, items, cdragon_items, rune_paths, version, assets_dir)
    except ChampionIconError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    total_bytes = sum(entry["bytes"] for section in ("champions", "items", "runes") for entry in manifest[section])
    print(f"Listo: {len(manifest['champions'])} campeones, {len(manifest['items'])} objetos, {len(manifest['runes'])} runas; {reused} reutilizados, {downloaded} descargados; {total_bytes} bytes.")
    print(f"Manifest: {assets_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
