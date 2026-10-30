"""Linhas de ônibus que passam perto de um imóvel, a partir do GTFS oficial da BHTRANS.

O índice (pontos com coordenadas + linhas de cada ponto) vem, nesta ordem:
1. do banco (tabela dados_app, chave "onibus_bh"), quando um admin enviou GTFS mais novo pelo painel;
2. do arquivo app/data/onibus_bh.json que acompanha o app (versão inicial).

Feeds municipais (convencional e suplementar). As linhas metropolitanas (DER-MG) não têm GTFS público.
"""
import csv
import io
import json
import zipfile
from collections import defaultdict
from datetime import datetime
from math import cos, radians
from pathlib import Path

from .geo import haversine_km

ARQUIVO = Path(__file__).resolve().parent / "data" / "onibus_bh.json"
CHAVE_DB = "onibus_bh"
_CELULA = 0.005  # graus (~550 m): a busca olha a célula do ponto e as vizinhas

_dados: dict | None = None
_origem: dict = {}
_grade: dict[tuple[int, int], list] = defaultdict(list)


# --- montar o índice a partir dos .zip GTFS ------------------------------------------------------

def montar_indice(feeds: dict[str, bytes]) -> dict:
    """{"convencional": bytes do zip, "suplementar": bytes do zip (opcional)} -> índice compacto.

    Lê o stop_times em fluxo (o do convencional passa de 150 MB descompactado)."""
    linhas, idx_linha, pontos, info = [], {}, {}, []
    for sistema, conteudo in feeds.items():
        try:
            z = zipfile.ZipFile(io.BytesIO(conteudo))
        except zipfile.BadZipFile as e:
            raise ValueError(f"o arquivo do sistema {sistema} não é um .zip válido") from e
        faltando = {"routes.txt", "trips.txt", "stops.txt", "stop_times.txt"} - set(z.namelist())
        if faltando:
            raise ValueError(f"o arquivo do sistema {sistema} não parece GTFS (faltam {', '.join(sorted(faltando))})")

        def ler(nome):
            return csv.DictReader(io.TextIOWrapper(z.open(nome), encoding="utf-8-sig"))

        fi = next(ler("feed_info.txt"), {}) if "feed_info.txt" in z.namelist() else {}
        ag = next(ler("agency.txt"), {}) if "agency.txt" in z.namelist() else {}
        info.append({"sistema": sistema, "agencia": ag.get("agency_name"), "publicador": fi.get("feed_publisher_name"),
                     "validade": [fi.get("feed_start_date"), fi.get("feed_end_date")], "versao": fi.get("feed_version")})
        rota_idx = {}
        for r in ler("routes.txt"):
            chave = (sistema, r.get("route_short_name", "").strip(), r.get("route_long_name", "").strip())
            if chave not in idx_linha:
                idx_linha[chave] = len(linhas)
                linhas.append([chave[1], chave[2].title(), sistema])
            rota_idx[r["route_id"]] = idx_linha[chave]
        viagem = {t["trip_id"]: rota_idx[t["route_id"]] for t in ler("trips.txt") if t["route_id"] in rota_idx}
        coords = {s["stop_id"]: (round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6))
                  for s in ler("stops.txt") if s.get("stop_lat") and s.get("stop_lon")}
        por_ponto = defaultdict(set)
        for st in ler("stop_times.txt"):
            if st["trip_id"] in viagem:
                por_ponto[st["stop_id"]].add(viagem[st["trip_id"]])
        for stop_id, rs in por_ponto.items():
            if stop_id in coords:
                pontos.setdefault(coords[stop_id], set()).update(rs)

    if len(pontos) < 1000 or len(linhas) < 50:
        raise ValueError(f"base pequena demais ({len(linhas)} linhas, {len(pontos)} pontos): confira se é o GTFS de BH")
    return {"fonte": "GTFS estático BHTRANS (" + " + ".join(feeds) + ")", "feeds": info, "linhas": linhas,
            "pontos": [[lat, lon, sorted(rs)] for (lat, lon), rs in sorted(pontos.items())],
            "gerado_em": datetime.utcnow().isoformat()}


# --- carregar / trocar a base em uso -------------------------------------------------------------

def _usar(dados: dict, origem: dict) -> None:
    global _dados, _origem
    _grade.clear()
    for p in dados["pontos"]:
        _grade[(int(p[0] // _CELULA), int(p[1] // _CELULA))].append(p)
    _dados, _origem = dados, origem


def _carregar() -> dict:
    if _dados is None:
        from .db import SessionLocal
        from .models import DadoApp
        try:
            with SessionLocal() as db:
                row = db.get(DadoApp, CHAVE_DB)
                if row:
                    _usar(json.loads(row.valor), {"tipo": "enviada pelo admin", "em": row.atualizado_em.isoformat(),
                                                  "por": row.atualizado_por})
                    return _dados
        except Exception:  # tabela ainda não existe (antes da 1ª inicialização): usa o arquivo
            pass
        dados = json.loads(ARQUIVO.read_text(encoding="utf-8")) if ARQUIVO.exists() else {"linhas": [], "pontos": []}
        _usar(dados, {"tipo": "arquivo original do app"})
    return _dados


def recarregar() -> None:
    """Esquece a base em memória; a próxima consulta lê de novo (banco ou arquivo)."""
    global _dados
    _dados = None


def disponivel() -> bool:
    return bool(_carregar()["pontos"])


def info() -> dict:
    d = _carregar()
    return {"fonte": d.get("fonte"), "feeds": d.get("feeds", []), "linhas": len(d["linhas"]),
            "pontos": len(d["pontos"]), "origem": _origem}


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
