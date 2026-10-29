from ..schemas import Filtros, FonteStatus
from .base import FetchError, Fetcher, Provider, Referencia

BASE = "https://www.vivareal.com.br/aluguel/minas-gerais/belo-horizonte"
TIPO_SLUG = {"apartamento": "apartamento_residencial", "casa": "casa_residencial", "cobertura": "cobertura_residencial"}


class VivaRealProvider(Provider):
    """O Viva Real protege o site com Cloudflare e bloqueia acesso automatizado (HTTP 403).

    O provedor faz uma requisição comum e reporta o resultado no status da fonte.
    Não há tentativa de contornar a proteção; para dados do Viva Real é preciso um acesso
    autorizado (ex.: API de parceiro do Grupo OLX/ZAP).
    """

    name = "vivareal"
    label = "Viva Real"

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int, ref: Referencia | None):
        tipo = next((TIPO_SLUG[t] for t in filtros.tipos if t in TIPO_SLUG), "apartamento_residencial")
        try:
            await fetcher.get(f"{BASE}/{tipo}/", use_cache=False)
        except FetchError as e:
            msg = ("Viva Real bloqueou o acesso automatizado (Cloudflare)" if e.status in (403, 429, 503)
                   else f"Viva Real respondeu HTTP {e.status}")
            return [], FonteStatus(fonte=self.name, ok=False, mensagem=msg)
        except Exception as e:  # rede/timeout
            return [], FonteStatus(fonte=self.name, ok=False, mensagem=f"Falha ao acessar Viva Real: {e}")
        return [], FonteStatus(fonte=self.name, ok=False,
                               mensagem="Viva Real acessível, mas a extração de anúncios não está implementada")
