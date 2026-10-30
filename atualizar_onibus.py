r"""Atualiza app/data/onibus_bh.json (pontos de ônibus e linhas de BH) a partir do GTFS oficial da BHTRANS.

Baixe no navegador os dois "gtfsfiles.zip" mais recentes em dados.pbh.gov.br
("GTFS estático do sistema convencional" e "... do sistema suplementar"), salve numa pasta como
bhtrans-gtfs-9.zip (convencional) e bhtrans-gtfs-687.zip (suplementar) e rode:

    .venv\Scripts\python.exe atualizar_onibus.py C:\caminho\da\pasta

(Os nomes 9/687 são os ids desses feeds no catálogo MobilityData, de onde veio a versão inicial.)
"""
import csv, io, json, sys, zipfile
from collections import defaultdict

pasta = sys.argv[1]
destino = sys.argv[2] if len(sys.argv) > 2 else "app/data/onibus_bh.json"
linhas, idx_linha, pontos = [], {}, {}
info = []
for arquivo, sistema in (("bhtrans-gtfs-9.zip", "convencional"), ("bhtrans-gtfs-687.zip", "suplementar")):
    z = zipfile.ZipFile(f"{pasta}/{arquivo}")
    ler = lambda nome: csv.DictReader(io.TextIOWrapper(z.open(nome), encoding="utf-8-sig"))
    fi = next(ler("feed_info.txt"), {})
    ag = next(ler("agency.txt"), {})
    info.append({"sistema": sistema, "agencia": ag.get("agency_name"), "publicador": fi.get("feed_publisher_name"),
                 "validade": [fi.get("feed_start_date"), fi.get("feed_end_date")], "versao": fi.get("feed_version")})
    rota_idx = {}
    for r in ler("routes.txt"):
        chave = (sistema, r["route_short_name"].strip(), r["route_long_name"].strip())
        if chave not in idx_linha:
            idx_linha[chave] = len(linhas)
            linhas.append([r["route_short_name"].strip(), r["route_long_name"].strip().title(), sistema])
        rota_idx[r["route_id"]] = idx_linha[chave]
    viagem = {t["trip_id"]: rota_idx[t["route_id"]] for t in ler("trips.txt") if t["route_id"] in rota_idx}
    coords = {s["stop_id"]: (round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6)) for s in ler("stops.txt")}
    por_ponto = defaultdict(set)
    for st in ler("stop_times.txt"):
        if st["trip_id"] in viagem:
            por_ponto[st["stop_id"]].add(viagem[st["trip_id"]])
    for stop_id, rs in por_ponto.items():
        if stop_id in coords:
            pontos.setdefault(coords[stop_id], set()).update(rs)

saida = {"fonte": "GTFS estático BHTRANS (convencional + suplementar), cópia MobilityData mdb-9/mdb-687",
         "feeds": info, "linhas": linhas,
         "pontos": [[lat, lon, sorted(rs)] for (lat, lon), rs in sorted(pontos.items())]}
json.dump(saida, open(destino, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(json.dumps(info, ensure_ascii=False, indent=1))
print("linhas:", len(linhas), "| pontos:", len(pontos))
