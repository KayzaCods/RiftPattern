"""El caché de PUUID no puede sobrevivir a una rotación de API key.

Riot cifra los PUUID POR key. Al renovarla el 6-ago los guardados dejaron de ser
descifrables y match-v5 empezó a contestar
`400 Bad Request - Exception decrypting <puuid>`. `resolve_all` solo llamaba a la
API `if key not in self._by_riot_id`, así que `data/puuids.json` era un caché
permanente que sobrevivía justo al evento que lo invalida: nueve días de ingesta
parada en silencio, con el banner diciendo que la key estaba viva.

Lo que se fija aquí es que re-resolver es el comportamiento por defecto, y que el
caché sigue sirviendo de respaldo cuando la API no responde — un run sin red no
debe quedarse sin PUUID.
"""
from __future__ import annotations

import json

import pytest

from lol_coach.accounts import AccountRegistry
from lol_coach.riot_api import RiotAPIError

CUENTAS = [{"game_name": "Jugador", "tag_line": "0001"}]
RIOT_ID = "Jugador#0001"


class ApiFalsa:
    """Devuelve el PUUID que le digas y cuenta las llamadas."""

    def __init__(self, puuid="nuevo-puuid", falla=False):
        self.puuid = puuid
        self.falla = falla
        self.llamadas = 0

    def account_by_riot_id(self, game_name, tag_line):
        self.llamadas += 1
        if self.falla:
            raise RiotAPIError("HTTP 403 Forbidden")
        return {"puuid": self.puuid}


def _cache(tmp_path, contenido=None):
    p = tmp_path / "puuids.json"
    if contenido is not None:
        p.write_text(json.dumps(contenido), encoding="utf-8")
    return p


def test_a_stale_cached_puuid_is_replaced(tmp_path):
    """El caso del 14-ago: el guardado es de la key vieja y ya no descifra."""
    ruta = _cache(tmp_path, {RIOT_ID: "puuid-de-la-key-vieja"})
    api = ApiFalsa(puuid="puuid-de-la-key-nueva")

    out = AccountRegistry(CUENTAS, ruta, api).resolve_all()

    assert out[RIOT_ID] == "puuid-de-la-key-nueva"
    assert json.loads(ruta.read_text(encoding="utf-8"))[RIOT_ID] == "puuid-de-la-key-nueva"


def test_it_asks_riot_even_when_the_cache_already_has_an_entry(tmp_path):
    """La regresión concreta: antes bastaba con que la clave existiera para no
    volver a preguntar nunca."""
    api = ApiFalsa(puuid="mismo")
    AccountRegistry(CUENTAS, _cache(tmp_path, {RIOT_ID: "mismo"}), api).resolve_all()

    assert api.llamadas == 1


def test_an_unchanged_puuid_does_not_rewrite_the_file(tmp_path):
    ruta = _cache(tmp_path, {RIOT_ID: "mismo"})
    antes = ruta.stat().st_mtime_ns
    AccountRegistry(CUENTAS, ruta, ApiFalsa(puuid="mismo")).resolve_all()

    assert ruta.stat().st_mtime_ns == antes


def test_the_cache_is_the_fallback_when_riot_does_not_answer(tmp_path):
    """Un run sin red o con rate-limit conserva el último valor conocido: quedarse
    sin PUUID pararia la ingesta por un motivo distinto del que arreglamos."""
    ruta = _cache(tmp_path, {RIOT_ID: "ultimo-conocido"})

    out = AccountRegistry(CUENTAS, ruta, ApiFalsa(falla=True)).resolve_all()

    assert out[RIOT_ID] == "ultimo-conocido"


def test_without_cache_a_failure_still_raises(tmp_path):
    """Sin respaldo no hay nada que conservar: el fallo tiene que verse."""
    with pytest.raises(RiotAPIError):
        AccountRegistry(CUENTAS, _cache(tmp_path), ApiFalsa(falla=True)).resolve_all()


def test_verify_false_keeps_the_old_cheap_behaviour(tmp_path):
    """La ruta barata sigue disponible para quien la quiera explicitamente."""
    api = ApiFalsa(puuid="nuevo")
    out = AccountRegistry(CUENTAS, _cache(tmp_path, {RIOT_ID: "viejo"}), api).resolve_all(verify=False)

    assert api.llamadas == 0
    assert out[RIOT_ID] == "viejo"


def test_a_missing_account_is_resolved_regardless_of_verify(tmp_path):
    api = ApiFalsa(puuid="recien-resuelto")
    out = AccountRegistry(CUENTAS, _cache(tmp_path, {}), api).resolve_all(verify=False)

    assert out[RIOT_ID] == "recien-resuelto"
    assert api.llamadas == 1
