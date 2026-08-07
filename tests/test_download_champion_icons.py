from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct

import pytest

from scripts import download_champion_icons as icons


def png(width: int = 120, height: int = 120) -> bytes:
    return icons.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


def test_own_inventory_only_returns_our_participants(tmp_path):
    db_path = tmp_path / "coach.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE matches (match_id TEXT, our_puuid TEXT);
            CREATE TABLE participants (match_id TEXT, puuid TEXT, champion_name TEXT);
        """)
        conn.executemany("INSERT INTO matches VALUES (?, ?)", [("one", "ours"), ("two", "ours")])
        conn.executemany("INSERT INTO participants VALUES (?, ?, ?)", [
            ("one", "ours", "Sona"), ("one", "enemy", "Bel'Veth"),
            ("two", "ours", "Sona"), ("two", "ours", "Leona"),
        ])
    assert icons.own_champion_names(db_path) == ["Leona", "Sona"]


def test_resolve_uses_catalog_id_not_display_name():
    catalog = {"Belveth": {"id": "Belveth", "name": "Bel'Veth"}}
    assert icons.resolve_champions(["Belveth"], catalog) == [
        {"champion_name": "Belveth", "id": "Belveth", "name": "Bel'Veth"}
    ]


def test_invalid_png_is_rejected(tmp_path):
    with pytest.raises(icons.ChampionIconError, match="firma PNG"):
        icons.build_icon_manifest(
            ["Sona"], {"Sona": {"id": "Sona", "name": "Sona"}}, "16.14.1",
            tmp_path / "champion", fetcher=lambda _url: (b"not a png", "image/png"),
        )
    assert not (tmp_path / "manifest.json").exists()


def test_valid_png_writes_manifest_and_reuses_existing_file(tmp_path):
    output = tmp_path / "champion"
    catalog = {"Sona": {"id": "Sona", "name": "Sona"}}
    calls: list[str] = []

    def fake_fetcher(url):
        calls.append(url)
        return png(120, 90), "image/png"

    manifest, reused, downloaded = icons.build_icon_manifest(
        ["Sona"], catalog, "16.14.1", output, fetcher=fake_fetcher,
        generated_at="2026-08-05T00:00:00Z",
    )
    entry = manifest["champions"][0]
    assert (reused, downloaded, calls) == (0, 1, [entry["url"]])
    assert entry["id"] == "Sona"
    assert entry["file"] == "champion/Sona.png"
    assert entry["width"] == 120 and entry["height"] == 90
    assert entry["sha256"] == hashlib.sha256(png(120, 90)).hexdigest()
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["version"] == "16.14.1"

    second, reused, downloaded = icons.build_icon_manifest(
        ["Sona"], catalog, "16.14.1", output,
        fetcher=lambda _url: pytest.fail("no debe descargar un PNG válido existente"),
    )
    assert (reused, downloaded) == (1, 0)
    assert second["champions"][0]["sha256"] == entry["sha256"]


def test_invalid_cached_png_is_replaced_only_with_a_valid_download(tmp_path):
    output = tmp_path / "champion"
    output.mkdir()
    (output / "Sona.png").write_bytes(b"partial")
    manifest, reused, downloaded = icons.build_icon_manifest(
        ["Sona"], {"Sona": {"id": "Sona", "name": "Sona"}}, "16.14.1", output,
        fetcher=lambda _url: (png(), "image/png"),
    )
    assert (reused, downloaded) == (0, 1)
    assert manifest["champions"][0]["width"] == 120


@pytest.mark.parametrize("champion_id", [
    "../escaped", "Sona/escaped", r"Sona\\escaped", "Sona?url=https://evil.test", "Sona#fragment",
])
def test_catalog_id_rejects_path_and_url_injection(tmp_path, champion_id):
    with pytest.raises(icons.ChampionIconError, match="ID de Data Dragon inv"):
        icons.build_icon_manifest(
            ["Sona"], {"Sona": {"id": champion_id, "name": "Sona"}}, "16.14.1",
            tmp_path / "champion", fetcher=lambda _url: pytest.fail("no debe descargar"),
        )
    assert not (tmp_path / "escaped.png").exists()


def test_existing_icon_symlink_is_rejected_without_following(tmp_path):
    output = tmp_path / "champion"
    output.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"no modificar")
    link = output / "Sona.png"
    try:
        os.symlink(outside, link)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks no disponibles en esta plataforma: {exc}")

    with pytest.raises(icons.ChampionIconError, match="enlace"):
        icons.build_icon_manifest(
            ["Sona"], {"Sona": {"id": "Sona", "name": "Sona"}}, "16.14.1", output,
            fetcher=lambda _url: pytest.fail("no debe descargar ni leer el enlace"),
        )
    assert outside.read_bytes() == b"no modificar"


def test_symlinked_output_directory_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "champion"
    try:
        os.symlink(outside, output, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks no disponibles en esta plataforma: {exc}")

    with pytest.raises(icons.ChampionIconError, match="enlaces"):
        icons.build_icon_manifest(
            ["Sona"], {"Sona": {"id": "Sona", "name": "Sona"}}, "16.14.1", output,
            fetcher=lambda _url: pytest.fail("no debe descargar en un directorio enlazado"),
        )
    assert list(outside.iterdir()) == []


def test_current_catalog_inventory_counts_are_the_scoped_assets():
    game_data = icons.ROOT / "data" / "game_data"
    version, champions, items, cdragon_items, rune_paths = icons.load_asset_catalogs(game_data)
    own = ["Alistar", "Ashe", "Belveth", "Braum", "Diana", "Ekko", "Garen", "Janna", "Leona", "Maokai", "Milio", "Mordekaiser", "Nautilus", "Rakan", "Shen", "Sona", "Soraka"]
    assert version == "16.14.1"
    assert len(icons.resolve_extended_champions(own, champions)) == 49
    assert len(icons.resolve_store_items(items, cdragon_items)) == 254
    runes = icons.resolve_runes(rune_paths)
    assert len(runes) == 62
    assert sum(rune["group"] == "keystone" for rune in runes) == 17
    assert {rune["tree"] for rune in runes} == {"Domination", "Inspiration", "Precision", "Resolve", "Sorcery"}


def test_store_item_filter_requires_sr_purchasable_and_cdragon_store():
    items = {
        "1001": {"name": "Correct", "maps": {"11": True}, "gold": {"purchasable": True}},
        "1002": {"name": "Not purchasable", "maps": {"11": True}, "gold": {"purchasable": False}},
        "1003": {"name": "Not SR", "maps": {"12": True}, "gold": {"purchasable": True}},
        "1004": {"name": "Not in store", "maps": {"11": True}, "gold": {"purchasable": True}},
    }
    cdragon = {"1001": {"inStore": True}, "1002": {"inStore": True}, "1003": {"inStore": True}, "1004": {"inStore": False}}
    assert icons.resolve_store_items(items, cdragon) == [{"id": "1001", "name": "Correct", "group": "summoners-rift"}]


@pytest.mark.parametrize("path", ["../outside.png", "/absolute.png", r"perk-images\\escape.png", "perk-images/../escape.png", "perk-images/a.png?x=1", "perk-images/a.png#fragment", "perk-images//a.png"])
def test_rune_icon_path_rejects_traversal_and_url_parts(path):
    with pytest.raises(icons.ChampionIconError, match="Ruta de icono de runa inv"):
        icons.validate_rune_icon_path(path)


def test_extended_manifest_records_sections_and_repairs_corrupt_cache(tmp_path):
    assets = tmp_path / "16.14.1"
    (assets / "item").mkdir(parents=True)
    (assets / "item" / "1001.png").write_bytes(b"corrupt")
    champions = {"Sona": {"id": "Sona", "name": "Sona", "tags": ["Support"]}}
    items = {"1001": {"name": "Boots", "maps": {"11": True}, "gold": {"purchasable": True}}}
    cdragon = {"1001": {"inStore": True}}
    paths = [
        {"key": key, "slots": [{"runes": [{"id": index + 1, "name": f"{key} key", "icon": f"perk-images/{key}.png"}]}]}
        for index, key in enumerate(["Domination", "Inspiration", "Precision", "Resolve", "Sorcery"])
    ]
    calls = []

    def fake_fetcher(url):
        calls.append(url)
        return png(32, 32), "image/png"

    manifest, reused, downloaded = icons.build_extended_icon_manifest(
        ["Sona"], champions, items, cdragon, paths, "16.14.1", assets,
        fetcher=fake_fetcher, generated_at="2026-08-05T00:00:00Z",
    )
    assert (len(manifest["champions"]), len(manifest["items"]), len(manifest["runes"])) == (1, 1, 5)
    assert (reused, downloaded) == (0, 7)
    assert manifest["items"][0]["hash"] == manifest["items"][0]["sha256"]
    assert manifest["runes"][0]["tree"] == "Domination"
    assert json.loads((assets / "manifest.json").read_text(encoding="utf-8"))["version"] == "16.14.1"

    _, reused, downloaded = icons.build_extended_icon_manifest(
        ["Sona"], champions, items, cdragon, paths, "16.14.1", assets,
        fetcher=lambda _url: pytest.fail("no debe descargar con una cach\u00e9 v\u00e1lida"),
    )
    assert (reused, downloaded) == (7, 0)


def test_cli_dry_run_resolves_without_network_or_writes(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "coach.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE matches (match_id TEXT, our_puuid TEXT);
            CREATE TABLE participants (match_id TEXT, puuid TEXT, champion_name TEXT);
            INSERT INTO matches VALUES ('one', 'ours');
            INSERT INTO participants VALUES ('one', 'ours', 'Sona');
        """)
    assets = tmp_path / "offline-assets"
    monkeypatch.setattr(icons, "fetch_png", lambda _url: pytest.fail("dry-run no debe usar red"))

    assert icons.main([
        "--db", str(db_path),
        "--game-data-dir", str(icons.ROOT / "data" / "game_data"),
        "--assets-dir", str(assets),
        "--dry-run",
    ]) == 0
    output = capsys.readouterr().out
    assert "Dry run:" in output
    assert "faltantes (no se descargan)" in output
    assert not assets.exists()
