"use strict";

function lerPreferencia(chave, padrao) {
  try {
    return localStorage.getItem(chave) || padrao;
  } catch {
    return padrao;
  }
}

// ---------- Estado ----------
const state = {
  token: localStorage.getItem("house_token"),
  me: null,
  result: null,       // SearchResponse exibido
  mode: lerPreferencia("house_modo", "mapa"),  // "lista" ou "mapa", lembrado entre visitas
  histScope: "meu",
  history: [],
  map: null,
  markers: {},
  ranking: {},       // "fonte:id" -> posição no relatório da IA (marcadores numerados no mapa)
};

const EXEMPLOS = [
  "casa ou apartamento 3 quartos num raio de 10 km do bairro gloria",
  "área privativa, apartamento no 1º andar ou cobertura 2 quartos num raio de 7 km do bairro gloria, total de até 2700",
  "apartamento 2 quartos no bairro buritis até R$ 3.000 com vaga aceita pet",
];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Todo texto vindo da API/anúncios passa por aqui antes de ir para innerHTML
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const brl = (v) => (v == null ? "–" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 }));
const dataHora = (s) => new Date(s.endsWith("Z") ? s : s + "Z").toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
const safeUrl = (u) => (/^https:\/\//.test(u || "") ? u : "#");

// Linhas de ônibus (GTFS da BHTRANS) a até 500 m: resumo + lista que abre ao clicar
function blocoOnibus(i) {
  const o = i.onibus;
  if (!o) return "";
  if (!o.linhas.length) return `<div class="onibus muted">🚌 nenhuma linha municipal a até ${o.raio_m} m</div>`;
  const itens = o.linhas.map((l) => `<li><b>${esc(l.numero)}</b> ${esc(l.nome)} <span class="muted">· ${l.distancia_m} m${l.sistema === "suplementar" ? " · suplementar" : ""}</span></li>`).join("");
  return `<details class="onibus"><summary>🚌 <b>${o.linhas.length}</b> linha${o.linhas.length > 1 ? "s" : ""} de ônibus a até ${o.raio_m} m
    <span class="muted">· ponto mais perto a ${o.ponto_mais_proximo_m} m</span></summary><ul>${itens}</ul></details>`;
}

// Google Street View (o "bonequinho") na coordenada do anúncio e Google Maps pelo endereço.
// Links públicos do Google Maps (map_action=pano / search), sem chave de API.
function linksMapa(i) {
  const out = [];
  if (i.latitude != null && i.longitude != null) {
    // Sem rua no endereço, a coordenada costuma ser o centro do bairro: a vista é aproximada
    const aprox = !/\b(rua|r\.|av\.?|avenida|alameda|al\.|travessa|pra[cç]a|rodovia|estrada|beco)\b/i.test(i.endereco || "");
    const sv = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${i.latitude},${i.longitude}`;
    out.push(`<a href="${esc(sv)}" target="_blank" rel="noopener noreferrer" title="${aprox ? "Localização aproximada (o anúncio não informa a rua)" : "Abre o Street View na localização do anúncio"}">🧍 Street View${aprox ? " (aprox.)" : ""}</a>`);
  }
  if (i.endereco) {
    const q = encodeURIComponent(`${i.endereco}${/belo horizonte/i.test(i.endereco) ? "" : ", Belo Horizonte - MG"}`);
    out.push(`<a href="https://www.google.com/maps/search/?api=1&query=${q}" target="_blank" rel="noopener noreferrer">🗺️ Google Maps</a>`);
  }
  return out.join(" · ");
}

// ---------- API ----------
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && path !== "/auth/login") {
    logout();
    throw new Error("Sessão expirada, entre novamente.");
  }
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((e) => e.msg).join("; ") : d || `Erro ${res.status}`);
  }
  return body;
}

// ---------- Auth ----------
function showAuth() {
  $("#auth-view").hidden = false;
  $("#app-view").hidden = true;
  $("#senha-view").hidden = true;
  api("/auth/config").then((c) => {
    $('[data-auth-tab="register"]').hidden = c.cadastro === "fechado";
  }).catch(() => {});
}

function showSenha(obrigatoria) {
  $("#auth-view").hidden = true;
  $("#app-view").hidden = true;
  $("#senha-view").hidden = false;
  $("#senha-form").reset();
  $("#senha-msg").textContent = "";
  $("#senha-aviso").textContent = obrigatoria
    ? "Sua senha é provisória. Crie uma senha nova (mínimo 8 caracteres) para continuar."
    : "Mínimo 8 caracteres.";
  $("#senha-cancelar").hidden = obrigatoria;
}

async function enterApp() {
  try {
    state.me = await api("/auth/me");
  } catch {
    return showAuth();
  }
  if (state.me.must_change_password) return showSenha(true);
  $("#auth-view").hidden = true;
  $("#senha-view").hidden = true;
  $("#app-view").hidden = false;
  $("#who-name").textContent = state.me.full_name || state.me.username;
  $$(".admin-only").forEach((el) => (el.hidden = !state.me.is_admin));
  switchView("buscar");
  loadFontes();
}

// ---------- Fontes ----------
const ROTULO_FONTE = {};

async function loadFontes() {
  let lista = [];
  try {
    lista = await api("/fontes");
  } catch {
    return;
  }
  let desmarcadas = [];
  try {
    desmarcadas = JSON.parse(localStorage.getItem("house_fontes_off") || "[]");
  } catch { /* storage indisponível */ }
  lista.forEach((f) => (ROTULO_FONTE[f.nome] = f.rotulo));
  $("#fontes").innerHTML = `<span>Fontes:</span>` + lista
    .map((f) => `<label class="inline"><input type="checkbox" value="${esc(f.nome)}" ${desmarcadas.includes(f.nome) ? "" : "checked"}> ${esc(f.rotulo)}</label>`)
    .join("");
}

$("#fontes").addEventListener("change", () => {
  const off = [...$$("#fontes input:not(:checked)")].map((i) => i.value);
  try {
    localStorage.setItem("house_fontes_off", JSON.stringify(off));
  } catch { /* storage indisponível */ }
});

const fonteNome = (n) => ROTULO_FONTE[n] || n;

function logout() {
  state.token = null;
  state.me = null;
  localStorage.removeItem("house_token");
  showAuth();
}

$$("[data-auth-tab]").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$("[data-auth-tab]").forEach((b) => b.classList.toggle("active", b === btn));
    $("#login-form").hidden = btn.dataset.authTab !== "login";
    $("#register-form").hidden = btn.dataset.authTab !== "register";
    $("#auth-msg").textContent = "";
  })
);

async function doLogin(username, password) {
  const tok = await api("/auth/login", { method: "POST", body: new URLSearchParams({ username, password }) });
  state.token = tok.access_token;
  localStorage.setItem("house_token", state.token);
  await enterApp();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#auth-msg").textContent = "";
  try {
    await doLogin(f.get("username"), f.get("password"));
  } catch (err) {
    $("#auth-msg").textContent = err.message;
  }
});

$("#register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  $("#auth-msg").textContent = "";
  try {
    const novo = await api("/auth/register", { method: "POST", json: { ...f, email: f.email || null, full_name: f.full_name || null } });
    if (!novo.is_active) {
      e.target.reset();
      $("#auth-msg").classList.add("ok");
      $("#auth-msg").textContent = "Conta criada! Um administrador precisa aprovar antes do primeiro acesso.";
      return;
    }
    await doLogin(f.username, f.password);
  } catch (err) {
    $("#auth-msg").classList.remove("ok");
    $("#auth-msg").textContent = err.message;
  }
});

$("#logout").addEventListener("click", logout);
$("#minha-senha").addEventListener("click", () => showSenha(false));
$("#senha-cancelar").addEventListener("click", enterApp);

$("#senha-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  if (f.password !== f.confirma) return ($("#senha-msg").textContent = "As senhas novas não conferem.");
  try {
    await api("/auth/me", { method: "PATCH", json: { senha_atual: f.senha_atual, password: f.password } });
    await enterApp();
  } catch (err) {
    $("#senha-msg").textContent = err.message;
  }
});

// ---------- Navegação ----------
function switchView(name) {
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
  if (name === "historico") loadHistory();
  if (name === "usuarios") { loadUsers(); loadOnibusAdmin(); }
  if (name === "regras") loadRegras();
  // O Leaflet não mede um mapa escondido: ao voltar para Buscar, redesenha com a área já visível
  if (name === "buscar" && state.mode === "mapa" && state.result) setTimeout(() => renderMap(visibleItems()), 60);
}
$$(".nav-btn").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

// ---------- Busca ----------
$("#examples").innerHTML = EXEMPLOS.map((t, i) => `<button type="button" class="example" data-i="${i}">${esc(t.length > 48 ? t.slice(0, 46) + "…" : t)}</button>`).join("");
$("#examples").addEventListener("click", (e) => {
  const b = e.target.closest(".example");
  if (!b) return;
  $("#q").value = EXEMPLOS[b.dataset.i];
  interpretar();
});

const ROTULO_TIPO = { apartamento: "Apartamento", casa: "Casa", cobertura: "Cobertura", "area-privativa": "Área privativa", kitnet: "Kitnet" };
const ROTULO_GAR = { seguro_fianca: "Seguro fiança", fiador: "Fiador", caucao: "Caução", titulo_capitalizacao: "Título de capitalização" };

function chipsFiltros(f) {
  const c = [];
  const add = (k, v) => c.push(`<span class="chip"><b>${esc(k)}</b>${esc(v)}</span>`);
  add("Tipo", f.tipos.map((t) => ROTULO_TIPO[t] || t).join(", "));
  if (f.quartos_min != null || f.quartos_max != null) {
    add("Quartos", f.quartos_min === f.quartos_max ? f.quartos_min
      : f.quartos_max == null ? `${f.quartos_min}+` : `${f.quartos_min ?? 0}–${f.quartos_max}`);
  }
  if (f.bairro) add("Bairro", f.bairro);
  if (f.raio_km) add("Raio", `${f.raio_km} km`);
  if (f.andar != null) add("Andar (apto)", f.andar === 0 ? "térreo" : `${f.andar}º`);
  if (f.preco_min) add("Aluguel mín.", brl(f.preco_min));
  if (f.preco_max) add("Aluguel máx.", brl(f.preco_max));
  if (f.total_max) add("Total máx.", brl(f.total_max));
  if (f.vagas_min) add("Vagas", `${f.vagas_min}+`);
  if (f.aceita_pet) add("Pet", "aceita");
  if (f.garantias?.length) add("Garantia", f.garantias.map((g) => ROTULO_GAR[g] || g).join(", "));
  if (f.andares_excluidos?.length) add("Sem andar", f.andares_excluidos.map((a) => (a === 0 ? "térreo" : `${a}º`)).join(", "));
  if (f.caracteristicas?.length) add("Desejos", f.caracteristicas.join(", "));
  if (f.interpretado_por && f.interpretado_por !== "regras") c.push(`<span class="chip ia">✨ entendido por IA</span>`);
  return c.join("");
}

let interpTimer;
async function interpretar() {
  const texto = $("#q").value.trim();
  if (!texto) return ($("#interp").innerHTML = "");
  try {
    const f = await api(`/interpretar?texto=${encodeURIComponent(texto)}`);
    $("#interp").innerHTML = chipsFiltros(f);
  } catch { /* ignora: só pré-visualização */ }
}
$("#q").addEventListener("input", () => {
  clearTimeout(interpTimer);
  interpTimer = setTimeout(interpretar, 350);
});
$("#q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#search-form").requestSubmit();
  }
});

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const texto = $("#q").value.trim();
  if (!texto) return;
  const btn = $("#search-btn");
  btn.disabled = true;
  $("#search-msg").textContent = "";
  $("#loading").hidden = false;
  const t0 = Date.now();
  const tick = setInterval(() => ($("#elapsed").textContent = `(${Math.round((Date.now() - t0) / 1000)} s)`), 1000);
  try {
    const fontes = [...$$("#fontes input:checked")].map((i) => i.value);
    if ($$("#fontes input").length && !fontes.length) throw new Error("Marque pelo menos uma fonte.");
    // "Refazer" do histórico compara com a busca de origem (uma vez só)
    const compararCom = state.compararCom;
    state.compararCom = null;
    const res = await api("/buscar", { method: "POST", json: { texto, fontes, max_detalhes: +$("#max-detalhes").value || 200, comparar_com: compararCom } });
    showResult(res);
  } catch (err) {
    $("#search-msg").textContent = err.message;
  } finally {
    clearInterval(tick);
    $("#elapsed").textContent = "";
    $("#loading").hidden = true;
    btn.disabled = false;
  }
});

function showResult(res) {
  state.result = res;
  $("#results").hidden = false;
  $("#res-title").textContent = `${res.total} ${res.total === 1 ? "imóvel encontrado" : "imóveis encontrados"}`;
  const meta = [];
  if (res.referencia) meta.push(`Referência: ${res.referencia.bairro} (${res.referencia.regiao})`);
  if (res.data) meta.push(`Busca de ${dataHora(res.data)}${res.usuario ? ` por ${res.usuario}` : ""}`);
  $("#res-meta").textContent = meta.join(" · ");
  const ia = res.ia;
  const iaChips = [];
  if (ia?.leitura) {
    const l = ia.leitura;
    iaChips.push(`<span class="source ia" title="${esc(`${l.novos} lidos agora, ${l.do_cache} do cache; ${l.sem_descricao} sem descrição; ${l.fora_do_limite} além do limite`)}">✨ IA leu ${l.anuncios_lidos} anúncios</span>`);
    if (ia.regras_usadas) iaChips.push(`<span class="source ia" title="Preferências da aba Regras da IA">🧠 seguindo ${ia.regras_usadas} regra${ia.regras_usadas > 1 ? "s" : ""}</span>`);
  } else if (ia?.ativa && ia.regras_usadas) {
    iaChips.push(`<span class="source ia" title="Preferências da aba Regras da IA">🧠 seguindo ${ia.regras_usadas} regra${ia.regras_usadas > 1 ? "s" : ""}</span>`);
  } else if (ia && !ia.ativa) {
    iaChips.push(`<span class="source off" title="Defina OPENAI_API_KEY ou ANTHROPIC_API_KEY no servidor para ativar">IA desligada (sem chave da API)</span>`);
  }
  for (const e of ia?.erros || []) iaChips.push(`<span class="source off">IA: ${esc(e)}</span>`);
  $("#res-sources").innerHTML = res.fontes
    .map((s) => `<span class="source ${s.ok ? "" : "off"}" title="${esc(s.mensagem)}">${esc(fonteNome(s.fonte))}: ${s.ok ? `${s.encontrados} encontrados` : esc(s.mensagem)}</span>`)
    .concat(iaChips)
    .join("");
  $("#interp").innerHTML = chipsFiltros(res.filtros);
  renderComparacao(res.comparacao);
  $("#relatorio").hidden = true;
  $("#rel-msg").textContent = "";
  state.ranking = {};
  renderResults();
}

// ---------- Relatório da IA ----------
function renderRelatorio(rel) {
  const porId = Object.fromEntries(rel.imoveis.map((i) => [`${i.fonte}:${i.id}`, i]));
  const lista = (itens, cls) => (itens?.length ? `<ul class="${cls}">${itens.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
  const itens = rel.ranking.map((r, n) => {
    const i = porId[r.id] || {};
    const foto = i.fotos?.[0];
    return `
      <article class="rel-item">
        <div class="rel-pos">${n + 1}º<span class="rel-nota" title="Alinhamento com a busca">${r.nota}/10</span></div>
        ${foto ? `<img class="rel-foto" src="${esc(safeUrl(foto))}" alt="" loading="eager" referrerpolicy="no-referrer">` : `<div class="rel-foto"></div>`}
        <div class="rel-corpo">
          <h3>${esc(r.titulo_curto)}
            ${i.latitude != null ? `<button type="button" class="btn small ghost" data-ver-mapa="${esc(r.id)}">📍 Ver no mapa</button>` : ""}
            <button type="button" class="btn small ghost" data-avaliar="${esc(r.id)}">⭐ Avaliar</button></h3>
          <div class="rel-meta">
            <strong>${brl(i.total_mensal ?? i.aluguel)}</strong>/mês
            · ${esc(i.endereco || i.bairro || "")}
            ${i.distancia_km != null ? ` · ${i.distancia_km.toLocaleString("pt-BR")} km` : ""}
            ${i.onibus ? ` · 🚌 ${i.onibus.linhas.length} linhas a ${i.onibus.raio_m} m` : ""}
            · <a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener noreferrer">ver anúncio ↗</a>
            ${linksMapa(i) ? ` · ${linksMapa(i)}` : ""}
          </div>
          <p>${esc(r.por_que)}${r.sem_ia ? ` <span class="badge" title="A IA não comentou este; entrou pela ordem do app">sem comentário da IA</span>` : ""}</p>
          ${blocoOnibus(i)}
          <div class="rel-cols">
            <div><h4>Pontos fortes</h4>${lista(r.pontos_fortes, "ok")}</div>
            <div><h4>Atenção</h4>${lista(r.pontos_de_atencao, "warn")}</div>
            <div><h4>Perguntar ao anunciante</h4>${lista(r.perguntas_para_o_anunciante, "ask")}</div>
          </div>
          <div class="fb-slot" data-fb-slot="${esc(r.id)}"></div>
        </div>
      </article>`;
  }).join("");
  $("#relatorio").innerHTML = `
    <div class="rel-head">
      <div>
        <h2>📝 Relatório: os ${rel.ranking.length} imóveis mais alinhados à sua busca</h2>
        <div class="muted">Gerado por ${esc(rel.modelo)} em ${dataHora(rel.gerado_em)}, entre ${rel.candidatos_avaliados} candidatos${rel.regras_usadas ? `, seguindo ${rel.regras_usadas} regra${rel.regras_usadas > 1 ? "s" : ""} da IA` : ""}.
          A IA só usa os dados dos anúncios — confirme tudo com o anunciante.</div>
      </div>
      <div class="rel-acoes">
        <button class="btn small" type="button" data-avaliar="">⭐ Avaliar relatório</button>
        <button class="btn small" id="rel-toggle" type="button">Recolher</button>
        <button class="btn small" id="rel-print" type="button">Imprimir / PDF</button>
      </div>
    </div>
    <div class="fb-slot" data-fb-slot=""></div>
    <div class="rel-conteudo">
      <p class="rel-resumo">${esc(rel.resumo)}</p>
      ${itens}
      <h3>Comparativo</h3>
      <p>${esc(rel.comparativo)}</p>
      ${rel.proximos_passos?.length ? `<h3>Próximos passos</h3>${lista(rel.proximos_passos, "passos")}` : ""}
    </div>`;
  $("#relatorio").hidden = false;
  $("#relatorio").classList.remove("recolhido");
  state.ranking = Object.fromEntries(rel.ranking.map((r, n) => [r.id, n + 1]));
  if (state.mode === "mapa") renderMap(visibleItems());  // numera os escolhidos no mapa
  marcarAvaliados();
}

// Imprime só depois que as fotos carregaram (até 8 s), com o relatório expandido
async function imprimirRelatorio() {
  const rel = $("#relatorio");
  rel.classList.remove("recolhido");
  const toggle = $("#rel-toggle");
  if (toggle) toggle.textContent = "Recolher";
  const pendentes = [...rel.querySelectorAll("img")].filter((img) => !img.complete);
  await Promise.race([
    Promise.all(pendentes.map((img) => new Promise((ok) => { img.onload = img.onerror = ok; }))),
    new Promise((ok) => setTimeout(ok, 8000)),
  ]);
  window.print();
}

// Recolher/expandir, imprimir e "ver no mapa" (o relatório longo empurrava o mapa para baixo)
$("#relatorio").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "rel-print") return imprimirRelatorio();
  if (b.id === "rel-toggle") {
    const recolhido = $("#relatorio").classList.toggle("recolhido");
    b.textContent = recolhido ? "Expandir" : "Recolher";
    return;
  }
  if ("avaliar" in b.dataset) {
    const slot = slotAvaliacao(b.dataset.avaliar);
    if (slot) slot.innerHTML = formAvaliacao(b.dataset.avaliar);
    return;
  }
  if ("cancelar" in b.dataset) {
    b.closest(".fb-form")?.remove();
    return;
  }
  if (b.dataset.verMapa) {
    if (state.mode !== "mapa") document.querySelector('[data-mode="mapa"]').click();
    setTimeout(() => {
      $("#map").scrollIntoView({ behavior: "smooth", block: "center" });
      state.markers[b.dataset.verMapa.replace(":", "-")]?.openPopup();
    }, 120);
  }
});

