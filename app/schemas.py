from datetime import datetime

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=128)
    email: str | None = None
    full_name: str | None = None


class UserUpdate(BaseModel):
    email: str | None = None
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=128)
    senha_atual: str | None = Field(default=None, description="Obrigatória para trocar a própria senha")
    is_admin: bool | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    full_name: str | None
    is_admin: bool
    is_active: bool
    must_change_password: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SearchRequest(BaseModel):
    texto: str = Field(examples=["casa ou apartamento 3 quartos num raio de 10 km do bairro gloria"])
    fontes: list[str] | None = Field(default=None, description="Nomes de GET /fontes; vazio = todas")
    max_detalhes: int = Field(default=120, ge=1, le=400, description="Máximo de anúncios abertos para ler detalhes")
    usar_ia: bool = Field(default=True, description="Ler a descrição dos anúncios com IA (se configurada)")
    comparar_com: int | None = Field(default=None, description="id de uma busca do histórico para mostrar novos/saídos")


class Filtros(BaseModel):
    tipos: list[str]
    quartos_min: int | None = None
    quartos_max: int | None = None
    bairro: str | None = None
    raio_km: float | None = None
    preco_max: float | None = None
    preco_min: float | None = None
    total_max: float | None = Field(default=None, description="Limite de aluguel + condomínio")
    vagas_min: int | None = None
    aceita_pet: bool | None = None
    garantias: list[str] = []
    andar: int | None = Field(default=None, description="Só se aplica a apartamentos; 0 = térreo")
    andares_excluidos: list[int] = Field(default=[], description="Ex.: 'não quero térreo' => [0]")
    caracteristicas: list[str] = Field(default=[], description="Desejos sem filtro exato (quintal, mobiliado...); ordenam")
    interpretado_por: str = Field(default="regras", description="'regras' ou o modelo de IA usado")


class Imovel(BaseModel):
    fonte: str
    id: str
    url: str
    titulo: str | None = None
    tipo: str | None = None
    endereco: str | None = None
    bairro: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    distancia_km: float | None = None
    quartos: int | None = None
    andar: int | None = Field(default=None, description="Lido do texto do anúncio; 0 = térreo")
    andar_alto: bool | None = None
    banheiros: int | None = None
    area_m2: float | None = None
    aluguel: float | None = None
    condominio: float | None = None
    iptu: float | None = None
    total_mensal: float | None = None
    vagas: int | None = None
    tem_estacionamento: bool | None = None
    aceita_pet: bool | None = Field(default=None, description="null = anúncio não informa")
    seguro_fianca: bool | None = None
    fiador: bool | None = None
    caucao: bool | None = None
    titulo_capitalizacao: bool | None = None
    caracteristicas: list[str] = []
    fotos: list[str] = []
    outras_fontes: list[dict] = Field(default=[], description="Mesmo imóvel anunciado em outros portais: [{fonte, url}]")
    mobiliado: bool | None = None
    destaques: list[str] = Field(default=[], description="Pontos fortes lidos pela IA na descrição")
    atende: list[str] = Field(default=[], description="Quais desejos da busca (filtros.caracteristicas) o anúncio atende")
    descricao: str | None = Field(default=None, exclude=True)
    onibus: dict | None = Field(default=None, description="Linhas de ônibus (BHTRANS) a até 500 m: pontos, linhas, distância")
    manual: bool = Field(default=False, description="Adicionado à mão pelo link (não veio da busca)")
    local_aproximado: bool = Field(default=False, description="Coordenada é o centro do bairro, não o endereço")
    avisos: list[str] = Field(default=[], description="O que não bate com os filtros da busca (anúncio manual)")
    novo: bool = Field(default=False, description="Não estava na busca comparada")
    preco_anterior: float | None = Field(default=None, description="Total mensal na busca comparada, se mudou")


class FonteStatus(BaseModel):
    fonte: str
    ok: bool
    mensagem: str | None = None
    encontrados: int = 0


class SearchResponse(BaseModel):
    texto: str
    filtros: Filtros
    referencia: dict | None = None
    fontes: list[FonteStatus]
    total: int
    imoveis: list[Imovel]
    historico_id: int | None = None
    data: datetime | None = None
    usuario: str | None = None
    ia: dict | None = Field(default=None, description="Uso de IA nesta busca: modelos, anúncios lidos, erros")
    comparacao: dict | None = Field(default=None, description="Diferenças para a busca anterior (ao refazer)")


class RegraIn(BaseModel):
    texto: str = Field(min_length=3, max_length=500)
    compartilhada: bool = True


class RegraUpdate(BaseModel):
    texto: str | None = Field(default=None, min_length=3, max_length=500)
    compartilhada: bool | None = None
    ativa: bool | None = None


class RegraOut(BaseModel):
    id: int
    texto: str
    compartilhada: bool
    ativa: bool
    origem: str
    autor: str
    minha: bool
    created_at: datetime
    updated_at: datetime


class FeedbackIn(BaseModel):
    imovel_id: str | None = Field(default=None, description="'fonte:id' do imóvel; vazio = relatório todo")
    nota: int = Field(ge=1, le=5)
    justificativa: str = Field(min_length=3, max_length=1000)
    gerar_regra: bool = True
    compartilhar: bool = True


class HistoricoItem(BaseModel):
    id: int
    usuario: str
    texto: str
    filtros: dict
    total: int
    data: datetime
    tem_resultados: bool
    tem_relatorio: bool = False
