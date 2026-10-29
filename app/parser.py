"""Interpreta o texto livre do usuário e transforma em filtros de busca."""
import re

from unidecode import unidecode

from .schemas import Filtros

TIPOS = {
    "apartamento": r"\b(apartamentos?|aptos?|aps?|ape)\b",
    "casa": r"\b(casas?|sobrados?)\b",
    "cobertura": r"\bcoberturas?\b",
    "area-privativa": r"\bareas?\s+privativas?\b",
    "kitnet": r"\b(kitnets?|kitinetes?|kit|studios?|estudios?|lofts?)\b",
}

GARANTIAS = {
    "seguro_fianca": r"seguro[\s-]*fianca",
    "fiador": r"\bfiador",
    "caucao": r"\bcaucao",
    "titulo_capitalizacao": r"titulo\s+de\s+capitaliza",
}

# Palavras que encerram o nome do bairro
_STOP = r"(?=\s+(?:com|que|e|ate|ate|por|ou|de\s+ate|aceit\w*|num|em\s+um|no\s+maximo|preco|valor|r\$|\d)\b|[,.;!?]|$)"


def normalize(texto: str) -> str:
    return re.sub(r"\s+", " ", unidecode(texto).lower()).strip()


def slugify(nome: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(nome)).strip("-")


def _valor(num: str, mil: str | None) -> float:
    num = num.replace(" ", "")
    if mil:
        return float(num.replace(".", "").replace(",", ".")) * 1000
    # "3.500" ou "3.500,00" -> 3500 ; "3500,50" -> 3500.5
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", num):
        num = num.replace(".", "")
    return float(num.replace(",", "."))


def parse_query(texto: str) -> Filtros:
    t = normalize(texto)

    tipos = [tipo for tipo, rx in TIPOS.items() if re.search(rx, t)]

    # Andar vale para apartamentos: "1 andar", "1º andar", "primeiro andar", "terreo"
    andar = None
    if m := re.search(r"\b(\d+)\s*(?:o|a|deg)?\s*andar\b", t):
        andar = int(m.group(1))
    elif m := re.search(r"\b(primeiro|segundo|terceiro|quarto|quinto)\s+andar\b", t):
        andar = ["primeiro", "segundo", "terceiro", "quarto", "quinto"].index(m.group(1)) + 1
    elif re.search(r"\bterreo\b", t):
        andar = 0
    if andar is not None and "apartamento" not in tipos:
        tipos.append("apartamento")
    # "casa ou apartamento com área privativa": o apartamento tem que ser área privativa
    elif re.search(r"\bcom\s+areas?\s+privativas?\b", t) and "apartamento" in tipos:
        tipos.remove("apartamento")

    if not tipos:
        tipos = ["apartamento", "casa"]

    quartos_min = quartos_max = None
    q = r"(?:quartos?|qtos?|dormitorios?|dorms?)"
    if m := re.search(rf"(?:entre\s+)?(\d+)\s*(?:a|e|-)\s*(\d+)\s*{q}", t):
        quartos_min, quartos_max = int(m.group(1)), int(m.group(2))
    elif m := re.search(rf"(?:exatamente|so|apenas)\s+(\d+)\s*{q}", t):
        quartos_min = quartos_max = int(m.group(1))
    elif m := re.search(rf"(?:ate|no maximo)\s+(\d+)\s*{q}", t):
        quartos_max = int(m.group(1))
    elif m := re.search(rf"(\d+)\s*\+?\s*{q}", t):
        quartos_min = int(m.group(1))

    raio_km = None
    if m := re.search(r"(\d+(?:[.,]\d+)?)\s*(km|quilometros?|kilometros?|m|metros)\b", t):
        raio_km = float(m.group(1).replace(",", "."))
        if m.group(2) in ("m", "metros"):
            raio_km /= 1000

    bairro = None
    padroes_bairro = [
        rf"\bbairros?\s+(?:do\s+|da\s+|de\s+)?([a-z][a-z\s'-]*?){_STOP}",
        rf"\b(?:perto|proximo|proxima|ao redor|em volta|arredores)\s+(?:do|da|de|ao|a)\s+([a-z][a-z\s'-]*?){_STOP}",
        rf"\b(?:raio\s+de\s+[\d.,]+\s*\w+\s+)(?:do|da|de|no|na)\s+([a-z][a-z\s'-]*?){_STOP}",
        rf"\b(?:no|na)\s+([a-z][a-z\s'-]*?){_STOP}",
    ]
    for rx in padroes_bairro:
        if m := re.search(rx, t):
            cand = m.group(1).strip()
            if cand and cand not in ("bairro", "regiao", "cidade", "belo horizonte", "bh"):
                bairro = cand
                break

    preco_min = preco_max = None
    num = r"(\d[\d.,]*)\s*(mil|k)?"
    if m := re.search(rf"entre\s+(?:r\$\s*)?{num}\s*(?:reais)?\s*e\s+(?:r\$\s*)?{num}", t):
        preco_min, preco_max = _valor(m.group(1), m.group(2)), _valor(m.group(3), m.group(4))
    else:
        if m := re.search(rf"(?:ate|maximo|max|no maximo|abaixo de|menos de)\s+(?:de\s+)?r?\$?\s*{num}(?!\s*(?:km|m\b|metros|quartos?|vagas?))", t):
            preco_max = _valor(m.group(1), m.group(2))
        if m := re.search(rf"(?:acima de|a partir de|minimo|mais de)\s+r?\$?\s*{num}(?!\s*(?:km|m\b|quartos?|vagas?))", t):
            preco_min = _valor(m.group(1), m.group(2))
        if preco_max is None and (m := re.search(rf"r\$\s*{num}", t)):
            preco_max = _valor(m.group(1), m.group(2))

    # "total de até 2700", "preço total 2700", "até 2700 no total" => limite de aluguel + condomínio
    total_max = None
    if preco_max is not None and re.search(r"\btotal\b|aluguel\s*\+\s*condominio|com\s+condominio", t):
        total_max, preco_max = preco_max, None

    vagas_min = None
    if m := re.search(r"(\d+)\s*vagas?", t):
        vagas_min = int(m.group(1))
    elif re.search(r"\b(vagas?|garagem|estacionamento)\b", t) and not re.search(
            r"\b(sem|nao precisa de?|dispensa)\s+(de\s+)?(vagas?|garagem|estacionamento)", t):
        # "vaga de garagem", "com garagem", "precisa de estacionamento"...
        vagas_min = 1

    aceita_pet = None
    if re.search(r"\b(aceit\w*|permit\w*|pode)\s+(pets?|animais|animal|cachorro|gato|cao)|pet\s*friendly", t):
        aceita_pet = True

    garantias = [g for g, rx in GARANTIAS.items() if re.search(rx, t)]

    return Filtros(
        tipos=tipos,
        quartos_min=quartos_min,
        quartos_max=quartos_max,
        bairro=bairro,
        raio_km=raio_km,
        preco_min=preco_min,
        preco_max=preco_max,
        total_max=total_max,
        vagas_min=vagas_min,
        aceita_pet=aceita_pet,
        garantias=garantias,
        andar=andar,
    )
