"""Ruta estatica /assets/ y resolucion de retratos.

La ruta sirve PNG de una carpeta versionada. Lo que se prueba aqui no es que
sirva un archivo -- es que NO sirva nada fuera de esa carpeta: un '..' en la URL
leeria cualquier cosa del disco, y el dashboard corre en la maquina del jugador.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
try:
    import dashboard  # noqa: E402
except Exception:
    # _Handler below subclasses dashboard.Handler at import time, so the
    # skip has to happen at module level.
    pytest.skip("requiere config.toml (dashboard carga config al importar)",
                allow_module_level=True)


@pytest.fixture
def assets(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    champ = root / "ddragon" / dashboard.DDRAGON_VERSION / "champion"
    champ.mkdir(parents=True)
    (champ / "Belveth.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    (root / "ddragon" / dashboard.DDRAGON_VERSION / "manifest.json").write_text(
        json.dumps({"champions": [
            {"champion_name": "Bel'Veth", "id": "Belveth", "file": "champion/Belveth.png"},
        ]}), encoding="utf-8")
    (tmp_path / "secreto.txt").write_text("no debe salir", encoding="utf-8")
    monkeypatch.setattr(dashboard, "ASSETS_ROOT", root.resolve())
    dashboard._champion_icon_index.cache_clear()
    yield root
    dashboard._champion_icon_index.cache_clear()


def test_icono_se_resuelve_por_id_canonico_no_por_nombre_visible(assets):
    # 'Bel'Veth' no es un filename: el apostrofe y la mayuscula interna no existen
    # en la CDN. El mapeo tiene que salir del manifiesto, no del nombre.
    url = dashboard.champion_icon_url("Bel'Veth")
    assert url == f"/assets/ddragon/{dashboard.DDRAGON_VERSION}/champion/Belveth.png"


def test_campeon_sin_retrato_no_rompe(assets):
    assert dashboard.champion_icon_url("Sona") is None
    assert dashboard.champion_icon_url(None) is None


def test_sin_manifiesto_el_indice_queda_vacio(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "ASSETS_ROOT", (tmp_path / "vacio").resolve())
    dashboard._champion_icon_index.cache_clear()
    try:
        assert dashboard.champion_icon_url("Sona") is None
    finally:
        dashboard._champion_icon_index.cache_clear()


class _Handler(dashboard.Handler):
    """Handler sin socket: captura lo que se habria escrito."""

    def __init__(self):
        self.status = None
        self.headers_sent = {}
        self.body = b""

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_sent[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        handler = self

        class _W:
            @staticmethod
            def write(data):
                handler.body += data
        return _W()

    def _json(self, payload, code=200):
        self.status = code
        self.body = json.dumps(payload).encode()


def test_sirve_png_dentro_de_la_raiz(assets):
    h = _Handler()
    h._serve_asset(f"ddragon/{dashboard.DDRAGON_VERSION}/champion/Belveth.png")
    assert h.status == 200
    assert h.headers_sent["Content-Type"] == "image/png"
    assert h.body.startswith(b"\x89PNG")


@pytest.mark.parametrize("rel", [
    "../secreto.txt",
    "../../secreto.txt",
    "ddragon/../../secreto.txt",
    "../secreto.png",                         # extension valida, fuera de la raiz
    "ddragon/16.14.1/champion/../../../../secreto.png",
])
def test_rechaza_salir_de_la_raiz(assets, rel):
    h = _Handler()
    h._serve_asset(rel)
    assert h.status == 404
    assert b"no debe salir" not in h.body


def test_rechaza_lo_que_no_sea_png(assets):
    h = _Handler()
    h._serve_asset(f"ddragon/{dashboard.DDRAGON_VERSION}/manifest.json")
    assert h.status == 404


def test_rechaza_separador_de_windows(assets):
    h = _Handler()
    h._serve_asset("ddragon\\..\\..\\secreto.png")
    assert h.status == 404
