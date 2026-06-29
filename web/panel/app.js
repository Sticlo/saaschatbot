(() => {
  const API = "/api/v1";
  const LEGACY_STORAGE_KEY = "saaschatbot_token";
  const THEME_STORAGE_KEY = "omitel_panel_theme";

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
    chatListTab: "all",
    syncInProgress: false,
    autoSyncRequested: false,
    syncWatchdog: null,
    searchQuery: "",
    searchPool: null,
    searchTimer: null,
    panelMode: "chats",
    aiProfile: null,
    clients: { plan: null, leads: [], business: "", city: "" },
    quickShortcuts: [],
    shortcutsDraft: [],
    aiStatus: null,
  };

  // Caché de resultados de media: msgId → {ok: bool, data?} para no repetir fetches
  const mediaCache = new Map();

  const $ = (id) => document.getElementById(id);

  const BACKEND_OFFLINE_MSG =
    "El servidor no responde. En una terminal ejecuta: ./scripts/dev.sh — luego recarga esta página (Cmd+R).";

  async function pingBackend(timeoutMs = 4000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch("/health", { signal: ctrl.signal, credentials: "include" });
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
  const loginStepPassword = $("login-step-password");
  const loginStepRegister = $("login-step-register");
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
    if (state.token) h.Authorization = `Bearer ${state.token}`;
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
          ? { ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...(options.headers || {}) }
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
        logout();
        throw new Error("Sesión expirada");
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

  function showLogin() {
    loginView.classList.remove("hidden");
    panelView.classList.add("hidden");
  }

  function showPanel() {
    loginView.classList.add("hidden");
    panelView.classList.remove("hidden");
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
    state.canEnqueueOutbound = false;
    state.canImportLeads = false;
    state.panelMode = "chats";
    state.aiProfile = null;
    state.clients = { leads: [], limits: null, search: "" };
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
    $("clients-panel").classList.add("hidden");
    $("sidebar-chats").classList.remove("hidden");
    $("mode-chats").classList.add("active");
    $("mode-clients").classList.remove("active");
    $("mode-ai").classList.remove("active");
    $("chat-area").classList.remove("ai-setup-mode", "clients-mode");
    $("business-name").textContent = "—";
    $("user-label").textContent = "";
  }

  function resetLoginForm() {
    loginEmail = "";
    showLoginStep("email");
    loginForm?.reset();
    [loginError, $("login-error-password"), $("login-error-register")].forEach((el) => {
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
    resetLoginForm();
    showLogin();
    loadAuthProviders();
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
        triggerAutoSync();
      } else if (prevStatus !== "connected") {
        state.autoSyncRequested = false;
        triggerAutoSync();
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
      clearConversations();
    }
  }

  function renderWaUi() {
    const connected = state.wa.status === "connected";
    const needsConnect = !connected && state.canConnectWa;
    const canManageWa = state.canConnectWa;

    $("wa-connect-btn").classList.toggle("hidden", !needsConnect);
    $("wa-disconnect-btn").classList.toggle("hidden", !connected || !canManageWa);
    $("wa-reset-chats-btn").classList.toggle("hidden", !connected || !canManageWa);
    $("wa-setup").classList.toggle("hidden", !needsConnect);
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
    if (!state.canConnectWa) return;
    showQrModal();
    $("qr-loading").textContent = "Generando QR… puede tardar hasta 30 segundos";
    $("wa-setup-error").classList.add("hidden");
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 4000);
      const health = await fetch("/health", { credentials: "include", signal: ctrl.signal });
      clearTimeout(t);
      if (!health.ok) throw new Error("Backend no responde");
    } catch (err) {
      const msg =
        err.message ||
        "No pudimos contactar el servidor. Inicia el backend con ./scripts/dev.sh";
      showQrError(msg);
      $("wa-setup-error").textContent = msg;
      $("wa-setup-error").classList.remove("hidden");
      return;
    }
    try {
      const wa = await api("/whatsapp/connect", { method: "POST" }, 90000);
      applyWaSession(wa);
      if (wa.qr_base64) showQrImage(wa.qr_base64);
      else showQrError("No se recibió QR. Intenta de nuevo en unos segundos.");
    } catch (err) {
      const msg = err.message || "Error al conectar";
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
    if (c.last_message_preview) return c.last_message_preview;
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

  function setConversations(rows) {
    state.conversations = sortConversations(rows);
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

  function renderMessages() {
    messagesEl.innerHTML = "";
    for (const m of dedupeMessages(state.messages)) {
      const timeHtml = `<span class="time">${formatTime(m.created_at)} · ${m.source}</span>`;
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
    const isClients = mode === "clients";
    const isAi = mode === "ai";

    $("mode-chats").classList.toggle("active", isChats);
    $("mode-clients").classList.toggle("active", isClients);
    $("mode-ai").classList.toggle("active", isAi);
    $("sidebar-chats").classList.toggle("hidden", !isChats);
    $("chat-area").classList.toggle("ai-setup-mode", isAi);
    $("chat-area").classList.toggle("clients-mode", isClients);
    $("ai-setup-panel").classList.toggle("hidden", !isAi);
    $("clients-panel").classList.toggle("hidden", !isClients);

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
      if (isClients) loadClientsPanel();
    }
    renderQuickShortcuts();
  }

  const MAPS_MOCK_PLANS = {
    lavander: {
      summary: "Tu lavandería encaja con negocios que generan mucha ropa sucia y necesitan un proveedor constante.",
      searches: [
        { label: "Hoteles", query: "hoteles", why: "Camas, toallas y sábanas todos los días." },
        { label: "Moteles", query: "moteles", why: "Alto volumen de ropa de cama y toallas." },
        { label: "Hostels", query: "hostels", why: "Rotación de huéspedes y lavandería frecuente." },
        { label: "Restaurantes", query: "restaurantes", why: "Manteles, delantales y paños de cocina." },
        { label: "Gimnasios", query: "gimnasios", why: "Toallas y uniformes de entrenadores." },
      ],
    },
    default: {
      summary: "Buscamos empresas locales que suelen comprar servicios como el tuyo y tienen teléfono visible en Google Maps.",
      searches: [
        { label: "Comercios del sector", query: "empresas", why: "Negocios relacionados con tu rubro." },
        { label: "Pymes locales", query: "pymes", why: "Empresas pequeñas con decisión rápida." },
        { label: "Oficinas", query: "oficinas", why: "Posibles clientes corporativos." },
        { label: "Restaurantes", query: "restaurantes", why: "Alto tráfico y necesidad de proveedores." },
      ],
    },
  };

  const MAPS_MOCK_NAMES = {
    hoteles: ["Hotel Plaza Real", "Hotel Andino", "Hotel Central Park", "Hotel Montaña Verde", "Hotel Río Grande"],
    moteles: ["Motel Aurora", "Motel Las Palmas", "Motel Express 24", "Motel El Descanso"],
    hostels: ["Hostel Nomada", "Backpackers House", "Hostel Centro", "The Traveler's Inn"],
    restaurantes: ["Restaurante La Fogata", "Asados del Norte", "Café & Brunch", "Sabor Criollo", "Mariscos del Puerto"],
    gimnasios: ["Gym PowerFit", "CrossBox Elite", "Fitness Total", "Iron Gym"],
    empresas: ["Comercializadora Andina", "Servicios Integrales SAS", "Grupo Empresarial Norte"],
    pymes: ["Distribuidora El Éxito", "Importaciones La 80", "Soluciones Locales SAS"],
    oficinas: ["Torre Empresarial 45", "Centro de Negocios Nova", "Oficinas Parque Central"],
  };

  function inferMapsMockPlan(businessText) {
    const lower = normalizeForSearch(businessText);
    if (lower.includes("lavander") || lower.includes("lavanderia") || lower.includes("tintorer")) {
      return MAPS_MOCK_PLANS.lavander;
    }
    return MAPS_MOCK_PLANS.default;
  }

  function buildMapsMockLeads(plan, city, limit = 12) {
    const cityLabel = city || "tu ciudad";
    const leads = [];
    const searches = plan.searches || [];
    let i = 0;
    while (leads.length < limit && i < limit * 3) {
      const search = searches[i % searches.length];
      const pool = MAPS_MOCK_NAMES[search.query] || MAPS_MOCK_NAMES.empresas;
      const name = pool[Math.floor(i / searches.length) % pool.length];
      const phoneBase = 3001000000 + (i * 1737) % 8999999;
      leads.push({
        name: `${name}${i >= searches.length ? ` ${Math.floor(i / searches.length) + 1}` : ""}`.trim(),
        phone: `+57${phoneBase}`,
        address: `Cra ${10 + (i % 40)} # ${20 + (i % 50)}-${30 + (i % 60)}, ${cityLabel}`,
        category: search.label,
      });
      i += 1;
    }
    return leads.slice(0, limit);
  }

  function renderMapsPlan(plan, business, city) {
    const card = $("maps-plan-card");
    const list = $("maps-plan-list");
    if (!card || !list) return;
    $("maps-plan-title").textContent = `Para «${business.slice(0, 60)}${business.length > 60 ? "…" : ""}» en ${city || "tu zona"}`;
    $("maps-plan-summary").textContent = plan.summary;
    list.innerHTML = (plan.searches || []).map((s) => `
      <li class="maps-plan-item">
        <strong>${escapeHtml(s.label)}</strong>
        <span class="muted small">${escapeHtml(s.why)}</span>
      </li>
    `).join("");
    card.classList.remove("hidden");
  }

  function renderMapsResults(leads, city) {
    const card = $("maps-results-card");
    const body = $("maps-results-body");
    if (!card || !body) return;
    $("maps-results-title").textContent = `${leads.length} posibles clientes`;
    $("maps-results-meta").textContent = `En ${city || "tu zona"} · con teléfono para contactar en frío`;
    body.innerHTML = leads.map((l) => `
      <tr>
        <td>${escapeHtml(l.name)}</td>
        <td>${escapeHtml(l.phone)}</td>
        <td>${escapeHtml(l.address)}</td>
        <td><span class="maps-type-pill">${escapeHtml(l.category)}</span></td>
      </tr>
    `).join("");
    card.classList.remove("hidden");
  }

  function downloadMapsMockExcel(leads, business, city) {
    const headers = ["Nombre", "Teléfono", "Dirección", "Tipo", "Negocio origen", "Ciudad"];
    const rows = leads.map((l) => [
      l.name,
      l.phone,
      l.address,
      l.category,
      business,
      city,
    ]);
    const escapeCsv = (v) => {
      const s = String(v ?? "");
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const csv = [headers, ...rows].map((row) => row.map(escapeCsv).join(",")).join("\n");
    const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const slug = normalizeForSearch(city || "clientes").replace(/\s+/g, "-").slice(0, 24) || "clientes";
    a.href = url;
    a.download = `clientes-${slug}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function resetMapsProspectPanel() {
    state.clients.plan = null;
    state.clients.leads = [];
    $("maps-plan-card")?.classList.add("hidden");
    $("maps-results-card")?.classList.add("hidden");
    $("maps-reset-btn")?.classList.add("hidden");
    $("maps-search-btn")?.classList.remove("hidden");
    $("maps-search-status").textContent = "";
    $("maps-business-input")?.removeAttribute("disabled");
    $("maps-city-input")?.removeAttribute("disabled");
  }

  function loadClientsPanel() {
    resetMapsProspectPanel();
    api("/outbound/business-profile")
      .then((profile) => {
        state.clients.business = profile?.maps_prospect_business || profile?.industry || "";
        state.clients.city = profile?.maps_prospect_city || "";
        if ($("maps-business-input")) $("maps-business-input").value = state.clients.business;
        if ($("maps-city-input")) $("maps-city-input").value = state.clients.city;
      })
      .catch(() => {});
  }

  async function persistMapsProspectFields(business, city) {
    try {
      await api("/outbound/business-profile", {
        method: "PUT",
        body: JSON.stringify({
          maps_prospect_business: business,
          maps_prospect_city: city,
        }),
      });
    } catch {
      /* mockup — no bloquear si falla */
    }
  }

  async function runMapsProspectMock() {
    const businessInput = $("maps-business-input");
    const cityInput = $("maps-city-input");
    const status = $("maps-search-status");
    const btn = $("maps-search-btn");
    const business = businessInput?.value.trim() || "";
    const city = cityInput?.value.trim() || "";

    if (business.length < 4) {
      status.textContent = "Cuéntanos un poco más sobre tu negocio (mínimo unas palabras).";
      return;
    }
    if (city.length < 2) {
      status.textContent = "Indica la ciudad o zona donde quieres buscar.";
      return;
    }

    state.clients.business = business;
    state.clients.city = city;
    persistMapsProspectFields(business, city);
    btn.disabled = true;
    businessInput.disabled = true;
    cityInput.disabled = true;
    $("maps-plan-card")?.classList.add("hidden");
    $("maps-results-card")?.classList.add("hidden");
    status.textContent = "La IA está analizando tu negocio…";

    await new Promise((r) => setTimeout(r, 900));
    const plan = inferMapsMockPlan(business);
    state.clients.plan = plan;
    renderMapsPlan(plan, business, city);

    status.textContent = "Buscando en Google Maps (vista previa)…";
    await new Promise((r) => setTimeout(r, 1200));

    const leads = buildMapsMockLeads(plan, city, 12);
    state.clients.leads = leads;
    renderMapsResults(leads, city);

    status.textContent = `✓ Vista previa lista — en producción traeremos hasta 100 contactos reales por día.`;
    btn.classList.add("hidden");
    $("maps-reset-btn")?.classList.remove("hidden");
    btn.disabled = false;
  }

  const BIZ_FIELD_IDS = [
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
      industry: $("biz-industry")?.value.trim() || "",
      products_services: $("biz-products")?.value.trim() || "",
      target_customer: $("biz-target")?.value.trim() || "",
      price_range: $("biz-prices")?.value.trim() || "",
      location_hours: $("biz-hours")?.value.trim() || "",
      tone: $("biz-tone")?.value.trim() || "",
      restrictions: $("biz-restrictions")?.value.trim() || "",
    };
  }

  function fillBizForm(profile) {
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
    await loadQuickShortcuts();
    renderAiShortcutsSummary();
  }

  async function saveAiSetup() {
    if (!state.canManageGlobal) return;
    const btn = $("biz-save-btn");
    btn.disabled = true;
    setBizSaveStatus("Guardando…");
    try {
      const payload = readBizForm();
      const profile = await api("/outbound/business-profile", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      state.aiProfile = profile;
      fillBizForm(profile);
      setBizSaveStatus("✓ Guardado — tu IA ya conoce tu negocio");
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
        const kind = s.type === "image" ? "Foto" : "Texto";
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
      const icon = s.type === "image" ? "🖼 " : "";
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
      const isImage = s.type === "image";
      const thumb = s.image_url
        ? `<img src="${escapeHtml(s.image_url)}" class="shortcut-thumb" alt="" />`
        : "";
      return `
        <div class="shortcut-editor-row" data-idx="${idx}">
          <input type="text" class="shortcut-label" maxlength="20" placeholder="Nombre — ej: Menú" value="${escapeHtml(s.label || "")}" />
          <div class="shortcut-type-row">
            <label><input type="radio" name="stype-${idx}" value="text" ${!isImage ? "checked" : ""} /> Texto</label>
            <label><input type="radio" name="stype-${idx}" value="image" ${isImage ? "checked" : ""} /> Foto</label>
          </div>
          <div class="shortcut-text-wrap ${isImage ? "hidden" : ""}">
            <textarea class="shortcut-text" rows="2" maxlength="500" placeholder="Ej: Hola! Aquí tienes nuestros precios…">${escapeHtml(s.text || "")}</textarea>
          </div>
          <div class="shortcut-image-wrap ${isImage ? "" : "hidden"}">
            <label class="btn ghost small wa-upload-btn">
              📷 Subir foto
              <input type="file" class="shortcut-image-file" accept="image/jpeg,image/png,image/webp,image/gif" hidden />
            </label>
            ${thumb}
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
      row.querySelector(".shortcut-image-file")?.addEventListener("change", async (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        try {
          const fd = new FormData();
          fd.append("file", file);
          const res = await api("/outbound/assets", { method: "POST", body: fd }, 30000);
          state.shortcutsDraft[idx].image_path = res.image_path;
          state.shortcutsDraft[idx].image_url = res.image_url;
          renderShortcutsEditor();
        } catch (err) {
          alert(err.message);
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
      .map((s) => ({
        id: s.id,
        label: s.label.trim(),
        type: s.type,
        text: s.type === "text" ? (s.text || "").trim() : null,
        image_path: s.type === "image" ? s.image_path : null,
      }))
      .filter((s) => (s.type === "text" ? s.text : s.image_path));

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
    if (!conv?.ai_active) return "";
    if (state.tenant && !state.tenant.ai_global_enabled) {
      return "IA global apagada — actívala arriba a la derecha";
    }
    if (conv.mode === "manual") return "Modo manual activo — la IA no interviene";
    if (state.aiStatus && !state.aiStatus.provider_ok && state.aiStatus.provider_error) {
      return state.aiStatus.provider_error;
    }
    return "";
  }

  function renderAiAlerts() {
    const banner = $("ai-alert-banner");
    const parts = [];
    if (state.tenant && !state.tenant.ai_global_enabled) {
      parts.push("IA global apagada — los chats con IA activa no responderán solos.");
    }
    if (state.aiStatus && state.aiStatus.configured && !state.aiStatus.provider_ok && state.aiStatus.provider_error) {
      parts.push(state.aiStatus.provider_error);
    }
    if (
      state.aiStatus?.ai_mode === "classify_only"
      && state.aiStatus?.daily_classifications_unlimited
      && state.aiStatus?.provider_ok
    ) {
      /* Sin banner — clasificación ilimitada en plan pagado */
    } else if (state.aiStatus?.daily_replies_unlimited && state.aiStatus?.provider_ok) {
      /* Premium — respuestas IA ilimitadas */
    } else {
      const aiLeft = state.aiStatus?.daily_classifications_remaining ?? state.aiStatus?.daily_replies_remaining;
      if (typeof aiLeft === "number" && aiLeft <= 50) {
        const label = state.aiStatus?.ai_mode === "classify_only" ? "clasificaciones" : "respuestas IA";
        parts.push(`Te quedan ${aiLeft} ${label} hoy — activa tu plan para IA ilimitada.`);
      }
    }
    if (!parts.length) {
      banner.classList.add("hidden");
      banner.textContent = "";
      return;
    }
    banner.textContent = parts.join(" · ");
    banner.classList.toggle("error", !!(state.aiStatus && !state.aiStatus.provider_ok));
    banner.classList.remove("hidden");
  }

  function syncChatToggles(conv) {
    $("toggle-mode-manual").checked = conv.mode === "manual";
    $("toggle-ai-chat").checked = !!conv.ai_active;
    $("toggle-mode-manual").disabled = !state.canWrite;
    $("toggle-ai-chat").disabled = !state.canWrite;
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
      const [me, tenant, wa, aiStatus] = await Promise.all([
        api("/auth/me"),
        api("/tenants/me"),
        api("/whatsapp/status", {}, 8000),
        api("/ai/status", {}, 15000).catch(() => null),
      ]);
      await loadQuickShortcuts();

      state.user = me;
      state.tenant = tenant;
      state.aiStatus = aiStatus;
      state.canWrite = roleAtLeast(me.role, "agent");
      state.canManageGlobal = me.role === "owner";
      state.canConnectWa = me.role === "owner";
      state.canEnqueueOutbound = me.role === "owner";
      state.canImportLeads = roleAtLeast(me.role, "agent");

      $("business-name").textContent = tenant.business_name;
      $("user-label").textContent = `${me.full_name} (${me.role})`;
      $("toggle-ai-global").checked = tenant.ai_global_enabled;
      $("toggle-ai-global").disabled = !state.canManageGlobal;

      applyWaSession(wa);
      if (state.wa.status === "connected" && !state.syncInProgress) {
        setConversations(await api("/conversations?archived=false", {}, 30000));
      } else {
        state.conversations = [];
      }
      sendForm.classList.toggle("disabled", !state.canWrite || state.wa.status !== "connected");
      messageInput.disabled = !state.canWrite || state.wa.status !== "connected";

      renderConversationList();
      renderWaUi();
      renderAiAlerts();
      connectWs();
      startWaHeartbeat();
      if (state.wa.status === "connected") startLivePoll();
    } catch (err) {
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${escapeHtml(err.message)}</span></li>`;
      throw err;
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
        if (state.wa.status === "connected") {
          triggerAutoSync();
        }
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
            }
          }
        }
        break;
      case "conversation.updated":
        if (event.conversation) {
          upsertConversation(event.conversation);
        }
        break;
      case "whatsapp.status":
        applyWaSession({
          status: event.status,
          qr_base64: event.qr_base64,
          phone_number: event.phone_number,
        });
        if (event.status !== "connected") {
          clearConversations();
        }
        break;
      case "tenant.settings":
        if (state.tenant) {
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
      case "outbound.queued":
      case "outbound.sent":
        if (state.panelMode === "clients") loadClientsPanel();
        if (event.type === "outbound.sent" && event.conversation_id) {
          fetchConversations()
            .then((rows) => {
              setConversations(rows);
              renderConversationList();
            })
            .catch(() => {});
        }
        break;
      case "ai.error":
        if (event.error) {
          state.aiStatus = {
            ...(state.aiStatus || {}),
            configured: true,
            provider_ok: false,
            provider_error: String(event.error).includes("402")
              ? "Sin saldo en DeepSeek — recarga en platform.deepseek.com"
              : String(event.error).includes("403")
                ? "DeepSeek rechazó la conexión (403) — revisa DEEPSEEK_API_KEY en .env"
                : String(event.error).slice(0, 200),
          };
          renderAiAlerts();
          if (state.activeId) {
            const conv = state.conversations.find((c) => c.id === state.activeId);
            if (conv) syncChatToggles(conv);
          }
        }
        break;
      default:
        break;
    }
  }

  function showLoginStep(step) {
    loginStepEmail?.classList.toggle("hidden", step !== "email");
    loginStepPassword?.classList.toggle("hidden", step !== "password");
    loginStepRegister?.classList.toggle("hidden", step !== "register");
    [loginError, $("login-error-password"), $("login-error-register")].forEach((el) => {
      if (el) {
        el.textContent = "";
        el.classList.add("hidden");
      }
    });
  }

  function setLoginError(el, message) {
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
  }

  async function lookupLoginEmail(email) {
    const res = await fetch(`${API}/auth/lookup-email`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "No pudimos verificar el correo");
    return data;
  }

  async function completeAuth(data) {
    state.token = data.access_token || null;
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
      const data = await lookupLoginEmail(loginEmail);
      if (data.exists) {
        $("login-email-display").textContent = loginEmail;
        showLoginStep("password");
        $("login-password")?.focus();
      } else {
        $("register-email-display").textContent = loginEmail;
        showLoginStep("register");
        $("register-business")?.focus();
      }
    } catch (err) {
      setLoginError(loginError, err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("login-back-btn")?.addEventListener("click", () => {
    $("login-password").value = "";
    showLoginStep("email");
  });

  $("register-back-btn")?.addEventListener("click", () => {
    $("register-business").value = "";
    $("register-owner").value = "";
    $("register-password").value = "";
    showLoginStep("email");
  });

  $("register-submit-btn")?.addEventListener("click", async () => {
    const business_name = ($("register-business")?.value || "").trim();
    const owner_name = ($("register-owner")?.value || "").trim();
    const password = $("register-password")?.value || "";
    const errEl = $("login-error-register");
    if (!business_name || !owner_name || password.length < 8) {
      setLoginError(errEl, "Completa todos los campos (contraseña mín. 8 caracteres)");
      return;
    }
    const btn = $("register-submit-btn");
    btn.disabled = true;
    try {
      const res = await fetch(`${API}/auth/register`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ business_name, owner_name, email: loginEmail, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "No pudimos crear la cuenta");
      await completeAuth(data);
    } catch (err) {
      setLoginError(errEl, err.message);
    } finally {
      btn.disabled = false;
    }
  });

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!loginStepEmail?.classList.contains("hidden")) {
      $("login-continue-btn")?.click();
      return;
    }
    if (loginStepPassword?.classList.contains("hidden")) return;

    const password = $("login-password")?.value || "";
    const errEl = $("login-error-password");
    try {
      const res = await fetch(`${API}/auth/login`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: loginEmail, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Login fallido");
      await completeAuth(data);
    } catch (err) {
      setLoginError(errEl, err.message);
    }
  });

  $("logout-btn").addEventListener("click", logout);
  $("wa-connect-btn").addEventListener("click", connectWhatsApp);
  $("wa-setup-btn").addEventListener("click", connectWhatsApp);
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
  $("mode-clients").addEventListener("click", () => switchPanelMode("clients"));
  $("mode-ai").addEventListener("click", () => switchPanelMode("ai"));
  $("biz-save-btn").addEventListener("click", () => saveAiSetup());
  $("ai-shortcuts-config-btn")?.addEventListener("click", () => openShortcutsModal());
  $("maps-search-btn").addEventListener("click", () => runMapsProspectMock());
  $("maps-reset-btn").addEventListener("click", () => resetMapsProspectPanel());
  $("maps-download-btn").addEventListener("click", () => {
    if (!state.clients.leads?.length) return;
    downloadMapsMockExcel(state.clients.leads, state.clients.business, state.clients.city);
  });

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

  sendForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.activeId || !state.canWrite) return;
    const text = messageInput.value.trim();
    if (!text) return;

    const btn = sendForm.querySelector("button");
    btn.disabled = true;
    try {
      await api("/whatsapp/status", {}, 8000).catch(() => null);
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
    if (!state.activeId || !state.canWrite) return;
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
    if (!state.activeId || !state.canWrite) return;
    try {
      const conv = await api(`/conversations/${state.activeId}/ai`, {
        method: "PATCH",
        body: JSON.stringify({ ai_active: e.target.checked }),
      });
      upsertConversation(conv);
      if (conv.id === state.activeId) syncChatToggles(conv);
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
    }
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

  syncThemeToggles(getTheme());
  bindThemeToggle($("theme-toggle"));
  bindThemeToggle($("theme-toggle-login"));
  loadAuthProviders();

  async function tryRestoreSession() {
    if (!(await pingBackend(4000))) {
      setBackendOfflineBanner(true);
      showLogin();
      return;
    }
    setBackendOfflineBanner(false);
    try {
      const me = await api("/auth/me", {}, 4000);
      state.user = me;
      showPanel();
      await loadInitial();
    } catch {
      showLogin();
    }
  }

  tryRestoreSession();
})();
