(() => {
  const API = "/api/v1";
  const STORAGE_KEY = "saaschatbot_token";

  const state = {
    token: localStorage.getItem(STORAGE_KEY),
    user: null,
    tenant: null,
    conversations: [],
    activeId: null,
    messages: [],
    ws: null,
    canWrite: false,
    canManageGlobal: false,
    canConnectWa: false,
    wa: { status: "disconnected", qr_base64: null, phone_number: null },
    chatListTab: "active",
    syncInProgress: false,
    autoSyncRequested: false,
    syncWatchdog: null,
    searchQuery: "",
    searchPool: null,
    searchTimer: null,
  };

  // Caché de resultados de media: msgId → {ok: bool, data?} para no repetir fetches
  const mediaCache = new Map();

  const $ = (id) => document.getElementById(id);

  const loginView = $("login-view");
  const panelView = $("panel-view");
  const loginForm = $("login-form");
  const loginError = $("login-error");
  const conversationList = $("conversation-list");
  const messagesEl = $("messages");
  const emptyChat = $("empty-chat");
  const activeChat = $("active-chat");
  const sendForm = $("send-form");
  const messageInput = $("message-input");
  const wsStatus = $("ws-status");
  const waStatus = $("wa-status");

  function headers(json = true) {
    const h = { Authorization: `Bearer ${state.token}` };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  async function api(path, options = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${API}${path}`, {
        ...options,
        signal: controller.signal,
        headers: { ...headers(), ...(options.headers || {}) },
      });
      if (res.status === 401) {
        logout();
        throw new Error("Sesión expirada");
      }
      const text = await res.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        data = text;
      }
      if (!res.ok) {
        const msg = data?.detail || (typeof data === "string" ? data : "Error");
        throw new Error(typeof msg === "object" ? JSON.stringify(msg) : msg);
      }
      return data;
    } catch (err) {
      if (err.name === "AbortError") throw new Error("Tiempo de espera agotado");
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
    state.user = null;
    state.tenant = null;
    state.conversations = [];
    state.activeId = null;
    state.messages = [];
    state.canWrite = false;
    state.canManageGlobal = false;
    state.canConnectWa = false;
    state.wa = { status: "disconnected", qr_base64: null, phone_number: null };
    setWsBadge(false);
    updateWaBadge("disconnected");
    hideQrModal();
    conversationList.innerHTML = "";
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
    $("business-name").textContent = "—";
    $("user-label").textContent = "";
  }

  function logout() {
    localStorage.removeItem(STORAGE_KEY);
    state.token = null;
    disconnectWs();
    resetPanelState();
    showLogin();
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
  let waHeartbeatTimer = null;

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
    if (!state.token) return;
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

  function startSyncWatchdog() {
    clearTimeout(state.syncWatchdog);
    state.syncWatchdog = setTimeout(() => {
      if (!state.syncInProgress) return;
      state.syncInProgress = false;
      fetchConversations()
        .then((rows) => {
          state.conversations = rows;
          renderConversationList();
        })
        .catch(() => renderConversationList());
    }, 90000);
  }

  function clearSyncWatchdog() {
    clearTimeout(state.syncWatchdog);
    state.syncWatchdog = null;
  }

  function showSyncingList() {
    state.syncInProgress = true;
    startSyncWatchdog();
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
      if (phoneChanged) {
        prepareForNewDevice();
        triggerAutoSync();
      } else if (prevStatus !== "connected") {
        state.autoSyncRequested = false;
        triggerAutoSync();
      } else if (!state.syncInProgress) {
        fetchConversations()
          .then((rows) => {
            state.conversations = rows;
            renderConversationList();
          })
          .catch(() => {});
      }
    } else if (state.wa.status === "connecting") {
      startWaPoll();
    } else {
      hideQrModal();
      stopWaPoll();
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
    $("empty-chat-label").classList.toggle("hidden", needsConnect && !state.conversations.length);
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
    return c.display_name || c.contact_name || c.display_phone || c.contact_phone || "Contacto";
  }

  function convSubtitle(c) {
    return c.display_phone || "";
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

  async function fetchConversations() {
    const archived = state.chatListTab === "archived";
    return api(`/conversations?archived=${archived}`);
  }

  async function fetchAllConversationsForSearch() {
    const [active, archived] = await Promise.all([
      api("/conversations?archived=false"),
      api("/conversations?archived=true"),
    ]);
    const seen = new Set();
    const merged = [];
    for (const row of active.concat(archived)) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      merged.push(row);
    }
    return merged;
  }

  function setChatListTab(tab) {
    state.chatListTab = tab;
    $("tab-chats-active").classList.toggle("active", tab === "active");
    $("tab-chats-archived").classList.toggle("active", tab === "archived");
  }

  function upsertConversation(conv) {
    const inArchivedTab = state.chatListTab === "archived";
    const belongsHere = !!conv.is_archived === inArchivedTab;
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
    else state.conversations.unshift(conv);
    state.conversations.sort((a, b) => {
      const ta = a.last_message_at ? new Date(a.last_message_at).getTime() : 0;
      const tb = b.last_message_at ? new Date(b.last_message_at).getTime() : 0;
      return tb - ta;
    });
    renderConversationList();
  }

  function renderConversationList() {
    conversationList.innerHTML = "";
    if (state.syncInProgress) {
      const syncLi = document.createElement("li");
      syncLi.className = "conversation-item sync-status";
      syncLi.innerHTML = '<span class="muted">Importando chats de tu celular…</span>';
      conversationList.appendChild(syncLi);
    }
    const emptyLabel =
      state.chatListTab === "archived" ? "Sin chats archivados" : "Sin conversaciones";

    let list = state.searchQuery && state.searchPool ? state.searchPool : state.conversations;
    if (state.searchQuery) {
      list = list.filter((c) => matchesSearch(c, state.searchQuery));
    }

    if (!list.length) {
      const msg = state.searchQuery ? "Sin resultados" : emptyLabel;
      conversationList.innerHTML = `<li class="conversation-item"><span class="muted">${msg}</span></li>`;
      return;
    }
    for (const c of list) {
      const li = document.createElement("li");
      li.className = "conversation-item" + (c.id === state.activeId ? " active" : "");
      li.dataset.id = c.id;
      const tags = [];
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

    fetch(mediaUrl, { headers: { Authorization: `Bearer ${state.token}` } })
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

  function syncChatToggles(conv) {
    $("toggle-mode-manual").checked = conv.mode === "manual";
    $("toggle-ai-chat").checked = !!conv.ai_active;
    $("toggle-mode-manual").disabled = !state.canWrite;
    $("toggle-ai-chat").disabled = !state.canWrite;
  }

  async function selectConversation(id) {
    state.activeId = id;
    const conv = state.conversations.find((c) => c.id === id);
    if (!conv) return;

    emptyChat.classList.add("hidden");
    activeChat.classList.remove("hidden");
    $("chat-title").textContent = convTitle(conv);
    $("chat-phone").textContent = convSubtitle(conv);
    syncChatToggles(conv);
    renderConversationList();

    try {
      state.messages = dedupeMessages(await api(`/conversations/${id}/messages`));
      renderMessages();
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
      const [me, tenant, wa] = await Promise.all([
        api("/auth/me"),
        api("/tenants/me"),
        api("/whatsapp/status", {}, 8000),
      ]);

      state.user = me;
      state.tenant = tenant;
      state.canWrite = roleAtLeast(me.role, "agent");
      state.canManageGlobal = me.role === "owner";
      state.canConnectWa = me.role === "owner";

      $("business-name").textContent = tenant.business_name;
      $("user-label").textContent = `${me.full_name} (${me.role})`;
      $("toggle-ai-global").checked = tenant.ai_global_enabled;
      $("toggle-ai-global").disabled = !state.canManageGlobal;

      applyWaSession(wa);
      if (state.wa.status === "connected" && !state.syncInProgress) {
        state.conversations = await fetchConversations();
      } else {
        state.conversations = [];
      }
      sendForm.classList.toggle("disabled", !state.canWrite || state.wa.status !== "connected");
      messageInput.disabled = !state.canWrite || state.wa.status !== "connected";

      renderConversationList();
      renderWaUi();
      connectWs();
      startWaHeartbeat();
    } catch (err) {
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${err.message}</span></li>`;
      throw err;
    }
  }

  function connectWs() {
    disconnectWs();
    if (!state.token) return;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/panel?token=${encodeURIComponent(state.token)}`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = () => setWsBadge(true);
    ws.onclose = () => {
      setWsBadge(false);
      if (state.token) setTimeout(connectWs, 3000);
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
        if (event.message && event.conversation?.id === state.activeId) {
          const key = event.message.id || `${event.message.body}|${event.message.created_at}`;
          const exists = state.messages.some(
            (m) => m.id === event.message.id || `${m.body}|${m.created_at}` === `${event.message.body}|${event.message.created_at}`
          );
          if (!exists) {
            state.messages.push(event.message);
            renderMessages();
          }
        }
        break;
      case "conversation.updated":
        if (event.conversation) {
          upsertConversation(event.conversation);
          if (event.conversation.id === state.activeId) syncChatToggles(event.conversation);
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
        showSyncingList();
        break;
      case "sync.completed":
        clearSyncWatchdog();
        state.syncInProgress = false;
        if (event.status === "completed") {
          fetchConversations()
            .then((rows) => {
              state.conversations = rows;
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
              state.conversations = rows;
              renderConversationList();
            })
            .catch(() => renderConversationList());
        }
        break;
      case "contacts.enriched":
        fetchConversations()
          .then((rows) => {
            state.conversations = rows;
            renderConversationList();
          })
          .catch(() => {});
        break;
      default:
        break;
    }
  }

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginError.classList.add("hidden");
    const email = $("login-email").value.trim();
    const password = $("login-password").value;

    try {
      const res = await fetch(`${API}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Login fallido");
      state.token = data.access_token;
      localStorage.setItem(STORAGE_KEY, state.token);
      showPanel();
      await loadInitial();
    } catch (err) {
      loginError.textContent = err.message;
      loginError.classList.remove("hidden");
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
      const stats = await api("/whatsapp/enrich-contacts", { method: "POST" }, 30000);
      state.conversations = await fetchConversations();
      renderConversationList();
      if (stats.names_fixed || stats.phones_fixed) {
        alert(stats.message || `Actualizados: ${stats.names_fixed} nombres`);
      }
    } catch (err) {
      state.conversations = await fetchConversations();
      renderConversationList();
      if (!String(err.message).includes("409")) alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  async function switchChatTab(tab) {
    setChatListTab(tab);
    state.activeId = null;
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
    conversationList.innerHTML = '<li class="conversation-item"><span class="muted">Cargando…</span></li>';
    try {
      state.conversations = await fetchConversations();
      renderConversationList();
    } catch (err) {
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${err.message}</span></li>`;
    }
  }

  $("tab-chats-active").addEventListener("click", () => switchChatTab("active"));
  $("tab-chats-archived").addEventListener("click", () => switchChatTab("archived"));

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
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
    }
  });

  $("toggle-ai-chat").addEventListener("change", async (e) => {
    if (!state.activeId || !state.canWrite) return;
    try {
      const conv = await api(`/conversations/${state.activeId}/ai`, {
        method: "PATCH",
        body: JSON.stringify({ ai_active: e.target.checked }),
      });
      upsertConversation(conv);
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
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
    if (document.visibilityState === "visible" && state.token) {
      refreshWaStatus();
    }
  });

  if (state.token) {
    showPanel();
    loadInitial().catch((err) => {
      console.error(err);
      if (err.message !== "Sesión expirada") {
        conversationList.innerHTML = `<li class="conversation-item"><span class="error">No se pudo cargar. Recarga la página.</span></li>`;
        return;
      }
      logout();
    });
  } else {
    showLogin();
  }
})();
