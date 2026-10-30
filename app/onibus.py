"""Linhas de ônibus que passam perto de um imóvel, a partir do GTFS oficial da BHTRANS.

O índice (app/data/onibus_bh.json) é gerado offline a partir dos feeds convencional e suplementar
(municipais). As linhas metropolitanas (DER-MG) não têm GTFS público e não entram aqui.
"""
import json
from collections import defaultdict
from math import cos, radians
from pathlib import Path

from .geo import haversine_km

_ARQUIVO = Path(__file__).resolve().parent / "data" / "onibus_bh.json"
_CELULA = 0.005  # graus (~550 m): a busca olha a célula do ponto e as vizinhas

_dados: dict | None = None
_grade: dict[tuple[int, int], list] = defaultdict(list)


def _carregar() -> dict:
    global _dados
    if _dados is None:
        _dados = json.loads(_ARQUIVO.read_text(encoding="utf-8")) if _ARQUIVO.exists() else {"linhas": [], "pontos": []}
        for p in _dados["pontos"]:
            _grade[(int(p[0] // _CELULA), int(p[1] // _CELULA))].append(p)
    return _dados


def disponivel() -> bool:
    return bool(_carregar()["pontos"])


def info() -> dict:
    d = _carregar()
    return {"fonte": d.get("fonte"), "feeds": d.get("feeds", []), "linhas": len(d["linhas"]), "pontos": len(d["pontos"])}


def linhas_proximas(lat: float, lon: float, raio_m: int = 500) -> dict | None:
    """Pontos a até `raio_m` metros e as linhas que passam neles, mais próximas primeiro."""
    d = _carregar()
    if not d["pontos"]:
        return None
    raio_km = raio_m / 1000
    alcance = int(raio_km / (111 * _CELULA * min(1, cos(radians(lat))))) + 1
    ci, cj = int(lat // _CELULA), int(lon // _CELULA)
    pontos = []
    for i in range(ci - alcance, ci + alcance + 1):
        for j in range(cj - alcance, cj + alcance + 1):
            for p in _grade.get((i, j), ()):
                dist = haversine_km(lat, lon, p[0], p[1])
                if dist <= raio_km:
                    pontos.append((dist, p))
    pontos.sort(key=lambda x: x[0])
    linhas: dict[int, float] = {}
    for dist, p in pontos:
        for idx in p[2]:
            linhas.setdefault(idx, dist)  # distância até o ponto mais perto onde a linha passa
    return {
        "raio_m": raio_m,
        "pontos": len(pontos),
        "ponto_mais_proximo_m": round(pontos[0][0] * 1000) if pontos else None,
        "linhas": [{"numero": d["linhas"][i][0], "nome": d["linhas"][i][1], "sistema": d["linhas"][i][2],
                    "distancia_m": round(dist * 1000)}
                   for i, dist in sorted(linhas.items(), key=lambda x: (x[1], d["linhas"][x[0]][0]))],
    }