async function carregarRelatorio(historicoId) {
  try {
    renderRelatorio(await api(`/historico/${historicoId}/relatorio`));
  } catch { /* busca sem relatório ainda */ }
}

$("#rel-btn").addEventListener("click", async () => {
  if (!state.result?.historico_id) return;
  const top = Math.round(+$("#rel-top").value);
  if (!(top >= 1 && top <= 40)) return ($("#rel-msg").textContent = "Escolha de 1 a 40 imóveis.");
  const btn = $("#rel-btn");
  btn.disabled = true;
  $("#rel-msg").textContent = `A IA está analisando os candidatos… (${top > 15 ? "1–3 min" : "até 1 min"})`;
  try {
    const rel = await api(`/historico/${state.result.historico_id}/relatorio`, { method: "POST", json: { top } });
    $("#rel-msg").textContent = "";
    renderRelatorio(rel);
    $("#relatorio").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    $("#rel-msg").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

function visibleItems() {
  let items = [...state.result.imoveis];
  if (state.soNovos) items = items.filter((i) => i.novo);
  if ($("#f-pet").checked) items = items.filter((i) => i.aceita_pet === true);
  if ($("#f-vaga").checked) items = items.filter((i) => i.vagas > 0);
  const by = $("#sort").value;
  const n = (v, d) => (v == null ? d : v);
  const cmp = {
    distancia: (a, b) => n(a.distancia_km, 1e9) - n(b.distancia_km, 1e9),
    total: (a, b) => n(a.total_mensal, 1e9) - n(b.total_mensal, 1e9),
    aluguel: (a, b) => n(a.aluguel, 1e9) - n(b.aluguel, 1e9),
    area: (a, b) => n(b.area_m2, 0) - n(a.area_m2, 0),
  }[by];
  return cmp ? items.sort(cmp) : items;
}

function flag(label, v) {
  const cls = v === true ? "yes" : v === false ? "no" : "unk";
  const title = v === true ? "sim" : v === false ? "não" : "não informado";
  return `<span class="flag ${cls}" title="${label}: ${title}">${label}${v == null ? " ?" : ""}</span>`;
}

function card(i) {
  const foto = i.fotos?.[0];
  const andar = i.andar != null ? (i.andar === 0 ? "térreo" : `${i.andar}º andar`) : i.andar_alto ? "andar alto" : null;
  const specs = [
    i.quartos != null && `🛏 ${i.quartos} quarto${i.quartos === 1 ? "" : "s"}`,
    i.banheiros != null && `🚿 ${i.banheiros}`,
    i.area_m2 != null && `📐 ${i.area_m2} m²`,
    `🚗 ${i.vagas == null ? "?" : i.vagas} vaga${i.vagas === 1 ? "" : "s"}`,
    andar && `🏢 ${andar}`,
  ].filter(Boolean);
  return `
    <article class="card" id="card-${esc(i.fonte)}-${esc(i.id)}">
      <div class="card-img">
        ${foto ? `<img src="${esc(safeUrl(foto))}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
        <span class="tipo">${esc(ROTULO_TIPO[i.tipo] || i.tipo || "imóvel")}</span>
        ${i.novo ? `<span class="novo">🆕 Novo</span>` : ""}
        ${i.distancia_km != null ? `<span class="dist">${i.distancia_km.toLocaleString("pt-BR")} km</span>` : ""}
      </div>
      <div class="card-body">
        <div>
          <div class="price">${brl(i.total_mensal ?? i.aluguel)} <small>/mês total</small>
            ${i.preco_anterior != null ? `<span class="era ${i.preco_anterior > (i.total_mensal ?? 0) ? "caiu" : "subiu"}">era ${brl(i.preco_anterior)}</span>` : ""}</div>
          <div class="breakdown">
            <span>Aluguel ${brl(i.aluguel)}</span>
            <span>Cond. ${brl(i.condominio)}</span>
            <span>IPTU ${brl(i.iptu)}</span>
          </div>
        </div>
        <div class="addr"><strong>${esc(i.bairro || "")}</strong>${esc(i.endereco || "")}</div>
        <div class="specs">${specs.map((s) => `<span>${esc(s)}</span>`).join("")}</div>
        ${blocoOnibus(i)}
        <div class="flags">
          ${flag("Pet", i.aceita_pet)}
          ${flag("Seguro fiança", i.seguro_fianca)}
          ${flag("Fiador", i.fiador)}
          ${flag("Caução", i.caucao)}
          ${i.mobiliado != null ? flag("Mobiliado", i.mobiliado) : ""}
        </div>
        ${i.atende?.length ? `<div class="flags">${i.atende.map((a) => `<span class="flag yes">✓ ${esc(a)}</span>`).join("")}</div>` : ""}
        ${i.destaques?.length ? `<ul class="destaques">${i.destaques.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
        <div class="card-foot">
          <span class="muted">${esc(fonteNome(i.fonte))} · #${esc(i.id)}</span>
          <a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener noreferrer">Ver anúncio ↗</a>
        </div>
        <div class="card-mapa">${linksMapa(i)}
        </div>
        ${i.outras_fontes?.length ? `<div class="muted also">Também em: ${i.outras_fontes
          .map((o) => `<a href="${esc(safeUrl(o.url))}" target="_blank" rel="noopener noreferrer">${esc(fonteNome(o.fonte))}</a>`)
          .join(", ")}</div>` : ""}
      </div>
    </article>`;
}

function renderResults() {
  const items = visibleItems();
  $("#cards").innerHTML = items.length
    ? items.map(card).join("")
    : `<div class="empty">Nenhum imóvel com esses filtros. Tente aumentar o raio, o preço ou reduzir os quartos.</div>`;
  if (state.mode === "mapa") renderMap(items);
}

["#sort", "#f-pet", "#f-vaga"].forEach((s) => $(s).addEventListener("change", () => state.result && renderResults()));

function aplicarModo() {
  $$("[data-mode]").forEach((x) => x.classList.toggle("active", x.dataset.mode === state.mode));
  $("#map").hidden = state.mode !== "mapa";
}

$$("[data-mode]").forEach((b) =>
  b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    try {
      localStorage.setItem("house_modo", state.mode);
    } catch { /* storage indisponível */ }
    aplicarModo();
    if (state.result) renderResults();
  })
);
aplicarModo();

// ---------- Mapa ----------
function renderMap(items) {
  if (!window.L) {
    $("#map").textContent = "Não foi possível carregar o mapa.";
    return;
  }
  if (!state.map) {
    state.map = L.map("map");
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap",
    }).addTo(state.map);
  }
  const map = state.map;
  map.eachLayer((l) => !(l instanceof L.TileLayer) && map.removeLayer(l));
  state.markers = {};
  const pts = [];
  const ref = state.result.referencia;
  if (ref) {
    pts.push([ref.latitude, ref.longitude]);
    L.circleMarker([ref.latitude, ref.longitude], { radius: 8, color: "#b42318", fillOpacity: 0.9 })
      .bindTooltip(`Referência: ${esc(ref.bairro)}`).addTo(map);
    if (state.result.filtros.raio_km) {
      L.circle([ref.latitude, ref.longitude], { radius: state.result.filtros.raio_km * 1000, color: "#2f6fed", weight: 1, fillOpacity: 0.04 }).addTo(map);
    }
  }
  for (const i of items) {
    if (i.latitude == null) continue;
    pts.push([i.latitude, i.longitude]);
    const pos = state.ranking[`${i.fonte}:${i.id}`];
    const icone = pos ? { icon: L.divIcon({ className: "pin-rank", html: `<span>${pos}</span>`, iconSize: [28, 28], iconAnchor: [14, 14] }), zIndexOffset: 1000 - pos } : {};
    const m = L.marker([i.latitude, i.longitude], icone).addTo(map).bindPopup(`
      ${pos ? `<b>${pos}º no relatório da IA</b><br>` : ""}
      <strong>${brl(i.total_mensal ?? i.aluguel)}</strong> /mês<br>
      ${esc(ROTULO_TIPO[i.tipo] || i.tipo)} · ${i.quartos ?? "?"} qts · ${i.vagas ?? "?"} vg<br>
      ${esc(i.endereco || i.bairro || "")}<br>
      ${i.distancia_km != null ? `${i.distancia_km} km · ` : ""}<a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener noreferrer">ver anúncio</a>
      ${i.onibus ? `<br>🚌 ${i.onibus.linhas.length} linhas de ônibus a até ${i.onibus.raio_m} m` : ""}
      <br>${linksMapa(i)}`);
    state.markers[`${i.fonte}-${i.id}`] = m;
  }
  setTimeout(() => {
    map.invalidateSize();
    if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 15 });
    else map.setView([-19.92, -43.94], 12);
  }, 30);
}

