"""Roda uma busca neste PC e publica o resultado no histórico do site (banco do Render/Neon).

Útil porque alguns portais (ex.: Netimóveis) bloqueiam o servidor na nuvem, mas respondem aqui.
A busca usa o cache e o banco locais; só o resultado final vai para o banco do site.

Uso (na pasta do projeto, com a .venv):
    .venv\\Scripts\\python.exe publicar_busca.py "casa 2 quartos 7 km do gloria até 2500"
    .venv\\Scripts\\python.exe publicar_busca.py "..." --usuario manucfe --relatorio 5

O banco do site vem de HOUSE_DESTINO_DATABASE_URL ou da linha DATABASE_URL do .env.local.
Para usar IA aqui, defina OPENAI_API_KEY (ou ANTHROPIC_API_KEY) antes de rodar.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent


def carregar_chaves_ia() -> None:
    """Lê OPENAI_API_KEY / ANTHROPIC_API_KEY do .env.local (se não estiverem no ambiente), para não
    precisar digitar a chave na linha de comando, onde ela ficaria no histórico do shell."""
    env = PASTA / ".env.local"
    if not env.exists():
        return
    for linha in env.read_text(encoding="utf-8").splitlines():
        nome, _, valor = linha.partition("=")
        if nome.strip() in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") and valor.strip() and not os.getenv(nome.strip()):
            os.environ[nome.strip()] = valor.strip().strip('"').strip("'")


def url_do_site() -> str:
    if url := os.getenv("HOUSE_DESTINO_DATABASE_URL"):
        return url
    env = PASTA / ".env.local"
    if env.exists():
        for linha in env.read_text(encoding="utf-8").splitlines():
            if linha.startswith("DATABASE_URL="):
                return linha.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("Banco do site não encontrado: defina HOUSE_DESTINO_DATABASE_URL ou crie o .env.local (neon init).")


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texto", help="o pedido, como você digitaria no site")
    ap.add_argument("--usuario", default="clovisguerim", help="dono da busca no site (padrão: clovisguerim)")
    ap.add_argument("--fontes", nargs="*", help="limitar fontes (ex.: netimoveis quintoandar); padrão: todas")
    ap.add_argument("--max-detalhes", type=int, default=200)
    ap.add_argument("--sem-ia", action="store_true", help="não ler os anúncios com IA")
    ap.add_argument("--relatorio", type=int, metavar="N", help="gera também o relatório da IA com os N melhores")
    ap.add_argument("--pdf", metavar="ARQUIVO", help="salva também um PDF (ex.: output/busca.pdf)")
    ap.add_argument("--pdf-top", type=int, default=30, metavar="N", help="quantos imóveis listar no PDF (padrão 30)")
    ap.add_argument("--nao-publicar", action="store_true", help="não grava no site (só PDF/saída na tela)")
    args = ap.parse_args()

    # A busca roda com o banco/cache locais (padrão do app); o destino é um engine separado
    os.chdir(PASTA)
    carregar_chaves_ia()  # antes de importar o app, que decide a IA pelas variáveis de ambiente
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app import llm
    from app.config import normalizar_url_db
    from app.db import Base, engine as engine_local
    from app.models import SearchHistory, User
    from app.routers.regras import regras_para
    from app.routers.search import executar_busca
    from app.schemas import SearchRequest

    Base.metadata.create_all(engine_local)  # cache local pode ainda não existir
    destino = None
    regras: list[str] = []
    if not args.nao_publicar:
        destino = create_engine(normalizar_url_db(url_do_site()), pool_pre_ping=True)
        with Session(destino) as db:
            user = db.query(User).filter(User.username == args.usuario).first()
            if not user:
                sys.exit(f"Usuário '{args.usuario}' não existe no site.")
            user_id = user.id
            regras = regras_para(db, user)  # as mesmas regras da IA que valem no site para esse usuário

    print(f"Buscando: {args.texto}")
    print(f"IA: {'ligada (' + llm.provedor() + ')' if llm.ativa() else 'desligada (regras)'}")
    if regras:
        print(f"Regras da IA: {len(regras)} (do site)")
    resposta = await executar_busca(SearchRequest(texto=args.texto, fontes=args.fontes,
                                                  max_detalhes=args.max_detalhes, usar_ia=not args.sem_ia), regras)
    for st in resposta.fontes:
        print(f"  {st.fonte:<12} {'ok ' if st.ok else 'ERRO'} {st.encontrados:>4}  {st.mensagem or ''}")
    print(f"Total: {resposta.total} imóveis")

    relatorio = None
    if args.relatorio and resposta.imoveis:
        if not llm.ativa():
            print("Relatório pulado: IA desligada (defina OPENAI_API_KEY).")
        else:
            relatorio = await llm.relatorio(resposta.texto, resposta.filtros, resposta.imoveis, args.relatorio, regras)
            relatorio.update(top=args.relatorio, gerado_em=datetime.utcnow().isoformat())

    if args.pdf:
        from app.exportar import html_busca_com_fotos, salvar_pdf
        html, com_foto, tentadas = await html_busca_com_fotos(resposta, relatorio, args.pdf_top)
        pdf = salvar_pdf(html, Path(args.pdf))
        print(f"PDF salvo em: {pdf}  (fotos: {com_foto} de {tentadas})")
        if com_foto < tentadas:
            print("  Algumas fotos não baixaram (rede/portal); rode de novo para tentar outra vez.")

    if destino is not None:
        with Session(destino) as db:
            row = SearchHistory(user_id=user_id, query=resposta.texto, filters_json=resposta.filtros.model_dump_json(),
                                total_results=resposta.total, results_json=resposta.model_dump_json(),
                                relatorio_json=json.dumps(relatorio, ensure_ascii=False) if relatorio else None)
            db.add(row)
            db.commit()
            print(f"Publicado no site: Histórico de '{args.usuario}', busca #{row.id}"
                  f"{' (com relatório)' if relatorio else ''}.")


if __name__ == "__main__":
    asyncio.run(main())
