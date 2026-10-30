"use strict";

// ---------- Estado ----------
const state = {
  token: localStorage.getItem("house_token"),
  me: null,
  result: null,       // SearchResponse exibido
  mode: "lista",
  histScope: "meu",
  history: [],
  map: null,
  markers: {},
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
  if (name === "usuarios") loadUsers();
  if (name === "buscar" && state.mode === "mapa") setTimeout(() => state.map?.invalidateSize(), 50);
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
    const res = await api("/buscar", { method: "POST", json: { texto, fontes, max_detalhes: +$("#max-detalhes").value || 200 } });
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
  } else if (ia && !ia.ativa) {
    iaChips.push(`<span class="source off" title="Defina OPENAI_API_KEY ou ANTHROPIC_API_KEY no servidor para ativar">IA desligada (sem chave da API)</span>`);
  }
  for (const e of ia?.erros || []) iaChips.push(`<span class="source off">IA: ${esc(e)}</span>`);
  $("#res-sources").innerHTML = res.fontes
    .map((s) => `<span class="source ${s.ok ? "" : "off"}" title="${esc(s.mensagem)}">${esc(fonteNome(s.fonte))}: ${s.ok ? `${s.encontrados} encontrados` : esc(s.mensagem)}</span>`)
    .concat(iaChips)
    .join("");
  $("#interp").innerHTML = chipsFiltros(res.filtros);
  $("#relatorio").hidden = true;
  $("#rel-msg").textContent = "";
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
        ${foto ? `<img class="rel-foto" src="${esc(safeUrl(foto))}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<div class="rel-foto"></div>`}
        <div class="rel-corpo">
          <h3>${esc(r.titulo_curto)}</h3>
          <div class="rel-meta">
            <strong>${brl(i.total_mensal ?? i.aluguel)}</strong>/mês
            · ${esc(i.endereco || i.bairro || "")}
            ${i.distancia_km != null ? ` · ${i.distancia_km.toLocaleString("pt-BR")} km` : ""}
            · <a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener noreferrer">ver anúncio ↗</a>
          </div>
          <p>${esc(r.por_que)}</p>
          <div class="rel-cols">
            <div><h4>Pontos fortes</h4>${lista(r.pontos_fortes, "ok")}</div>
            <div><h4>Atenção</h4>${lista(r.pontos_de_atencao, "warn")}</div>
            <div><h4>Perguntar ao anunciante</h4>${lista(r.perguntas_para_o_anunciante, "ask")}</div>
          </div>
        </div>
      </article>`;
  }).join("");
  $("#relatorio").innerHTML = `
    <div class="rel-head">
      <div>
        <h2>📝 Relatório: os ${rel.ranking.length} imóveis mais alinhados à sua busca</h2>
        <div class="muted">Gerado por ${esc(rel.modelo)} em ${dataHora(rel.gerado_em)}, entre ${rel.candidatos_avaliados} candidatos.
          A IA só usa os dados dos anúncios — confirme tudo com o anunciante.</div>
      </div>
      <button class="btn small" id="rel-print">Imprimir / PDF</button>
    </div>
    <p class="rel-resumo">${esc(rel.resumo)}</p>
    ${itens}
    <h3>Comparativo</h3>
    <p>${esc(rel.comparativo)}</p>
    ${rel.proximos_passos?.length ? `<h3>Próximos passos</h3>${lista(rel.proximos_passos, "passos")}` : ""}`;
  $("#relatorio").hidden = false;
  $("#rel-print").addEventListener("click", () => window.print());
}

async function carregarRelatorio(historicoId) {
  try {
    renderRelatorio(await api(`/historico/${historicoId}/relatorio`));
  } catch { /* busca sem relatório ainda */ }
}

$("#rel-btn").addEventListener("click", async () => {
  if (!state.result?.historico_id) return;
  const top = Math.round(+$("#rel-top").value);
  if (!(top >= 1 && top <= 20)) return ($("#rel-msg").textContent = "Escolha de 1 a 20 imóveis.");
  const btn = $("#rel-btn");
  btn.disabled = true;
  $("#rel-msg").textContent = "A IA está analisando os candidatos… (até 1 min)";
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
        ${i.distancia_km != null ? `<span class="dist">${i.distancia_km.toLocaleString("pt-BR")} km</span>` : ""}
      </div>
      <div class="card-body">
        <div>
          <div class="price">${brl(i.total_mensal ?? i.aluguel)} <small>/mês total</small></div>
          <div class="breakdown">
            <span>Aluguel ${brl(i.aluguel)}</span>
            <span>Cond. ${brl(i.condominio)}</span>
            <span>IPTU ${brl(i.iptu)}</span>
          </div>
        </div>
        <div class="addr"><strong>${esc(i.bairro || "")}</strong>${esc(i.endereco || "")}</div>
        <div class="specs">${specs.map((s) => `<span>${esc(s)}</span>`).join("")}</div>
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

$$("[data-mode]").forEach((b) =>
  b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    $$("[data-mode]").forEach((x) => x.classList.toggle("active", x === b));
    $("#map").hidden = state.mode !== "mapa";
    if (state.result) renderResults();
  })
);

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
    const m = L.marker([i.latitude, i.longitude]).addTo(map).bindPopup(`
      <strong>${brl(i.total_mensal ?? i.aluguel)}</strong> /mês<br>
      ${esc(ROTULO_TIPO[i.tipo] || i.tipo)} · ${i.quartos ?? "?"} qts · ${i.vagas ?? "?"} vg<br>
      ${esc(i.endereco || i.bairro || "")}<br>
      ${i.distancia_km != null ? `${i.distancia_km} km · ` : ""}<a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener noreferrer">ver anúncio</a>`);
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
        `<span class="source ${d.ia_ativa ? "ia" : "off"}">IA ${d.ia_ativa ? `ligada (${esc(d.ia_provedor)} · ${esc(d.ia_modelo)})` : "desligada"}</span>`])
      .join("");
  } catch (err) {
    $("#diag").innerHTML = `<span class="source off">${esc(err.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
});

// ---------- Início ----------
if (state.token) enterApp();
else showAuth();
