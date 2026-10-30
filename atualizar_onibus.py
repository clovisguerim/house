r"""Regenera o arquivo app/data/onibus_bh.json (base de ônibus que acompanha o app) a partir do GTFS da BHTRANS.

No site, o jeito mais simples é o painel de admin (aba Usuários > Base de ônibus), que guarda a base
no banco. Este script serve para atualizar o arquivo do repositório.

Baixe no navegador os "gtfsfiles.zip" mais recentes em dados.pbh.gov.br ("GTFS estático do sistema
convencional" e, se quiser, "... do sistema suplementar") e rode:

    .venv\Scripts\python.exe atualizar_onibus.py convencional.zip [suplementar.zip]
"""
import json
import sys
from pathlib import Path

from app.onibus import ARQUIVO, montar_indice

if len(sys.argv) < 2:
    sys.exit(__doc__)
feeds = {"convencional": Path(sys.argv[1]).read_bytes()}
if len(sys.argv) > 2:
    feeds["suplementar"] = Path(sys.argv[2]).read_bytes()
indice = montar_indice(feeds)
ARQUIVO.write_text(json.dumps(indice, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(json.dumps(indice["feeds"], ensure_ascii=False, indent=1))
print(f"{len(indice['linhas'])} linhas, {len(indice['pontos'])} pontos -> {ARQUIVO}")
