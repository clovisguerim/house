# House – busca de imóveis para alugar em BH

> 📖 **[Manual do usuário](docs/MANUAL.md)** – como usar o site, com prints de cada tela.

API (FastAPI; SQLite no PC, Postgres no Render) com interface web, que recebe um texto livre como
`casa ou apartamento 3 quartos num raio de 10 km do bairro gloria`,
transforma em filtros e busca anúncios de locação em Belo Horizonte.

## Rodar

```powershell
.\run.ps1            # cria .venv se preciso, instala deps e sobe em http://127.0.0.1:8000
```

Interface web: http://127.0.0.1:8000/ — login/cadastro, busca por texto, resultados em lista ou
mapa, filtros rápidos (pet, vaga), ordenação, exportar CSV, histórico (admin vê o de todos os
usuários e reabre os resultados salvos) e gestão de usuários.

Documentação interativa: http://127.0.0.1:8000/docs (botão **Authorize** usa usuário/senha).

Contas administradoras criadas automaticamente: `clovisguerim` / `123456` e `manucfe` / `123456`.
Troque as senhas e defina `HOUSE_SECRET_KEY` antes de expor a API.

## Contas e segurança

- As contas `clovisguerim` e `manucfe` nascem com a senha `123456`, que é **provisória**: até ser
  trocada, o usuário só consegue acessar a tela de troca de senha.
- Cadastro (`HOUSE_CADASTRO`): `aprovacao` (padrão – a conta nasce inativa e um admin aprova
  marcando "Ativo" em Usuários), `aberto` ou `fechado` (só admin cria contas).
- Senha criada ou redefinida por um admin também é provisória.
- A chave de assinatura dos tokens é gerada na primeira execução em `.secret_key` (ou use
  `HOUSE_SECRET_KEY`). Não versionar esse arquivo.

## Publicar na internet (Render, grátis)

