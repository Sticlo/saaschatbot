(() => {
  const host = location.hostname;
  if (
    (host === "localhost" || host === "127.0.0.1") &&
    location.port === "8000" &&
    location.pathname.startsWith("/panel")
  ) {
    location.replace(
      `${location.protocol}//${host}:4200${location.pathname}${location.search}${location.hash}`
    );
    return;
  }

  const API = "/api/v1";
  const LEGACY_STORAGE_KEY = "saaschatbot_token";
  const THEME_STORAGE_KEY = "omitel_panel_theme";
  const MAX_ALERT_RECIPIENTS = 5;

  const state = {
    token: null,
    user: null,
    tenant: null,
    conversations: [],
    activeId: null,
    messages: [],
    ws: null,
    canWrite: false,
    canManageGlobal: false,
    canConnectWa: false,
    wa: { status: "disconnected", qr_base64: null, phone_number: null, chatwoot_inbox_url: null },
    chatListTab: "interested",
    interestedCount: 0,
    subscription: null,
    syncInProgress: false,
    autoSyncRequested: false,
    syncWatchdog: null,
    searchQuery: "",
    searchPool: null,
    searchTimer: null,
    panelMode: "chats",
    aiProfile: null,
    quickShortcuts: [],
    shortcutsDraft: [],
    appointmentsDate: null,
    appointmentsDay: null,
    appointmentsStaffId: null,
    staff: [],
    appointmentModalConversationId: null,
    aiStatus: null,
    aiNotice: null,
    aiNoticeTimer: null,
  };

  // Caché de resultados de media: msgId → {ok: bool, data?} para no repetir fetches
  const mediaCache = new Map();
  let pendingImage = null; // foto pegada o adjunta en el chat, antes de enviarla

  const $ = (id) => document.getElementById(id);

  function isLocalDev() {
    const host = location.hostname;
    return host === "localhost" || host === "127.0.0.1";
  }

  // El backend inyecta la URL del sitio: en producción el panel vive en otro dominio.
  const SITE_URL = String(
    document.querySelector('meta[name="omitel-site-url"]')?.getAttribute("content") || ""
  ).replace(/\/$/, "");

  function siteUrl(path) {
    return SITE_URL && !isLocalDev() ? `${SITE_URL}${path}` : path;
  }

  document.querySelectorAll("a[data-site-path]").forEach((a) => {
    a.setAttribute("href", siteUrl(a.getAttribute("data-site-path")));
  });

  function isDevToolsEnabled() {
    return isLocalDev() || new URLSearchParams(location.search).has("debug");
  }

  function humanizeWaError(msg) {
    const raw = (msg || "").trim();
    const lower = raw.toLowerCase();
    if (!raw) return "No pudimos conectar WhatsApp. Intenta de nuevo en unos segundos.";
    if (lower.includes("dev.sh") || lower.includes("waha-docker") || lower.includes("evolution-mac")) {
      return "No pudimos conectar WhatsApp. Intenta de nuevo o recarga la página.";
    }
    if (lower.includes("abort") || lower.includes("timeout") || lower.includes("tardó")) {
      return "La conexión tardó demasiado. Revisa tu internet e intenta otra vez.";
    }
    if (lower.includes("backend") || lower.includes("servidor")) {
      return "El servidor no responde. Espera un momento y recarga la página.";
    }
    if (
      lower.includes("vincular nuevos dispositivos") ||
      lower.includes("vincular dispositivo") ||
      lower.includes("unable to link") ||
      lower.includes("link device")
    ) {
      return (
        "WhatsApp no deja vincular ahora. En el celular: Ajustes → Dispositivos vinculados " +
        "y cerrá sesiones que no uses. Esperá 2–3 minutos, tocá Conectar otra vez y escaneá el QR nuevo."
      );
    }
    return raw;
  }

  const BACKEND_OFFLINE_MSG = isLocalDev()
    ? "El servidor no responde. En una terminal ejecuta: ./scripts/dev.sh — luego recarga esta página (Cmd+R)."
    : "El servidor no responde. Espera un momento y recarga la página.";

  async function pingBackend(timeoutMs = 4000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(`${API}/auth/providers`, {
        signal: ctrl.signal,
        credentials: "include",
      });
      clearTimeout(timer);
      return res.ok;
    } catch {
      clearTimeout(timer);
      return false;
    }
  }

  function setBackendOfflineBanner(offline, message) {
    const banner = $("login-offline-banner");
    const continueBtn = $("login-continue-btn");
    if (banner) {
      banner.textContent = message || BACKEND_OFFLINE_MSG;
      banner.classList.toggle("hidden", !offline);
    }
    if (continueBtn) continueBtn.disabled = !!offline;
  }

  async function ensureBackendOnline() {
    const ok = await pingBackend(4000);
    setBackendOfflineBanner(!ok);
    return ok;
  }

  function bindOAuthLink(el) {
    if (!el) return;
    el.addEventListener("click", async (event) => {
      event.preventDefault();
      const href = el.getAttribute("href") || "";
      if (!(await ensureBackendOnline())) {
        setLoginError(loginError, BACKEND_OFFLINE_MSG);
        return;
      }
      window.location.assign(href);
    });
  }

  function getTheme() {
    try {
      const stored = localStorage.getItem(THEME_STORAGE_KEY);
      return stored === "light" ? "light" : "dark";
    } catch {
      return "dark";
    }
  }

  function syncThemeToggles(theme) {
    const checked = theme === "light";
    const panelToggle = $("theme-toggle");
    const loginToggle = $("theme-toggle-login");
    if (panelToggle) panelToggle.checked = checked;
    if (loginToggle) loginToggle.checked = checked;
  }

  function applyTheme(theme) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
    syncThemeToggles(next);
  }

  function bindThemeToggle(el) {
    if (!el) return;
    el.addEventListener("change", () => {
      applyTheme(el.checked ? "light" : "dark");
    });
  }

  const loginView = $("login-view");
  const panelView = $("panel-view");
  const loginForm = $("login-form");
  const loginError = $("login-error");
  const loginStepEmail = $("login-step-email");
  const loginStepRegister = $("login-step-register");
  const loginStepSent = $("login-step-sent");
  let loginEmail = "";
  const conversationList = $("conversation-list");
  const messagesEl = $("messages");
  const emptyChat = $("empty-chat");
  const activeChat = $("active-chat");
  const sendForm = $("send-form");
  const messageInput = $("message-input");
  const wsStatus = $("ws-status");
  const waStatus = $("wa-status");

  function headers(json = true) {
    const h = {};
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  async function api(path, options = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const isForm = options.body instanceof FormData;
    try {
      const res = await fetch(`${API}${path}`, {
        ...options,
        credentials: "include",
        signal: controller.signal,
        headers: isForm
          ? { ...(options.headers || {}) }
          : { ...headers(), ...(options.headers || {}) },
      });
      const text = await res.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        data = text;
      }
      if (res.status === 401) {
        const e = new Error(
          "Sesión expirada. Recarga la página (Cmd+R) o entra de nuevo desde la landing."
        );
        e.status = 401;
        throw e;
      }
      if (!res.ok) {
        const detail = data?.detail;
        let msg = detail || (typeof data === "string" ? data : "Error");
        if (typeof msg === "object") msg = JSON.stringify(msg);
        if (res.status === 503 && path.includes("/whatsapp/")) {
          msg = detail || "Servicio de WhatsApp no disponible. Revisa que Evolution o WAHA esté corriendo.";
        } else if (res.status === 404 && path.includes("/interest")) {
          msg = "Función no disponible — reinicia el backend (uvicorn) para cargar la última versión.";
        } else if (res.status === 404 && detail === "Conversación no encontrada") {
          msg = "Conversación no encontrada";
        } else if (res.status === 404 && !detail) {
          msg = "No encontrado";
        }
        const e = new Error(msg);
        e.status = res.status;
        throw e;
      }
      return data;
    } catch (err) {
      if (err.name === "AbortError") throw new Error("Tiempo de espera agotado");
      const raw = String(err?.message || err || "");
      if (
        raw === "Load failed" ||
        raw === "Failed to fetch" ||
        raw.includes("NetworkError") ||
        raw.includes("network")
      ) {
        throw new Error(
          "No pudimos contactar el servidor. Inicia el backend (puerto 8000) con ./scripts/dev.sh"
        );
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function siteLoginUrl(nextPath) {
    const next = encodeURIComponent(nextPath || "/panel");
    if (isLocalDev() && location.port === "8000") {
      return `${location.protocol}//${location.hostname}:4200/login?next=${next}`;
    }
    return siteUrl(`/login?next=${next}`);
  }

  function redirectToSiteLogin() {
    const search = location.search || "";
    const nextPath = `/panel${search}`;
    window.location.href = siteLoginUrl(nextPath);
  }

  function showLogin() {
    redirectToSiteLogin();
  }

  function showPanel() {
    loginView?.classList.add("hidden");
    panelView?.classList.remove("hidden");
  }

  function resetPanelState() {
    stopWaPoll();
    stopWaHeartbeat();
    stopLivePoll();
    state.user = null;
    state.tenant = null;
    state.conversations = [];
    state.activeId = null;
    state.messages = [];
    state._messagesSig = "";
    state.canWrite = false;
    state.canManageGlobal = false;
    state.canConnectWa = false;
    state.chatListTab = "interested";
    state.interestedCount = 0;
    state.subscription = null;
    state.panelMode = "chats";
    state.aiProfile = null;
    state.quickShortcuts = [];
    state.shortcutsDraft = [];
    state.wa = { status: "disconnected", qr_base64: null, phone_number: null, chatwoot_inbox_url: null };
    setWsBadge(false);
    updateWaBadge("disconnected");
    hideQrModal();
    conversationList.innerHTML = "";
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
    $("ai-setup-panel").classList.add("hidden");
    $("appointments-panel").classList.add("hidden");
    $("sidebar-chats").classList.remove("hidden");
    $("mode-chats").classList.add("active");
    $("mode-appointments").classList.remove("active");
    $("mode-ai").classList.remove("active");
    $("chat-area").classList.remove("ai-setup-mode", "appointments-mode");
    $("business-name").textContent = "—";
    $("user-label").textContent = "";
    setChatListTab("interested");
    updateInterestedBadge();
  }

  function resetLoginForm() {
    loginEmail = "";
    showLoginStep("email");
    loginForm?.reset();
    [loginError, $("login-error-register")].forEach((el) => {
      if (el) {
        el.textContent = "";
        el.classList.add("hidden");
      }
    });
  }

  async function logout() {
    try {
      await fetch(`${API}/auth/logout`, { method: "POST", credentials: "include" });
    } catch {
      /* ignore */
    }
    try {
      localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    state.token = null;
    disconnectWs();
    resetPanelState();
    redirectToSiteLogin();
  }

  function roleAtLeast(role, minimum) {
    const order = { viewer: 0, agent: 1, owner: 2 };
    return (order[role] ?? 0) >= (order[minimum] ?? 0);
  }

  function formatTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("es-CO", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" });
  }

  function updateWaBadge(status) {
    state.wa.status = status || state.wa.status;
    if (state.wa.status === "connected" && state.wa.phone_number) {
      waStatus.textContent = `WA: ${state.wa.phone_number}`;
    } else if (state.wa.status === "connecting") {
      waStatus.textContent = "WA: Escanea el QR";
    } else {
      waStatus.textContent = "WA: Desconectado";
    }
    waStatus.className = "badge " + (state.wa.status === "connected" ? "wa-connected" : "wa-disconnected");
    renderWaUi();
  }

  function qrSrc(base64) {
    if (!base64) return "";
    if (base64.startsWith("data:")) return base64;
    return `data:image/png;base64,${base64}`;
  }

  let waPollTimer = null;
  let syncPollTimer = null;
  let waHeartbeatTimer = null;
  let livePollTimer = null;
  let convPollTimer = null;
  let syncHealthTimer = null;

  function updateSyncStatus(parts) {
    const el = $("sync-status");
    if (!el) return;
    const ws = state.ws && state.ws.readyState === WebSocket.OPEN ? "WS✓" : "WS✗";
    const pull = parts.pull || state._lastPull || "—";
    const wh = parts.webhook || state._lastWebhook || "—";
    el.textContent = `Sync: ${ws} · Pull ${pull} · WH ${wh}`;
    el.classList.remove("ok", "warn", "err");
    if (String(pull).startsWith("ERR") || String(wh).includes("nunca")) {
      el.classList.add("err");
    } else if (String(pull).includes("+") || ws === "WS✓") {
      el.classList.add("ok");
    } else {
      el.classList.add("warn");
    }
  }

  function stopLivePoll() {
    if (livePollTimer) {
      clearInterval(livePollTimer);
      livePollTimer = null;
    }
    if (convPollTimer) {
      clearInterval(convPollTimer);
      convPollTimer = null;
    }
    if (syncHealthTimer) {
      clearInterval(syncHealthTimer);
      syncHealthTimer = null;
    }
  }

  let pullInFlight = false;

  async function pullActiveChat() {
    if (!state.activeId || state.wa.status !== "connected" || pullInFlight) return;
    const chatId = state.activeId;
    pullInFlight = true;
    try {
      const data = await api(`/conversations/${chatId}/sync-live`, { method: "POST" }, 12000);
      if (state.activeId !== chatId) return;

      const pullLabel = data.imported > 0 ? `+${data.imported}` : "ok";
      state._lastPull = pullLabel;
      updateSyncStatus({ pull: pullLabel });

      if (data.conversation) upsertConversation(data.conversation);

      const msgs = dedupeMessages(data.messages || []);
      const sig = msgs.map((m) => m.id || `${m.body}|${m.created_at}`).join("\n");
      if (sig !== state._messagesSig) {
        state._messagesSig = sig;
        state.messages = msgs;
        renderMessages();
      }
    } catch (err) {
      if (state.activeId !== chatId) return;
      // Si la conversación ya no existe, limpiar selección y recargar lista
      if (err.status === 404) {
        state.activeId = null;
        fetchConversations().then((rows) => {
          if (rows.length) { setConversations(rows); renderConversationList(); }
        }).catch(() => {});
        return;
      }
      state._lastPull = `ERR`;
      updateSyncStatus({ pull: `ERR` });
      console.warn("sync-live:", err.message);
      try {
        const msgs = dedupeMessages(await api(`/conversations/${chatId}/messages`, {}, 8000));
        if (state.activeId !== chatId) return;
        const sig = msgs.map((m) => m.id || `${m.body}|${m.created_at}`).join("\n");
        if (sig !== state._messagesSig) {
          state._messagesSig = sig;
          state.messages = msgs;
          renderMessages();
        }
        state._lastPull = "bd";
        updateSyncStatus({ pull: "bd" });
      } catch (fallbackErr) {
        if (fallbackErr.status === 404) {
          state.activeId = null;
          fetchConversations().then((rows) => {
            if (rows.length) { setConversations(rows); renderConversationList(); }
          }).catch(() => {});
          return;
        }
        console.warn("messages fallback:", fallbackErr.message);
      }
    } finally {
      pullInFlight = false;
    }
  }

  async function refreshSyncHealth() {
    if (!state.user || state.wa.status !== "connected") return;
    try {
      const report = await api("/whatsapp/debug/sync", {}, 12000);
      const lastWh = report.webhook?.last_received_at;
      state._lastWebhook = lastWh
        ? new Date(lastWh).toLocaleTimeString("es-CO", { hour: "2-digit", minute: "2-digit" })
        : report.webhook?.live_pull_alive
          ? "pull●"
          : "nunca";
      if (report.webhook?.url_mismatch) state._lastWebhook = "URL mal";
      updateSyncStatus({ webhook: state._lastWebhook });
      if (report.errors?.length) console.warn("sync debug:", report.errors);
    } catch (err) {
      console.warn("sync health:", err.message);
    }
  }

  function startLivePoll() {
    stopLivePoll();
    updateSyncStatus({});
    refreshSyncHealth();
    livePollTimer = setInterval(() => {
      if (!state.user || state.wa.status !== "connected") {
        stopLivePoll();
        return;
      }
      pullActiveChat();
    }, 4000);
    convPollTimer = setInterval(async () => {
      if (!state.user || state.wa.status !== "connected") return;
      try {
        const rows = await fetchConversations();
        if (rows.length) {
          setConversations(rows);
          renderConversationList();
        }
      } catch {
        /* ignore */
      }
    }, 8000);
    syncHealthTimer = setInterval(refreshSyncHealth, 20000);
  }

  function stopWaPoll() {
    if (waPollTimer) {
      clearInterval(waPollTimer);
      waPollTimer = null;
    }
  }

  function stopWaHeartbeat() {
    if (waHeartbeatTimer) {
      clearInterval(waHeartbeatTimer);
      waHeartbeatTimer = null;
    }
  }

  async function refreshWaStatus() {
    if (!state.user) return;
    try {
      const wa = await api("/whatsapp/status", {}, 8000);
      applyWaSession(wa);
    } catch {
      /* ignore */
    }
  }

  function startWaHeartbeat() {
    stopWaHeartbeat();
    waHeartbeatTimer = setInterval(refreshWaStatus, 15000);
  }

  function startWaPoll() {
    stopWaPoll();
    waPollTimer = setInterval(async () => {
      if (state.wa.status !== "connecting") {
        stopWaPoll();
        return;
      }
      try {
        const wa = await api("/whatsapp/status", {}, 8000);
        applyWaSession(wa);
      } catch {
        /* ignore */
      }
    }, 3000);
  }

  function stopSyncPoll() {
    if (syncPollTimer) {
      clearInterval(syncPollTimer);
      syncPollTimer = null;
    }
  }

  function startSyncPoll() {
    stopSyncPoll();
    syncPollTimer = setInterval(() => {
      if (!state.syncInProgress) {
        stopSyncPoll();
        return;
      }
      fetchConversations()
        .then((rows) => {
          if (rows.length) {
            setConversations(rows);
            renderConversationList();
          }
        })
        .catch(() => {});
    }, 2000);
  }

  function startSyncWatchdog() {
    clearTimeout(state.syncWatchdog);
    state.syncWatchdog = setTimeout(() => {
      if (!state.syncInProgress) return;
      state.syncInProgress = false;
      stopSyncPoll();
      fetchConversations()
        .then((rows) => {
          setConversations(rows);
          renderConversationList();
        })
        .catch(() => renderConversationList());
    }, 120000);
  }

  function clearSyncWatchdog() {
    clearTimeout(state.syncWatchdog);
    state.syncWatchdog = null;
    stopSyncPoll();
  }

  function showSyncingList() {
    state.syncInProgress = true;
    startSyncWatchdog();
    startSyncPoll();
    renderConversationList();
  }

  async function triggerAutoSync() {
    if (state.autoSyncRequested || state.wa.status !== "connected") return;
    state.autoSyncRequested = true;
    showSyncingList();
    try {
      await api("/whatsapp/sync", { method: "POST" }, 15000);
    } catch (err) {
      if (!String(err.message).includes("409")) {
        console.warn("Auto-sync:", err.message);
      }
    }
  }

  function applyWaSession(wa) {
    if (!wa) return;
    const prevStatus = state.wa.status || "disconnected";
    const prevPhone = state.wa.phone_number;
    const status = wa.status || "disconnected";
    const nextPhone = status === "connected" ? (wa.phone_number ?? null) : null;
    const phoneChanged =
      status === "connected" &&
      prevStatus === "connected" &&
      prevPhone &&
      nextPhone &&
      prevPhone !== nextPhone;
    state.wa = {
      status,
      qr_base64: wa.qr_base64 ?? null,
      phone_number: nextPhone,
      chatwoot_inbox_url: wa.chatwoot_inbox_url ?? null,
    };
    updateWaBadge(state.wa.status);
    sendForm.classList.toggle("disabled", !state.canWrite || state.wa.status !== "connected");
    messageInput.disabled = !state.canWrite || state.wa.status !== "connected";
    if (state.wa.qr_base64 && state.wa.status === "connecting") {
      showQrImage(state.wa.qr_base64);
    }
    if (state.wa.status === "connected") {
      hideQrModal();
      stopWaPoll();
      startLivePoll();
      if (phoneChanged) {
        prepareForNewDevice();
        state.autoSyncRequested = true;
        fetchConversations()
          .then((rows) => {
            setConversations(rows);
            renderConversationList();
          })
          .catch(() => {});
      } else if (prevStatus !== "connected") {
        state.autoSyncRequested = true;
        fetchConversations()
          .then((rows) => {
            setConversations(rows);
            renderConversationList();
          })
          .catch(() => {});
      } else if (!state.syncInProgress) {
        fetchConversations()
          .then((rows) => {
            setConversations(rows);
            renderConversationList();
          })
          .catch(() => {});
      }
    } else if (state.wa.status === "connecting") {
      startWaPoll();
    } else {
      hideQrModal();
      stopWaPoll();
      stopLivePoll();
      state.syncInProgress = false;
      state.autoSyncRequested = false;
      renderConversationList();
    }
    renderOnboarding();
  }

  function applyUserFromMe(me) {
    if (!me) return;
    state.user = me;
    state.canWrite = roleAtLeast(me.role, "agent");
    state.canManageGlobal = me.role === "owner";
    state.canConnectWa = !!me;
    const userLabel = $("user-label");
    if (userLabel) userLabel.textContent = `${me.full_name} (${me.role})`;
    const aiGlobal = $("toggle-ai-global");
    if (aiGlobal) aiGlobal.disabled = !state.canManageGlobal;
  }

  function renderWaUi() {
    const connected = state.wa.status === "connected";
    const needsConnect = !connected;

    $("wa-connect-btn")?.classList.toggle("hidden", !needsConnect);
    $("wa-setup")?.classList.toggle("hidden", !needsConnect);
    const dropped = needsConnect && state.conversations.length > 0;
    const setupTitle = $("wa-setup-title");
    if (setupTitle) {
      setupTitle.textContent = dropped ? "WhatsApp se desconectó" : "Conecta tu WhatsApp";
      $("wa-setup-text").innerHTML = dropped
        ? "Estamos intentando reconectar solos y tus chats se conservan. Si en un par de minutos no vuelve, escanea el código QR otra vez."
        : "Escanea el código QR con WhatsApp en tu celular. Cuando lleguen mensajes, la IA los clasifica y los verás en <strong>Interesados</strong>.";
    }
    $("wa-disconnect-btn")?.classList.toggle("hidden", !connected || !state.canConnectWa);
    $("wa-reset-chats-btn")?.classList.toggle("hidden", !connected || !state.canConnectWa);
    const cwLink = $("wa-chatwoot-link");
    if (cwLink) {
      const showCw = connected && !!state.wa.chatwoot_inbox_url;
      cwLink.classList.toggle("hidden", !showCw);
      if (showCw) cwLink.href = state.wa.chatwoot_inbox_url;
    }
  }

  function showQrModal() {
    $("qr-modal").classList.remove("hidden");
    $("qr-loading").classList.remove("hidden");
    $("qr-image").classList.add("hidden");
    $("qr-error").classList.add("hidden");
    $("qr-phone").classList.add("hidden");
  }

  function hideQrModal() {
    $("qr-modal").classList.add("hidden");
  }

  function showQrImage(base64) {
    $("qr-loading").classList.add("hidden");
    $("qr-error").classList.add("hidden");
    const img = $("qr-image");
    img.src = qrSrc(base64);
    img.classList.remove("hidden");
  }

  function showQrError(msg) {
    $("qr-loading").classList.add("hidden");
    $("qr-image").classList.add("hidden");
    const err = $("qr-error");
    err.textContent = msg;
    err.classList.remove("hidden");
  }

  async function connectWhatsApp() {
    if (!state.user) {
      const check = await probeSession(5000);
      if (check.user) {
        state.user = check.user;
        applyUserFromMe(check.user);
      }
    }
    showQrModal();
    $("qr-loading").textContent = "Generando QR… puede tardar hasta 30 segundos";
    $("wa-setup-error").classList.add("hidden");
    if (!state.user) {
      showQrError("No hay sesión activa. Entra desde omitel y vuelve al panel.");
      return;
    }
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 4000);
      const health = await fetch(`${API}/auth/providers`, {
        credentials: "include",
        signal: ctrl.signal,
      });
      clearTimeout(t);
      if (!health.ok) throw new Error("Backend no responde");
    } catch (err) {
      const msg = humanizeWaError(
        err.message || "No pudimos contactar el servidor. Intenta de nuevo en unos segundos."
      );
      showQrError(msg);
      $("wa-setup-error").textContent = msg;
      $("wa-setup-error").classList.remove("hidden");
      return;
    }
    try {
      const wa = await api("/whatsapp/connect", { method: "POST" }, 90000);
      applyWaSession(wa);
      if (wa.qr_base64) showQrImage(wa.qr_base64);
      else showQrError("No se recibió el código QR. Intenta de nuevo en unos segundos.");
    } catch (err) {
      const msg = humanizeWaError(err.message || "Error al conectar WhatsApp");
      showQrError(msg);
      $("wa-setup-error").textContent = msg;
      $("wa-setup-error").classList.remove("hidden");
    }
  }

  function setWsBadge(online) {
    wsStatus.textContent = online ? "Tiempo real" : "Sin tiempo real";
    wsStatus.className = "badge " + (online ? "online" : "offline");
  }

  function convTitle(c) {
    const name = String(c.contact_name || "").trim();
    const phone = String(c.contact_phone || "").trim();
    const digits = name.replace(/\D/g, "");
    const looksLikePhone = digits.length >= 10 && digits.length <= 13 && /^\+?\d/.test(name);
    if (name && name !== phone && !looksLikePhone) return name;
    return c.display_name || c.contact_name || c.display_phone || c.contact_phone || "Contacto";
  }

  function applyChatContactPhone(conv) {
    const phoneEl = $("chat-phone");
    if (!phoneEl) return;
    phoneEl.textContent = convDisplayPhone(conv);
    phoneEl.title = "";
  }

  function refreshActiveChatHeader(conv) {
    if (!conv || conv.id !== state.activeId) return;
    $("chat-title").textContent = convTitle(conv);
    applyChatContactPhone(conv);
    syncChatToggles(conv);
  }

  function convDisplayPhone(c) {
    if (c.display_phone) return c.display_phone;
    const phone = c.contact_phone || "";
    if (phone.startsWith("lid:")) {
      const tail = phone.slice(4);
      const ref = tail.length >= 5 ? tail.slice(-5) : tail;
      return ref ? `Sin número · ref ····${ref}` : "Sin número visible";
    }
    return phone;
  }

  function convSubtitle(c) {
    if (c.last_message_preview) return mediaPreview(c.last_message_preview);
    return c.display_phone || "";
  }

  function phoneTailDigits(phone) {
    const digits = String(phone || "").replace(/\D/g, "");
    if (!digits) return "";
    return digits.length >= 10 ? digits.slice(-10) : digits;
  }

  function convSamePerson(a, b) {
    if (!a || !b) return false;
    if (a.id === b.id) return true;
    const jidA = String(a.contact_jid || "");
    const jidB = String(b.contact_jid || "");
    if (jidA && jidB && jidA === jidB) return true;
    const tailA = phoneTailDigits(a.contact_phone);
    const tailB = phoneTailDigits(b.contact_phone);
    if (tailA && tailB && tailA === tailB) return true;
    return false;
  }

  function normalizeForSearch(str) {
    return String(str || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  function matchesSearch(conv, query) {
    const hay = normalizeForSearch(`${conv.contact_name || ""} ${conv.contact_phone || ""} ${conv.display_phone || ""}`);
    const tokens = normalizeForSearch(query).split(/\s+/).filter(Boolean);
    if (!tokens.length) return true;
    const words = hay.split(/\s+/).filter(Boolean);
    return tokens.every((token) =>
      hay.includes(token) || words.some((w) => w.includes(token) || token.includes(w))
    );
  }

  function sortConversations(rows) {
    return [...(rows || [])].sort((a, b) => {
      const ta = a.last_message_at ? new Date(a.last_message_at).getTime() : 0;
      const tb = b.last_message_at ? new Date(b.last_message_at).getTime() : 0;
      return tb - ta;
    });
  }

  function recalculateInterestedCount(rows) {
    const list = rows || state.conversations;
    state.interestedCount = list.filter((c) => getConversationInterest(c) === "interested").length;
    updateInterestedBadge();
  }

  function updateInterestedBadge() {
    const badge = $("interested-count-badge");
    if (!badge) return;
    const n = state.interestedCount || 0;
    badge.textContent = String(n);
    badge.classList.toggle("hidden", n < 1);
  }

  function pulseInterestedTab() {
    const tab = $("tab-chats-interested");
    if (!tab) return;
    tab.classList.add("tab-pulse");
    setTimeout(() => tab.classList.remove("tab-pulse"), 2400);
  }

  function renderOnboarding() {}

  function formatPlanDate(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString("es-CO", { day: "numeric", month: "short", timeZone: "America/Bogota" });
  }

  function renderPlanStatus() {
    const bar = $("plan-status-bar");
    const label = $("plan-status-label");
    const cta = $("plan-status-cta");
    if (!bar || !label) return;

    const parts = [];
    const sub = state.subscription;
    if (sub) {
      if (sub.is_trial) {
        const left = sub.trial_days_left;
        parts.push(left ? `Prueba gratis: ${left === 1 ? "queda 1 día" : `quedan ${left} días`}` : "Periodo de prueba");
        if (cta) cta.textContent = "Activar plan";
        cta?.classList.remove("hidden");
      } else if (sub.trial_expired) {
        parts.push("Tu prueba gratis terminó: la IA no está respondiendo");
        if (cta) cta.textContent = "Activar plan";
        cta?.classList.remove("hidden");
      } else if (sub.needs_payment) {
        parts.push("Pago pendiente: tu plan venció");
        if (cta) cta.textContent = "Renovar plan";
        cta?.classList.remove("hidden");
      } else if (sub.is_paid && sub.plan?.name) {
        parts.push(/^plan\b/i.test(sub.plan.name) ? sub.plan.name : `Plan ${sub.plan.name}`);
        if (sub.cancel_at_period_end && sub.current_period_end) {
          parts.push(`Se cancela el ${formatPlanDate(sub.current_period_end)}`);
          if (cta) cta.textContent = "Reactivar";
          cta?.classList.remove("hidden");
        } else if (sub.renewal_failing) {
          parts.push("No pudimos cobrar la renovación");
          if (cta) cta.textContent = "Actualizar pago";
          cta?.classList.remove("hidden");
        } else if (sub.next_charge_at) {
          parts.push(`Cobro automático: ${formatPlanDate(sub.next_charge_at)}`);
          cta?.classList.add("hidden");
        } else {
          if (sub.current_period_end) parts.push(`Vence el ${formatPlanDate(sub.current_period_end)}`);
          if (cta) cta.textContent = "Mi plan";
          cta?.classList.remove("hidden");
        }
      } else if (sub.plan?.name) {
        parts.push(sub.plan.name);
        cta?.classList.add("hidden");
      }
    }

    const ai = state.aiStatus;
    if (ai && !ai.plan_required && !ai.daily_replies_unlimited && !ai.daily_classifications_unlimited) {
      const left = ai.daily_classifications_remaining ?? ai.daily_replies_remaining;
      const cap = ai.daily_classifications_limit ?? ai.daily_replies_limit;
      if (typeof left === "number" && typeof cap === "number") {
        const kind = ai.ai_mode === "classify_only" ? "Clasificaciones" : "Respuestas IA";
        parts.push(`${kind} hoy: ${left}/${cap}`);
      }
    }

    if (!parts.length) {
      bar.classList.add("hidden");
      return;
    }
    label.textContent = parts.join(" · ");
    bar.classList.remove("hidden");
  }

  function setConversations(rows) {
    state.conversations = sortConversations(rows);
    if (state.activeId) {
      const active = state.conversations.find((c) => c.id === state.activeId);
      if (active) syncChatToggles(active);
    }
  }

  function getConversationInterest(conv) {
    if (conv.interest_status === "interested") return "interested";
    if (conv.interest_status === "not_interested") return "not_interested";
    if (conv.status === "excluded") return "not_interested";
    return null;
  }

  function matchesInterestTab(conv, tab) {
    if (tab === "all") return true;
    const interest = getConversationInterest(conv);
    if (tab === "interested") return interest === "interested";
    if (tab === "not_interested") return interest === "not_interested";
    return true;
  }

  async function fetchConversations() {
    const rows = sortConversations(await api("/conversations?archived=false"));
    if (state.chatListTab === "all") return rows;
    return rows.filter((c) => matchesInterestTab(c, state.chatListTab));
  }

  async function fetchAllConversationsForSearch() {
    return sortConversations(await api("/conversations?archived=false"));
  }

  function setChatListTab(tab) {
    state.chatListTab = tab;
    $("tab-chats-interested").classList.toggle("active", tab === "interested");
    $("tab-chats-not-interested").classList.toggle("active", tab === "not_interested");
    $("tab-chats-all").classList.toggle("active", tab === "all");
  }

  function upsertConversation(conv) {
    const belongsHere = matchesInterestTab(conv, state.chatListTab);
    const idx = state.conversations.findIndex((c) => c.id === conv.id);
    if (!belongsHere) {
      if (idx >= 0) {
        state.conversations.splice(idx, 1);
        if (state.activeId === conv.id) {
          state.activeId = null;
          emptyChat.classList.remove("hidden");
          activeChat.classList.add("hidden");
        }
        renderConversationList();
      }
      return;
    }
    if (idx >= 0) state.conversations[idx] = { ...state.conversations[idx], ...conv };
    else state.conversations.push(conv);
    state.conversations = sortConversations(state.conversations);
    renderConversationList();
    refreshActiveChatHeader(conv);
  }

  function renderConversationList() {
    conversationList.innerHTML = "";
    if (state.syncInProgress) {
      const syncLi = document.createElement("li");
      syncLi.className = "conversation-item sync-status";
      syncLi.innerHTML = state.wa.chatwoot_inbox_url
        ? '<span class="muted">Sincronizando chats desde Chatwoot…</span>'
        : '<span class="muted">Importando chats y contactos de tu celular…</span>';
      conversationList.appendChild(syncLi);
    }
    const emptyLabels = {
      interested: "Sin contactos interesados",
      not_interested: "Sin contactos no interesados",
      all: "Sin conversaciones",
    };
    const emptyLabel = emptyLabels[state.chatListTab] || emptyLabels.all;

    let list = state.searchQuery && state.searchPool ? state.searchPool : state.conversations;
    if (state.searchQuery) {
      list = list.filter((c) => matchesSearch(c, state.searchQuery));
    } else if (state.chatListTab !== "all") {
      list = list.filter((c) => matchesInterestTab(c, state.chatListTab));
    } else {
      list = sortConversations(list);
    }

    if (!list.length) {
      if (state.syncInProgress) return;
      const msg = state.searchQuery ? "Sin resultados" : emptyLabel;
      conversationList.innerHTML = `<li class="conversation-item"><span class="muted">${msg}</span></li>`;
      return;
    }
    for (const c of list) {
      const li = document.createElement("li");
      li.className = "conversation-item" + (c.id === state.activeId ? " active" : "");
      li.dataset.id = c.id;
      const tags = [];
      if (c.interest_status === "interested") tags.push('<span class="interest-tag interested">interesado</span>');
      else if (c.interest_status === "not_interested") tags.push('<span class="interest-tag not">no interesado</span>');
      if (c.imported_legacy && !c.bait_sent) tags.push('<span class="personal-tag">personal</span>');
      if (c.mode === "manual") tags.push('<span class="mode-tag">manual</span>');
      if (c.ai_active) tags.push('<span class="ai-tag">IA</span>');
      const subtitle = convSubtitle(c);
      li.innerHTML = `
        <div class="row">
          <span class="name">${escapeHtml(convTitle(c))}</span>
          ${c.unread_count ? `<span class="unread">${c.unread_count}</span>` : ""}
        </div>
        <div class="meta">${subtitle ? escapeHtml(subtitle) + " " : ""}${tags.join("")}</div>
      `;
      li.addEventListener("click", () => selectConversation(c.id));
      conversationList.appendChild(li);
    }
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function dedupeMessages(messages) {
    const seen = new Set();
    const out = [];
    for (const m of messages) {
      const key = m.id || `${m.body}|${m.created_at}|${m.direction}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(m);
    }
    return out;
  }

  const MEDIA_TYPES = ["image", "sticker", "video", "audio", "ptt", "document"];

  function detectMediaType(body) {
    const b = (body || "").trim().toLowerCase();
    for (const t of MEDIA_TYPES) {
      if (b.startsWith(`[${t}`)) return t;
    }
    return null;
  }

  function mediaCaption(body) {
    if (!detectMediaType(body)) return "";
    return (body || "").trim().split("\n").slice(1).join("\n").trim();
  }

  function mediaPreview(body) {
    const type = detectMediaType(body);
    if (!type) return body;
    const name = { image: "Foto", sticker: "Sticker", video: "Video", audio: "Audio", ptt: "Nota de voz", document: "Archivo" }[type];
    return `${mediaIcon(type)} ${mediaCaption(body).replace(/\s+/g, " ") || name}`;
  }

  function mediaIcon(type) {
    return {
      image: "🖼️", sticker: "🎭", video: "🎬",
      audio: "🎵", ptt: "🎤", document: "📄",
    }[type] || "📎";
  }

  function mediaLabel(type) {
    return {
      image: "Ver imagen", sticker: "Ver sticker", video: "Ver video",
      audio: "Escuchar audio", ptt: "Escuchar nota de voz", document: "Abrir archivo",
    }[type] || "Ver media";
  }

  function _renderMediaElement(type, base64, mimetype) {
    const src = `data:${mimetype};base64,${base64}`;
    const mt = type;
    let el;
    if (mt === "image" || mt === "sticker") {
      el = document.createElement("img");
      el.src = src;
      el.className = "media-img";
      el.alt = mt;
      el.addEventListener("click", () => window.open(src));
    } else if (mt === "video") {
      el = document.createElement("video");
      el.src = src;
      el.controls = true;
      el.className = "media-video";
    } else if (mt === "audio" || mt === "ptt") {
      el = document.createElement("audio");
      el.src = src;
      el.controls = true;
      el.className = "media-audio";
    } else {
      el = document.createElement("a");
      el.href = src;
      el.download = `archivo.${mimetype.split("/")[1] || "bin"}`;
      el.textContent = "⬇ Descargar archivo";
      el.className = "media-download";
    }
    return el;
  }

  function buildMediaBubble(m, type, timeHtml) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + (m.direction === "in" ? "in" : "out");

    const inner = document.createElement("div");
    inner.className = "media-wrap";
    const caption = mediaCaption(m.body);
    if (caption) {
      timeHtml = `<p class="media-caption">${escapeHtml(caption)}</p>${timeHtml}`;
    }
    if (m.transcript) {
      const isVoice = type === "audio" || type === "ptt";
      const text = isVoice ? `🎤 “${escapeHtml(m.transcript)}”` : `👁️ ${escapeHtml(m.transcript)}`;
      const title = isVoice ? "Transcripción de la nota de voz" : "Lo que la IA entendió de la imagen";
      timeHtml = `<p class="media-transcript" title="${title}">${text}</p>${timeHtml}`;
    }

    // Si ya tenemos resultado en caché, mostrar directamente sin spinner
    const cached = mediaCache.get(m.id);
    if (cached) {
      if (cached.ok) {
        const el = _renderMediaElement(cached.media_type || type, cached.base64, cached.mimetype);
        inner.appendChild(el);
      } else {
        // Fallido: mostrar ícono discreto sin texto alarmante
        const ph = document.createElement("div");
        ph.className = "media-placeholder failed";
        ph.innerHTML = `${mediaIcon(type)} <span class="media-type-label">${mediaLabel(type)}</span>`;
        inner.appendChild(ph);
      }
      inner.insertAdjacentHTML("beforeend", timeHtml);
      wrap.appendChild(inner);
      return wrap;
    }

    inner.innerHTML = `<div class="media-placeholder loading">${mediaIcon(type)} <span>${mediaLabel(type)}…</span></div>${timeHtml}`;
    wrap.appendChild(inner);

    const placeholder = inner.querySelector(".media-placeholder");
    const mediaUrl = `${API}/conversations/${m.conversation_id}/messages/${m.id}/media`;

    fetch(mediaUrl, { credentials: "include", headers: headers(false) })
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(({ base64, media_type, mimetype }) => {
        mediaCache.set(m.id, { ok: true, base64, media_type: media_type || type, mimetype });
        const el = _renderMediaElement(media_type || type, base64, mimetype);
        placeholder.replaceWith(el);
      })
      .catch(() => {
        mediaCache.set(m.id, { ok: false });
        placeholder.classList.remove("loading");
        placeholder.classList.add("failed");
        // Mostrar solo el ícono y tipo, sin texto de error alarmante
        placeholder.innerHTML = `${mediaIcon(type)} <span class="media-type-label">${mediaLabel(type)}</span>`;
      });

    return wrap;
  }

  function outboundTicks(status) {
    if (status === "read") return '<span class="ticks read" title="Leído">✓✓</span>';
    if (status === "delivered") return '<span class="ticks delivered" title="Entregado">✓✓</span>';
    if (status === "failed") return '<span class="ticks failed" title="No se envió">!</span>';
    return '<span class="ticks sent" title="Enviado">✓</span>';
  }

  function renderMessages() {
    messagesEl.innerHTML = "";
    for (const m of dedupeMessages(state.messages)) {
      const ticks = m.direction === "out" ? outboundTicks(m.status) : "";
      const timeHtml = `<span class="time">${formatTime(m.created_at)} · ${m.source}${ticks}</span>`;
      const mediaType = detectMediaType(m.body);

      let div;
      if (mediaType && m.id) {
        div = buildMediaBubble(m, mediaType, timeHtml);
      } else {
        div = document.createElement("div");
        div.className = "msg " + (m.direction === "in" ? "in" : "out");
        const caption = m.body && !detectMediaType(m.body) ? escapeHtml(m.body) : escapeHtml(m.body);
        div.innerHTML = `${caption}${timeHtml}`;
      }
      messagesEl.appendChild(div);
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function switchPanelMode(mode) {
    state.panelMode = mode;
    const isChats = mode === "chats";
    const isAppointments = mode === "appointments";
    const isAi = mode === "ai";

    $("mode-chats").classList.toggle("active", isChats);
    $("mode-appointments").classList.toggle("active", isAppointments);
    $("mode-ai").classList.toggle("active", isAi);
    $("sidebar-chats").classList.toggle("hidden", !isChats);
    $("chat-area").classList.toggle("ai-setup-mode", isAi);
    $("chat-area").classList.toggle("appointments-mode", isAppointments);
    $("ai-setup-panel").classList.toggle("hidden", !isAi);
    $("appointments-panel").classList.toggle("hidden", !isAppointments);

    if (isChats) {
      if (state.activeId) {
        emptyChat.classList.add("hidden");
        activeChat.classList.remove("hidden");
      } else {
        emptyChat.classList.remove("hidden");
        activeChat.classList.add("hidden");
      }
    } else {
      emptyChat.classList.add("hidden");
      activeChat.classList.add("hidden");
      if (isAi) loadAiSetupPanel();
      if (isAppointments) loadAppointmentsPanel();
    }
    renderQuickShortcuts();
  }

  function isoToTimeInput(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const h = String(d.getHours()).padStart(2, "0");
    const m = String(d.getMinutes()).padStart(2, "0");
    return `${h}:${m}`;
  }

  function formatDateISO(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function parseDateISO(value) {
    const [y, m, d] = String(value || "").split("-").map(Number);
    if (!y || !m || !d) return new Date();
    return new Date(y, m - 1, d);
  }

  function shiftDateISO(iso, days) {
    const d = parseDateISO(iso);
    d.setDate(d.getDate() + days);
    return formatDateISO(d);
  }

  function formatDayLabel(iso) {
    return parseDateISO(iso).toLocaleDateString("es-CO", {
      weekday: "long",
      day: "numeric",
      month: "long",
    });
  }

  function setAppointmentsStatus(msg, isError = false) {
    const el = $("appointments-load-status");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("error", Boolean(isError && msg));
  }

  function setScheduleStatus(msg) {
    const el = $("schedule-save-status");
    if (el) el.textContent = msg || "";
  }

  function hideAppointmentModal() {
    $("appointment-modal")?.classList.add("hidden");
    $("appointment-form-error")?.classList.add("hidden");
    state.appointmentModalConversationId = null;
  }

  function showAppointmentFormError(msg) {
    const el = $("appointment-form-error");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function fillScheduleForm(schedule) {
    if (!schedule) return;
    if ($("schedule-open")) $("schedule-open").value = schedule.open_time || "08:00";
    if ($("schedule-close")) $("schedule-close").value = schedule.close_time || "18:00";
    if ($("schedule-slot-minutes")) {
      $("schedule-slot-minutes").value = String(schedule.slot_minutes || 60);
    }
    const bookingBox = $("schedule-ai-booking");
    if (bookingBox) {
      const allowed = schedule.ai_booking_allowed !== false;
      bookingBox.checked = allowed && !!schedule.ai_booking_enabled;
      bookingBox.disabled = !allowed;
      bookingBox.title = allowed ? "" : "Tu plan no incluye que la IA agende citas. Escríbenos para activarlo.";
    }
    if ($("schedule-staff-label") && schedule.staff_label !== undefined) {
      $("schedule-staff-label").value = schedule.staff_label || "";
    }
  }

  const WEEKDAY_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];

  function describeWorkDays(days) {
    const sorted = [...(days || [])].sort((a, b) => a - b);
    if (sorted.length === 7) return "Todos los días";
    const runs = [];
    sorted.forEach((d) => {
      const last = runs[runs.length - 1];
      if (last && d === last[1] + 1) last[1] = d;
      else runs.push([d, d]);
    });
    return runs
      .map(([a, b]) => {
        if (a === b) return WEEKDAY_SHORT[a];
        if (b === a + 1) return `${WEEKDAY_SHORT[a]}, ${WEEKDAY_SHORT[b]}`;
        return `${WEEKDAY_SHORT[a]}–${WEEKDAY_SHORT[b]}`;
      })
      .join(", ");
  }

  function describeStaffHours(member) {
    if (!member.start_time && !member.end_time) return "horario del negocio";
    return `${member.start_time || "apertura"}–${member.end_time || "cierre"}`;
  }

  function renderStaffList() {
    const list = $("staff-list");
    if (!list) return;
    const canEdit = !!state.canManageGlobal;
    $("staff-add-btn")?.classList.toggle("hidden", !canEdit);
    if (!state.staff.length) {
      list.innerHTML =
        '<li class="muted staff-empty">Sin equipo: todas las citas van a una sola agenda.</li>';
      return;
    }
    list.innerHTML = state.staff
      .map((m) => {
        const tags = [
          !m.is_active ? '<span class="staff-tag staff-tag-off">No disponible</span>' : "",
          m.phone ? '<span class="staff-tag" title="Recibe aviso de sus citas por WhatsApp">📲 Aviso</span>' : "",
          m.upcoming_count
            ? `<span class="staff-tag">${m.upcoming_count} cita${m.upcoming_count === 1 ? "" : "s"}</span>`
            : "",
        ].join("");
        return `<li class="staff-item${m.is_active ? "" : " staff-item-off"}" data-staff-id="${escapeHtml(m.id)}"
            ${canEdit ? 'role="button" tabindex="0"' : ""}>
          <span class="staff-avatar" aria-hidden="true">${escapeHtml((m.name || "?").charAt(0).toUpperCase())}</span>
          <span class="staff-info">
            <strong>${escapeHtml(m.name)}</strong>
            <span class="muted small">${escapeHtml(describeWorkDays(m.work_days))} · ${escapeHtml(describeStaffHours(m))}</span>
          </span>
          <span class="staff-tags">${tags}</span>
        </li>`;
      })
      .join("");
    if (!canEdit) return;
    list.querySelectorAll(".staff-item").forEach((el) => {
      const open = () => openStaffModal(state.staff.find((m) => m.id === el.getAttribute("data-staff-id")));
      el.addEventListener("click", open);
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });
    });
  }

  async function loadStaff() {
    try {
      state.staff = await api("/appointments/staff");
    } catch (err) {
      state.staff = [];
      setScheduleStatus(err.message);
    }
    renderStaffList();
  }

  function renderStaffChips(dayPayload) {
    const box = $("appointments-staff-chips");
    if (!box) return;
    const team = dayPayload?.staff || [];
    box.classList.toggle("hidden", !team.length);
    if (!team.length) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = team
      .map((m) => {
        const active = m.id === dayPayload.staff_id;
        return `<button type="button" role="tab" aria-selected="${active}" data-staff-id="${escapeHtml(m.id)}"
          class="appointments-staff-chip${active ? " active" : ""}${m.is_active ? "" : " off"}"
          title="${m.is_active ? "" : "No disponible para citas nuevas"}">${escapeHtml(m.name)}</button>`;
      })
      .join("");
    box.querySelectorAll(".appointments-staff-chip").forEach((el) => {
      el.addEventListener("click", () => {
        state.appointmentsStaffId = el.getAttribute("data-staff-id");
        loadAppointmentsDay(state.appointmentsDate);
      });
    });
  }

  function renderAppointmentsSlots(dayPayload) {
    const list = $("appointments-slot-list");
    if (!list) return;
    const slots = dayPayload?.slots || [];
    if (!slots.length) {
      const who = (dayPayload?.staff || []).find((m) => m.id === dayPayload?.staff_id);
      list.innerHTML = dayPayload?.off_day && who
        ? `<li class="muted appointments-empty">${escapeHtml(who.name)} no trabaja este día.</li>`
        : '<li class="muted appointments-empty">No hay bloques para este día. Ajusta el horario del negocio.</li>';
      return;
    }
    list.innerHTML = slots
      .map((slot) => {
        const busy = slot.status === "busy" && slot.appointment;
        const cls = busy ? "appointments-slot-busy" : "appointments-slot-free";
        const time = `${escapeHtml(slot.start)} – ${escapeHtml(slot.end)}`;
        let statusHtml;
        if (busy) {
          const name = escapeHtml(slot.appointment.client_name || "Cliente");
          const notes = slot.appointment.notes
            ? `<span class="appointments-slot-notes">${escapeHtml(slot.appointment.notes)}</span>`
            : "";
          statusHtml = `<span class="appointments-slot-status">Ocupado · ${name}</span>${notes}`;
        } else {
          statusHtml = '<span class="appointments-slot-status">Libre · clic para agendar</span>';
        }
        const data = busy
          ? `data-appointment-id="${escapeHtml(slot.appointment.id)}"`
          : `data-slot-start="${escapeHtml(slot.start)}" data-slot-end="${escapeHtml(slot.end)}"`;
        return `<li class="appointments-slot ${cls}" ${data} role="button" tabindex="0">
          <span class="appointments-slot-time">${time}</span>
          <div class="appointments-slot-meta">${statusHtml}</div>
        </li>`;
      })
      .join("");

    list.querySelectorAll(".appointments-slot").forEach((el) => {
      el.addEventListener("click", () => {
        const appointmentId = el.getAttribute("data-appointment-id");
        if (appointmentId) {
          const appt = slots
            .map((s) => s.appointment)
            .find((a) => a && a.id === appointmentId);
          openAppointmentModal({ appointment: appt });
      return;
        }
        openAppointmentModal({
          start: el.getAttribute("data-slot-start"),
          end: el.getAttribute("data-slot-end"),
        });
      });
    });
  }

  async function loadAppointmentsDay(iso) {
    state.appointmentsDate = iso;
    const label = $("appointments-today-label");
    const heading = $("appointments-day-heading");
    if (label) label.textContent = formatDayLabel(iso);
    if (heading) {
      const todayIso = formatDateISO(new Date());
      heading.textContent = iso === todayIso ? "Hoy" : "Agenda";
    }
    if ($("appointment-date")) $("appointment-date").value = iso;

    const list = $("appointments-slot-list");
    if (list) list.innerHTML = '<li class="muted appointments-loading">Cargando agenda…</li>';
    setAppointmentsStatus("");

    try {
      const staffParam = state.appointmentsStaffId
        ? `&staff_id=${encodeURIComponent(state.appointmentsStaffId)}`
        : "";
      const day = await api(`/appointments/day?day=${encodeURIComponent(iso)}${staffParam}`);
      state.appointmentsDay = day;
      state.appointmentsStaffId = day.staff_id || null;
      renderStaffChips(day);
      renderAppointmentsSlots(day);
    } catch (err) {
      if (list) {
        list.innerHTML = `<li class="error">${escapeHtml(err.message)}</li>`;
      }
      setAppointmentsStatus(err.message, true);
    }
  }

  async function loadAppointmentsPanel() {
    setScheduleStatus("");
    if (!state.appointmentsDate) {
      state.appointmentsDate = formatDateISO(new Date());
    }
    try {
      const schedule = await api("/appointments/schedule");
      fillScheduleForm(schedule);
    } catch (err) {
      setScheduleStatus(err.message);
    }
    await Promise.all([loadStaff(), loadAppointmentsDay(state.appointmentsDate)]);
  }

  function fillAppointmentStaffSelect(appointment) {
    const field = $("appointment-staff-field");
    const select = $("appointment-staff");
    if (!field || !select) return;
    const team = state.appointmentsDay?.staff || [];
    field.classList.toggle("hidden", !team.length);
    if (!team.length) {
      select.innerHTML = "";
      return;
    }
    const isEdit = Boolean(appointment?.id);
    const options = team
      .filter((m) => m.is_active || m.id === appointment?.staff_id)
      .map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`);
    if (!isEdit) options.unshift('<option value="">Quien esté libre</option>');
    select.innerHTML = options.join("");
    select.value = isEdit ? appointment.staff_id || "" : state.appointmentsStaffId || "";
    if (select.selectedIndex < 0) select.selectedIndex = 0;
  }

  function hideStaffModal() {
    $("staff-modal")?.classList.add("hidden");
    $("staff-form-error")?.classList.add("hidden");
  }

  function showStaffFormError(msg) {
    const el = $("staff-form-error");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  const WEEKDAY_LONG = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];

  function businessHours() {
    const schedule = state.appointmentsDay?.schedule || {};
    return { open: schedule.open_time || "08:00", close: schedule.close_time || "18:00" };
  }

  function selectedStaffDays() {
    return [...document.querySelectorAll("#staff-days input:checked")].map((b) => Number(b.value));
  }

  function updateStaffDaysSummary() {
    const el = $("staff-days-summary");
    if (!el) return;
    const days = selectedStaffDays().sort((a, b) => a - b);
    let text = "Ningún día";
    if (days.length === 7) text = "Todos los días";
    else if (days.length && days[days.length - 1] - days[0] === days.length - 1 && days.length > 2) {
      text = `De ${WEEKDAY_LONG[days[0]]} a ${WEEKDAY_LONG[days[days.length - 1]]}`;
    } else if (days.length) {
      text = describeWorkDays(days);
    }
    el.textContent = text;
    el.classList.toggle("warn", !days.length);
  }

  function staffHoursMode() {
    return document.querySelector('input[name="staff-hours-mode"]:checked')?.value || "business";
  }

  function setStaffHoursMode(mode) {
    document.querySelectorAll('input[name="staff-hours-mode"]').forEach((r) => {
      r.checked = r.value === mode;
    });
    const custom = mode === "custom";
    $("staff-custom-hours")?.classList.toggle("hidden", !custom);
    if (custom) {
      const { open, close } = businessHours();
      if (!$("staff-start").value) $("staff-start").value = open;
      if (!$("staff-end").value) $("staff-end").value = close;
    }
  }

  function updateStaffActiveHint() {
    const on = !!$("staff-active")?.checked;
    $("staff-active-title").textContent = on ? "Recibe citas" : "En pausa";
    $("staff-active-hint").textContent = on
      ? "La IA y tu equipo le pueden agendar."
      : "No se le agendan citas nuevas (vacaciones, incapacidad). Sus citas actuales se mantienen.";
  }

  function openStaffModal(member = null) {
    const isEdit = Boolean(member?.id);
    $("staff-modal-title").textContent = isEdit ? `Editar a ${member.name}` : "Agregar al equipo";
    $("staff-id").value = member?.id || "";
    $("staff-name").value = member?.name || "";
    $("staff-phone").value = member?.phone || "";
    $("staff-start").value = member?.start_time || "";
    $("staff-end").value = member?.end_time || "";
    const { open, close } = businessHours();
    $("staff-business-hours").textContent = `${open}–${close}`;
    setStaffHoursMode(member?.start_time || member?.end_time ? "custom" : "business");
    $("staff-active").checked = member ? !!member.is_active : true;
    updateStaffActiveHint();
    const days = member?.work_days || [0, 1, 2, 3, 4, 5];
    document.querySelectorAll("#staff-days input").forEach((box) => {
      box.checked = days.includes(Number(box.value));
    });
    updateStaffDaysSummary();
    $("staff-delete-btn")?.classList.toggle("hidden", !isEdit);
    $("staff-form-error")?.classList.add("hidden");
    $("staff-modal")?.classList.remove("hidden");
    $("staff-name")?.focus();
  }

  async function refreshAfterStaffChange() {
    await loadStaff();
    await loadAppointmentsDay(state.appointmentsDate);
  }

  async function submitStaffForm(event) {
    event.preventDefault();
    const id = $("staff-id")?.value?.trim();
    const custom = staffHoursMode() === "custom";
    const payload = {
      name: $("staff-name")?.value?.trim(),
      phone: $("staff-phone")?.value?.trim() || null,
      work_days: selectedStaffDays(),
      start_time: custom ? $("staff-start")?.value || null : null,
      end_time: custom ? $("staff-end")?.value || null : null,
      is_active: !!$("staff-active")?.checked,
    };
    if (!payload.work_days.length) {
      showStaffFormError("Elige al menos un día de trabajo");
      return;
    }
    if (custom && payload.start_time && payload.end_time && payload.end_time <= payload.start_time) {
      showStaffFormError("La hora de salida debe ser después de la de entrada");
      return;
    }
    const btn = $("staff-save-btn");
    btn.disabled = true;
    try {
      const saved = id
        ? await api(`/appointments/staff/${id}`, { method: "PATCH", body: JSON.stringify(payload) })
        : await api("/appointments/staff", { method: "POST", body: JSON.stringify(payload) });
      hideStaffModal();
      if (!id) state.appointmentsStaffId = saved.id;
      await refreshAfterStaffChange();
      setScheduleStatus(id ? `✓ ${saved.name} actualizado` : `✓ ${saved.name} agregado al equipo`);
    } catch (err) {
      showStaffFormError(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteCurrentStaff() {
    const id = $("staff-id")?.value?.trim();
    const member = state.staff.find((m) => m.id === id);
    if (!id || !confirm(`¿Quitar a ${member?.name || "esta persona"} del equipo?`)) return;
    try {
      await api(`/appointments/staff/${id}`, { method: "DELETE" });
      hideStaffModal();
      if (state.appointmentsStaffId === id) state.appointmentsStaffId = null;
      await refreshAfterStaffChange();
    } catch (err) {
      showStaffFormError(err.message);
    }
  }

  function openAppointmentModal({ appointment = null, start = "", end = "" } = {}) {
    const isEdit = Boolean(appointment?.id);
    $("appointment-modal-title").textContent = isEdit ? "Editar cita" : "Nueva cita";
    $("appointment-id").value = appointment?.id || "";
    if (appointment?.starts_at) {
      $("appointment-date").value = formatDateISO(new Date(appointment.starts_at));
    } else {
      $("appointment-date").value = state.appointmentsDate || formatDateISO(new Date());
    }
    $("appointment-start").value = start || isoToTimeInput(appointment?.starts_at) || "09:00";
    $("appointment-end").value = end || isoToTimeInput(appointment?.ends_at) || "10:00";
    $("appointment-client").value = appointment?.client_name || "";
    $("appointment-phone").value = appointment?.client_phone || "";
    $("appointment-notes").value = appointment?.notes || "";
    fillAppointmentStaffSelect(appointment);
    state.appointmentModalConversationId = appointment?.conversation_id || null;

    $("appointment-delete-btn")?.classList.toggle("hidden", !isEdit);
    const chatBtn = $("appointment-open-chat-btn");
    if (chatBtn) {
      const showChat = Boolean(appointment?.conversation_id);
      chatBtn.classList.toggle("hidden", !showChat);
    }
    $("appointment-form-error")?.classList.add("hidden");
    $("appointment-modal")?.classList.remove("hidden");
    $("appointment-client")?.focus();
  }

  async function saveAppointmentSchedule() {
    if (!state.canManageGlobal) {
      setScheduleStatus("Solo el dueño puede cambiar el horario");
      return;
    }
    const btn = $("schedule-save-btn");
    btn.disabled = true;
    setScheduleStatus("Guardando…");
    try {
      const payload = {
        open_time: $("schedule-open")?.value || "08:00",
        close_time: $("schedule-close")?.value || "18:00",
        slot_minutes: Number($("schedule-slot-minutes")?.value || 60),
        ai_booking_enabled: !!$("schedule-ai-booking")?.checked,
        staff_label: $("schedule-staff-label")?.value?.trim() || "",
      };
      const saved = await api("/appointments/schedule", { method: "PUT", body: JSON.stringify(payload) });
      fillScheduleForm(saved);
      setScheduleStatus(saved.ai_booking_enabled ? "✓ Guardado · la IA agenda citas" : "✓ Horario guardado");
      await loadAppointmentsDay(state.appointmentsDate);
    } catch (err) {
      setScheduleStatus(err.message);
    } finally {
    btn.disabled = false;
    }
  }

  async function submitAppointmentForm(event) {
    event.preventDefault();
    const id = $("appointment-id")?.value?.trim();
    const payload = {
      date: $("appointment-date")?.value,
      start_time: $("appointment-start")?.value,
      end_time: $("appointment-end")?.value,
      client_name: $("appointment-client")?.value?.trim(),
      client_phone: $("appointment-phone")?.value?.trim() || null,
      notes: $("appointment-notes")?.value?.trim() || null,
    };
    if (!$("appointment-staff-field")?.classList.contains("hidden")) {
      const staffId = $("appointment-staff")?.value || null;
      if (staffId || !id) payload.staff_id = staffId;
    }
    const btn = $("appointment-save-btn");
    btn.disabled = true;
    $("appointment-form-error")?.classList.add("hidden");
    try {
      if (id) {
        await api(`/appointments/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await api("/appointments", { method: "POST", body: JSON.stringify(payload) });
      }
      hideAppointmentModal();
      const dayIso = payload.date || state.appointmentsDate;
      if (dayIso !== state.appointmentsDate) {
        state.appointmentsDate = dayIso;
      }
      await loadAppointmentsDay(state.appointmentsDate);
      setAppointmentsStatus(id ? "Cita actualizada" : "Cita creada");
    } catch (err) {
      showAppointmentFormError(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteCurrentAppointment() {
    const id = $("appointment-id")?.value?.trim();
    if (!id || !confirm("¿Eliminar esta cita?")) return;
    try {
      await api(`/appointments/${id}`, { method: "DELETE" });
      hideAppointmentModal();
      await loadAppointmentsDay(state.appointmentsDate);
      setAppointmentsStatus("Cita eliminada");
    } catch (err) {
      showAppointmentFormError(err.message);
    }
  }

  async function openAppointmentChat() {
    const convId = state.appointmentModalConversationId;
    if (!convId) return;
    hideAppointmentModal();
    switchPanelMode("chats");
    await selectConversation(convId);
  }

  const BIZ_FIELD_IDS = [
    "biz-name",
    "biz-industry",
    "biz-products",
    "biz-target",
    "biz-prices",
    "biz-hours",
    "biz-tone",
    "biz-restrictions",
  ];

  function readBizForm() {
    return {
      business_name: $("biz-name")?.value.trim() || undefined,
      industry: $("biz-industry")?.value.trim() || "",
      products_services: $("biz-products")?.value.trim() || "",
      target_customer: $("biz-target")?.value.trim() || "",
      price_range: $("biz-prices")?.value.trim() || "",
      location_hours: $("biz-hours")?.value.trim() || "",
      tone: $("biz-tone")?.value.trim() || "",
      restrictions: $("biz-restrictions")?.value.trim() || "",
    };
  }

  function setBusinessName(name, isPlaceholder) {
    if (!name) return;
    $("business-name").textContent = name;
    if (state.tenant) state.tenant.business_name = name;
    $("business-name-cta").classList.toggle("hidden", !isPlaceholder || !state.canManageGlobal);
  }

  function openBusinessNameSetup() {
    switchPanelMode("ai");
    $("biz-name")?.focus();
  }

  function fillBizForm(profile) {
    $("biz-name").value = profile && !profile.business_name_is_placeholder ? profile.business_name : "";
    $("biz-industry").value = profile?.industry || "";
    $("biz-products").value = profile?.products_services || "";
    $("biz-target").value = profile?.target_customer || "";
    $("biz-prices").value = profile?.price_range || "";
    $("biz-hours").value = profile?.location_hours || "";
    $("biz-tone").value = profile?.tone || "";
    $("biz-restrictions").value = profile?.restrictions || "";
    renderBizSummary(profile);
    updateAiSetupControls();
  }

  function renderBizSummary(profile) {
    const list = $("biz-summary-list");
    if (!list) return;
    const lines = profile?.ai_summary || [];
    if (!lines.length) {
      list.innerHTML = '<li class="muted">Completa las preguntas y guarda.</li>';
      return;
    }
    list.innerHTML = lines.map((line) => `<li><span class="biz-check">✓</span> ${escapeHtml(line)}</li>`).join("");
  }

  function updateAiSetupControls() {
    const ro = !state.canManageGlobal;
    BIZ_FIELD_IDS.forEach((id) => {
      const el = $(id);
      if (el) el.disabled = ro;
    });
    if ($("biz-save-btn")) $("biz-save-btn").disabled = ro;
    ["alert-threshold", "alert-save-btn", "alert-test-btn"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = ro;
    });
    document.querySelectorAll("#alert-recipients input, #alert-recipients select, #alert-recipients button")
      .forEach((el) => { el.disabled = ro; });
    updateAlertAddButton();
  }

  function alertRecipientRow(r) {
    const scope = r.scope === "sales" ? "sales" : "all";
    return `
      <div class="alert-recipient">
        <input type="text" class="alert-name" maxlength="60" placeholder="Nombre (ej. Carlos, despachos)" value="${escapeHtml(r.name || "")}" />
        <input type="tel" class="alert-phone" maxlength="32" placeholder="+57 300 123 4567" value="${escapeHtml(r.phone || "")}" />
        <select class="alert-scope" aria-label="Qué alertas recibe">
          <option value="all"${scope === "all" ? " selected" : ""}>Todas las alertas</option>
          <option value="sales"${scope === "sales" ? " selected" : ""}>Solo ventas y citas</option>
        </select>
        <button type="button" class="alert-remove" aria-label="Quitar número" title="Quitar">×</button>
      </div>`;
  }

  function renderAlertRecipients(list) {
    const rows = list && list.length ? list : [{ name: "", phone: "", scope: "all" }];
    $("alert-recipients").innerHTML = rows.map(alertRecipientRow).join("");
    updateAlertAddButton();
  }

  function readAlertRecipients() {
    return [...document.querySelectorAll("#alert-recipients .alert-recipient")].map((row) => ({
      name: row.querySelector(".alert-name").value.trim(),
      phone: row.querySelector(".alert-phone").value.trim(),
      scope: row.querySelector(".alert-scope").value,
    }));
  }

  function updateAlertAddButton() {
    const btn = $("alert-add-btn");
    if (!btn) return;
    const count = document.querySelectorAll("#alert-recipients .alert-recipient").length;
    btn.classList.toggle("hidden", count >= MAX_ALERT_RECIPIENTS);
    btn.disabled = !state.canManageGlobal;
  }

  function addAlertRecipient() {
    if (!state.canManageGlobal) return;
    const current = readAlertRecipients();
    if (current.length >= MAX_ALERT_RECIPIENTS) return;
    renderAlertRecipients([...current, { name: "", phone: "", scope: current.length ? "sales" : "all" }]);
    const names = document.querySelectorAll("#alert-recipients .alert-name");
    names[names.length - 1]?.focus();
  }

  function removeAlertRecipient(button) {
    if (!state.canManageGlobal) return;
    button.closest(".alert-recipient")?.remove();
    if (!document.querySelector("#alert-recipients .alert-recipient")) renderAlertRecipients([]);
    updateAlertAddButton();
  }

  function setAlertStatus(text) {
    const el = $("alert-save-status");
    if (el) el.textContent = text || "";
  }

  function fillInterestAlert(cfg) {
    renderAlertRecipients(cfg?.recipients || []);
    updateAiSetupControls();
    $("alert-threshold").value = cfg?.alert_threshold || 10;
    const pending = $("alert-pending");
    if (pending) {
      const n = cfg?.pending_count || 0;
      pending.textContent = n
        ? `Ahora mismo tienes ${n} interesado${n === 1 ? "" : "s"} sin responder.`
        : "Ahora mismo no tienes interesados sin responder.";
    }
  }

  async function loadInterestAlert() {
    setAlertStatus("");
    try {
      fillInterestAlert(await api("/tenants/me/interest-alert"));
    } catch (err) {
      setAlertStatus(err.message);
    }
  }

  async function saveInterestAlert() {
    if (!state.canManageGlobal) return;
    $("alert-save-btn").disabled = true;
    setAlertStatus("Guardando…");
    try {
      const cfg = await api("/tenants/me/interest-alert", {
        method: "PUT",
        body: JSON.stringify({
          recipients: readAlertRecipients(),
          alert_threshold: Number($("alert-threshold").value) || 10,
        }),
      });
      fillInterestAlert(cfg);
      const n = (cfg.recipients || []).length;
      setAlertStatus(
        n
          ? `✓ Guardado — ${n === 1 ? "1 número recibirá" : `${n} números recibirán`} las alertas`
          : "✓ Alertas desactivadas"
      );
    } catch (err) {
      setAlertStatus(err.message);
    } finally {
      updateAiSetupControls();
    }
  }

  async function testInterestAlert() {
    if (!state.canManageGlobal) return;
    $("alert-test-btn").disabled = true;
    setAlertStatus("Enviando prueba…");
    try {
      await api("/tenants/me/interest-alert/test", { method: "POST" }, 20000);
      setAlertStatus("✓ Prueba enviada a todos los números guardados — revisen su WhatsApp");
    } catch (err) {
      setAlertStatus(err.message);
    } finally {
      updateAiSetupControls();
    }
  }

  function setBizSaveStatus(text) {
    const el = $("biz-save-status");
    if (el) el.textContent = text || "";
  }

  async function loadAiSetupPanel() {
    setBizSaveStatus("");
    try {
      const profile = await api("/outbound/business-profile");
      state.aiProfile = profile;
      fillBizForm(profile);
    } catch (err) {
      setBizSaveStatus(err.message);
      fillBizForm(null);
    }
    await Promise.all([loadQuickShortcuts(), loadInterestAlert()]);
    renderAiShortcutsSummary();
  }

  async function saveAiSetup() {
    if (!state.canManageGlobal) return;
    const payload = readBizForm();
    if (!payload.business_name && state.aiProfile?.business_name_is_placeholder) {
      setBizSaveStatus("Escribe el nombre de tu negocio (paso 1)");
      $("biz-name").focus();
      return;
    }
    const btn = $("biz-save-btn");
    btn.disabled = true;
    setBizSaveStatus("Guardando…");
    try {
      const profile = await api("/outbound/business-profile", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      state.aiProfile = profile;
      fillBizForm(profile);
      setBusinessName(profile.business_name, profile.business_name_is_placeholder);
      setBizSaveStatus("✓ Guardado — tu IA ya conoce tu negocio");
      renderOnboarding();
    } catch (err) {
      setBizSaveStatus(err.message);
    } finally {
      updateAiSetupControls();
    }
  }

  function refreshAiSetupIfVisible() {
    if (state.panelMode === "ai") loadAiSetupPanel();
  }

  function newShortcutDraft() {
    return {
      id: crypto.randomUUID(),
      label: "",
      type: "text",
      text: "",
      image_path: null,
      image_url: null,
    };
  }

  async function loadQuickShortcuts() {
    try {
      state.quickShortcuts = await api("/quick-shortcuts");
    } catch {
      state.quickShortcuts = [];
    }
    renderQuickShortcuts();
    renderAiShortcutsSummary();
  }

  function renderAiShortcutsSummary() {
    const list = $("ai-shortcuts-summary");
    const btn = $("ai-shortcuts-config-btn");
    if (!list) return;
    const items = state.quickShortcuts || [];
    if (!items.length) {
      list.innerHTML = '<li class="muted">Aún no tienes atajos — agrega Menú, Precios, Horarios…</li>';
    } else {
      list.innerHTML = items.map((s) => {
        const kind = s.type === "image" ? "Foto" : s.type === "document" ? "PDF" : "Texto";
        return `<li><strong>${escapeHtml(s.label)}</strong> <span class="muted small">· ${kind}</span></li>`;
      }).join("");
    }
    if (btn) btn.classList.toggle("hidden", !state.canManageGlobal);
  }

  function renderQuickShortcuts() {
    const wrap = $("quick-shortcuts-wrap");
    const bar = $("quick-shortcuts-bar");
    if (!wrap || !bar) return;

    const show = state.panelMode === "chats" && state.activeId && state.canWrite;
    wrap.classList.toggle("hidden", !show);

    if (!show) {
      bar.innerHTML = "";
      return;
    }

    const items = state.quickShortcuts || [];
    const pills = items.map((s) => {
      const icon = s.type === "image" ? "🖼 " : s.type === "document" ? "📄 " : "";
      return `<button type="button" class="quick-shortcut-pill" data-shortcut-id="${escapeHtml(s.id)}" title="Enviar ${escapeHtml(s.label)}">${icon}${escapeHtml(s.label)}</button>`;
    }).join("");

    const editBtn = state.canManageGlobal
      ? `<button type="button" class="quick-shortcut-pill quick-shortcut-edit" id="edit-shortcuts-btn" title="Configurar atajos (todos los chats)">⚙ Atajos</button>`
      : "";

    bar.innerHTML = pills + editBtn;
    bar.querySelectorAll("[data-shortcut-id]").forEach((btn) => {
      btn.addEventListener("click", () => sendQuickShortcut(btn.dataset.shortcutId));
    });
    $("edit-shortcuts-btn")?.addEventListener("click", openShortcutsModal);
  }

  async function sendQuickShortcut(shortcutId) {
    if (!state.activeId || !state.canWrite) return;
    const pill = document.querySelector(`[data-shortcut-id="${shortcutId}"]`);
    if (pill) pill.disabled = true;
    try {
      await api("/whatsapp/status", {}, 8000).catch(() => null);
      const msg = await api(`/conversations/${state.activeId}/messages/shortcut`, {
        method: "POST",
        body: JSON.stringify({ shortcut_id: shortcutId }),
      });
      const exists = state.messages.some((m) => m.id === msg.id);
      if (!exists) {
        state.messages.push(msg);
        state._messagesSig = state.messages
          .map((m) => m.id || `${m.body}|${m.created_at}`)
          .join("\n");
        renderMessages();
      }
    } catch (err) {
      alert(err.message);
    } finally {
      if (pill) pill.disabled = false;
    }
  }

  function openShortcutsModal() {
    state.shortcutsDraft = (state.quickShortcuts || []).map((s) => ({ ...s }));
    if (!state.shortcutsDraft.length) state.shortcutsDraft.push(newShortcutDraft());
    renderShortcutsEditor();
    $("shortcuts-modal").classList.remove("hidden");
  }

  function closeShortcutsModal() {
    $("shortcuts-modal").classList.add("hidden");
  }

  function renderShortcutsEditor() {
    const list = $("shortcuts-editor-list");
    if (!list) return;
    list.innerHTML = state.shortcutsDraft.map((s, idx) => {
      const type = s.type || "text";
      const isFile = type === "image" || type === "document";
      const hasFile = type === "image" ? !!s.image_path : type === "document" && !!s.file_path;
      const preview = type === "image" && s.image_url
        ? `<img src="${escapeHtml(s.image_url)}" class="shortcut-thumb" alt="" />`
        : type === "document" && s.file_path
          ? `<span class="small">📄 ${escapeHtml(s.file_name || "catalogo.pdf")}</span>`
          : "";
      const uploadLabel = s.uploading
        ? "⏳ Leyendo archivo…"
        : type === "image" ? "📷 Subir foto" : "📄 Subir PDF";
      const accept = type === "image"
        ? "image/jpeg,image/png,image/webp,image/gif"
        : "application/pdf,.pdf";
      const contentHint = s.content_failed
        ? "No pude leer el archivo automáticamente. Escribe aquí tus productos y precios para que la IA los conozca."
        : "La IA usa esto para responder precios y productos sin reenviar el archivo. Puedes corregirlo.";
      return `
        <div class="shortcut-editor-row" data-idx="${idx}">
          <input type="text" class="shortcut-label" maxlength="20" placeholder="Nombre — ej: Menú" value="${escapeHtml(s.label || "")}" />
          <div class="shortcut-type-row">
            <label><input type="radio" name="stype-${idx}" value="text" ${type === "text" ? "checked" : ""} /> Texto</label>
            <label><input type="radio" name="stype-${idx}" value="image" ${type === "image" ? "checked" : ""} /> Foto</label>
            <label><input type="radio" name="stype-${idx}" value="document" ${type === "document" ? "checked" : ""} /> PDF</label>
          </div>
          <div class="shortcut-text-wrap ${isFile ? "hidden" : ""}">
            <textarea class="shortcut-text" rows="2" maxlength="500" placeholder="Ej: Hola! Aquí tienes nuestros precios…">${escapeHtml(s.text || "")}</textarea>
          </div>
          <div class="shortcut-image-wrap ${isFile ? "" : "hidden"}">
            <label class="btn ghost small wa-upload-btn">
              ${uploadLabel}
              <input type="file" class="shortcut-file-input" accept="${accept}" ${s.uploading ? "disabled" : ""} hidden />
            </label>
            ${preview}
          </div>
          <div class="shortcut-content-wrap ${isFile && hasFile ? "" : "hidden"}">
            <label class="small muted">Lo que la IA sabe de este archivo</label>
            <textarea class="shortcut-content" rows="4" maxlength="4000" placeholder="Ej: Tenis blancos $120.000 (tallas 36-42)…">${escapeHtml(s.content || "")}</textarea>
            <p class="small muted">${contentHint}</p>
          </div>
          <button type="button" class="btn-link shortcut-remove-btn">Quitar</button>
        </div>`;
    }).join("");

    list.querySelectorAll(".shortcut-editor-row").forEach((row) => {
      const idx = parseInt(row.dataset.idx, 10);
      row.querySelector(".shortcut-label")?.addEventListener("input", (e) => {
        state.shortcutsDraft[idx].label = e.target.value;
      });
      row.querySelectorAll(`input[name="stype-${idx}"]`).forEach((radio) => {
        radio.addEventListener("change", (e) => {
          state.shortcutsDraft[idx].type = e.target.value;
          renderShortcutsEditor();
        });
      });
      row.querySelector(".shortcut-text")?.addEventListener("input", (e) => {
        state.shortcutsDraft[idx].text = e.target.value;
      });
      row.querySelector(".shortcut-content")?.addEventListener("input", (e) => {
        state.shortcutsDraft[idx].content = e.target.value;
      });
      row.querySelector(".shortcut-file-input")?.addEventListener("change", async (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        const draft = state.shortcutsDraft[idx];
        draft.uploading = true;
        renderShortcutsEditor();
        try {
          const fd = new FormData();
          fd.append("file", file);
          const res = await api("/quick-shortcuts/files", { method: "POST", body: fd }, 90000);
          draft.type = res.type;
          if (res.type === "document") {
            draft.file_path = res.path;
            draft.file_name = res.file_name;
            draft.image_path = null;
            draft.image_url = null;
          } else {
            draft.image_path = res.path;
            draft.image_url = res.url;
            draft.file_path = null;
            draft.file_name = null;
          }
          draft.content = res.content || "";
          draft.content_failed = !res.content_ok;
        } catch (err) {
          alert(err.message);
        } finally {
          draft.uploading = false;
          renderShortcutsEditor();
        }
      });
      row.querySelector(".shortcut-remove-btn")?.addEventListener("click", () => {
        state.shortcutsDraft.splice(idx, 1);
        if (!state.shortcutsDraft.length) state.shortcutsDraft.push(newShortcutDraft());
        renderShortcutsEditor();
      });
    });
  }

  async function saveQuickShortcuts() {
    const payload = state.shortcutsDraft
      .filter((s) => s.label?.trim())
      .map((s) => {
        const type = s.type || "text";
        return {
        id: s.id,
        label: s.label.trim(),
          type,
          text: type === "text" ? (s.text || "").trim() : null,
          image_path: type === "image" ? s.image_path : null,
          file_path: type === "document" ? s.file_path : null,
          file_name: type === "document" ? s.file_name : null,
          content: type !== "text" ? (s.content || "").trim() || null : null,
        };
      })
      .filter((s) => (s.type === "text" ? s.text : s.type === "image" ? s.image_path : s.file_path));

    $("shortcuts-save-btn").disabled = true;
    try {
      state.quickShortcuts = await api("/quick-shortcuts", {
        method: "PUT",
        body: JSON.stringify({ shortcuts: payload }),
      });
      closeShortcutsModal();
      renderQuickShortcuts();
    } catch (err) {
      alert(err.message);
    } finally {
      $("shortcuts-save-btn").disabled = false;
    }
  }

  function aiBlockReasonForConv(conv) {
    if (!conv) return "";
    if (state.tenant && !state.tenant.ai_global_enabled) {
      return "Activa «IA global» arriba a la derecha para que la IA pueda responder.";
    }
    if (!conv.ai_active) {
      if (conv.imported_legacy && !conv.bait_sent) {
        return "Chat personal — activa «IA activa» si quieres que responda en este hilo.";
      }
      return "Activa «IA activa» para que responda sola a este contacto.";
    }
    if (conv.mode === "manual") return "Modo manual activo — apágalo para que la IA responda";
    return "";
  }

  async function patchConversationAi(enabled) {
    if (!state.activeId) {
      alert("Selecciona un chat primero.");
      return;
    }
    if (!state.canWrite) {
      alert("Tu usuario no puede editar chats. Pide acceso de agente o dueño.");
      return;
    }
    const input = $("toggle-ai-chat");
    const prev = !!input?.checked;
    if (input) input.checked = !!enabled;
    try {
      const conv = await api(`/conversations/${state.activeId}/ai`, {
        method: "PATCH",
        body: JSON.stringify({ ai_active: !!enabled }),
      });
      upsertConversation(conv);
      if (conv.id === state.activeId) syncChatToggles(conv);
      if (enabled && conv.ai_active && conv.mode !== "manual") {
        try {
          await api(`/conversations/${state.activeId}/ai/trigger`, { method: "POST" }, 20000);
        } catch (triggerErr) {
          const msg = String(triggerErr.message || "");
          if (!/pendiente|manual|global|desactivada/i.test(msg)) {
            console.warn("IA trigger:", msg);
          }
        }
      }
    } catch (err) {
      if (input) input.checked = prev;
      alert(err.message || "No se pudo cambiar la IA en este chat");
    }
  }

  function showAiNotice(text, isError) {
    state.aiNotice = { text, isError: !!isError };
    renderAiAlerts();
    clearTimeout(state.aiNoticeTimer);
    state.aiNoticeTimer = setTimeout(() => {
      state.aiNotice = null;
      renderAiAlerts();
    }, 120000);
  }

  async function refreshAiStatus() {
    try {
      const fresh = await api("/ai/status", {}, 15000);
      if (!fresh) return;
      state.aiStatus = fresh;
      renderAiAlerts();
      if (state.activeId) {
        const conv = state.conversations.find((c) => c.id === state.activeId);
        if (conv) syncChatToggles(conv);
      }
    } catch (_) {
      /* panel sin sesión o backend caído */
    }
  }

  function renderAiAlerts() {
    const banner = $("ai-alert-banner");
    const parts = [];
    if (state.tenant && !state.tenant.ai_global_enabled) {
      parts.push("IA global apagada — los chats con IA activa no responderán solos.");
    }
    if (state.aiNotice) parts.push(state.aiNotice.text);
    if (!parts.length) {
      banner.classList.add("hidden");
      banner.textContent = "";
      renderPlanStatus();
      return;
    }
    banner.textContent = parts.join(" · ");
    banner.classList.toggle("error", !!state.aiNotice?.isError);
    banner.classList.remove("hidden");
    renderPlanStatus();
  }

  function syncChatToggles(conv) {
    $("toggle-mode-manual").checked = conv.mode === "manual";
    $("toggle-ai-chat").checked = !!conv.ai_active;
    const canEdit = state.canWrite && state.wa.status === "connected";
    $("toggle-mode-manual").disabled = !canEdit;
    $("toggle-ai-chat").disabled = !canEdit;
    syncInterestButtons(conv);
    const canRetry = state.canWrite && conv.ai_active && state.tenant?.ai_global_enabled && conv.mode !== "manual";
    $("ai-retry-btn").classList.toggle("hidden", !canRetry);
    $("ai-retry-btn").disabled = !canRetry;
    const hint = aiBlockReasonForConv(conv);
    const hintEl = $("chat-ai-hint");
    if (hint) {
      hintEl.textContent = hint;
      hintEl.classList.remove("hidden");
    } else {
      hintEl.textContent = "";
      hintEl.classList.add("hidden");
    }
  }

  function syncInterestButtons(conv) {
    const interestedBtn = $("mark-interested-btn");
    const notInterestedBtn = $("mark-not-interested-btn");
    if (!interestedBtn || !notInterestedBtn) return;
    const status = conv?.interest_status || null;
    interestedBtn.classList.toggle("active", status === "interested");
    interestedBtn.classList.toggle("interested", status === "interested");
    notInterestedBtn.classList.toggle("active", status === "not_interested");
    notInterestedBtn.classList.toggle("not-interested", status === "not_interested");
    const disabled = !state.canWrite;
    interestedBtn.disabled = disabled;
    notInterestedBtn.disabled = disabled;
  }

  async function setConversationInterest(status) {
    if (!state.activeId || !state.canWrite) return;
    const conv = state.conversations.find((c) => c.id === state.activeId);
    const next = conv?.interest_status === status ? null : status;
    const interestedBtn = $("mark-interested-btn");
    const notInterestedBtn = $("mark-not-interested-btn");
    interestedBtn.disabled = true;
    notInterestedBtn.disabled = true;
    try {
      const updated = await api(`/conversations/${state.activeId}/interest`, {
        method: "PATCH",
        body: JSON.stringify({ interest_status: next }),
      });
      upsertConversation(updated);
      if (updated.id === state.activeId) syncChatToggles(updated);
    } catch (err) {
      alert(err.message);
      syncInterestButtons(conv);
    }
  }

  async function selectConversation(id) {
    pullInFlight = false;
    if (pendingImage && pendingImage.conversationId !== id) clearPendingImage();
    state.activeId = id;
    state.messages = [];
    state._messagesSig = "";
    const conv = state.conversations.find((c) => c.id === id);
    if (!conv) return;

    emptyChat.classList.add("hidden");
    activeChat.classList.remove("hidden");
    $("chat-title").textContent = convTitle(conv);
    applyChatContactPhone(conv);
    syncChatToggles(conv);
    renderConversationList();
    renderQuickShortcuts();

    try {
      const msgs = dedupeMessages(await api(`/conversations/${id}/messages`));
      if (state.activeId !== id) return;
      state.messages = msgs;
      state._messagesSig = msgs
        .map((m) => m.id || `${m.body}|${m.created_at}`)
        .join("\n");
      renderMessages();
      if (state.wa.status === "connected") pullActiveChat();
      if (conv.unread_count) {
        conv.unread_count = 0;
        renderConversationList();
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function loadInitial() {
    conversationList.innerHTML = '<li class="conversation-item"><span class="muted">Cargando…</span></li>';
    try {
      const me = state.user || (await api("/auth/me"));
      applyUserFromMe(me);

      const [tenant, wa, aiStatus, bizProfile, subscription] = await Promise.all([
        api("/tenants/me").catch(() => null),
        api("/whatsapp/status", {}, 8000).catch(() => ({
          status: "disconnected",
          qr_base64: null,
          phone_number: null,
          chatwoot_inbox_url: null,
        })),
        api("/ai/status", {}, 15000).catch(() => null),
        api("/outbound/business-profile").catch(() => null),
        api("/subscriptions/me").catch(() => null),
      ]);
      await loadQuickShortcuts().catch(() => {});

      state.tenant = tenant;
      state.aiStatus = aiStatus;
      state.aiProfile = bizProfile;
      state.subscription = subscription;

      if (tenant) {
        setBusinessName(tenant.business_name, !!bizProfile?.business_name_is_placeholder);
      $("toggle-ai-global").checked = tenant.ai_global_enabled;
      }

      applyWaSession(wa);
      setChatListTab(state.chatListTab);
      if (!state.syncInProgress) {
        try {
          const all = sortConversations(await api("/conversations?archived=false", {}, 30000));
          recalculateInterestedCount(all);
          const filtered =
            state.chatListTab === "all"
              ? all
              : all.filter((c) => matchesInterestTab(c, state.chatListTab));
          setConversations(filtered);
        } catch {
          state.conversations = [];
          recalculateInterestedCount([]);
        }
      } else {
        state.conversations = [];
        recalculateInterestedCount([]);
      }
      sendForm.classList.toggle("disabled", !state.canWrite || state.wa.status !== "connected");
      messageInput.disabled = !state.canWrite || state.wa.status !== "connected";

      renderConversationList();
      renderWaUi();
      renderAiAlerts();
      renderOnboarding();
      if (
        bizProfile?.business_name_is_placeholder &&
        state.canManageGlobal &&
        !sessionStorage.getItem("omitel_name_prompted")
      ) {
        sessionStorage.setItem("omitel_name_prompted", "1");
        openBusinessNameSetup();
      }
      connectWs();
      startWaHeartbeat();
      if (state.wa.status === "connected") startLivePoll();
    } catch (err) {
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${escapeHtml(err.message)}</span></li>`;
    } finally {
      renderWaUi();
    }
  }

  function connectWs() {
    disconnectWs();
    if (!state.user) return;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/panel`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = () => {
      setWsBadge(true);
      updateSyncStatus({});
    };
    ws.onclose = () => {
      setWsBadge(false);
      if (state.user) setTimeout(connectWs, 3000);
    };
    ws.onerror = () => setWsBadge(false);
    ws.onmessage = (ev) => {
      try {
        handleEvent(JSON.parse(ev.data));
      } catch (e) {
        console.warn("WS parse error", e);
      }
    };
  }

  function disconnectWs() {
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.close();
      state.ws = null;
    }
    setWsBadge(false);
  }

  function clearConversations() {
    state.conversations = [];
    state.activeId = null;
    state.messages = [];
    state._messagesSig = "";
    state.searchPool = null;
    mediaCache.clear();
    renderConversationList();
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
  }

  function prepareForNewDevice() {
    state.autoSyncRequested = false;
    state.syncInProgress = false;
    clearSyncWatchdog();
    clearConversations();
  }

  function handleEvent(event) {
    switch (event.type) {
      case "connected":
        break;
      case "conversations.cleared":
        prepareForNewDevice();
        break;
      case "message.in":
      case "message.out":
        if (event.conversation) upsertConversation(event.conversation);
        if (event.message) {
          const sameChat = event.conversation?.id === state.activeId;
          if (sameChat) {
            const key = event.message.id || `${event.message.body}|${event.message.created_at}`;
            const exists = state.messages.some(
              (m) => m.id === event.message.id || `${m.body}|${m.created_at}` === `${event.message.body}|${event.message.created_at}`
            );
            if (!exists) {
              state.messages.push(event.message);
              state._messagesSig = state.messages
                .map((m) => m.id || `${m.body}|${m.created_at}`)
                .join("\n");
              renderMessages();
            } else if (event.message.id) {
              const idx = state.messages.findIndex((m) => m.id === event.message.id);
              if (idx >= 0 && event.message.status && event.message.status !== state.messages[idx].status) {
                state.messages[idx] = event.message;
              renderMessages();
              }
            }
          }
        }
        break;
      case "message.updated":
        if (event.message && event.conversation?.id === state.activeId) {
          const idx = state.messages.findIndex((m) => m.id === event.message.id);
          if (idx >= 0) {
            state.messages[idx] = event.message;
            renderMessages();
          }
        }
        break;
      case "conversation.updated":
        if (event.conversation) {
          const prev = state.conversations.find((c) => c.id === event.conversation.id);
          const wasInterested = prev && getConversationInterest(prev) === "interested";
          const nowInterested = getConversationInterest(event.conversation) === "interested";
          upsertConversation(event.conversation);
          if (!wasInterested && nowInterested) {
            pulseInterestedTab();
          }
          api("/conversations?archived=false", {}, 15000)
            .then((rows) => recalculateInterestedCount(sortConversations(rows)))
            .catch(() => {});
          renderOnboarding();
        }
        break;
      case "whatsapp.status":
        applyWaSession({
          status: event.status,
          qr_base64: event.qr_base64,
          phone_number: event.phone_number,
        });
        break;
      case "tenant.settings":
        if (state.tenant) {
          setBusinessName(event.business_name, event.business_name_is_placeholder);
          state.tenant.ai_global_enabled = event.ai_global_enabled;
          $("toggle-ai-global").checked = event.ai_global_enabled;
          renderAiAlerts();
          if (state.activeId) {
            const conv = state.conversations.find((c) => c.id === state.activeId);
            if (conv) syncChatToggles(conv);
          }
        }
        if (event.whatsapp_status) {
          applyWaSession({
            status: event.whatsapp_status,
            qr_base64: null,
            phone_number: event.whatsapp_status === "connected" ? state.wa.phone_number : null,
          });
        }
        break;
      case "sync.started":
        if (!state.syncInProgress) showSyncingList();
        break;
      case "sync.progress":
        if (!state.syncInProgress) showSyncingList();
        fetchConversations()
          .then((rows) => {
            if (rows.length) {
              setConversations(rows);
              renderConversationList();
            }
          })
          .catch(() => {});
        break;
      case "sync.completed":
        clearSyncWatchdog();
        state.syncInProgress = false;
        if (event.status === "completed") {
          fetchConversations()
            .then((rows) => {
              setConversations(rows);
              renderConversationList();
              if (state.activeId) {
                return api(`/conversations/${state.activeId}/messages`).then((msgs) => {
                  state.messages = dedupeMessages(msgs);
                  renderMessages();
                });
              }
            })
            .catch(() => renderConversationList());
        } else if (event.status === "failed") {
          fetchConversations()
            .then((rows) => {
              setConversations(rows);
              renderConversationList();
            })
            .catch(() => renderConversationList());
        }
        break;
      case "contacts.enriched":
        fetchConversations()
          .then((rows) => {
            setConversations(rows);
            renderConversationList();
          })
          .catch(() => {});
        break;
      case "ai.error":
      case "ai.handoff": {
        const text = String(event.error || event.message || "").slice(0, 220);
        if (!text) break;
        showAiNotice(text, event.type === "ai.error");
        const hintEl = $("chat-ai-hint");
        if (hintEl && state.activeId && event.conversation_id === state.activeId) {
          hintEl.textContent = text;
          hintEl.classList.remove("hidden");
        }
        if (event.type === "ai.error") refreshAiStatus();
          if (state.activeId) {
            const conv = state.conversations.find((c) => c.id === state.activeId);
            if (conv) syncChatToggles(conv);
        }
        break;
      }
      default:
        break;
    }
  }

  function showLoginStep(step) {
    loginStepEmail?.classList.toggle("hidden", step !== "email");
    loginStepRegister?.classList.toggle("hidden", step !== "register");
    loginStepSent?.classList.toggle("hidden", step !== "sent");
    [loginError, $("login-error-register")].forEach((el) => {
      if (el) {
        el.textContent = "";
        el.classList.add("hidden");
      }
    });
  }

  function showMagicLinkSent(email, message, devLink) {
    $("login-sent-email").textContent = email;
    $("login-sent-message").textContent = message || "Te enviamos un enlace seguro a tu correo.";
    const devWrap = $("login-dev-link-wrap");
    const devAnchor = $("login-dev-link");
    if (devLink && devWrap && devAnchor) {
      devAnchor.href = devLink;
      devWrap.classList.remove("hidden");
    } else {
      devWrap?.classList.add("hidden");
    }
    showLoginStep("sent");
  }

  async function requestMagicLink({ email, business_name, owner_name, accept_legal, accept_marketing }) {
    const body = { email };
    if (business_name) body.business_name = business_name;
    if (owner_name) body.owner_name = owner_name;
    if (accept_legal) body.accept_legal = true;
    if (accept_marketing) body.accept_marketing = true;
    const res = await fetch(`${API}/auth/magic-link`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "No pudimos enviar el enlace");
    return data;
  }

  function setLoginError(el, message) {
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
  }

  async function sendLoginMagicLink(withSignup = false) {
    const business_name = withSignup ? ($("register-business")?.value || "").trim() : "";
    const owner_name = withSignup ? ($("register-owner")?.value || "").trim() : "";
    if (withSignup && (!business_name || !owner_name)) {
      setLoginError($("login-error-register"), "Completa el nombre del negocio y tu nombre");
      return;
    }
    const accept_legal = Boolean($("register-accept-legal")?.checked);
    const accept_marketing = Boolean($("register-accept-marketing")?.checked);
    if (withSignup && !accept_legal) {
      setLoginError(
        $("login-error-register"),
        "Para crear la cuenta debes aceptar los Términos y Condiciones y la Política de Tratamiento de Datos Personales."
      );
      return;
    }
    const data = await requestMagicLink({
      email: loginEmail,
      ...(withSignup ? { business_name, owner_name, accept_legal, accept_marketing } : {}),
    });
    if (data.needs_signup) {
      if (withSignup && data.message) {
        setLoginError($("login-error-register"), data.message);
        return;
      }
      $("register-email-display").textContent = loginEmail;
      showLoginStep("register");
      $("register-business")?.focus();
      return;
    }
    if (data.sent) {
      showMagicLinkSent(loginEmail, data.message, data.dev_link);
    }
  }

  async function completeAuth(data) {
    state.token = null;
    try {
      localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    showPanel();
    await loadInitial();
  }

  async function loadAuthProviders() {
    const stack = $("login-oauth");
    const divider = $("login-oauth-divider");
    const googleBtn = $("oauth-google");
    const githubBtn = $("oauth-github");

    googleBtn?.classList.add("hidden");
    githubBtn?.classList.add("hidden");
    stack?.classList.add("hidden");
    divider?.classList.add("hidden");

    if (!(await pingBackend(4000))) {
      setBackendOfflineBanner(true);
      googleBtn?.classList.remove("hidden");
      stack?.classList.remove("hidden");
      divider?.classList.remove("hidden");
      bindOAuthLink(googleBtn);
      bindOAuthLink(githubBtn);
      return;
    }
    setBackendOfflineBanner(false);

    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 5000);
      const res = await fetch(`${API}/auth/providers`, { signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) {
        googleBtn?.classList.remove("hidden");
        stack?.classList.remove("hidden");
        divider?.classList.remove("hidden");
        return;
      }
      const data = await res.json();
      let visible = false;
      if (data.google) {
        googleBtn?.classList.remove("hidden");
        visible = true;
      }
      if (data.github) {
        githubBtn?.classList.remove("hidden");
        visible = true;
      }
      if (visible) {
        stack?.classList.remove("hidden");
        divider?.classList.remove("hidden");
      }
      bindOAuthLink(googleBtn);
      bindOAuthLink(githubBtn);
    } catch {
      setBackendOfflineBanner(true);
      googleBtn?.classList.remove("hidden");
      stack?.classList.remove("hidden");
      divider?.classList.remove("hidden");
      bindOAuthLink(googleBtn);
      bindOAuthLink(githubBtn);
    }
  }

  $("login-continue-btn")?.addEventListener("click", async () => {
    loginEmail = ($("login-email")?.value || "").trim().toLowerCase();
    if (!loginEmail) {
      setLoginError(loginError, "Ingresa tu correo");
      return;
    }
    if (!(await ensureBackendOnline())) {
      setLoginError(loginError, BACKEND_OFFLINE_MSG);
      return;
    }
    const btn = $("login-continue-btn");
    btn.disabled = true;
    try {
      await sendLoginMagicLink(false);
    } catch (err) {
      setLoginError(loginError, err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("register-back-btn")?.addEventListener("click", () => {
    $("register-business").value = "";
    $("register-owner").value = "";
    $("register-accept-legal").checked = false;
    $("register-accept-marketing").checked = false;
    showLoginStep("email");
  });

  $("login-sent-back-btn")?.addEventListener("click", () => {
    $("login-dev-link-wrap")?.classList.add("hidden");
    resetLoginForm();
  });

  $("register-submit-btn")?.addEventListener("click", async () => {
    const errEl = $("login-error-register");
    const btn = $("register-submit-btn");
    btn.disabled = true;
    try {
      await sendLoginMagicLink(true);
    } catch (err) {
      setLoginError(errEl, err.message);
    } finally {
      btn.disabled = false;
    }
  });

  loginForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!loginStepEmail?.classList.contains("hidden")) {
      $("login-continue-btn")?.click();
    }
  });

  $("logout-btn").addEventListener("click", logout);
  $("wa-connect-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    connectWhatsApp();
  });
  $("wa-setup-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    connectWhatsApp();
  });
  $("wa-disconnect-btn").addEventListener("click", async () => {
    if (!confirm("¿Desvincular WhatsApp? Se borrarán todos los chats del panel.")) return;
    try {
      await api("/whatsapp/disconnect", { method: "POST" }, 15000);
      clearConversations();
      await refreshWaStatus();
    } catch (err) {
      alert(err.message);
    }
  });
  $("wa-reset-chats-btn").addEventListener("click", async () => {
    if (!confirm("¿Borrar chats importados de otro celular y empezar limpio?")) return;
    try {
      await api("/whatsapp/reset-binding", { method: "POST" }, 30000);
      prepareForNewDevice();
      await triggerAutoSync();
    } catch (err) {
      alert(err.message);
    }
  });
  $("qr-modal-close").addEventListener("click", hideQrModal);
  $("qr-modal-backdrop").addEventListener("click", hideQrModal);
  $("refresh-chats").addEventListener("click", async () => {
    const btn = $("refresh-chats");
    btn.disabled = true;
    try {
      const stats = await api("/whatsapp/enrich-contacts", { method: "POST" }, 120000);
      setConversations(await fetchConversations());
      renderConversationList();
      if (stats.names_fixed || stats.phones_fixed) {
        alert(stats.message || `Actualizados: ${stats.names_fixed} nombres`);
      }
    } catch (err) {
      setConversations(await fetchConversations());
      renderConversationList();
      if (!String(err.message).includes("409")) alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  let debugReport = null;
  let debugSyncReport = null;
  let debugTab = "sync";

  function setDebugTab(tab) {
    debugTab = tab;
    $("debug-tab-sync").classList.toggle("active", tab === "sync");
    $("debug-tab-names").classList.toggle("active", tab === "names");
    $("debug-sync-panel").classList.toggle("hidden", tab !== "sync");
    $("debug-names-panel").classList.toggle("hidden", tab !== "names");
    $("debug-run-btn").classList.toggle("hidden", tab !== "names");
    $("debug-run-sync-btn").classList.toggle("hidden", tab !== "sync");
  }

  function showDebugModal() {
    $("debug-modal").classList.remove("hidden");
    setDebugTab(debugTab);
  }

  function hideDebugModal() {
    $("debug-modal").classList.add("hidden");
  }

  function renderDebugSummary(report) {
    const t = report.timing_ms || {};
    const api = report.evolution_api || {};
    const app = report.app_db || {};
    const sync = report.sync_queue || {};
    const cards = [
      { label: "Tiempo total", value: `${t.total ?? "—"} ms` },
      { label: "findChats API", value: `${t.find_chats_api ?? "—"} ms · ${api.find_chats_count ?? 0} chats` },
      { label: "findContacts API", value: `${t.find_contacts_api ?? "—"} ms · ${api.find_contacts_count ?? 0} contactos` },
      { label: "Evolution DB", value: `${t.evolution_db ?? "—"} ms` },
      { label: "Conversaciones app", value: `${app.total_conversations ?? 0} (${app.with_real_name ?? 0} con nombre)` },
      { label: "Sin nombre", value: `${app.placeholder_names ?? 0} chats` },
      { label: "Cola sync", value: sync.pending_jobs ? `${sync.pending_jobs} pendiente(s)` : "vacía" },
    ];
    $("debug-summary").innerHTML = cards.map((c) => (
      `<div class="debug-stat"><strong>${escapeHtml(String(c.value))}</strong><span>${escapeHtml(c.label)}</span></div>`
    )).join("");
    $("debug-summary").classList.remove("hidden");
  }

  function renderDebugSample(report) {
    const rows = report.sample_missing_names || [];
    if (!rows.length) {
      $("debug-sample").innerHTML = '<p class="muted small">No hay chats sin nombre en la muestra (o todos tienen nombre).</p>';
      $("debug-sample").classList.remove("hidden");
      return;
    }
    const head = `
      <table class="debug-table">
        <thead>
          <tr>
            <th>Teléfono</th>
            <th>Nombre guardado</th>
            <th>findChats</th>
            <th>findContacts</th>
            <th>Resuelto</th>
            <th>Motivo</th>
          </tr>
        </thead>
        <tbody>
    `;
    const body = rows.map((row) => `
      <tr>
        <td>${escapeHtml(row.contact_phone || "—")}</td>
        <td>${escapeHtml(row.contact_name || "—")}</td>
        <td>${row.in_find_chats ? escapeHtml(row.find_chats_push_name || "sí") : "no"}</td>
        <td>${row.in_find_contacts ? escapeHtml(row.find_contacts_push_name || "sí") : "no"}</td>
        <td>${escapeHtml(row.resolved_name_api || row.resolved_name_db || "—")}</td>
        <td class="reason">${escapeHtml(row.reason || "—")}</td>
      </tr>
    `).join("");
    $("debug-sample").innerHTML = `${head}${body}</tbody></table>`;
    $("debug-sample").classList.remove("hidden");
  }

  function renderSyncDebug(report) {
    const wh = report.webhook || {};
    const urls = report.urls || {};
    const msgs = report.messages || {};
    const cards = [
      { label: "Último webhook", value: wh.last_received_at || "nunca" },
      { label: "Cola webhook", value: `${wh.queue_depth ?? 0} pendiente(s)` },
      { label: "Worker webhook", value: wh.worker_alive ? "activo" : "inactivo" },
      { label: "URL Evolution", value: wh.evolution_config?.url || "—" },
      { label: "URL esperada", value: urls.webhook_expected || "—" },
      { label: "Msgs últimos 15 min", value: msgs.last_15_minutes ?? 0 },
      { label: "Último mensaje", value: msgs.last_message_preview || "—" },
    ];
    $("debug-sync-summary").innerHTML = cards.map((c) => (
      `<div class="debug-stat"><strong>${escapeHtml(String(c.value))}</strong><span>${escapeHtml(c.label)}</span></div>`
    )).join("");
    $("debug-sync-summary").classList.remove("hidden");

    const trace = wh.recent_trace || [];
    if (!trace.length) {
      $("debug-sync-trace").innerHTML = '<p class="muted small">Sin webhooks registrados aún. Escribe desde el celular y vuelve a ejecutar.</p>';
    } else {
      const rows = trace.map((row) => `
        <tr>
          <td>${escapeHtml(row.at || "")}</td>
          <td>${escapeHtml(row.event || "")}</td>
          <td>${escapeHtml(row.result || "")}</td>
          <td>${escapeHtml(row.message_id || "")}</td>
          <td class="reason">${escapeHtml(row.detail || "")}</td>
        </tr>`).join("");
      $("debug-sync-trace").innerHTML = `
        <table class="debug-table">
          <thead><tr><th>Hora</th><th>Evento</th><th>Resultado</th><th>Msg ID</th><th>Detalle</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
    }
    $("debug-sync-trace").classList.remove("hidden");
  }

  async function runSyncDebug() {
    const runBtn = $("debug-run-sync-btn");
    runBtn.disabled = true;
    $("debug-sync-summary").classList.add("hidden");
    $("debug-sync-trace").classList.add("hidden");
    try {
      const report = await api("/whatsapp/debug/sync", {}, 20000);
      debugSyncReport = report;
      renderSyncDebug(report);
      $("debug-json").value = JSON.stringify(report, null, 2);
      if (report.hints?.length) {
        console.info("Sync hints:", report.hints);
      }
      if (report.errors?.length) {
        console.warn("Sync errors:", report.errors);
      }
    } catch (err) {
      $("debug-json").value = JSON.stringify({ error: err.message }, null, 2);
      alert(err.message);
    } finally {
      runBtn.disabled = false;
    }
  }

  async function runChatsDebug() {
    const runBtn = $("debug-run-btn");
    const copyBtn = $("debug-copy-btn");
    runBtn.disabled = true;
    copyBtn.disabled = true;
    $("debug-loading").classList.remove("hidden");
    $("debug-summary").classList.add("hidden");
    $("debug-sample").classList.add("hidden");
    $("debug-json").value = "";
    try {
      const report = await api("/whatsapp/debug/chats?sample_limit=25", {}, 120000);
      debugReport = report;
      renderDebugSummary(report);
      renderDebugSample(report);
      $("debug-json").value = JSON.stringify(report, null, 2);
      if (report.errors && report.errors.length) {
        alert(`Diagnóstico con advertencias:\n${report.errors.join("\n")}`);
      }
    } catch (err) {
      $("debug-json").value = JSON.stringify({ error: err.message }, null, 2);
      alert(err.message);
    } finally {
      $("debug-loading").classList.add("hidden");
      runBtn.disabled = false;
      copyBtn.disabled = false;
    }
  }

  $("debug-chats").addEventListener("click", () => {
    showDebugModal();
    if (!debugSyncReport) runSyncDebug();
  });
  $("debug-tab-sync").addEventListener("click", () => setDebugTab("sync"));
  $("debug-tab-names").addEventListener("click", () => setDebugTab("names"));
  $("debug-run-sync-btn").addEventListener("click", runSyncDebug);
  $("debug-modal-close").addEventListener("click", hideDebugModal);
  $("debug-modal-backdrop").addEventListener("click", hideDebugModal);
  $("debug-close-btn").addEventListener("click", hideDebugModal);
  $("debug-run-btn").addEventListener("click", runChatsDebug);
  $("debug-copy-btn").addEventListener("click", async () => {
    const text = $("debug-json").value
      || (debugTab === "sync" && debugSyncReport ? JSON.stringify(debugSyncReport, null, 2) : "")
      || (debugReport ? JSON.stringify(debugReport, null, 2) : "");
    if (!text) {
      alert("Ejecuta el diagnóstico primero.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      $("debug-copy-btn").textContent = "¡Copiado!";
      setTimeout(() => { $("debug-copy-btn").textContent = "Copiar reporte"; }, 2000);
    } catch {
      $("debug-json").focus();
      $("debug-json").select();
      alert("Selecciona el JSON y cópialo manualmente (Cmd+C).");
    }
  });

  async function switchChatTab(tab) {
    setChatListTab(tab);
    state.activeId = null;
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
    conversationList.innerHTML = '<li class="conversation-item"><span class="muted">Cargando…</span></li>';
    try {
      setConversations(await fetchConversations());
      renderConversationList();
    } catch (err) {
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${escapeHtml(err.message)}</span></li>`;
    }
  }

  $("tab-chats-interested").addEventListener("click", () => switchChatTab("interested"));
  $("tab-chats-not-interested").addEventListener("click", () => switchChatTab("not_interested"));
  $("tab-chats-all").addEventListener("click", () => switchChatTab("all"));

  $("mode-chats").addEventListener("click", () => switchPanelMode("chats"));
  $("mode-appointments").addEventListener("click", () => switchPanelMode("appointments"));
  $("mode-ai").addEventListener("click", () => switchPanelMode("ai"));
  $("schedule-save-btn")?.addEventListener("click", () => saveAppointmentSchedule());
  $("appointments-prev-day")?.addEventListener("click", () => {
    loadAppointmentsDay(shiftDateISO(state.appointmentsDate, -1));
  });
  $("appointments-next-day")?.addEventListener("click", () => {
    loadAppointmentsDay(shiftDateISO(state.appointmentsDate, 1));
  });
  $("appointments-today-btn")?.addEventListener("click", () => {
    loadAppointmentsDay(formatDateISO(new Date()));
  });
  $("appointments-add-btn")?.addEventListener("click", () => openAppointmentModal());
  $("appointment-form")?.addEventListener("submit", submitAppointmentForm);
  $("appointment-delete-btn")?.addEventListener("click", () => deleteCurrentAppointment());
  $("appointment-open-chat-btn")?.addEventListener("click", () => openAppointmentChat());
  $("appointment-modal-close")?.addEventListener("click", hideAppointmentModal);
  $("appointment-modal-backdrop")?.addEventListener("click", hideAppointmentModal);
  $("staff-add-btn")?.addEventListener("click", () => openStaffModal());
  $("staff-form")?.addEventListener("submit", submitStaffForm);
  $("staff-delete-btn")?.addEventListener("click", () => deleteCurrentStaff());
  $("staff-modal-close")?.addEventListener("click", hideStaffModal);
  $("staff-cancel-btn")?.addEventListener("click", hideStaffModal);
  $("staff-days")?.addEventListener("change", updateStaffDaysSummary);
  $("staff-active")?.addEventListener("change", updateStaffActiveHint);
  document.querySelectorAll('input[name="staff-hours-mode"]').forEach((r) => {
    r.addEventListener("change", () => setStaffHoursMode(r.value));
  });
  $("staff-modal-backdrop")?.addEventListener("click", hideStaffModal);
  $("biz-save-btn").addEventListener("click", () => saveAiSetup());
  $("business-name-cta").addEventListener("click", openBusinessNameSetup);
  $("alert-save-btn").addEventListener("click", () => saveInterestAlert());
  $("alert-test-btn").addEventListener("click", () => testInterestAlert());
  $("alert-add-btn").addEventListener("click", () => addAlertRecipient());
  $("alert-recipients").addEventListener("click", (e) => {
    const btn = e.target.closest(".alert-remove");
    if (btn) removeAlertRecipient(btn);
  });
  $("ai-shortcuts-config-btn")?.addEventListener("click", () => openShortcutsModal());
  $("shortcuts-add-btn").addEventListener("click", () => {
    if (state.shortcutsDraft.length >= 12) return;
    state.shortcutsDraft.push(newShortcutDraft());
    renderShortcutsEditor();
  });
  $("shortcuts-save-btn").addEventListener("click", () => saveQuickShortcuts());
  $("shortcuts-cancel-btn").addEventListener("click", closeShortcutsModal);
  $("shortcuts-modal-close").addEventListener("click", closeShortcutsModal);
  $("shortcuts-modal-backdrop").addEventListener("click", closeShortcutsModal);

  $("chat-search").addEventListener("input", (e) => {
    state.searchQuery = e.target.value.trim();
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(async () => {
      if (state.searchQuery) {
        try {
          state.searchPool = await fetchAllConversationsForSearch();
        } catch (err) {
          console.warn("search fetch failed", err);
          state.searchPool = state.conversations;
        }
      } else {
        state.searchPool = null;
      }
      renderConversationList();
    }, 200);
    renderConversationList();
  });

  const CHAT_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
  const CHAT_IMAGE_MAX_SIDE = 2560;
  const CHAT_IMAGE_SOFT_LIMIT = 1.5 * 1024 * 1024;

  function loadImage(blob) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => resolve({ img, url });
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("No se pudo leer la imagen. Prueba con otra foto."));
      };
      img.src = url;
    });
  }

  // Capturas de pantalla en PNG pesan varios MB: se pasan a JPEG y se achican antes de subir.
  async function prepareChatImage(file) {
    const { img, url } = await loadImage(file);
    const big = Math.max(img.naturalWidth, img.naturalHeight) > CHAT_IMAGE_MAX_SIDE;
    if (CHAT_IMAGE_TYPES.includes(file.type) && file.size <= CHAT_IMAGE_SOFT_LIMIT && !big) {
      return { blob: file, mimetype: file.type, previewUrl: url };
    }
    URL.revokeObjectURL(url);
    const scale = Math.min(1, CHAT_IMAGE_MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    if (!blob) throw new Error("No se pudo preparar la foto.");
    return { blob, mimetype: "image/jpeg", previewUrl: URL.createObjectURL(blob) };
  }

  function clearPendingImage() {
    if (pendingImage) URL.revokeObjectURL(pendingImage.previewUrl);
    pendingImage = null;
    $("composer-attachment").classList.add("hidden");
    $("composer-attachment-img").removeAttribute("src");
    $("attach-image-input").value = "";
    messageInput.placeholder = "Escribe un mensaje…";
  }

  async function setPendingImage(file) {
    if (!file || !state.activeId) return;
    if (!state.canWrite) {
      alert("Tu usuario no puede enviar mensajes. Pide acceso de agente o dueño.");
      return;
    }
    if (sendForm.classList.contains("disabled")) {
      alert("Conecta WhatsApp para enviar fotos.");
      return;
    }
    if (!file.type.startsWith("image/")) {
      alert("Solo puedes enviar fotos (JPG, PNG o WEBP).");
      return;
    }
    try {
      const prepared = await prepareChatImage(file);
      clearPendingImage();
      pendingImage = { ...prepared, conversationId: state.activeId };
      $("composer-attachment-img").src = prepared.previewUrl;
      $("composer-attachment").classList.remove("hidden");
      messageInput.placeholder = "Agrega un comentario (opcional)…";
      messageInput.focus();
    } catch (err) {
      alert(err.message);
    }
  }

  // Vista Previa de macOS copia en TIFF junto a un PNG: se prefiere lo que el navegador sabe dibujar.
  const CLIPBOARD_IMAGE_PREFERENCE = ["image/png", "image/jpeg", "image/webp", "image/gif"];

  function imageFromClipboard(e) {
    const data = e.clipboardData;
    if (!data) return null;
    const files = Array.from(data.items || [])
      .filter((i) => i.kind === "file" && i.type.startsWith("image/"))
      .map((i) => i.getAsFile())
      .concat(Array.from(data.files || []).filter((f) => f.type.startsWith("image/")))
      .filter(Boolean);
    const rank = (f) => {
      const idx = CLIPBOARD_IMAGE_PREFERENCE.indexOf(f.type);
      return idx === -1 ? CLIPBOARD_IMAGE_PREFERENCE.length : idx;
    };
    return files.sort((a, b) => rank(a) - rank(b))[0] || null;
  }

  const isEditable = (el) => el instanceof HTMLElement && el.matches("input, textarea, select, [contenteditable]");

  // Safari solo dispara «paste» dentro de un campo editable: con Cmd/Ctrl+V fuera de la caja,
  // se enfoca la caja del mensaje antes de que llegue el pegado.
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== "v" || e.altKey) return;
    if (activeChat.classList.contains("hidden") || isEditable(document.activeElement)) return;
    if (!state.activeId || !state.canWrite || sendForm.classList.contains("disabled")) return;
    messageInput.focus();
  });

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  async function sendPendingImage(caption) {
    const image = pendingImage;
    const fd = new FormData();
    fd.append("file", image.blob, `foto.${image.mimetype.split("/")[1]}`);
    fd.append("caption", caption);
    const msg = await api(`/conversations/${image.conversationId}/messages/image`, { method: "POST", body: fd }, 30000);
    try {
      mediaCache.set(msg.id, { ok: true, base64: await blobToBase64(image.blob), media_type: "image", mimetype: image.mimetype });
    } catch {
      /* se descargará de WhatsApp como cualquier otra foto */
    }
    clearPendingImage();
    return msg;
  }

  document.addEventListener("paste", (e) => {
    if (activeChat.classList.contains("hidden")) return;
    if (isEditable(e.target) && e.target !== messageInput) return;
    const file = imageFromClipboard(e);
    if (!file) return;
    e.preventDefault();
    setPendingImage(file);
  });

  $("attach-image-btn").addEventListener("click", () => $("attach-image-input").click());
  $("attach-image-input").addEventListener("change", (e) => setPendingImage(e.target.files?.[0]));
  $("composer-attachment-remove").addEventListener("click", () => {
    clearPendingImage();
    messageInput.focus();
  });

  let dragDepth = 0;
  const hasDraggedFile = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  activeChat.addEventListener("dragenter", (e) => {
    if (!hasDraggedFile(e)) return;
    e.preventDefault();
    dragDepth += 1;
    sendForm.classList.add("dragging");
  });
  activeChat.addEventListener("dragover", (e) => {
    if (hasDraggedFile(e)) e.preventDefault();
  });
  activeChat.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) sendForm.classList.remove("dragging");
  });
  activeChat.addEventListener("drop", (e) => {
    if (!hasDraggedFile(e)) return;
    e.preventDefault();
    dragDepth = 0;
    sendForm.classList.remove("dragging");
    setPendingImage(e.dataTransfer.files?.[0]);
  });

  sendForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.activeId || !state.canWrite) return;
    const text = messageInput.value.trim();
    if (pendingImage) {
      const btn = sendForm.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        const msg = await sendPendingImage(text);
        messageInput.value = "";
        if (msg.conversation_id === state.activeId && !state.messages.some((m) => m.id === msg.id)) {
          state.messages.push(msg);
          state._messagesSig = state.messages.map((m) => m.id || `${m.body}|${m.created_at}`).join("\n");
          renderMessages();
        }
      } catch (err) {
        alert(err.message);
      } finally {
        btn.disabled = false;
      }
      return;
    }
    if (!text) return;

    const btn = sendForm.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const msg = await api(`/conversations/${state.activeId}/messages`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      messageInput.value = "";
      const exists = state.messages.some((m) => m.id === msg.id);
      if (!exists) {
        state.messages.push(msg);
        state._messagesSig = state.messages
          .map((m) => m.id || `${m.body}|${m.created_at}`)
          .join("\n");
        renderMessages();
      }
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("toggle-mode-manual").addEventListener("change", async (e) => {
    if (!state.activeId || !state.canWrite || state.wa.status !== "connected") {
      e.target.checked = !e.target.checked;
      return;
    }
    const mode = e.target.checked ? "manual" : "auto";
    try {
      const conv = await api(`/conversations/${state.activeId}/mode`, {
        method: "PATCH",
        body: JSON.stringify({ mode }),
      });
      upsertConversation(conv);
      if (conv.id === state.activeId) syncChatToggles(conv);
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
    }
  });

  $("mark-interested-btn").addEventListener("click", () => setConversationInterest("interested"));
  $("mark-not-interested-btn").addEventListener("click", () => setConversationInterest("not_interested"));

  $("toggle-ai-chat").addEventListener("change", async (e) => {
    const enabled = !!e.target.checked;
    await patchConversationAi(enabled);
  });

  $("ai-retry-btn").addEventListener("click", async () => {
    if (!state.activeId || !state.canWrite) return;
    const btn = $("ai-retry-btn");
    btn.disabled = true;
    try {
      await api(`/conversations/${state.activeId}/ai/trigger`, { method: "POST" });
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("toggle-ai-global").addEventListener("change", async (e) => {
    if (!state.canManageGlobal) return;
    try {
      const tenant = await api("/tenants/me", {
        method: "PATCH",
        body: JSON.stringify({ ai_global_enabled: e.target.checked }),
      });
      state.tenant = tenant;
      renderAiAlerts();
      if (state.activeId) {
        const conv = state.conversations.find((c) => c.id === state.activeId);
        if (conv) syncChatToggles(conv);
      }
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
    }
  });

  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendForm.requestSubmit();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.user) {
      refreshWaStatus();
    }
  });

  try {
    localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    /* ignore */
  }

  if (isDevToolsEnabled()) {
    document.querySelectorAll(".dev-only").forEach((el) => el.classList.remove("hidden"));
  }

  showPanel();
  syncThemeToggles(getTheme());
  bindThemeToggle($("theme-toggle"));
  bindThemeToggle($("theme-toggle-login"));

  // Devuelve { user, status }. status 0 = no se pudo contactar el backend.
  async function probeSession(timeoutMs = 5000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${API}/auth/me`, {
        credentials: "include",
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!res.ok) return { user: null, status: res.status };
      const text = await res.text();
      return { user: text ? JSON.parse(text) : null, status: 200 };
    } catch {
      clearTimeout(timer);
      return { user: null, status: 0 };
    }
  }

  async function tryRestoreSession() {
    let result = await probeSession(5000);
    // Reintenta si el backend no respondió (reload, red lenta) — sin botar al usuario.
    for (let i = 0; i < 2 && result.status === 0; i++) {
      setBackendOfflineBanner(true);
      await new Promise((resolve) => setTimeout(resolve, 600));
      result = await probeSession(5000);
    }

    if (result.status === 401) {
      redirectToSiteLogin();
      return;
    }
    if (!result.user) {
      // Backend caído: no tiene sentido mandar al login. Mostramos aviso y reintentamos.
      setBackendOfflineBanner(true);
      setTimeout(tryRestoreSession, 3000);
      return;
    }

    state.user = result.user;
    applyUserFromMe(result.user);
    setBackendOfflineBanner(false);
    renderWaUi();
    renderOnboarding();
    connectWs();
    try {
      await loadInitial();
    } catch (err) {
      console.error(err);
      renderWaUi();
    }
  }

  tryRestoreSession().catch((err) => console.error(err));

  window.addEventListener("pageshow", (event) => {
    if (event.persisted && !state.user) {
      tryRestoreSession().catch((err) => console.error(err));
    }
  });
})();
