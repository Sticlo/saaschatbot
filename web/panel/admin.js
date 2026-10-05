(function () {
  "use strict";

  const API = "/api/v1/platform";
  const $ = (id) => document.getElementById(id);

  const state = {
    tab: "tenants",
    tenants: [],
    accounts: [],
    plans: [],
    filter: "all",
    selectedId: null,
    detail: null,
    historyKind: "all",
    history: [],
    historyDone: false,
  };

  const ACTION_LABELS = {
    "tenant.registered": "Se registró la empresa",
    "tenant.updated": "Actualizó datos de la empresa",
    "tenant.profile_updated": "Actualizó el perfil del negocio",
    "tenant.disclaimer_accepted": "Aceptó el aviso legal",
    "tenant.interest_alert_updated": "Cambió las alertas de interesados",
    "user.login": "Inició sesión",
    "user.invited": "Invitó a un usuario",
    "user.deactivated": "Desactivó un usuario",
    "user.role_updated": "Cambió el rol de un usuario",
    "user.password_changed": "Cambió su contraseña",
    "user.password_reset": "Restableció su contraseña",
    "user.logout_all": "Cerró sesión en todos los equipos",
    "whatsapp.connect_started": "Empezó a conectar WhatsApp",
    "whatsapp.reconnect_started": "Reconectó WhatsApp",
    "whatsapp.disconnected": "Desconectó WhatsApp desde el panel",
    "whatsapp.binding_reset": "Reinició el vínculo de WhatsApp",
    "whatsapp.chats_synced": "Sincronizó chats",
    "whatsapp.contacts_enriched": "Actualizó contactos",
    "message.sent_manual": "Envió un mensaje manual",
    "message.sent_shortcut": "Envió un atajo",
    "quick_shortcuts.updated": "Editó los atajos / catálogo",
    "appointments.created": "Creó una cita",
    "appointments.updated": "Editó una cita",
    "appointments.deleted": "Borró una cita",
    "appointments.schedule_updated": "Cambió el horario de citas",
    "outbound.template_created": "Creó una plantilla",
    "outbound.template_updated": "Editó una plantilla",
    "outbound.template_deleted": "Borró una plantilla",
    "outbound.business_profile_updated": "Actualizó el perfil comercial",
    "subscription.activated": "Activó su plan (pago)",
    "subscription.cancelled": "Canceló su plan",
    "subscription.resumed": "Reanudó su plan",
    "billing.auto_renew_authorized": "Autorizó el cobro automático",
    "billing.payment_method_removed": "Quitó su método de pago",
  };

  const WA_LABELS = {
    connected: ["Conectado", "ok"],
    connecting: ["Conectando", "warn"],
    disconnected: ["Sin conectar", "bad"],
    restricted: ["Restringido", "bad"],
    banned: ["Bloqueado", "bad"],
  };

  const SUB_LABELS = {
    trial: ["Prueba", ""],
    active: ["Pagando", "ok"],
    past_due: ["Pago pendiente", "warn"],
    cancelled: ["Cancelado", "bad"],
    none: ["Sin plan", "bad"],
  };

  function subLabel(sub) {
    if (sub.trial_expired) return ["Prueba vencida", "bad"];
    return SUB_LABELS[sub.status] || [sub.status, ""];
  }

  const ACTIVATION_LABELS = {
    pending: ["Sin activar", "warn"],
    active: ["WhatsApp conectado", "ok"],
    paying: ["Pagando", "ok"],
    suspended: ["Suspendida", "bad"],
  };

  const LOGIN_LABELS = {
    password: "Correo y contraseña",
    google: "Google",
    magic_link: "Enlace al correo",
  };

  const FILTERS = {
    tenants: [
      ["all", "Todas"],
      ["pending", "Sin activar"],
      ["problems", "Con problemas"],
      ["whatsapp", "Sin WhatsApp"],
      ["payment", "Pago pendiente"],
      ["trial", "En prueba"],
      ["trial_expired", "Prueba vencida"],
      ["suspended", "Suspendidas"],
    ],
    accounts: [
      ["all", "Todas"],
      ["pending", "Empresa sin activar"],
      ["password", "Correo y contraseña"],
      ["google", "Google"],
      ["never", "Nunca ha entrado"],
    ],
  };

  const HEADS = {
    tenants: ["Empresa", "Estado", "WhatsApp", "Plan", "IA hoy", "Chats", "Problemas", "Última actividad"],
    accounts: ["Correo", "Empresa", "Cómo entra", "Rol", "Estado de la empresa", "Registro", "Último ingreso"],
  };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function badge(text, tone) {
    return `<span class="badge ${tone || ""}">${esc(text)}</span>`;
  }

  function fmtDate(iso, withTime) {
    if (!iso) return "—";
    const d = new Date(iso);
    const opts = withTime
      ? { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }
      : { day: "2-digit", month: "short", year: "numeric" };
    return d.toLocaleString("es-CO", opts);
  }

  function ago(iso) {
    if (!iso) return "—";
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 1) return "ahora";
    if (mins < 60) return `hace ${mins} min`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `hace ${hours} h`;
    const days = Math.round(hours / 24);
    return days < 60 ? `hace ${days} d` : fmtDate(iso);
  }

  function limitText(value) {
    if (value === null || value === undefined) return "—";
    if (Number(value) < 0) return "0 (sin plan)";
    return Number(value) === 0 ? "sin límite" : String(value);
  }

  let toastTimer = null;
  function toast(msg, isError) {
    const el = $("admin-toast");
    el.textContent = msg;
    el.classList.toggle("error", !!isError);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
  }

  async function api(path, options = {}) {
    const res = await fetch(`${API}${path}`, {
      credentials: "include",
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) {
      const err = new Error(typeof data?.detail === "string" ? data.detail : `Error ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ---------- Lista ----------

  function matchesTenantFilter(t) {
    const sub = t.subscription || {};
    switch (state.filter) {
      case "pending": return t.activation === "pending";
      case "problems": return t.problems_7d > 0 || (t.whatsapp_status !== "connected" && t.conversations > 0);
      case "whatsapp": return t.whatsapp_status !== "connected";
      case "payment": return !!sub.needs_payment;
      case "trial": return sub.status === "trial" && !sub.trial_expired;
      case "trial_expired": return !!sub.trial_expired;
      case "suspended": return !t.is_active;
      default: return true;
    }
  }

  function matchesAccountFilter(a) {
    switch (state.filter) {
      case "pending": return a.activation === "pending";
      case "password": return a.login_method === "password";
      case "google": return a.login_method === "google";
      case "never": return !a.last_login_at;
      default: return true;
    }
  }

  function renderListChrome() {
    document.querySelectorAll("#admin-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
    $("admin-filters").innerHTML = FILTERS[state.tab].map(([key, label]) =>
      `<button type="button" data-filter="${key}" class="${key === state.filter ? "active" : ""}">${esc(label)}</button>`
    ).join("");
    $("admin-head").innerHTML = `<tr>${HEADS[state.tab].map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
    $("admin-search").placeholder = state.tab === "tenants"
      ? "Buscar empresa, slug o correo…"
      : "Buscar correo, nombre o empresa…";
  }

  function renderKpis() {
    let cards;
    if (state.tab === "tenants") {
      const all = state.tenants;
      cards = [
        [all.length, "Empresas"],
        [all.filter((t) => t.activation === "pending").length, "Sin activar (nunca conectaron WhatsApp)"],
        [all.filter((t) => t.activation === "paying").length, "Pagando"],
        [all.filter((t) => t.problems_7d > 0).length, "Con problemas (7 días)"],
      ];
    } else {
      const all = state.accounts;
      cards = [
        [all.length, "Cuentas"],
        [all.filter((a) => a.login_method === "password").length, "Con correo y contraseña"],
        [all.filter((a) => a.login_method === "google").length, "Con Google"],
        [all.filter((a) => a.activation === "pending").length, "Empresa sin activar"],
      ];
    }
    $("admin-kpis").innerHTML = cards
      .map(([n, label]) => `<div class="admin-kpi"><b>${n}</b><span>${esc(label)}</span></div>`).join("");
  }

  function renderRows() {
    if (state.tab === "accounts") return renderAccountRows();
    const rows = state.tenants.filter(matchesTenantFilter);
    $("admin-empty").classList.toggle("hidden", rows.length > 0);
    $("admin-rows").innerHTML = rows.map((t) => {
      const [actText, actTone] = ACTIVATION_LABELS[t.activation] || [t.activation, ""];
      const [waText, waTone] = WA_LABELS[t.whatsapp_status] || [t.whatsapp_status, ""];
      const sub = t.subscription || {};
      const [subText, subTone] = subLabel(sub);
      const extra = [];
      if (!t.ai_enabled) extra.push(badge("IA apagada", "warn"));
      if (t.has_overrides) extra.push(badge("Ajustes", ""));
      return `
        <tr data-id="${esc(t.id)}" class="${t.id === state.selectedId ? "selected" : ""}">
          <td><b>${esc(t.business_name)}</b> ${extra.join(" ")}
            <span class="sub">${esc(t.owner_email || t.slug)}</span></td>
          <td>${badge(actText, actTone)}</td>
          <td>${badge(waText, waTone)}</td>
          <td>${badge(subText, subTone)}<span class="sub">${esc(sub.plan_name || "")}</span></td>
          <td>${t.ai_replies_today}</td>
          <td>${t.conversations}<span class="sub">${t.interested} interesados</span></td>
          <td>${t.problems_7d ? badge(String(t.problems_7d), "bad") : '<span class="admin-muted">0</span>'}
            ${t.last_problem_at ? `<span class="sub">${esc(ago(t.last_problem_at))}</span>` : ""}</td>
          <td>${esc(ago(t.last_activity_at))}</td>
        </tr>`;
    }).join("");
  }

  function renderAccountRows() {
    const rows = state.accounts.filter(matchesAccountFilter);
    $("admin-empty").classList.toggle("hidden", rows.length > 0);
    $("admin-rows").innerHTML = rows.map((a) => {
      const [actText, actTone] = ACTIVATION_LABELS[a.activation] || [a.activation, ""];
      return `
        <tr data-id="${esc(a.tenant_id)}" class="${a.tenant_id === state.selectedId ? "selected" : ""}">
          <td><b>${esc(a.email)}</b>${a.is_active ? "" : " " + badge("Desactivada", "bad")}
            <span class="sub">${esc(a.full_name || "")}</span></td>
          <td>${esc(a.business_name)}</td>
          <td>${esc(LOGIN_LABELS[a.login_method] || a.login_method)}</td>
          <td>${esc(a.role)}</td>
          <td>${badge(actText, actTone)}</td>
          <td>${esc(fmtDate(a.created_at))}</td>
          <td>${a.last_login_at ? esc(ago(a.last_login_at)) : badge("Nunca", "warn")}</td>
        </tr>`;
    }).join("");
  }

  async function loadList() {
    const q = $("admin-search").value.trim();
    const qs = q ? `?q=${encodeURIComponent(q)}` : "";
    try {
      if (state.tab === "tenants") state.tenants = await api(`/tenants${qs}`);
      else state.accounts = await api(`/accounts${qs}`);
      renderKpis();
      renderRows();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function switchTab(tab) {
    if (tab === state.tab) return;
    state.tab = tab;
    state.filter = "all";
    renderListChrome();
    renderKpis();
    renderRows();
    loadList();
  }

  // ---------- Ficha ----------

  function renderDetail() {
    const d = state.detail;
    const el = $("admin-detail");
    if (!d) {
      el.innerHTML = '<p class="admin-muted admin-detail-empty">Elige una empresa para ver qué le ha pasado y ajustar su plan.</p>';
      return;
    }
    const [waText, waTone] = WA_LABELS[d.whatsapp.status] || [d.whatsapp.status, ""];
    const sub = d.subscription || {};
    const [subText, subTone] = subLabel(sub);
    const usage = d.usage || {};
    const overrides = d.overrides || {};
    const ovLimits = overrides.limits || {};
    const owner = (d.users || []).find((u) => u.role === "owner");

    const planOptions = state.plans.map((p) =>
      `<option value="${esc(p.id)}" ${p.id === sub.plan_id ? "selected" : ""}>${esc(p.name)} — $${Number(p.price_cop).toLocaleString("es-CO")}</option>`
    ).join("");

    const featureChecks = Object.entries(d.catalog.features).map(([key, label]) =>
      `<label><input type="checkbox" data-feature="${esc(key)}" ${d.features[key] ? "checked" : ""} /> ${esc(label)}</label>`
    ).join("");

    const planMembers = d.plan_limits.max_team_members;
    const limitInputs = Object.entries(d.catalog.limits).map(([key, label]) => {
      const fromPlan = key === "max_team_members" ? planMembers : null;
      const placeholder = fromPlan !== null && fromPlan !== undefined ? `Plan: ${fromPlan}` : "Del plan";
      const value = ovLimits[key] ?? "";
      return `<label>${esc(label)}<input type="number" min="0" data-limit="${esc(key)}" value="${esc(value)}" placeholder="${esc(placeholder)}" /></label>`;
    }).join("");

    const usersRows = (d.users || []).map((u) => `
      <tr>
        <td>${esc(u.email)}${u.is_active ? "" : " " + badge("inactivo", "bad")}</td>
        <td>${esc(u.role)}</td>
        <td class="admin-muted">${esc(LOGIN_LABELS[u.login_method] || u.login_method || "")}</td>
        <td class="admin-muted">${esc(u.last_login_at ? ago(u.last_login_at) : "nunca")}</td>
      </tr>`).join("");
    const [actText, actTone] = ACTIVATION_LABELS[d.activation] || [d.activation, ""];

    const replyLimit = usage.plan_required ? "sin plan" : usage.daily_replies_unlimited ? "sin límite" : usage.daily_replies_limit;
    const trialEnd = esc(fmtDate(sub.trial_ends_at));
    const planNote = sub.trial_expired
      ? `la prueba terminó el ${trialEnd} · la IA no responde`
      : sub.status === "trial"
        ? `prueba hasta el ${trialEnd} (${sub.trial_days_left ?? "—"} d)`
        : sub.current_period_end ? `vence ${esc(fmtDate(sub.current_period_end))}${sub.auto_renew ? " · cobro automático" : ""}`
        : sub.is_paid ? "sin vencimiento · cortesía" : "";

    el.innerHTML = `
      <h2>${esc(d.business_name)}</h2>
      <div class="admin-muted">${esc(owner?.email || "")} · ${esc(d.slug)} · creada ${esc(fmtDate(d.created_at))}</div>
      <div class="admin-badges">
        ${badge(actText, actTone)}
        ${badge(`WhatsApp: ${waText}`, waTone)}
        ${badge(subText, subTone)}
        ${d.ai_global_enabled ? "" : badge("El cliente apagó la IA", "warn")}
        ${d.features.ai_replies ? "" : badge("IA pausada por ti", "warn")}
        ${d.alert_phone_set ? "" : badge("Sin teléfono de alertas", "warn")}
      </div>

      <h3>Cómo va</h3>
      <div class="admin-grid">
        <div class="admin-card"><span>WhatsApp</span><b>${esc(d.whatsapp.phone_number || waText)}</b>
          <small>${d.whatsapp.status === "connected"
            ? `conectado ${esc(ago(d.whatsapp.last_connected_at))}`
            : `se cayó ${esc(ago(d.whatsapp.last_disconnected_at))}`}</small></div>
        <div class="admin-card"><span>Plan</span><b>${esc(sub.plan_name || "—")}</b>
          <small>${planNote}</small></div>
        <div class="admin-card"><span>IA hoy</span><b>${usage.daily_replies_used ?? 0} / ${esc(replyLimit)}</b>
          <small>respuestas · ${usage.daily_classifications_used ?? 0} clasificaciones</small></div>
        <div class="admin-card"><span>Chats</span><b>${d.stats.conversations} · ${d.stats.interested} interesados</b>
          <small>${d.stats.new_conversations_7d} nuevos en 7 días · último ${esc(ago(d.stats.last_activity_at))}</small></div>
      </div>

      <h3>Plan y cortesías</h3>
      <div class="admin-row">
        <label for="admin-plan">Plan</label>
        <select id="admin-plan">${planOptions}</select>
        <button type="button" class="admin-btn admin-btn-ghost" data-action="plan">Cambiar plan</button>
      </div>
      <div class="admin-row">
        <label for="admin-days">Regalar días de plan</label>
        <input type="number" id="admin-days" min="1" max="365" placeholder="30" />
        <button type="button" class="admin-btn admin-btn-ghost" data-action="days">Regalar</button>
      </div>
      <div class="admin-row">
        <label>Cortesía permanente</label>
        <button type="button" class="admin-btn admin-btn-ghost" data-action="unlimited">Dar plan sin vencimiento</button>
      </div>
      <div class="admin-row">
        <label for="admin-trial">Extender prueba (días)</label>
        <input type="number" id="admin-trial" min="1" max="60" placeholder="3" />
        <button type="button" class="admin-btn admin-btn-ghost" data-action="trial">Dar</button>
      </div>
      <div class="admin-row">
        <label>Contador de IA</label>
        <button type="button" class="admin-btn admin-btn-ghost" data-action="reset-ai">Reiniciar el de hoy</button>
      </div>

      <h3>Funciones y límites de esta empresa</h3>
      <div class="admin-checks">${featureChecks}</div>
      <div class="admin-limits">${limitInputs}</div>
      <p class="admin-muted" style="margin:0 0 8px;font-size:12px">Vacío = lo que diga su plan. Efectivo ahora:
        ${esc(limitText(d.limits.ai_daily_replies))} respuestas/día ·
        ${esc(limitText(d.limits.ai_daily_classifications))} clasificaciones/día ·
        ${esc(d.limits.max_team_members ?? "—")} usuarios.</p>
      <label class="admin-muted" for="admin-note">Nota interna (solo la ves tú)</label>
      <textarea id="admin-note" maxlength="2000" placeholder="Ej: le dimos 15 días por la caída del 3 de oct">${esc(overrides.note || "")}</textarea>
      <div class="admin-row" style="margin-top:8px">
        <button type="button" class="admin-btn" data-action="overrides">Guardar ajustes</button>
      </div>

      <h3>Acceso</h3>
      <div class="admin-row">
        ${d.is_active
          ? '<button type="button" class="admin-btn admin-btn-danger" data-action="suspend">Suspender empresa</button><span class="admin-muted">No podrán entrar al panel y la IA deja de responder.</span>'
          : '<button type="button" class="admin-btn" data-action="reactivate">Reactivar empresa</button>'}
      </div>

      <h3>Equipo</h3>
      <table class="admin-users"><tbody>${usersRows}</tbody></table>

      <h3>Historial</h3>
      <div class="admin-history-tabs" id="admin-history-tabs">
        ${[["all", "Todo"], ["problems", "Problemas"], ["system", "Sistema"], ["platform", "Tus cambios"]]
          .map(([k, label]) => `<button type="button" data-kind="${k}" class="${k === state.historyKind ? "active" : ""}">${label}</button>`).join("")}
      </div>
      <ul class="admin-history" id="admin-history"></ul>
      <div class="admin-row" style="margin-top:8px">
        <button type="button" class="admin-btn admin-btn-ghost" id="admin-history-more">Cargar más</button>
      </div>
    `;
    renderHistory();
  }

  function historyText(item) {
    if (item.message) return item.message;
    return ACTION_LABELS[item.action] || item.action;
  }

  function renderHistory() {
    const list = $("admin-history");
    if (!list) return;
    if (!state.history.length) {
      list.innerHTML = '<li><span></span><span class="admin-muted">Sin eventos todavía.</span></li>';
    } else {
      list.innerHTML = state.history.map((item) => {
        const kind = item.is_problem ? "problem" : item.action.startsWith("platform.") ? "platform" : "";
        const who = item.user_email ? `<div class="who">${esc(item.user_email)}</div>` : "";
        return `<li class="${kind}"><time>${esc(fmtDate(item.created_at, true))}</time>
          <div><div class="what">${esc(historyText(item))}</div>${who}</div></li>`;
      }).join("");
    }
    const more = $("admin-history-more");
    if (more) more.classList.toggle("hidden", state.historyDone);
  }

  const HISTORY_PAGE = 50;

  async function loadHistory(reset) {
    if (!state.selectedId) return;
    const offset = reset ? 0 : state.history.length;
    try {
      const rows = await api(
        `/tenants/${state.selectedId}/history?kind=${state.historyKind}&limit=${HISTORY_PAGE}&offset=${offset}`
      );
      state.history = reset ? rows : state.history.concat(rows);
      state.historyDone = rows.length < HISTORY_PAGE;
      renderHistory();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function selectTenant(id) {
    state.selectedId = id;
    state.historyKind = "all";
    state.history = [];
    renderRows();
    try {
      state.detail = await api(`/tenants/${id}`);
      renderDetail();
      loadHistory(true);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function collectOverrides() {
    const root = $("admin-detail");
    const features = {};
    root.querySelectorAll("[data-feature]").forEach((input) => {
      // Solo se guarda lo que se quita: activado es lo normal.
      if (!input.checked) features[input.dataset.feature] = false;
    });
    const limits = {};
    root.querySelectorAll("[data-limit]").forEach((input) => {
      const raw = input.value.trim();
      if (raw !== "") limits[input.dataset.limit] = Number(raw);
    });
    return { features, limits, note: $("admin-note").value };
  }

  async function patch(body, okMsg) {
    try {
      const res = await api(`/tenants/${state.selectedId}`, { method: "PATCH", body: JSON.stringify(body) });
      state.detail = res.tenant;
      renderDetail();
      loadHistory(true);
      loadList();
      toast(res.changed.length ? okMsg : "No había nada que cambiar");
    } catch (err) {
      toast(err.message, true);
    }
  }

  function intFrom(id) {
    const n = parseInt($(id).value, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  async function onDetailAction(action) {
    const name = state.detail?.business_name || "esta empresa";
    switch (action) {
      case "plan":
        return patch({ plan_id: $("admin-plan").value }, "Plan cambiado");
      case "days": {
        const days = intFrom("admin-days");
        if (!days) return toast("Escribe cuántos días", true);
        if (!confirm(`¿Regalar ${days} días de plan a ${name}?`)) return;
        return patch({ grant_paid_days: days }, `${days} días regalados`);
      }
      case "unlimited":
        if (!confirm(`¿Dar a ${name} su plan pagado sin fecha de vencimiento y sin cobros?`)) return;
        return patch({ grant_unlimited: true }, "Plan sin vencimiento activado");
      case "trial": {
        const days = intFrom("admin-trial");
        if (!days) return toast("Escribe cuántos días", true);
        return patch({ extend_trial_days: days }, `Prueba extendida ${days} días`);
      }
      case "reset-ai":
        return patch({ reset_ai_today: true }, "Contador de IA reiniciado");
      case "overrides":
        return patch({ overrides: collectOverrides() }, "Ajustes guardados");
      case "suspend":
        if (!confirm(`¿Suspender ${name}? No podrán entrar y la IA dejará de responder.`)) return;
        return patch({ is_active: false }, "Empresa suspendida");
      case "reactivate":
        return patch({ is_active: true }, "Empresa reactivada");
    }
  }

  // ---------- Arranque ----------

  function bind() {
    let searchTimer = null;
    $("admin-search").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadList, 300);
    });
    $("admin-refresh").addEventListener("click", () => {
      loadList();
      if (state.selectedId) selectTenant(state.selectedId);
    });
    $("admin-tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-tab]");
      if (btn) switchTab(btn.dataset.tab);
    });
    $("admin-filters").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-filter]");
      if (!btn) return;
      state.filter = btn.dataset.filter;
      document.querySelectorAll("#admin-filters button").forEach((b) => b.classList.toggle("active", b === btn));
      renderRows();
    });
    $("admin-rows").addEventListener("click", (e) => {
      const row = e.target.closest("tr[data-id]");
      if (row) selectTenant(row.dataset.id);
    });
    $("admin-detail").addEventListener("click", (e) => {
      const actionBtn = e.target.closest("[data-action]");
      if (actionBtn) {
        onDetailAction(actionBtn.dataset.action);
        return;
      }
      const tab = e.target.closest("#admin-history-tabs button[data-kind]");
      if (tab) {
        state.historyKind = tab.dataset.kind;
        document.querySelectorAll("#admin-history-tabs button").forEach((b) => b.classList.toggle("active", b === tab));
        loadHistory(true);
        return;
      }
      if (e.target.id === "admin-history-more") loadHistory(false);
    });
  }

  async function init() {
    try {
      const me = await api("/me");
      $("admin-email").textContent = me.email;
    } catch (err) {
      const gate = $("admin-gate-text");
      if (err.status === 401) {
        gate.innerHTML = 'Tu sesión no está activa. <a class="admin-link" href="/panel">Entra al panel</a> y vuelve a esta página.';
      } else if (err.status === 404) {
        gate.textContent = "Esta cuenta no tiene acceso a la consola de plataforma.";
      } else {
        gate.textContent = err.message;
      }
      return;
    }
    $("admin-gate").classList.add("hidden");
    $("admin-main").classList.remove("hidden");
    bind();
    try {
      state.plans = await api("/plans");
    } catch (err) {
      toast(err.message, true);
    }
    renderListChrome();
    await loadList();
  }

  init();
})();