1. Crie um Postgres gratuito (ex.: [Neon](https://neon.tech)) e copie a connection string.
2. No [Render](https://render.com): New → Blueprint → este repositório (usa `render.yaml` + `Dockerfile`).
3. Cole a connection string em `DATABASE_URL` (e, se quiser IA, `ANTHROPIC_API_KEY`).
4. Depois do deploy, veja `HOUSE_SENHA_INICIAL_ADMIN` em Environment, entre como `clovisguerim`
   ou `manucfe` com ela e troque a senha (é obrigatório).

Limites do plano grátis: o serviço hiberna após ~15 min sem uso e o primeiro acesso depois disso
demora. Busca limitada a `HOUSE_BUSCAS_POR_HORA` por usuário comum (padrão 20) e uma por vez.
IPs de data center podem ser bloqueados por alguns portais – a resposta mostra o status de cada fonte.

## Acesso de outros aparelhos (Tailscale)

O servidor escuta só em `127.0.0.1`. Para usar do celular ou de outro computador sem abrir porta:

1. Instale o Tailscale (https://tailscale.com/download) neste PC e faça login.
2. Rode `.\publicar-tailscale.ps1` → mostra o endereço `https://<maquina>.<tailnet>.ts.net/`.
   (Na primeira vez o Tailscale pode pedir para ligar HTTPS/MagicDNS na tailnet – o comando mostra o link.)
3. Para outra pessoa acessar: no painel do Tailscale, compartilhe esta máquina com ela
   ("Share") ou convide para a tailnet; ela instala o Tailscale e entra com a conta dela.
4. Opcional: `.\iniciar-com-windows.ps1` faz o House iniciar sozinho no login do Windows.

O PC precisa estar ligado e conectado. `.\publicar-tailscale.ps1 -Parar` remove a publicação.

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| POST | `/auth/register` | cadastro (`username`, `password`, `email?`, `full_name?`) |
| POST | `/auth/login` | form `username`/`password` → token JWT |
| GET/PATCH | `/auth/me` | dados / alteração do próprio usuário |
| GET/POST | `/admin/users` | listar / criar usuários (admin; `?is_admin=true` cria admin) |
| GET/PATCH/DELETE | `/admin/users/{id}` | ver / editar (inclui `is_admin`, `is_active`, senha) / excluir |
| POST | `/buscar` | busca: `{"texto": "...", "fontes": [...], "max_detalhes": 120, "comparar_com": <id do histórico>}` |
| GET | `/interpretar?texto=...` | mostra os filtros entendidos, sem buscar |
| GET | `/historico` | buscas do usuário (quem, quando, texto, filtros, total) |
| GET/DELETE | `/historico/{id}` | reabre os resultados salvos / exclui (dono ou admin) |
| GET | `/admin/historico?usuario=` | buscas de todos os usuários (admin) |
| POST/GET | `/historico/{id}/relatorio` | gera (`top` 1–40, `prioridades`, `observacao`) / reabre o relatório da IA |
| GET | `/relatorio/criterios` | critérios que podem ser priorizados no relatório |
| POST | `/historico/{id}/anuncios` | inclui anúncio por link (`url`, `texto` colado se o site bloquear, `campos` à mão) |
| DELETE | `/historico/{id}/anuncios/{imovel_id}` | remove um anúncio adicionado à mão |
| POST/GET | `/historico/{id}/feedback` | avalia o relatório (nota 1–5 + justificativa; vira regra) / lista avaliações |
| GET/POST/DELETE | `/admin/onibus` | base de ônibus em uso / envia GTFS (multipart: convencional, suplementar) / volta à original |
| GET/POST | `/regras` | regras da IA visíveis (compartilhadas + suas) / cria regra |
| PATCH/DELETE | `/regras/{id}` | edita (texto, compartilhada, ativa) / exclui — autor ou admin |

## O que o texto entende

- **tipo**: casa, apartamento/apto, cobertura, kitnet/studio (nenhum citado = casa + apartamento)
- **quartos**: `3 quartos` (= 3 ou mais), `2 a 3 quartos`, `exatamente 3 quartos`, `até 2 quartos`
- **local**: `bairro X`, `perto do X`, `no X`; **raio**: `10 km`, `500 m`
- **preço do aluguel**: `até R$ 3.500`, `até 3 mil`, `entre 2000 e 3000`, `acima de 1500`
- **vaga**: `com vaga`/`com garagem`, `2 vagas`
- **pet**: `aceita pet`, `aceita animais`, `pet friendly`
- **garantia**: `seguro fiança`, `fiador`, `caução`, `título de capitalização`

## Campos de cada imóvel

`aluguel`, `condominio`, `iptu`, `total_mensal` (aluguel + condomínio, como o site mostra),
`endereco`, `bairro`, `latitude/longitude`, `distancia_km`, `quartos`, `banheiros`, `area_m2`,
`vagas`, `tem_estacionamento`, `aceita_pet`, `seguro_fianca`, `fiador`, `caucao`,
`titulo_capitalizacao`, `caracteristicas`, `fotos`, `url`.

`aceita_pet` e as garantias são `true`/`false`/`null`: vêm das características do anúncio
("Aceita animais") e do texto da descrição. `null` = o anúncio não informa. Nos filtros de pet e
garantia só são removidos anúncios que dizem explicitamente que **não** aceitam; os que confirmam
aparecem primeiro.

## IA (Claude ou OpenAI)

Com `ANTHROPIC_API_KEY` (Claude) ou `OPENAI_API_KEY` (OpenAI, padrão `gpt-5.4-mini`) definida no
ambiente do servidor, a busca usa IA em duas etapas. Com as duas chaves vale a Anthropic, ou o que
estiver em `HOUSE_LLM_PROVEDOR` (`anthropic`/`openai`). `HOUSE_LLM_MODELO_INTERPRETAR` e
`HOUSE_LLM_MODELO_EXTRAIR` trocam o modelo de cada etapa.

1. **Interpretar o texto** – vira os mesmos filtros (tipo, quartos, bairro, raio, preço/total, vaga,
   pet, garantias, andar), entendendo sinônimos e negações ("sem térreo"), mais uma lista de
   **desejos** sem filtro exato (quintal, mobiliado, perto do metrô…) que servem para ordenar.
2. **Ler a descrição dos anúncios** (os mais próximos primeiro, em lotes) – completa pet, garantias,
   andar e mobiliado quando o portal não traz esses dados estruturados, marca quais desejos cada
   anúncio atende e resume até 4 destaques. Dado estruturado do portal tem prioridade sobre a IA.

Preço, distância, quartos e vagas continuam filtrados por código. Cada anúncio é lido uma vez e fica
em cache por 30 dias (por anúncio + desejos), então buscas repetidas quase não custam. Sem chave,
ou com qualquer erro da IA, a busca segue com as regras (`parser.py`) e a resposta informa em `ia`.

| Variável | Padrão | Para quê |
|---|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | – | liga a IA (Claude / OpenAI) |
| `HOUSE_LLM_PROVEDOR` | `auto` | `anthropic` ou `openai` quando as duas chaves existem |
| `HOUSE_LLM` | `auto` | `off` desliga mesmo com chave |
| `HOUSE_LLM_MODELO_INTERPRETAR` | `claude-opus-5-5` / `gpt-5.4-mini` | modelo da interpretação |
| `HOUSE_LLM_MODELO_EXTRAIR` | `claude-opus-5-5` / `gpt-5.4-mini` | modelo da leitura dos anúncios (`claude-haiku-5-5` é ~40× mais barato) |
| `HOUSE_LLM_EFFORT` | `low` | esforço de raciocínio |
| `HOUSE_LLM_MAX_ANUNCIOS` | `300` | anúncios lidos por busca |

Na API, `"usar_ia": false` em `/buscar` pula a leitura dos anúncios.

## Fontes

`GET /fontes` lista as fontes; na interface há uma caixa por fonte. O mesmo imóvel anunciado em
mais de um portal (mesmo aluguel e quartos, a até 150 m) vira um card só, com os outros links em
`outras_fontes`. O bairro de referência é localizado uma vez (pelo Netimóveis) e vale para todas.

- **QuintoAndar** – funciona. Listagens públicas por bairro/região/vizinhos + página de cada imóvel.
  Pet vem de campo estruturado. Não usa fiador/caução (garantia própria); andar quase nunca vem.
- **Chaves na Mão** – funciona. Bairro de referência + bairros vizinhos dentro do raio (até 30 páginas,
  só `?pg=2` é permitido pelo robots.txt) + página dos anúncios mais próximos.
- **Lopes** – funciona, mas tem pouquíssimo aluguel residencial em BH.
- **Casa Mineira** – bloqueia o cliente da API com desafio do Cloudflare; o provedor está pronto e
  informa o bloqueio (volta a funcionar se o site liberar).
- **Netimóveis** – funciona. Lê as páginas públicas de listagem (dados estruturados JSON-LD) e
  abre a página de cada anúncio para coordenadas, IPTU, vagas, pet e garantias. Respeita o
  `robots.txt` do site, que proíbe paginação (`pag=`) e a `/api/`; por isso cada página de
  listagem (bairro + regiões de BH dentro do raio, por tipo) contribui com até 21 anúncios.
  As páginas ficam em cache no SQLite por 12 h.
- **Viva Real** – o site bloqueia acesso automatizado via Cloudflare (HTTP 403). A API informa
  isso em `fontes[].mensagem` e não tenta contornar o bloqueio. Para incluir o Viva Real é
  preciso acesso autorizado (ex.: integração de parceiro do Grupo OLX/ZAP).

## Rodar a busca no PC e publicar no site

Alguns portais (ex.: Netimóveis) bloqueiam o servidor na nuvem, mas respondem no seu PC. Este comando
roda a busca aqui e grava o resultado no Histórico do site (banco do `.env.local`):

```powershell
.venv\Scripts\python.exe publicar_busca.py "casa 2 quartos 7 km do gloria até 2500 com vaga"
.venv\Scripts\python.exe publicar_busca.py "..." --usuario manucfe --relatorio 5   # relatório exige IA
```

Para usar IA aqui, defina `$env:OPENAI_API_KEY` antes. Opções: `--fontes`, `--max-detalhes`, `--sem-ia`,
`--pdf output/arquivo.pdf` (PDF com fotos, via Edge/Chrome), `--pdf-top N`, `--nao-publicar`.

## Ônibus perto do imóvel (GTFS BHTRANS)

`app/data/onibus_bh.json` traz os pontos de ônibus de BH e as linhas de cada um, gerado do GTFS
oficial da BHTRANS (convencional + suplementar; cópia MobilityData, validade 2023). As linhas
metropolitanas (DER-MG) não têm GTFS público. No site, um admin atualiza a base em **Usuários →
Base de ônibus** enviando os `gtfsfiles.zip` da PBH (fica guardada no banco). Para atualizar o arquivo
do repositório (baixe no navegador em dados.pbh.gov.br):

```powershell
.venv\Scripts\python.exe atualizar_onibus.py convencional.zip [suplementar.zip]
```