// ---------- CSV ----------
$("#csv").addEventListener("click", () => {
  if (!state.result) return;
  const cols = ["fonte", "id", "tipo", "bairro", "endereco", "distancia_km", "quartos", "banheiros", "area_m2", "vagas", "andar",
    "aluguel", "condominio", "iptu", "total_mensal", "aceita_pet", "seguro_fianca", "fiador", "caucao", "titulo_capitalizacao",
    "mobiliado", "destaques", "url"];
  const cell = (v) => {
    const s = v == null ? "" : typeof v === "boolean" ? (v ? "sim" : "não") : Array.isArray(v) ? v.join(" | ") : String(v);
    return /[";\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [cols.join(";"), ...visibleItems().map((i) => cols.map((c) => cell(i[c])).join(";"))];
  const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `busca-${state.result.historico_id ?? "imoveis"}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// ---------- Histórico ----------
$$("[data-scope]").forEach((b) =>
  b.addEventListener("click", () => {
    state.histScope = b.dataset.scope;
    $$("[data-scope]").forEach((x) => x.classList.toggle("active", x === b));
    loadHistory();
  })
);

async function loadHistory() {
  $("#hist-msg").textContent = "";
  try {
    state.history = await api(state.histScope === "todos" ? "/admin/historico" : "/historico");
    renderHistory();
  } catch (err) {
    $("#hist-msg").textContent = err.message;
  }
}

function renderHistory() {
  const q = $("#hist-filter").value.trim().toLowerCase();
  const rows = state.history.filter((h) => !q || h.texto.toLowerCase().includes(q) || h.usuario.toLowerCase().includes(q));
  $("#hist-body").innerHTML = rows.length
    ? rows.map((h) => `
      <tr>
        <td>${dataHora(h.data)}</td>
        <td>${esc(h.usuario)}</td>
        <td class="texto">${esc(h.texto)}<div class="chips">${chipsFiltros(h.filtros)}</div></td>
        <td class="num">${h.total}${h.tem_relatorio ? ` <span class="badge" title="Tem relatório da IA">📝</span>` : ""}</td>
        <td class="actions">
          <button class="btn small" data-open="${h.id}" ${h.tem_resultados ? "" : "disabled title='Busca antiga, sem resultados salvos'"}>Abrir</button>
          <button class="btn small" data-rerun="${h.id}">Refazer</button>
          <button class="btn small ghost danger" data-del="${h.id}">Excluir</button>
        </td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="muted">Nenhuma busca ainda.</td></tr>`;
}
$("#hist-filter").addEventListener("input", renderHistory);

$("#hist-body").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.open) {
      const res = await api(`/historico/${b.dataset.open}`);
      switchView("buscar");
      $("#q").value = res.texto;
      showResult(res);
      carregarRelatorio(b.dataset.open);
    } else if (b.dataset.rerun) {
      const h = state.history.find((x) => String(x.id) === b.dataset.rerun);
      state.compararCom = h.tem_resultados ? h.id : null;
      switchView("buscar");
      $("#q").value = h.texto;
      $("#search-form").requestSubmit();
    } else if (b.dataset.del) {
      if (!confirm("Excluir esta busca do histórico?")) return;
      await api(`/historico/${b.dataset.del}`, { method: "DELETE" });
      state.history = state.history.filter((x) => String(x.id) !== b.dataset.del);
      renderHistory();
    }
  } catch (err) {
    $("#hist-msg").textContent = err.message;
  }
});

// ---------- Usuários (admin) ----------
async function loadUsers() {
  $("#users-msg").textContent = "";
  try {
    const users = await api("/admin/users");
    $("#users-body").innerHTML = users.map((u) => `
      <tr>
        <td><strong>${esc(u.username)}</strong>${u.is_active ? "" : ` <span class="badge pend">pendente</span>`}${u.must_change_password ? ` <span class="badge" title="Ainda não trocou a senha provisória">senha provisória</span>` : ""}</td>
        <td>${esc(u.full_name || "")}</td>
        <td>${esc(u.email || "")}</td>
        <td>${dataHora(u.created_at)}</td>
        <td><input type="checkbox" data-uid="${u.id}" data-field="is_admin" ${u.is_admin ? "checked" : ""} ${u.id === state.me.id ? "disabled" : ""}></td>
        <td><input type="checkbox" data-uid="${u.id}" data-field="is_active" ${u.is_active ? "checked" : ""} ${u.id === state.me.id ? "disabled" : ""}></td>
        <td class="actions">
          <button class="btn small" data-pass="${u.id}">Nova senha</button>
          <button class="btn small ghost danger" data-udel="${u.id}" ${u.id === state.me.id ? "disabled" : ""}>Excluir</button>
        </td>
      </tr>`).join("");
  } catch (err) {
    $("#users-msg").textContent = err.message;
  }
}

function usersMsg(text, ok = true) {
  $("#users-msg").textContent = text;
  $("#users-msg").classList.toggle("ok", ok);
}

$("#users-body").addEventListener("change", async (e) => {
  const cb = e.target;
  if (!cb.dataset.uid) return;
  try {
    await api(`/admin/users/${cb.dataset.uid}`, { method: "PATCH", json: { [cb.dataset.field]: cb.checked } });
    usersMsg("Usuário atualizado.");
  } catch (err) {
    cb.checked = !cb.checked;
    usersMsg(err.message, false);
  }
});

$("#users-body").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.pass) {
      const password = prompt("Nova senha (mínimo 6 caracteres):");
      if (!password) return;
      await api(`/admin/users/${b.dataset.pass}`, { method: "PATCH", json: { password } });
      usersMsg("Senha alterada.");
    } else if (b.dataset.udel) {
      if (!confirm("Excluir este usuário e todo o histórico dele?")) return;
      await api(`/admin/users/${b.dataset.udel}`, { method: "DELETE" });
      usersMsg("Usuário excluído.");
      loadUsers();
    }
  } catch (err) {
    usersMsg(err.message, false);
  }
});

$("#new-user").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    username: f.get("username"),
    password: f.get("password"),
    full_name: f.get("full_name") || null,
    email: f.get("email") || null,
  };
  try {
    await api(`/admin/users?is_admin=${f.get("is_admin") === "on"}`, { method: "POST", json: body });
    e.target.reset();
    usersMsg("Usuário criado.");
    loadUsers();
  } catch (err) {
    usersMsg(err.message, false);
  }
});

$("#diag-btn").addEventListener("click", async () => {
  const btn = $("#diag-btn");
  btn.disabled = true;
  $("#diag").innerHTML = `<span class="muted">Testando…</span>`;
  try {
    const d = await api("/admin/diagnostico");
    $("#diag").innerHTML = d.fontes
      .map((f) => `<span class="source ${f.ok ? "" : "off"}">${esc(fonteNome(f.fonte))}: ${f.ok ? "ok" : esc(f.status ? `bloqueado (HTTP ${f.status})` : f.erro)} · ${f.segundos}s</span>`)
      .concat([`<span class="source">${d.bairros_na_tabela} bairros na tabela</span>`,
        d.onibus?.pontos ? `<span class="source" title="${esc(d.onibus.fonte || "")}">🚌 ${d.onibus.linhas} linhas · ${d.onibus.pontos} pontos (GTFS BHTRANS ${esc((d.onibus.feeds?.[0]?.validade?.[1] || "").slice(0, 4))})</span>` : `<span class="source off">sem dados de ônibus</span>`,
        `<span class="source ${d.ia_ativa ? "ia" : "off"}">IA ${d.ia_ativa ? `ligada (${esc(d.ia_provedor)} · ${esc(d.ia_modelo)})` : "desligada"}</span>`])
      .join("");
  } catch (err) {
    $("#diag").innerHTML = `<span class="source off">${esc(err.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
});

// ---------- Base de ônibus (admin) ----------
const dataGtfs = (s) => (s && s.length === 8 ? `${s.slice(6, 8)}/${s.slice(4, 6)}/${s.slice(0, 4)}` : "?");

async function loadOnibusAdmin() {
  try {
    const o = await api("/admin/onibus");
    const feeds = (o.feeds || []).map((f) => `${esc(f.sistema)}: validade ${dataGtfs(f.validade?.[0])} a ${dataGtfs(f.validade?.[1])}${f.versao ? ` (versão ${esc(f.versao)})` : ""}`).join(" · ");
    const origem = o.origem?.tipo === "enviada pelo admin"
      ? `enviada por <b>${esc(o.origem.por || "?")}</b> em ${dataHora(o.origem.em)}`
      : "arquivo original do app";
    $("#onibus-atual").innerHTML = `<b>${o.linhas}</b> linhas · <b>${o.pontos}</b> pontos · ${origem}<br>${feeds}`;
    $("#onibus-original").hidden = o.origem?.tipo !== "enviada pelo admin";
  } catch (err) {
    $("#onibus-atual").textContent = err.message;
  }
}

$("#onibus-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const dados = new FormData(form);
  if (!dados.get("suplementar")?.name) dados.delete("suplementar");
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  $("#onibus-msg").textContent = "Enviando e processando… (até 1–2 min)";
  try {
    await api("/admin/onibus", { method: "POST", body: dados });
    form.reset();
    $("#onibus-msg").textContent = "Base atualizada. As próximas buscas já usam os dados novos.";
    loadOnibusAdmin();
  } catch (err) {
    $("#onibus-msg").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#onibus-original").addEventListener("click", async () => {
  if (!confirm("Descartar a base enviada e voltar ao arquivo original do app?")) return;
  try {
    await api("/admin/onibus", { method: "DELETE" });
    $("#onibus-msg").textContent = "Voltou à base original.";
    loadOnibusAdmin();
  } catch (err) {
    $("#onibus-msg").textContent = err.message;
  }
});

// ---------- Comparação com a busca anterior (ao refazer) ----------
const ROTULO_STATUS = {
  fora_do_ar: ["off", "fora do ar"],
  no_ar: ["", "ainda no ar"],
  nao_verificado: ["nd", "não verificado"],
};

function renderComparacao(c) {
  state.soNovos = false;
  if (!c) {
    $("#comparacao").hidden = true;
    return;
  }
  const saidos = c.removidos.map((r) => {
    const i = r.imovel;
    const [cls, rotulo] = ROTULO_STATUS[r.status] || ["nd", r.status];
    return `<li><span class="source ${cls}" title="${esc(r.detalhe)}">${rotulo}</span>
      ${esc(ROTULO_TIPO[i.tipo] || i.tipo || "imóvel")} · ${esc(i.bairro || "")} · ${brl(i.total_mensal ?? i.aluguel)}
      · <a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener noreferrer">anúncio ↗</a>
      <span class="muted">(${esc(fonteNome(i.fonte))})</span></li>`;
  }).join("");
  $("#comparacao").innerHTML = `
    <div class="cmp-head">
      <span>🔄 Comparado com a busca de <b>${dataHora(c.data_anterior)}</b> (${c.total_anterior} imóveis):</span>
      <span class="source ia">🆕 ${c.novos} novo${c.novos === 1 ? "" : "s"}</span>
      <span class="source off">${c.removidos.length} saíram${c.fora_do_ar ? ` · ${c.fora_do_ar} fora do ar` : ""}</span>
      ${c.mudou_preco ? `<span class="source">${c.mudou_preco} mudaram de preço</span>` : ""}
      ${c.novos ? `<label class="inline"><input type="checkbox" id="so-novos"> mostrar só os novos</label>` : ""}
    </div>
    ${c.removidos.length ? `<details><summary>Ver os ${c.removidos.length} que saíram desde a última busca</summary><ul class="saidos">${saidos}</ul></details>` : ""}`;
  $("#comparacao").hidden = false;
  $("#so-novos")?.addEventListener("change", (e) => {
    state.soNovos = e.target.checked;
    renderResults();
  });
}

// ---------- Avaliação do relatório (feedback -> regras) ----------
function formAvaliacao(imovelId) {
  const estrelas = [1, 2, 3, 4, 5].map((n) => `<label class="inline"><input type="radio" name="nota" value="${n}" required> ${"★".repeat(n)}</label>`).join("");
  return `
    <form class="fb-form" data-imovel="${esc(imovelId)}">
      <div class="estrelas">${estrelas}</div>
      <textarea name="justificativa" rows="2" minlength="3" maxlength="1000" required
        placeholder="Por que essa nota? Ex.: casa geminada não é o que procuro; condomínio alto deveria pesar contra"></textarea>
      <div class="fb-opcoes">
        <label class="inline"><input type="checkbox" name="gerar_regra" checked> transformar em regra para a IA</label>
        <label class="inline"><input type="checkbox" name="compartilhar" checked> compartilhar a regra com todos</label>
      </div>
      <div class="row">
        <button class="btn small primary" type="submit">Enviar avaliação</button>
        <button class="btn small ghost" type="button" data-cancelar>Cancelar</button>
        <span class="fb-msg muted"></span>
      </div>
    </form>`;
}

function slotAvaliacao(imovelId) {
  return [...$$("[data-fb-slot]")].find((el) => el.dataset.fbSlot === imovelId);
}

function mostrarAvaliado(imovelId, nota, texto, regra) {
  const slot = slotAvaliacao(imovelId);
  if (!slot) return;
  slot.innerHTML = `<div class="fb-ok">✓ Avaliado com ${"★".repeat(nota)} — ${esc(texto)}
    ${regra ? `<br>🧠 Regra ${regra.compartilhada ? "compartilhada" : "privada"} criada: “${esc(regra.texto)}” <span class="muted">(edite em Regras da IA)</span>` : ""}</div>`;
}

async function marcarAvaliados() {
  if (!state.result?.historico_id) return;
  try {
    const fbs = await api(`/historico/${state.result.historico_id}/feedback`);
    for (const f of fbs) mostrarAvaliado(f.imovel_id || "", f.nota, f.justificativa, null);
  } catch { /* sem avaliações */ }
}

$("#relatorio").addEventListener("submit", async (e) => {
  const form = e.target.closest(".fb-form");
  if (!form) return;
  e.preventDefault();
  const f = new FormData(form);
  const imovelId = form.dataset.imovel;
  const msg = form.querySelector(".fb-msg");
  const corpo = {
    imovel_id: imovelId || null,
    nota: +f.get("nota"),
    justificativa: f.get("justificativa"),
    gerar_regra: f.get("gerar_regra") === "on",
    compartilhar: f.get("compartilhar") === "on",
  };
  form.querySelector("button[type=submit]").disabled = true;
  msg.textContent = corpo.gerar_regra ? "Salvando e gerando a regra…" : "Salvando…";
  try {
    const r = await api(`/historico/${state.result.historico_id}/feedback`, { method: "POST", json: corpo });
    mostrarAvaliado(imovelId, corpo.nota, corpo.justificativa, r.regra);
    if (r.aviso) slotAvaliacao(imovelId)?.insertAdjacentHTML("beforeend", `<div class="muted">${esc(r.aviso)}</div>`);
  } catch (err) {
    msg.textContent = err.message;
    form.querySelector("button[type=submit]").disabled = false;
  }
});

// ---------- Regras da IA ----------
async function loadRegras() {
  $("#regras-msg").textContent = "";
  try {
    state.regras = await api("/regras");
    renderRegras();
  } catch (err) {
    $("#regras-msg").textContent = err.message;
  }
}

function renderRegras() {
  const pode = (r) => r.minha || state.me?.is_admin;
  $("#regras-body").innerHTML = state.regras.length
    ? state.regras.map((r) => `
      <tr data-regra="${r.id}">
        <td class="texto regra-texto">${esc(r.texto)}</td>
        <td>${esc(r.autor)}${r.minha ? ` <span class="badge">você</span>` : ""}</td>
        <td>${r.origem === "feedback" ? "avaliação" : "manual"}<div class="muted">${dataHora(r.updated_at)}</div></td>
        <td><input type="checkbox" data-campo="compartilhada" ${r.compartilhada ? "checked" : ""} ${pode(r) ? "" : "disabled"}></td>
        <td><input type="checkbox" data-campo="ativa" ${r.ativa ? "checked" : ""} ${pode(r) ? "" : "disabled"}></td>
        <td class="actions">${pode(r) ? `<button class="btn small" data-editar="${r.id}">Editar</button>
          <button class="btn small ghost danger" data-excluir="${r.id}">Excluir</button>` : ""}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="muted">Nenhuma regra ainda. Avalie um relatório da IA ou escreva uma regra acima.</td></tr>`;
}

$("#nova-regra").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/regras", { method: "POST", json: { texto: f.get("texto"), compartilhada: f.get("compartilhada") === "on" } });
    e.target.reset();
    e.target.querySelector("[name=compartilhada]").checked = true;
    loadRegras();
  } catch (err) {
    $("#regras-msg").textContent = err.message;
  }
});

$("#regras-body").addEventListener("change", async (e) => {
  const cb = e.target;
  const id = cb.closest("tr")?.dataset.regra;
  if (!cb.dataset.campo || !id) return;
  try {
    await api(`/regras/${id}`, { method: "PATCH", json: { [cb.dataset.campo]: cb.checked } });
  } catch (err) {
    cb.checked = !cb.checked;
    $("#regras-msg").textContent = err.message;
  }
});

$("#regras-body").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const tr = b.closest("tr");
  const celula = tr.querySelector(".regra-texto");
  try {
    if (b.dataset.excluir) {
      if (!confirm("Excluir esta regra? A IA deixa de segui-la.")) return;
      await api(`/regras/${b.dataset.excluir}`, { method: "DELETE" });
      loadRegras();
    } else if (b.dataset.editar) {
      const atual = state.regras.find((r) => String(r.id) === b.dataset.editar).texto;
      celula.innerHTML = `<textarea rows="2" maxlength="500">${esc(atual)}</textarea>
        <div class="row"><button class="btn small primary" data-salvar="${b.dataset.editar}">Salvar</button>
        <button class="btn small ghost" data-cancelar-edicao>Cancelar</button></div>`;
    } else if (b.dataset.salvar) {
      const texto = celula.querySelector("textarea").value.trim();
      await api(`/regras/${b.dataset.salvar}`, { method: "PATCH", json: { texto } });
      loadRegras();
    } else if ("cancelarEdicao" in b.dataset) {
      renderRegras();
    }
  } catch (err) {
    $("#regras-msg").textContent = err.message;
  }
});

// ---------- Início ----------
if (state.token) enterApp();
else showAuth();
