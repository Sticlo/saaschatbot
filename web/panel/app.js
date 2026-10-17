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
    chatListTab: "all",
    interestedCount: 0,
    onboarding: { alertCount: null, bookingEnabled: null, totalChats: 0 },
    subscription: null,
    syncInProgress: false,
    autoSyncRequested: false,
    syncWatchdog: null,
    searchQuery: "",
    searchPool: null,
    searchTimer: null,
    panelMode: "chats",
    aiProfile: null,
    aiQuestions: [],
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

  const tour = { active: false, autoChecked: false, steps: [], index: 0, root: null, target: null, cleanup: null, raf: 0 };
  const diag = { data: null, timer: 0, polls: 0, wasConnected: false, loading: false };
  const setupProgress = { done: null };
  let stripAction = null;
  let chatHintAction = null;
  let listEmptyAction = null;
  const CHECK_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  const STRIP_ICONS = {
    pause: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><rect x="7" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor"/><rect x="13.6" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor"/></svg>',
    warn: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M12 7.5v5.5M12 16.6v.01"/></svg>',
    wa: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.6A8 8 0 1 1 20 12zM4 4l16 16"/></svg>',
  };
  // El QR no se puede escanear con el mismo celular que lo muestra: ahí se vincula con código.
  const waLink = { mode: "qr", editingPhone: false };
  const PAIR_PHONE_KEY = "omitel.pairPhone";
  let waConnectInFlight = null;

  // Caché de resultados de media: msgId → {ok: bool, data?} para no repetir fetches
  const mediaCache = new Map();
  let pendingImage = null; // foto pegada o adjunta en el chat, antes de enviarla
  let pendingFile = null; // documento o video adjunto con el «+», antes de enviarlo

  const $ = (id) => document.getElementById(id);

  // Indicativos de Latinoamérica (+ EE. UU./Canadá y España). `len`: dígitos del número local de celular.
  const PHONE_COUNTRIES = [
    { iso: "CO", name: "Colombia", flag: "🇨🇴", dial: "57", len: 10, example: "300 123 4567" },
    { iso: "AR", name: "Argentina", flag: "🇦🇷", dial: "54", len: 11, example: "9 11 2345 6789" },
    { iso: "BO", name: "Bolivia", flag: "🇧🇴", dial: "591", len: 8, example: "7123 4567" },
    { iso: "BR", name: "Brasil", flag: "🇧🇷", dial: "55", len: 11, example: "11 91234 5678" },
    { iso: "CA", name: "Canadá", flag: "🇨🇦", dial: "1", len: 10, example: "416 555 0123" },
    { iso: "CL", name: "Chile", flag: "🇨🇱", dial: "56", len: 9, example: "9 1234 5678" },
    { iso: "CR", name: "Costa Rica", flag: "🇨🇷", dial: "506", len: 8, example: "8312 3456" },
    { iso: "CU", name: "Cuba", flag: "🇨🇺", dial: "53", len: 8, example: "5123 4567" },
    { iso: "EC", name: "Ecuador", flag: "🇪🇨", dial: "593", len: 9, example: "99 123 4567" },
    { iso: "SV", name: "El Salvador", flag: "🇸🇻", dial: "503", len: 8, example: "7012 3456" },
    { iso: "ES", name: "España", flag: "🇪🇸", dial: "34", len: 9, example: "612 34 56 78" },
    { iso: "US", name: "Estados Unidos", flag: "🇺🇸", dial: "1", len: 10, example: "201 555 0123" },
    { iso: "GT", name: "Guatemala", flag: "🇬🇹", dial: "502", len: 8, example: "5123 4567" },
    { iso: "HN", name: "Honduras", flag: "🇭🇳", dial: "504", len: 8, example: "9123 4567" },
    { iso: "MX", name: "México", flag: "🇲🇽", dial: "52", len: 10, example: "55 1234 5678" },
    { iso: "NI", name: "Nicaragua", flag: "🇳🇮", dial: "505", len: 8, example: "8123 4567" },
    { iso: "PA", name: "Panamá", flag: "🇵🇦", dial: "507", len: 8, example: "6123 4567" },
    { iso: "PY", name: "Paraguay", flag: "🇵🇾", dial: "595", len: 9, example: "961 456 789" },
    { iso: "PE", name: "Perú", flag: "🇵🇪", dial: "51", len: 9, example: "912 345 678" },
    { iso: "PR", name: "Puerto Rico", flag: "🇵🇷", dial: "1", len: 10, example: "787 234 5678" },
    { iso: "DO", name: "República Dominicana", flag: "🇩🇴", dial: "1", len: 10, example: "809 234 5678" },
    { iso: "UY", name: "Uruguay", flag: "🇺🇾", dial: "598", len: 8, example: "94 231 234" },
    { iso: "VE", name: "Venezuela", flag: "🇻🇪", dial: "58", len: 10, example: "412 123 4567" },
  ];
  const PHONE_OTHER = "OTHER";
  const NANP_AREAS = { PR: ["787", "939"], DO: ["809", "829", "849"] };

  const phoneCountry = (iso) => PHONE_COUNTRIES.find((c) => c.iso === iso) || null;

  // +1 lo comparten EE. UU., Canadá, Puerto Rico y República Dominicana: decide el código de área.
  function countryForDigits(digits) {
    if (digits.startsWith("1")) {
      const area = digits.slice(1, 4);
      const nanp = Object.keys(NANP_AREAS).find((iso) => NANP_AREAS[iso].includes(area));
      return phoneCountry(nanp || "US");
    }
    return PHONE_COUNTRIES
      .filter((c) => c.dial !== "1" && digits.startsWith(c.dial))
      .sort((a, b) => b.dial.length - a.dial.length)[0] || null;
  }

  function defaultPhoneCountry() {
    const own = String(state.wa?.phone_number || "").replace(/\D/g, "");
    return (own && countryForDigits(own)?.iso) || "CO";
  }

  function splitPhone(value) {
    const raw = String(value || "").trim();
    const digits = raw.replace(/\D/g, "");
    if (!digits) return { iso: defaultPhoneCountry(), national: "" };
    if (/[a-z]/i.test(raw)) return { iso: PHONE_OTHER, national: raw };
    if (!raw.startsWith("+")) return { iso: defaultPhoneCountry(), national: raw };
    const country = countryForDigits(digits);
    if (!country) return { iso: PHONE_OTHER, national: raw };
    return { iso: country.iso, national: digits.slice(country.dial.length) };
  }

  function phoneCountryOptions(selected) {
    const options = PHONE_COUNTRIES.map(
      (c) => `<option value="${c.iso}"${c.iso === selected ? " selected" : ""}>${c.flag} +${c.dial} ${c.name}</option>`
    );
    options.push(`<option value="${PHONE_OTHER}"${selected === PHONE_OTHER ? " selected" : ""}>Otro país</option>`);
    return options.join("");
  }

  // Cerrado muestra solo «🇨🇴 +57»; al abrirlo, la lista con los nombres.
  function showPhoneCountryNames(select, full) {
    [...select.options].forEach((opt) => {
      const country = phoneCountry(opt.value);
      if (!country) return;
      opt.textContent = full || !opt.selected
        ? `${country.flag} +${country.dial} ${country.name}`
        : `${country.flag} +${country.dial}`;
    });
  }

  function syncPhonePlaceholder(input, select) {
    const country = phoneCountry(select.value);
    input.placeholder = country ? country.example : "+44 7700 900123";
  }

  // Envuelve un <input type="tel"> con el selector de país; el input queda solo con el número local.
  function enhancePhoneInput(input) {
    if (!input || input.dataset.phoneEnhanced) return;
    input.dataset.phoneEnhanced = "1";
    const wrap = document.createElement("div");
    wrap.className = "phone-field";
    const select = document.createElement("select");
    select.className = "phone-country";
    select.setAttribute("aria-label", "País");
    input.parentNode.insertBefore(wrap, input);
    wrap.append(select, input);
    input.classList.add("phone-number");
    setPhoneValue(input, input.value);
    select.addEventListener("mousedown", () => showPhoneCountryNames(select, true));
    select.addEventListener("focus", () => showPhoneCountryNames(select, true));
    select.addEventListener("blur", () => showPhoneCountryNames(select, false));
    select.addEventListener("change", () => {
      syncPhonePlaceholder(input, select);
      showPhoneCountryNames(select, false);
      input.focus();
    });
    // Si pegan o escriben el número completo con +, el indicativo pasa al selector.
    const absorbPrefix = () => {
      if (input.value.trim().startsWith("+")) setPhoneValue(input, input.value);
    };
    input.addEventListener("blur", absorbPrefix);
    input.addEventListener("paste", () => setTimeout(absorbPrefix, 0));
  }

  function setPhoneValue(input, value) {
    const select = input.parentNode.querySelector(".phone-country");
    if (!select) {
      input.value = value || "";
      return;
    }
    const { iso, national } = splitPhone(value);
    select.innerHTML = phoneCountryOptions(iso);
    showPhoneCountryNames(select, false);
    input.value = national;
    syncPhonePlaceholder(input, select);
  }

  function getPhoneValue(input) {
    if (!input) return "";
    const raw = input.value.trim();
    let digits = raw.replace(/\D/g, "");
    if (!digits || /[a-z]/i.test(raw)) return raw;
    const country = phoneCountry(input.parentNode.querySelector(".phone-country")?.value);
    if (raw.startsWith("+") || !country) return `+${digits}`;
    digits = digits.replace(/^0+/, "");
    if (digits.startsWith(country.dial) && digits.length === country.dial.length + country.len) {
      digits = digits.slice(country.dial.length);
    }
    // WhatsApp en Argentina usa +54 9 antes del código de área.
    if (country.iso === "AR" && digits.length === 10) digits = `9${digits}`;
    return `+${country.dial}${digits}`;
  }

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

  function syncThemeColor(theme) {
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "light" ? "#f4f6fb" : "#0b141a");
  }

  function applyTheme(theme) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    syncThemeColor(next);
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

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";
  const EASE_DRAWER = "cubic-bezier(0.32, 0.72, 0, 1)";

  function animateIn(el, { x = 0, y = 0, scale = 1, opacity = 0, duration = 200, easing = EASE_OUT, delay = 0, origin } = {}) {
    if (!el?.animate || reducedMotion.matches) return;
    if (origin) el.style.transformOrigin = origin;
    el.animate(
      [
        { opacity, transform: `translate(${x}px, ${y}px) scale(${scale})` },
        { opacity: 1, transform: "none" },
      ],
      { duration, easing, delay, fill: "backwards" }
    );
  }

  function mountTabPill(bar) {
    if (!bar) return;
    const pill = document.createElement("span");
    pill.className = "tab-pill";
    pill.setAttribute("aria-hidden", "true");
    bar.prepend(pill);
    bar.classList.add("has-pill");
    const place = () => {
      const tab = bar.querySelector(":scope > .active:not(.hidden)");
      if (!tab || !tab.offsetWidth) {
        pill.classList.remove("is-placed");
        return;
      }
      pill.style.width = `${tab.offsetWidth}px`;
      pill.style.height = `${tab.offsetHeight}px`;
      pill.style.transform = `translate(${tab.offsetLeft}px, ${tab.offsetTop}px)`;
      if (!pill.classList.contains("is-placed")) {
        pill.classList.add("is-placed");
        requestAnimationFrame(() => pill.classList.add("is-live"));
      }
    };
    new MutationObserver((records) => {
      if (records.some((r) => r.target !== pill)) place();
    }).observe(bar, { subtree: true, attributes: true, attributeFilter: ["class"] });
    const resize = new ResizeObserver(place);
    resize.observe(bar);
    bar.querySelectorAll(":scope > button").forEach((tab) => resize.observe(tab));
    place();
  }

  let toastTimer = null;
  function toast(text, { tone = "ok", duration } = {}) {
    let el = $("omi-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "omi-toast";
      el.className = "omi-toast";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML = '<span class="omi-toast-icon" aria-hidden="true"></span><span class="omi-toast-text"></span>';
      el.addEventListener("click", () => el.classList.remove("is-open"));
      document.body.appendChild(el);
      el.getBoundingClientRect();
    }
    el.dataset.tone = tone;
    el.querySelector(".omi-toast-icon").textContent = tone === "error" ? "!" : "✓";
    el.querySelector(".omi-toast-text").textContent = text;
    el.classList.add("is-open");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("is-open"), duration || Math.min(7000, 2600 + text.length * 40));
  }

  function waitForNetwork(ms) {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        window.removeEventListener("online", done);
        resolve();
      };
      const timer = setTimeout(done, navigator.onLine === false ? 60000 : ms);
      window.addEventListener("online", done);
    });
  }

  let netPillTimer = null;
  function setNetPill(offline) {
    clearTimeout(netPillTimer);
    let el = $("net-pill");
    if (!el) {
      el = document.createElement("div");
      el.id = "net-pill";
      el.className = "net-pill";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML = '<span class="net-pill-dot" aria-hidden="true"></span><span class="net-pill-text"></span>';
      document.body.appendChild(el);
      el.getBoundingClientRect();
    }
    const text = el.querySelector(".net-pill-text");
    if (offline) {
      el.dataset.state = "offline";
      text.textContent = "Sin conexión · lo que escribas se envía al volver";
      netPillTimer = setTimeout(() => el.classList.add("is-open"), 700);
      return;
    }
    if (!el.classList.contains("is-open")) return;
    el.dataset.state = "online";
    text.textContent = "Conexión restablecida";
    netPillTimer = setTimeout(() => el.classList.remove("is-open"), 1800);
  }

  const SKEL_W = [62, 48, 70, 55, 40, 66, 58, 44];
  function skeletonHtml(kind, n = 6, tag = "li") {
    const rows = Array.from({ length: n }, (_, i) => i);
    if (kind === "chats") {
      return rows
        .map(
          (i) => `<li class="skel-row" aria-hidden="true"><span class="skel-lines">` +
            `<span class="skel" style="width:${SKEL_W[i % 8]}%"></span>` +
            `<span class="skel skel-sm" style="width:${SKEL_W[(i + 3) % 8] + 20}%"></span></span></li>`
        )
        .join("");
    }
    if (kind === "messages") {
      const bubbles = [["in", 58], ["in", 38], ["out", 52], ["in", 66], ["out", 34], ["out", 48]];
      return `<div class="skel-thread" aria-hidden="true">${bubbles
        .map(([d, w]) => `<span class="skel skel-bubble ${d}" style="width:${w}%"></span>`)
        .join("")}</div>`;
    }
    return rows
      .map(
        (i) => `<${tag} class="skel-card" aria-hidden="true"><span class="skel" style="width:${SKEL_W[i % 8] - 10}%"></span>` +
          `<span class="skel skel-sm" style="width:${SKEL_W[(i + 2) % 8] + 15}%"></span></${tag}>`
      )
      .join("");
  }

  function showSkeleton(el, kind, n, delay = 120) {
    if (!el) return () => {};
    const timer = setTimeout(() => {
      el.innerHTML = skeletonHtml(kind, n, /^(UL|OL)$/.test(el.tagName) ? "li" : "div");
      el.setAttribute("aria-busy", "true");
    }, delay);
    return () => {
      clearTimeout(timer);
      el.removeAttribute("aria-busy");
    };
  }

  const msgCache = new Map();
  function cacheMessages(id, msgs) {
    if (!id || !msgs?.length) return;
    msgCache.delete(id);
    msgCache.set(id, msgs);
    if (msgCache.size > 30) msgCache.delete(msgCache.keys().next().value);
  }

  function setStatusText(id, text) {
    const el = $(id);
    const msg = text || "";
    if (msg.startsWith("✓ ")) {
      toast(msg.slice(2));
      if (el) el.textContent = "";
      return;
    }
    if (el) el.textContent = msg;
  }

  mountTabPill(document.querySelector(".sidebar-mode-tabs"));
  mountTabPill(document.querySelector(".chat-tabs"));
  mountTabPill(document.querySelector(".ai-sections"));
  document.querySelector(".ai-sections").addEventListener("click", (e) => {
    const tab = e.target.closest(".ai-section-tab");
    if (tab) showAiSection(tab.dataset.aiSection);
  });

  const sendForm = $("send-form");
  const messageInput = $("message-input");
  const wsStatus = $("ws-status");
  const waStatus = $("wa-status");

  function headers(json = true) {
    const h = {};
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  function networkError(msg) {
    const e = new Error(msg);
    e.network = true;
    return e;
  }

  const READ_RETRY_MS = [1200, 2500, 4000];
  /** Las lecturas se reintentan solas: un micro-corte de red no debe convertirse en un error en pantalla. */
  async function api(path, options = {}, timeoutMs = 8000) {
    const isRead = !options.method || options.method === "GET";
    for (let attempt = 0; ; attempt++) {
      try {
        return await apiOnce(path, options, timeoutMs);
      } catch (err) {
        if (!isRead || !err.network || attempt >= READ_RETRY_MS.length || navigator.onLine === false) throw err;
        await new Promise((resolve) => setTimeout(resolve, READ_RETRY_MS[attempt]));
      }
    }
  }

  async function apiOnce(path, options = {}, timeoutMs = 8000) {
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
      if (err.name === "AbortError") throw networkError("La conexión está lenta. Intenta de nuevo.");
      const raw = String(err?.message || err || "");
      if (
        raw === "Load failed" ||
        raw === "Failed to fetch" ||
        raw.includes("NetworkError") ||
        raw.includes("network")
      ) {
        throw networkError("Sin conexión a internet. Revisa tu señal.");
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
    state.chatListTab = "all";
    state.interestedCount = 0;
    state.onboarding = { alertCount: null, bookingEnabled: null, totalChats: 0 };
    state.subscription = null;
    state.panelMode = "chats";
    state.aiProfile = null;
    tryAi.turns = [];
    renderTryAi();
    state.quickShortcuts = [];
    state.shortcutsDraft = [];
    state.results = null;
    document.querySelectorAll(".results-slot").forEach((slot) => {
      slot.classList.add("hidden");
      slot.innerHTML = "";
    });
    state.wa = { status: "disconnected", qr_base64: null, phone_number: null, chatwoot_inbox_url: null };
    setWsBadge(false);
    updateWaBadge("disconnected");
    hideQrModal();
    conversationList.innerHTML = "";
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
    $("ai-setup-panel").classList.add("hidden");
    $("appointments-panel").classList.add("hidden");
    $("inventory-panel").classList.add("hidden");
    $("orders-panel").classList.add("hidden");
    $("ai-alert-banner").classList.add("hidden");
    setupProgress.done = null;
    $("ai-questions-badge").classList.add("hidden");
    $("orders-badge").classList.add("hidden");
    msgCache.clear();
    state.convCache = null;
    state.productsLoaded = false;
    $("mode-orders").classList.add("hidden");
    state.orders = [];
    $("sidebar-chats").classList.remove("hidden");
    $("mode-chats").classList.add("active");
    $("mode-orders").classList.remove("active");
    $("mode-appointments").classList.remove("active");
    $("mode-inventory").classList.remove("active");
    $("mode-ai").classList.remove("active");
    $("chat-area").classList.remove("ai-setup-mode", "appointments-mode");
    $("business-name").textContent = "—";
    $("user-label").textContent = "";
    setChatListTab("all");
    updateInterestedBadge();
    endTour({ remember: false });
    tour.autoChecked = false;
    renderOnboarding();
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

  function formatClock(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString("es-CO", { hour: "numeric", minute: "2-digit" });
  }

  function daysAgo(iso) {
    const d = new Date(iso);
    const today = new Date();
    const a = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
    const b = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
    return Math.round((b - a) / 86400000);
  }

  function capitalize(s) {
    return s ? s[0].toUpperCase() + s.slice(1) : s;
  }

  function dayLabel(iso) {
    const n = daysAgo(iso);
    const d = new Date(iso);
    if (n === 0) return "Hoy";
    if (n === 1) return "Ayer";
    if (n > 1 && n < 7) return capitalize(d.toLocaleDateString("es-CO", { weekday: "long" }));
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString("es-CO", { day: "numeric", month: "long", ...(sameYear ? {} : { year: "numeric" }) });
  }

  function listTime(iso) {
    if (!iso) return "";
    const n = daysAgo(iso);
    if (n === 0) return formatClock(iso);
    if (n === 1) return "Ayer";
    if (n > 1 && n < 7) return capitalize(new Date(iso).toLocaleDateString("es-CO", { weekday: "short" }).replace(".", ""));
    return new Date(iso).toLocaleDateString("es-CO", { day: "2-digit", month: "2-digit", year: "2-digit" });
  }

  const AVATAR_HUES = [158, 200, 265, 330, 20, 42, 185, 290];
  function avatarHtml(name, cls = "avatar") {
    const clean = String(name || "").replace(/[^\p{L}\p{N} ]/gu, "").trim();
    const parts = clean.split(/\s+/).filter(Boolean);
    const initials = /^\d/.test(clean) || !parts.length
      ? ""
      : (parts[0][0] + (parts[1]?.[0] || "")).toUpperCase();
    let h = 0;
    for (const ch of clean) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    const hue = AVATAR_HUES[h % AVATAR_HUES.length];
    const inner = initials ||
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 12a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zm0 2c-4.4 0-8 2.2-8 5v1h16v-1c0-2.8-3.6-5-8-5z"/></svg>';
    return `<span class="${cls}" style="--av-h:${hue}" aria-hidden="true">${inner}</span>`;
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
      pairing_code: wa.pairing_code ?? null,
      phone_number: nextPhone,
      chatwoot_inbox_url: wa.chatwoot_inbox_url ?? null,
    };
    updateWaBadge(state.wa.status);
    sendForm.classList.toggle("disabled", !state.canWrite || state.wa.status !== "connected");
    messageInput.disabled = !state.canWrite || state.wa.status !== "connected";
    if (state.wa.status === "connecting") {
      if (waLink.mode === "code") {
        if (state.wa.pairing_code && !waConnectInFlight && !waLink.editingPhone) {
          showPairCode(state.wa.pairing_code);
        }
      } else if (state.wa.qr_base64) {
        showQrImage(state.wa.qr_base64);
      }
    }
    if (state.wa.status === "connected") {
      hideQrModal();
      stopWaPoll();
      startLivePoll();
      if (nextPhone !== prevPhone) {
        checkAlertRecipients();
        if (state.canManageGlobal) refreshReadinessSoon();
      }
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
    const roleName = { owner: "Dueño", admin: "Administrador", agent: "Asesor", viewer: "Solo lectura" }[me.role] || me.role;
    if (userLabel) userLabel.textContent = `${me.full_name} · ${roleName}`;
    const avatar = $("topbar-avatar");
    if (avatar) avatar.textContent = (me.full_name || me.email || "O").trim().charAt(0).toUpperCase();
    const aiGlobal = $("toggle-ai-global");
    if (aiGlobal) aiGlobal.disabled = !state.canManageGlobal;
  }

  function renderWaUi() {
    const connected = state.wa.status === "connected";
    const needsConnect = !connected;

    $("wa-connect-btn")?.classList.toggle("hidden", !needsConnect);
    $("wa-setup")?.classList.toggle("hidden", !needsConnect);
    if (connected && currentTourStep()?.key === "wa") tourGo(tour.index + 1);
    const dropped = needsConnect && state.conversations.length > 0;
    const setupTitle = $("wa-setup-title");
    if (setupTitle) {
      const onPhone = preferredWaMode() === "code";
      setupTitle.textContent = dropped ? "WhatsApp se desconectó" : "Conecta tu WhatsApp";
      $("wa-setup-text").innerHTML = dropped
        ? "Estamos intentando reconectar solos y tus chats se conservan. Si en un par de minutos no vuelve, vincúlalo otra vez."
        : onPhone
          ? "Conéctalo desde este mismo celular con un código. En un minuto la IA empieza a atender a tus clientes."
          : "Escanea el código QR con tu celular. En un minuto la IA empieza a atender a tus clientes.";
      $("wa-setup-btn").textContent = onPhone ? "Conectar con código" : "Mostrar código QR";
      $("wa-setup-alt-btn").textContent = onPhone
        ? "Prefiero escanear un código QR"
        : "¿Solo tienes este celular? Conéctate con un código";
    }
    renderSetupGuide();
    if (connected && !diag.wasConnected) {
      diag.polls = 0;
      loadDiagnostic();
    }
    diag.wasConnected = connected;
    $("wa-disconnect-btn")?.classList.toggle("hidden", !connected || !state.canManageGlobal);
    $("wa-unlink-ready-btn")?.classList.toggle("hidden", !connected || !state.canManageGlobal);
    $("wa-reset-chats-btn")?.classList.toggle("hidden", !connected || !state.canConnectWa);
    const cwLink = $("wa-chatwoot-link");
    if (cwLink) {
      const showCw = connected && !!state.wa.chatwoot_inbox_url;
      cwLink.classList.toggle("hidden", !showCw);
      if (showCw) cwLink.href = state.wa.chatwoot_inbox_url;
    }
  }

  function preferredWaMode() {
    return window.matchMedia("(pointer: coarse) and (max-width: 820px)").matches ? "code" : "qr";
  }

  function isPhoneLayout() {
    return window.matchMedia("(max-width: 768px)").matches;
  }

  function formatPairCode(code) {
    const clean = String(code || "").replace(/[^a-z0-9]/gi, "").toUpperCase();
    return clean.length === 8 ? `${clean.slice(0, 4)}-${clean.slice(4)}` : clean;
  }

  function setWaLinkMode(mode) {
    waLink.mode = mode;
    const code = mode === "code";
    $("qr-hint").classList.toggle("hidden", code);
    $("qr-image").classList.add("hidden");
    $("pair-form").classList.add("hidden");
    $("pair-result").classList.add("hidden");
    $("qr-mode-switch").textContent = code
      ? "Prefiero escanear un código QR"
      : "¿Solo tienes este celular? Conéctate con un código";
  }

  function showQrModal() {
    setWaLinkMode("qr");
    $("qr-modal").classList.remove("hidden");
    $("qr-loading").classList.remove("hidden");
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
    $("pair-result").classList.add("hidden");
    if (waLink.mode === "code") {
      waLink.editingPhone = true;
      $("pair-form").classList.remove("hidden");
    }
    const err = $("qr-error");
    err.textContent = msg;
    err.classList.remove("hidden");
  }

  function showPairForm() {
    setWaLinkMode("code");
    waLink.editingPhone = true;
    $("qr-modal").classList.remove("hidden");
    $("qr-loading").classList.add("hidden");
    $("qr-error").classList.add("hidden");
    $("qr-phone").classList.add("hidden");
    $("pair-form").classList.remove("hidden");
    const input = $("pair-phone");
    if (!input.value) input.value = localStorage.getItem(PAIR_PHONE_KEY) || "";
    input.focus();
  }

  function showPairCode(code) {
    $("qr-loading").classList.add("hidden");
    $("qr-error").classList.add("hidden");
    $("pair-form").classList.add("hidden");
    const btn = $("pair-code");
    const next = formatPairCode(code);
    if (btn.textContent !== next) {
      btn.textContent = next;
      $("pair-copied").textContent = "Toca el código para copiarlo";
    }
    $("pair-result").classList.remove("hidden");
  }

  async function copyPairCode() {
    const code = $("pair-code").textContent.replace(/-/g, "");
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      $("pair-copied").textContent = "¡Copiado! Pégalo en WhatsApp";
    } catch {
      $("pair-copied").textContent = "Escríbelo tal cual en WhatsApp";
    }
  }

  function connectWhatsApp(mode = preferredWaMode()) {
    if (waConnectInFlight) {
      $("qr-modal").classList.remove("hidden");
      return waConnectInFlight;
    }
    if (mode === "code") {
      showPairForm();
      return Promise.resolve();
    }
    waConnectInFlight = requestWhatsAppQr().finally(() => {
      waConnectInFlight = null;
    });
    return waConnectInFlight;
  }

  function requestPairCode(phone) {
    if (waConnectInFlight) return waConnectInFlight;
    waConnectInFlight = (async () => {
      setWaLinkMode("code");
      waLink.editingPhone = false;
      $("qr-error").classList.add("hidden");
      $("qr-loading").textContent = "Pidiendo el código a WhatsApp… puede tardar hasta 30 segundos";
      $("qr-loading").classList.remove("hidden");
      $("wa-setup-error").classList.add("hidden");
      try {
        const wa = await api(
          "/whatsapp/connect",
          { method: "POST", body: JSON.stringify({ phone }) },
          90000
        );
        localStorage.setItem(PAIR_PHONE_KEY, phone);
        applyWaSession(wa);
        if (wa.pairing_code) showPairCode(wa.pairing_code);
        else if (wa.status !== "connected") showQrError("WhatsApp no entregó el código. Intenta de nuevo en unos segundos.");
      } catch (err) {
        showQrError(humanizeWaError(err.message || "No pudimos pedir el código"));
      }
    })().finally(() => {
      waConnectInFlight = null;
    });
    return waConnectInFlight;
  }

  async function requestWhatsAppQr() {
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
    $("chat-avatar").outerHTML = avatarHtml(convTitle(conv), "avatar chat-avatar").replace("<span ", '<span id="chat-avatar" ');
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
    if (rows) state.onboarding.totalChats = rows.length;
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

  function tourStorageKey() {
    return `omitel_tour_done:${state.tenant?.id || state.user?.tenant_id || ""}`;
  }

  function canSeeGuide() {
    return !!state.user && state.canManageGlobal && state.onboarding.alertCount !== null;
  }

  function setupIncomplete() {
    return !!setupSteps()?.some((s) => !s.done);
  }

  function renderOnboarding() {
    $("guide-btn")?.classList.toggle("hidden", !canSeeGuide());
    renderSetupGuide();
  }

  // —— Primeros pasos: lo que falta para que la IA venda, en orden y con un botón cada uno ——


  function setupSteps() {
    if (!state.user || !state.canManageGlobal || !state.aiProfile || state.onboarding.alertCount === null) return null;
    const onPhone = preferredWaMode() === "code";
    const readiness = state.aiProfile.readiness || { ready: true, total: 0, items: [] };
    const missing = readiness.items.filter((i) => !i.done);
    const started = missing.length < readiness.total;
    const tips = state.aiProfile.tips || [];
    const ownNumber = tips.some((t) => t.key === "alerts-own");
    return [
      {
        key: "wa",
        title: "Conecta tu WhatsApp",
        minutes: 1,
        text: onPhone ? "Desde este mismo celular, con un código. Tus chats siguen en tu WhatsApp." : "Escaneas un código con tu celular y listo. Tus chats siguen en tu WhatsApp.",
        done: state.wa.status === "connected",
        doneText: "WhatsApp conectado",
        cta: onPhone ? "Conectar con código" : "Mostrar código QR",
        run: () => connectWhatsApp(),
      },
      {
        key: "biz",
        title: "Cuéntale qué vendes",
        minutes: 3,
        text: started
          ? `Te falta: ${listEs(missing.map((i) => i.label.toLowerCase()))}.`
          : state.tenant?.ai_autostart_pending
            ? "Qué vendes, precios, cómo te pagan, envíos y horario. Al terminar, tu IA se enciende sola."
            : "Qué vendes, precios, cómo te pagan, envíos y horario. Así responde sin decir «te confirmo».",
        done: !!readiness.ready,
        doneText: state.tenant?.ai_global_enabled ? "Tu negocio quedó listo y tu IA está encendida" : "Tu negocio quedó listo",
        cta: started ? "Seguir" : "Empezar",
        run: () => goToReadinessField(missing[0]?.field || "biz-products"),
      },
      {
        key: "alerts",
        title: "¿A dónde te avisamos?",
        minutes: 0.5,
        text: ownNumber
          ? "Pusiste el número del negocio y así el aviso no suena. Pon tu WhatsApp personal."
          : "Tu WhatsApp personal. Te escribimos cuando alguien quiera comprar.",
        done: state.onboarding.alertCount > 0 && !ownNumber,
        doneText: "Ya sabemos a dónde avisarte",
        cta: ownNumber ? "Cambiar número" : "Poner mi número",
        run: () => goToReadinessField("alert-recipients"),
      },
      {
        key: "try",
        title: "Pruébala tú primero",
        minutes: 2,
        text: "Escríbele como si fueras un cliente. No se le envía nada a nadie.",
        done: !tips.some((t) => t.key === "try"),
        doneText: "Ya la probaste",
        cta: "Probar mi IA",
        run: () => goToReadinessField("try-ai-input"),
      },
    ];
  }

  function setupGuideActive(steps) {
    if (!steps || steps.every((s) => s.done)) return false;
    return !(state.wa.status !== "connected" && state.conversations.length > 0);
  }

  function announceSetupProgress(steps) {
    const doneKeys = steps.filter((s) => s.done).map((s) => s.key);
    const before = setupProgress.done;
    setupProgress.done = doneKeys;
    if (!before) return;
    const fresh = steps.find((s) => s.done && !before.includes(s.key));
    if (!fresh) return;
    const next = steps.find((s) => !s.done);
    if (next) {
      toast(`${fresh.doneText}. Sigue: «${next.title}».`, { duration: 6000 });
    } else {
      toast(
        state.tenant?.ai_global_enabled
          ? "¡Listo! Tu IA quedó lista para vender."
          : "¡Listo! Ya puedes encender tu IA arriba a la derecha.",
        { duration: 7000 }
      );
    }
  }

  function renderSetupGuide() {
    const guide = $("setup-guide");
    const waSetup = $("wa-setup");
    const list = $("setup-guide-steps");
    if (!guide || !waSetup) return;
    const steps = setupSteps();
    const active = setupGuideActive(steps);
    if (list.contains(waSetup)) guide.after(waSetup);
    guide.classList.toggle("hidden", !active);
    $("empty-ready")?.classList.toggle("hidden", active || state.wa.status !== "connected");
    if (steps) announceSetupProgress(steps);
    if (!active) {
      renderStatusStrip();
      return;
    }

    const doneCount = steps.filter((s) => s.done).length;
    const left = steps.length - doneCount;
    const current = steps.find((s) => !s.done);
    const first = firstName();
    const minutes = Math.max(1, Math.ceil(steps.filter((s) => !s.done).reduce((n, s) => n + s.minutes, 0)));
    $("setup-guide-hello").textContent = doneCount ? `Vas muy bien${first ? `, ${first}` : ""}` : `Hola${first ? `, ${first}` : ""}`;
    $("setup-guide-title").textContent = doneCount
      ? `Te ${left === 1 ? "falta 1 paso" : `faltan ${left} pasos`}`
      : `Tu IA lista en ${steps.length} pasos`;
    $("setup-guide-count").textContent = `${doneCount} de ${steps.length} listos · unos ${minutes} min`;
    $("setup-guide-bar").style.width = `${Math.max(4, Math.round((doneCount / steps.length) * 100))}%`;
    list.innerHTML = steps
      .map((s, i) => {
        const isCurrent = s === current;
        const cls = ["setup-step", s.done && "is-done", isCurrent && "is-current"].filter(Boolean).join(" ");
        return `<li class="${cls}" data-step="${s.key}">
          <span class="setup-step-mark" aria-hidden="true">${s.done ? CHECK_ICON : i + 1}</span>
          <div class="setup-step-body">
            <div class="setup-step-head">
              <strong>${escapeHtml(s.done ? s.doneText : s.title)}</strong>
              ${s.done ? '<span class="sr-only">(listo)</span>' : `<span class="setup-step-time">${s.minutes < 1 ? "30 seg" : `${s.minutes} min`}</span>`}
            </div>
            ${isCurrent ? `<p class="setup-step-text">${escapeHtml(s.text)}</p><div class="setup-step-action"></div>` : ""}
          </div>
          ${!s.done && !isCurrent ? `<button type="button" class="link-btn setup-step-go">${escapeHtml(s.cta)}</button>` : ""}
        </li>`;
      })
      .join("");
    const action = list.querySelector(".is-current .setup-step-action");
    if (current.key === "wa") {
      action.appendChild(waSetup);
    } else {
      action.innerHTML = `<button type="button" class="btn primary setup-step-cta">${escapeHtml(current.cta)}</button>`;
    }
    renderStatusStrip();
  }

  $("setup-guide-steps").addEventListener("click", (e) => {
    const btn = e.target.closest(".setup-step-cta, .setup-step-go");
    if (!btn) return;
    const key = btn.closest(".setup-step")?.dataset.step;
    setupSteps()?.find((s) => s.key === key)?.run();
  });

  // —— Por qué la IA no responde: siempre en palabras del negocio y con qué hacer ——

  const goToPlan = () => {
    location.href = siteUrl("/mi-plan");
  };
  function turnOnGlobalAi() {
    const input = $("toggle-ai-global");
    if (!input || input.checked) return;
    input.checked = true;
    input.dispatchEvent(new Event("change"));
  }

  function aiOffReason() {
    const sub = state.subscription;
    const ai = state.aiStatus;
    if (sub?.trial_expired || sub?.needs_payment || ai?.plan_required) {
      return {
        tone: "warn",
        icon: STRIP_ICONS.warn,
        title: sub?.needs_payment ? "Tu plan venció: la IA no está respondiendo" : "Tu prueba gratis terminó: la IA no está respondiendo",
        short: sub?.needs_payment ? "Plan vencido: la IA no responde." : "Prueba terminada: la IA no responde.",
        text: "Tus clientes no reciben respuesta automática hasta que actives tu plan.",
        action: { label: "Activar plan", run: goToPlan },
      };
    }
    if (ai && (ai.configured === false || ai.provider_ok === false)) {
      return {
        tone: "error",
        icon: STRIP_ICONS.warn,
        title: "La IA tiene una falla de nuestro lado",
        short: "Falla nuestra, no tuya: ya la revisamos.",
        text: "No es nada de tu configuración: ya lo estamos revisando. Mientras tanto, responde tú desde aquí.",
      };
    }
    if (ai && !ai.daily_replies_unlimited && ai.daily_replies_remaining === 0) {
      return {
        tone: "warn",
        icon: STRIP_ICONS.warn,
        title: "La IA llegó al límite de respuestas de hoy",
        short: "Límite de hoy: mañana vuelve.",
        text: "Mañana vuelve a responder sola. Si te escriben mucho, mejora tu plan.",
        action: { label: "Ver planes", run: goToPlan },
      };
    }
    if (state.wa.status !== "connected" && state.conversations.length > 0) {
      return {
        tone: "warn",
        icon: STRIP_ICONS.wa,
        title: "WhatsApp desconectado",
        short: "WhatsApp desconectado.",
        text: "No llegan mensajes nuevos y la IA no puede responder. Tus chats están guardados.",
        action: state.canConnectWa ? { label: "Reconectar", run: () => connectWhatsApp() } : null,
      };
    }
    if (state.tenant && !state.tenant.ai_global_enabled && state.canManageGlobal && state.aiProfile && !state.aiProfile.readiness?.ready) {
      return {
        tone: "pause",
        icon: STRIP_ICONS.pause,
        title: state.tenant.ai_autostart_pending ? "Tu IA se enciende sola al terminar" : "Tu IA está en pausa",
        text: state.tenant.ai_autostart_pending
          ? "Cuando le cuentes qué vendes, se enciende y empieza a responder. Mientras tanto contestas tú."
          : "Para encenderla, termina de contarle qué vendes. Mientras tanto contestas tú.",
        action: { label: "Completar", run: () => goToReadinessField(missingReadiness()[0]?.field || "biz-products") },
        readinessPause: true,
      };
    }
    if (state.tenant && !state.tenant.ai_global_enabled) {
      return {
        tone: "pause",
        icon: STRIP_ICONS.pause,
        title: "Tu IA está en pausa",
        text: state.canManageGlobal
          ? "No le responde a nadie: aquí contestas tú. Enciéndela cuando quieras."
          : "No le responde a nadie. El dueño de la cuenta la puede encender.",
        action: state.canManageGlobal ? { label: "Encender", run: turnOnGlobalAi } : null,
      };
    }
    return null;
  }

  function statusStripInfo() {
    if (state.aiNotice) {
      return {
        tone: state.aiNotice.isError ? "error" : "info",
        icon: STRIP_ICONS.warn,
        title: state.aiNotice.isError ? "La IA no pudo responder un mensaje" : "Aviso de tu IA",
        text: state.aiNotice.text,
      };
    }
    const guideOnScreen = $("setup-guide").getClientRects().length > 0;
    const steps = setupSteps();
    const setupPending = !!steps && !steps.every((s) => s.done);
    const off = aiOffReason();
    // La guía ya explica que la IA se enciende al completar el negocio: repetirlo arriba es ruido.
    if (off && !(off.readinessPause && (guideOnScreen || setupPending))) return off;
    if (!setupPending || guideOnScreen) return null;
    const done = steps.filter((s) => s.done).length;
    const next = steps.find((s) => !s.done);
    return {
      tone: "setup",
      icon: `<span class="status-strip-count">${done}/${steps.length}</span>`,
      title: "Primeros pasos",
      text: `Sigue: ${next.title}`,
      action: { label: next.cta, run: next.run },
    };
  }

  function renderStatusStrip() {
    const strip = $("ai-alert-banner");
    if (!strip) return;
    const info = statusStripInfo();
    strip.classList.toggle("hidden", !info);
    stripAction = info?.action?.run || null;
    renderPlanStatus();
    if (!info) return;
    strip.dataset.tone = info.tone;    strip.querySelector(".status-strip-icon").innerHTML = info.icon;
    strip.querySelector(".status-strip-title").textContent = info.title;
    strip.querySelector(".status-strip-text").textContent = info.text || "";
    const btn = strip.querySelector(".status-strip-btn");
    btn.textContent = info.action?.label || "";
    btn.classList.toggle("hidden", !info.action);
  }

  $("ai-alert-banner").querySelector(".status-strip-btn").addEventListener("click", () => stripAction?.());

  function buildTourSteps() {
    const steps = [
      {
        key: "hello",
        title: "Hola, soy Omi",
        text: "En 10 minutos dejamos tu IA vendiendo: conectas WhatsApp, le cuentas qué vendes y cómo te pagan, subes tus productos con foto, pones tu número para los avisos y la pruebas tú primero.",
        next: "Empezar",
      },
    ];
    if (state.wa.status !== "connected" && state.canConnectWa) {
      steps.push({
        key: "wa",
        mode: "chats",
        target: () => ($("wa-setup")?.getClientRects().length ? $("wa-setup") : $("wa-connect-btn")),
        title: "Conecta tu WhatsApp",
        text: preferredWaMode() === "code"
          ? "Toca el botón verde y escribe el número de este WhatsApp. Te damos un código para escribirlo en WhatsApp › Dispositivos vinculados."
          : "Toca el botón verde y escanea el código con tu celular: WhatsApp › Dispositivos vinculados › Vincular dispositivo.",
        hint: "Cuando quede conectado, sigo solo.",
      });
    }
    steps.push({
      key: "biz",
      mode: "ai",
      target: () => document.querySelector(".ai-setup-form-card"),
      focus: () => $("biz-name"),
      title: "Cuéntale de tu negocio",
      text: "Lo que más preguntan los clientes: qué vendes, cómo te pagan (con el número de la cuenta), si haces envíos y dónde estás. Sin eso la IA tiene que decir «te confirmo». Luego toca «Guardar mi negocio».",
      done: () => !!state.aiProfile?.readiness?.ready,
    });
    steps.push({
      key: "products",
      mode: "inventory",
      target: () => $("products-card"),
      title: "Sube lo que vendes, con foto",
      text: "Un producto por cada cosa que vendes (no «Ropa» en general), con su precio y al menos una foto. Cuando un cliente pida verlo, la IA se la manda sola. Si solo das servicios, sigue.",
      done: () => (state.products || []).some((x) => (x.photos || []).length),
    });
    steps.push({
      key: "alerts",
      mode: "ai",
      target: () => $("alerts-card"),
      focus: () => $("alert-recipients")?.querySelector("input[type=tel], input[type=text], input"),
      title: "Tu WhatsApp personal",
      text: "Pon el número donde quieres que te avisen — no el del negocio — y toca «Guardar alertas». Así no se te enfría un cliente.",
      done: () => state.onboarding.alertCount > 0,
    });
    if (state.onboarding.bookingAllowed !== false) {
      steps.push({
        key: "citas-ask",
        mode: "ai",
        target: () => $("mode-appointments"),
        title: "¿Das citas?",
        text: "Solo si atiendes con turno o reserva (barbería, consultorio, spa…). Si vendes productos y los envías, no las necesitas: la IA ofrecería horarios a tus clientes.",
        next: "No doy citas",
        nextGoTo: "try",
        secondary: { label: "Sí, doy citas", goTo: "citas" },
        advanceOnClick: true,
      });
      steps.push({
        key: "citas",
        mode: "appointments",
        target: () => document.querySelector(".appointments-schedule-card"),
        title: "Horario y agenda",
        text: "Pon a qué hora abres y cierras. Activa «La IA agenda citas sola» y guarda. Listo.",
        done: () => !!state.onboarding.bookingEnabled,
      });
    }
    steps.push({
      key: "try",
      mode: "ai",
      target: () => $("try-ai-card"),
      focus: () => $("try-ai-input"),
      title: "Pruébala tú primero",
      text: "Escríbele como si fueras un cliente: pregunta precios, pide fotos y compra algo. No se le envía nada a nadie. Si algo no te gusta, ajústalo en «Tu negocio» y vuelve a probar.",
      done: () => tryAi.turns.some((t) => t.from === "ai"),
    });
    steps.push({
      key: "bye",
      mode: "chats",
      title: "Listo",
      text: "Aquí ves los chats; en «Interesados», a los que quieren comprar. Si usas este WhatsApp también para cosas personales, apaga «IA activa» en esos chats para que no les responda. Si te pierdes, toca tu inicial arriba a la derecha y «Ver la guía otra vez».",
      next: "Entendido",
    });
    return steps;
  }

  function ensureTourRoot() {
    if (tour.root) return tour.root;
    const root = document.createElement("div");
    root.className = "tour hidden";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "tour-title");
    root.innerHTML = `
      <div class="tour-backdrop"></div>
      <div class="tour-ring" aria-hidden="true"></div>
      <div class="tour-pop">
        <div class="tour-pop-head">
          <span class="tour-avatar" aria-hidden="true">O</span>
          <span class="tour-who">Omi · tu guía</span>
          <span class="tour-count"></span>
        </div>
        <h3 id="tour-title" class="tour-title"></h3>
        <p class="tour-text"></p>
        <p class="tour-done hidden">✓ Ya está listo. Revísalo si quieres y sigue.</p>
        <p class="tour-hint"></p>
        <div class="tour-progress" aria-hidden="true"><span></span></div>
        <div class="tour-actions">
          <button type="button" class="tour-skip">Saltar guía</button>
          <span class="tour-spacer"></span>
          <button type="button" class="btn ghost small tour-back">Atrás</button>
          <button type="button" class="btn ghost small tour-secondary hidden"></button>
          <button type="button" class="btn primary small tour-next">Siguiente</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    root.querySelector(".tour-skip").addEventListener("click", () => endTour());
    root.querySelector(".tour-back").addEventListener("click", () => tourGo(tour.index - 1));
    root.querySelector(".tour-next").addEventListener("click", () => {
      const goTo = tour.steps[tour.index]?.nextGoTo;
      const i = goTo ? tour.steps.findIndex((s) => s.key === goTo) : -1;
      tourGo(i >= 0 ? i : tour.index + 1);
    });
    root.querySelector(".tour-secondary").addEventListener("click", () => {
      const goTo = tour.steps[tour.index]?.secondary?.goTo;
      const i = tour.steps.findIndex((s) => s.key === goTo);
      tourGo(i >= 0 ? i : tour.index + 1);
    });
    root.querySelector(".tour-text").addEventListener("click", () => {
      if (!root.classList.contains("tour-docked")) return;
      root.querySelector(".tour-pop").classList.toggle("tour-expanded");
      scheduleTourLayout();
    });
    root.querySelector(".tour-backdrop").addEventListener("click", () => {
      const pop = root.querySelector(".tour-pop");
      pop.classList.remove("tour-nudge");
      void pop.offsetWidth;
      pop.classList.add("tour-nudge");
    });
    new ResizeObserver(scheduleTourLayout).observe(root.querySelector(".tour-pop"));
    tour.root = root;
    return root;
  }

  function startTour() {
    if (!canSeeGuide()) return;
    if (state.activeId) {
      state.activeId = null;
      renderConversationList();
    }
    tour.steps = buildTourSteps();
    tour.active = true;
    document.body.classList.add("tour-on");
    ensureTourRoot().classList.remove("hidden");
    document.addEventListener("keydown", onTourKey);
    window.addEventListener("resize", scheduleTourLayout);
    window.addEventListener("scroll", scheduleTourLayout, true);
    window.addEventListener("scrollend", scheduleTourLayout, true);
    tourGo(0);
  }

  function endTour({ remember = true } = {}) {
    if (!tour.active) return;
    tour.active = false;
    tour.cleanup?.();
    tour.cleanup = null;
    tour.target = null;
    tour.root?.classList.add("hidden");
    document.body.classList.remove("tour-on", "has-tour-dock");
    document.documentElement.style.removeProperty("--tour-dock-space");
    document.removeEventListener("keydown", onTourKey);
    window.removeEventListener("resize", scheduleTourLayout);
    window.removeEventListener("scroll", scheduleTourLayout, true);
    window.removeEventListener("scrollend", scheduleTourLayout, true);
    if (remember) localStorage.setItem(tourStorageKey(), "1");
    setTimeout(maybeShowDiagnostic, 500);
  }

  function onTourKey(e) {
    if (e.key === "Escape" && $("qr-modal")?.classList.contains("hidden")) endTour();
  }

  function currentTourStep() {
    return tour.active ? tour.steps[tour.index] : null;
  }

  function tourGo(index) {
    if (!tour.active) return;
    if (index >= tour.steps.length) {
      endTour();
      return;
    }
    tour.index = Math.max(0, index);
    const step = tour.steps[tour.index];
    tour.cleanup?.();
    tour.cleanup = null;

    if (step.mode && state.panelMode !== step.mode) switchPanelMode(step.mode);

    const root = tour.root;
    const total = tour.steps.length;
    root.querySelector(".tour-count").textContent = `${tour.index + 1} de ${total}`;
    root.querySelector(".tour-title").textContent = step.title;
    root.querySelector(".tour-text").textContent = step.text;
    const hint = root.querySelector(".tour-hint");
    hint.textContent = step.hint || "";
    hint.classList.toggle("hidden", !step.hint);
    root.querySelector(".tour-done").classList.toggle("hidden", !step.done?.());
    root.querySelector(".tour-progress span").style.width = `${Math.round(((tour.index + 1) / total) * 100)}%`;
    root.querySelector(".tour-back").classList.toggle("hidden", tour.index === 0);
    root.querySelector(".tour-skip").classList.toggle("hidden", tour.index === total - 1);
    root.querySelector(".tour-next").textContent = step.next || "Siguiente";
    const secondary = root.querySelector(".tour-secondary");
    secondary.textContent = step.secondary?.label || "";
    secondary.classList.toggle("hidden", !step.secondary);
    const pop = root.querySelector(".tour-pop");
    pop.classList.remove("tour-pop-in", "tour-expanded");
    void pop.offsetWidth;
    pop.classList.add("tour-pop-in");

    setTimeout(() => {
      if (!tour.active || tour.steps[tour.index] !== step) return;
      const el = step.target?.() || null;
      revealAiGroup(el);
      tour.target = el && el.getClientRects().length ? el : null;
      if (tour.target) {
        const r = tour.target.getBoundingClientRect();
        if (r.top < 70 || r.bottom > window.innerHeight - 40) {
          const tall = r.height > window.innerHeight * 0.5;
          tour.target.scrollIntoView({
            block: tall ? "start" : "center",
            behavior: prefersReducedMotion() ? "auto" : "smooth",
          });
        }
        if (step.advanceOnClick) {
          const target = tour.target;
          const onClick = () => setTimeout(() => {
            if (tour.active && tour.steps[tour.index] === step) tourGo(tour.index + 1);
          }, 120);
          target.addEventListener("click", onClick);
          tour.cleanup = () => target.removeEventListener("click", onClick);
        }
        const resize = new ResizeObserver(scheduleTourLayout);
        resize.observe(tour.target);
        const stopClick = tour.cleanup;
        tour.cleanup = () => {
          stopClick?.();
          resize.disconnect();
        };
        const input = step.focus?.();
        if (input) setTimeout(() => input.focus({ preventScroll: true }), 350);
      }
      layoutTour();
      setTimeout(() => {
        if (tour.steps[tour.index] !== step) return;
        root.querySelector(".tour-done").classList.toggle("hidden", !step.done?.());
        layoutTour();
      }, 450);
      if (!step.focus) root.querySelector(".tour-next").focus({ preventScroll: true });
    }, 30);
  }

  function prefersReducedMotion() {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  }

  function scheduleTourLayout() {
    if (!tour.active || tour.raf) return;
    tour.raf = requestAnimationFrame(() => {
      tour.raf = 0;
      layoutTour();
    });
  }

  function visibleBox(el) {
    let top = 4;
    let bottom = window.innerHeight - 4;
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.position === "fixed") break;
      if (p === el || cs.overflowY === "visible") continue;
      const r = p.getBoundingClientRect();
      top = Math.max(top, r.top);
      bottom = Math.min(bottom, r.bottom);
    }
    return { top, bottom };
  }

  function bottomNavTop() {
    const nav = document.querySelector(".sidebar-mode-tabs");
    if (!nav || !nav.getClientRects().length || getComputedStyle(nav).position !== "fixed") return window.innerHeight;
    return nav.getBoundingClientRect().top;
  }

  function layoutTour() {
    if (!tour.active || !tour.root) return;
    const W = window.innerWidth;
    const H = window.innerHeight;
    const pad = 8;
    let hole = { x: W / 2, y: H / 2, w: 0, h: 0, r: 0 };
    let tall = false;
    if (tour.target) {
      const r = tour.target.getBoundingClientRect();
      const clip = visibleBox(tour.target);
      const top = Math.max(r.top - pad, clip.top);
      const bottom = Math.min(r.bottom + pad, clip.bottom);
      hole = { x: r.left - pad, y: top, w: r.width + pad * 2, h: Math.max(bottom - top, 0), r: 12 };
      tall = r.height > H * 0.5;
    }
    const { x, y, w, h } = hole;
    const rr = Math.min(hole.r, w / 2, h / 2);
    const path =
      `M0 0H${W}V${H}H0Z ` +
      `M${x + rr} ${y}H${x + w - rr}A${rr} ${rr} 0 0 1 ${x + w} ${y + rr}` +
      `V${y + h - rr}A${rr} ${rr} 0 0 1 ${x + w - rr} ${y + h}` +
      `H${x + rr}A${rr} ${rr} 0 0 1 ${x} ${y + h - rr}` +
      `V${y + rr}A${rr} ${rr} 0 0 1 ${x + rr} ${y}Z`;
    tour.root.querySelector(".tour-backdrop").style.clipPath = `path(evenodd, "${path}")`;

    const ring = tour.root.querySelector(".tour-ring");
    ring.classList.toggle("hidden", !tour.target);
    Object.assign(ring.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });

    const pop = tour.root.querySelector(".tour-pop");
    const gap = 16;
    const margin = 12;
    const floor = bottomNavTop();
    tour.root.classList.remove("tour-docked");
    const pw = pop.offsetWidth;
    const fullH = pop.offsetHeight;
    const roomRight = x + w + gap + pw <= W - margin;
    const roomLeft = x - gap - pw >= margin;
    const roomBelow = y + h + gap + fullH <= floor - margin;
    const roomAbove = y - gap - fullH >= margin;
    const docked = Boolean(tour.target) && !roomRight && !roomLeft && (tall || (!roomBelow && !roomAbove));
    tour.root.classList.toggle("tour-docked", docked);
    if (!docked) pop.classList.remove("tour-expanded");
    const ph = docked ? pop.offsetHeight : fullH;
    document.body.classList.toggle("has-tour-dock", docked);
    document.documentElement.style.setProperty("--tour-dock-space", `${docked ? ph + margin * 2 : 0}px`);
    let left;
    let top;
    if (!tour.target) {
      left = (W - pw) / 2;
      top = (floor - ph) / 2;
    } else if (docked) {
      left = isPhoneLayout() ? (W - pw) / 2 : W - pw - margin * 2;
      top = floor - ph - margin * (isPhoneLayout() ? 1 : 2);
    } else if (tall) {
      left = roomRight ? x + w + gap : x - gap - pw;
      top = floor - ph - margin * 2;
    } else if (roomBelow) {
      left = x + w / 2 - pw / 2;
      top = y + h + gap;
    } else if (roomAbove) {
      left = x + w / 2 - pw / 2;
      top = y - gap - ph;
    } else {
      left = roomRight ? x + w + gap : x - gap - pw;
      top = y + h / 2 - ph / 2;
    }
    pop.style.left = `${Math.round(Math.min(Math.max(left, margin), W - pw - margin))}px`;
    pop.style.top = `${Math.round(Math.min(Math.max(top, margin), floor - ph - margin))}px`;
  }

  async function loadOnboardingStatus() {
    const [alerts, schedule] = await Promise.all([
      api("/tenants/me/interest-alert").catch(() => null),
      api("/appointments/schedule").catch(() => null),
    ]);
    state.onboarding.alertCount = alerts ? (alerts.recipients || []).length : 0;
    if (schedule) {
      state.onboarding.bookingEnabled = !!schedule.ai_booking_enabled;
      state.onboarding.bookingAllowed = schedule.ai_booking_allowed !== false;
    }
    renderOnboarding();
    renderAvisosDot();
    loadDiagnostic();
  }

  // —— Diagnóstico: lo que el negocio se perdía en el chat antes de Omitel ——
  const DIAG_POLL_MS = 8000;
  const DIAG_MAX_POLLS = 60;
  // Supuesto explícito en el texto: de cada 4 clientes sin respuesta, 1 habría comprado.
  const DIAG_BUYERS_RATIO = 4;

  function diagSeenKey(res) {
    const tenantId = state.tenant?.id || state.user?.tenant_id || "";
    return `omitel_diag_seen:${tenantId}:${res?.diagnostic?.connection_id || ""}`;
  }

  function renderDiagEntry(analyzing) {
    $("diag-analyzing")?.classList.toggle("hidden", !analyzing);
    $("diag-open")?.classList.toggle("hidden", !diag.data);
  }

  async function loadDiagnostic() {
    if (diag.loading || !state.canManageGlobal || state.wa.status !== "connected") return;
    clearTimeout(diag.timer);
    diag.loading = true;
    let res;
    try {
      res = await api("/whatsapp/diagnostic");
    } catch {
      return;
    } finally {
      diag.loading = false;
    }
    if (res?.status === "analyzing") {
      renderDiagEntry(!diag.data);
      if (diag.polls++ < DIAG_MAX_POLLS) diag.timer = setTimeout(loadDiagnostic, DIAG_POLL_MS);
      return;
    }
    diag.data = res?.status === "ready" ? res : null;
    renderDiagEntry(false);
    maybeShowDiagnostic();
  }

  function maybeShowDiagnostic() {
    if (!diag.data || tour.active) return;
    if ([...document.querySelectorAll(".modal")].some((m) => !m.classList.contains("hidden"))) return;
    if (localStorage.getItem(diagSeenKey(diag.data)) === "1") return;
    openDiagnostic();
  }

  function formatWait(seconds) {
    const minutes = Math.round(seconds / 60);
    if (minutes < 1) return "menos de un minuto";
    if (minutes < 60) return pluralEs(minutes, "minuto", "minutos");
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (hours < 24) return rest && hours < 10 ? `${hours} h ${rest} min` : pluralEs(hours, "hora", "horas");
    return pluralEs(Math.round(hours / 24), "día", "días");
  }

  /** Lo que más le duele al negocio va de titular; el resto, debajo sin repetirlo. */
  function diagHeadlineKind(d) {
    if (d.unanswered > 0) return "unanswered";
    if (d.median_reply_seconds >= 30 * 60) return "slow";
    if (d.after_hours > 0) return "after_hours";
    return "all_answered";
  }

  function diagHeadline(d, kind) {
    if (kind === "unanswered") {
      const one = d.unanswered === 1;
      return `<strong>${pluralEs(d.unanswered, "persona", "personas")}</strong> te ${one ? "preguntó" : "preguntaron"} por lo que vendes y ${one ? "se quedó" : "se quedaron"} sin respuesta`;
    }
    if (kind === "slow") return `Quien te pregunta por lo que vendes espera <strong>${formatWait(d.median_reply_seconds)}</strong> por una respuesta`;
    if (kind === "after_hours") {
      return `<strong>${pluralEs(d.after_hours, "persona", "personas")}</strong> te ${d.after_hours === 1 ? "escribió" : "escribieron"} fuera de tu horario`;
    }
    return "Le respondiste a todos los que te preguntaron.";
  }

  function diagMoneyHtml(d, ticket) {
    if (!(d.unanswered > 0)) return "";
    if (ticket) {
      const lost = Math.round((d.unanswered * ticket) / DIAG_BUYERS_RATIO);
      return `<p class="diag-money">Si solo 1 de cada ${DIAG_BUYERS_RATIO} te hubiera comprado, eran <strong>≈ ${formatCop(lost)}</strong> en ventas.</p>`;
    }
    return `<form class="diag-ticket">
      <label for="diag-ticket-input">¿Cuánto vale en promedio una venta o servicio tuyo?</label>
      <div class="diag-ticket-row">
        <input id="diag-ticket-input" name="ticket" inputmode="numeric" maxlength="12" placeholder="50.000" />
        <button type="submit" class="btn ghost small">Ver cuánto es</button>
      </div>
      <p class="diag-ticket-status"></p>
    </form>`;
  }

  function diagnosticHtml(res) {
    const d = res.diagnostic || {};
    const kind = diagHeadlineKind(d);
    const stats = [
      `<li><strong>${pluralEs(d.clients, "persona", "personas")}</strong> te ${d.clients === 1 ? "preguntó" : "preguntaron"} por precios, productos, envíos o citas</li>`,
    ];
    if (d.after_hours > 0 && kind !== "after_hours") {
      stats.push(`<li><strong>${pluralEs(d.after_hours, "persona", "personas")}</strong> te ${d.after_hours === 1 ? "escribió" : "escribieron"} fuera de tu horario</li>`);
    }
    if (d.median_reply_seconds != null && kind !== "slow") {
      stats.push(`<li>Normalmente tardas <strong>${formatWait(d.median_reply_seconds)}</strong> en contestarles</li>`);
    }
    const topics = (d.topics || []).slice(0, 3);
    const topicsHtml = topics.length
      ? `<p class="diag-topics-title">Lo que más te preguntan</p>
         <div class="diag-topics">${topics
           .map((t) => `<span>${escapeHtml(t.label)} <em>${pluralEs(t.chats, "chat", "chats")}</em></span>`)
           .join("")}</div>`
      : "";
    const cta = setupIncomplete() ? "Configurar mi IA (2 min)" : "¡Entendido!";
    return `<p class="diag-kicker">Tus clientes en WhatsApp, últimos ${d.window_days || 30} días</p>
      <h3 id="diag-title" class="diag-headline">${diagHeadline(d, kind)}</h3>
      <ul class="diag-stats">${stats.join("")}</ul>
      ${diagMoneyHtml(d, res.avg_ticket_cop)}
      ${topicsHtml}
      <p class="diag-omi">Desde hoy la IA les responde en segundos, de día y de noche, y te avisa cuando alguien quiere comprar.</p>
      <button type="button" class="btn primary diag-cta" data-diag-cta>${cta}</button>
      <p class="diag-fine">Contamos solo los chats donde te preguntaron por lo que vendes; dejamos por fuera grupos, conversaciones personales y notificaciones. Son chats de antes de conectar Omitel, por eso no aparecen en tu panel. No guardamos esos mensajes: solo estos números.</p>`;
  }

  function openDiagnostic() {
    if (!diag.data) return;
    localStorage.setItem(diagSeenKey(diag.data), "1");
    $("diag-body").innerHTML = diagnosticHtml(diag.data);
    $("diag-modal").classList.remove("hidden");
    $("diag-body").querySelector("[data-diag-cta]")?.focus();
  }

  function closeDiagnostic() {
    $("diag-modal").classList.add("hidden");
  }

  async function saveDiagTicket(form) {
    const status = form.querySelector(".diag-ticket-status");
    const digits = String(form.querySelector("input[name=ticket]")?.value || "").replace(/\D/g, "");
    if (!digits) {
      status.textContent = "Escribe el valor, por ejemplo 50.000";
      return;
    }
    status.textContent = "Calculando…";
    try {
      state.results = await api("/results/settings", {
        method: "PUT",
        body: JSON.stringify({ avg_ticket_cop: Number(digits) }),
      });
      resultsLoadedAt = Date.now();
      diag.data = { ...diag.data, avg_ticket_cop: state.results?.avg_ticket_cop ?? Number(digits) };
      $("diag-body").innerHTML = diagnosticHtml(diag.data);
    } catch (err) {
      status.textContent = err.message || "No se pudo guardar";
    }
  }

  $("diag-open")?.addEventListener("click", openDiagnostic);
  $("diag-modal-close")?.addEventListener("click", closeDiagnostic);
  $("diag-modal-backdrop")?.addEventListener("click", closeDiagnostic);
  $("diag-modal")?.addEventListener("submit", (e) => {
    const form = e.target.closest?.(".diag-ticket");
    if (!form) return;
    e.preventDefault();
    saveDiagTicket(form);
  });
  $("diag-modal")?.addEventListener("click", (e) => {
    if (!e.target.closest?.("[data-diag-cta]")) return;
    const configure = setupIncomplete();
    closeDiagnostic();
    if (configure) setupSteps()?.find((s) => !s.done)?.run();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("diag-modal")?.classList.contains("hidden")) closeDiagnostic();
  });

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
    state.convCache = rows;
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
    const emptyStates = {
      interested: state.tenant?.ai_global_enabled || !state.tenant
        ? ["Aún no hay interesados", "Cuando alguien pregunte precios o quiera comprar, la IA lo marca y aparece aquí."]
        : ["Aún no hay interesados", "Tu IA está apagada. Cuando la enciendas, marcará aquí a quien pregunte precios o quiera comprar."],
      not_interested: ["Nadie descartado", "Aquí quedan los contactos que dijeron que no o que solo saludaron."],
      all: state.wa.status === "connected"
        ? ["Todo tranquilo por ahora", "Cuando te escriba un cliente, el chat aparece aquí y tu IA le responde."]
        : ["Tus chats aparecerán aquí", "Conecta tu WhatsApp y verás cada conversación con tus clientes."],
    };
    const emptyState = emptyStates[state.chatListTab] || emptyStates.all;

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
      const [title, hint] = state.searchQuery
        ? ["Sin resultados", `No encontramos chats con «${state.searchQuery}». Prueba con el nombre o el número.`]
        : emptyState;
      const fix = !state.searchQuery && state.chatListTab === "interested" ? aiOffReason()?.action : null;
      listEmptyAction = fix || null;
      conversationList.innerHTML = `
        <li class="list-empty">
          <span class="list-empty-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="${state.searchQuery ? "M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14zM20 20l-4-4" : "M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.6A8 8 0 1 1 21 12z"}"/></svg></span>
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(hint)}</span>
          ${listEmptyAction ? `<button type="button" class="btn primary list-empty-btn">${escapeHtml(listEmptyAction.label)}</button>` : ""}
        </li>`;
      animateIn(conversationList.firstElementChild, { y: 6, duration: 220 });
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
      if (c.ai_active && aiAllowedInChat(c)) tags.push('<span class="ai-tag">IA</span>');
      const subtitle = convSubtitle(c);
      const title = convTitle(c);
      li.classList.toggle("has-unread", Boolean(c.unread_count));
      li.innerHTML = `
        ${avatarHtml(title)}
        <div class="conv-body">
          <div class="row">
            <span class="name">${escapeHtml(title)}</span>
            <span class="conv-time">${escapeHtml(listTime(c.last_message_at))}</span>
          </div>
          <div class="meta">
            <span class="conv-preview">${subtitle ? escapeHtml(subtitle) : ""}</span>
            ${tags.join("")}
            ${c.unread_count ? `<span class="unread">${c.unread_count}</span>` : ""}
          </div>
        </div>
      `;
      li.addEventListener("click", () => selectConversation(c.id));
      conversationList.appendChild(li);
    }
    conversationList.insertAdjacentHTML("beforeend", listBrandHtml());
  }

  function listBrandHtml() {
    const name = state.tenant?.business_name || $("business-name").textContent.trim();
    return `
      <li class="list-brand" aria-hidden="true">
        <svg class="list-brand-mark" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.6A8 8 0 1 1 21 12z"/></svg>
        ${name && name !== "—" ? `<span class="list-brand-name">${escapeHtml(name)}</span>` : ""}
        <span class="list-brand-by">atendido con Omitel</span>
      </li>`;
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
    const caption = mediaCaption(body).replace(/\s+/g, " ");
    return caption ? `${name} · ${caption}` : name;
  }

  function mediaIcon(type) {
    const paths = {
      image: "M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9.5h.01",
      sticker: "M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9.5h.01",
      video: "M4 6h12v12H4zM16 10l4-2.5v9L16 14",
      audio: "M12 4a3 3 0 013 3v5a3 3 0 01-6 0V7a3 3 0 013-3zM6 11a6 6 0 0012 0M12 17v3",
      ptt: "M12 4a3 3 0 013 3v5a3 3 0 01-6 0V7a3 3 0 013-3zM6 11a6 6 0 0012 0M12 17v3",
    };
    const d = paths[type] || "M7 3h7l5 5v13H7zM14 3v5h5";
    return `<svg class="media-icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="${d}"/></svg>`;
  }

  function mediaLabel(type) {
    return {
      image: "Ver imagen", sticker: "Ver sticker", video: "Ver video",
      audio: "Escuchar audio", ptt: "Escuchar nota de voz", document: "Abrir archivo",
    }[type] || "Ver media";
  }

  const lightbox = { el: null, img: null, zoom: 1, x: 0, y: 0 };

  function ensureLightbox() {
    if (lightbox.el) return lightbox.el;
    const el = document.createElement("div");
    el.className = "lightbox";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "Foto");
    el.innerHTML = `
      <div class="lightbox-bar">
        <button type="button" class="lightbox-btn" data-lb-close aria-label="Cerrar">
          <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
        <a class="lightbox-btn" data-lb-save download="foto.jpg" aria-label="Guardar foto">
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19h14"/></svg>
        </a>
      </div>
      <img class="lightbox-img" alt="Foto" draggable="false" />`;
    document.body.appendChild(el);
    lightbox.el = el;
    lightbox.img = el.querySelector(".lightbox-img");
    el.querySelector("[data-lb-close]").addEventListener("click", () => closeLightbox());
    el.addEventListener("click", (e) => {
      if (e.target === el && lightbox.zoom === 1 && !lightbox.dragged) closeLightbox();
    });
    bindLightboxGestures(el, lightbox.img);
    return el;
  }

  function applyLightboxTransform(extraY = 0) {
    const { img, zoom, x, y } = lightbox;
    img.style.transform = `translate(${x}px, ${y + extraY}px) scale(${zoom})`;
  }

  function bindLightboxGestures(el, img) {
    let start = null;
    let lastTap = 0;
    el.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".lightbox-bar")) return;
      start = { x: e.clientX, y: e.clientY, ox: lightbox.x, oy: lightbox.y, t: Date.now() };
      lightbox.dragged = false;
      img.classList.add("is-dragging");
    });
    el.addEventListener("pointermove", (e) => {
      if (!start) return;
      const dx = e.clientX - start.x;
      const dy = e.clientY - start.y;
      if (Math.abs(dx) + Math.abs(dy) > 6) lightbox.dragged = true;
      if (lightbox.zoom > 1) {
        lightbox.x = start.ox + dx;
        lightbox.y = start.oy + dy;
        applyLightboxTransform();
      } else if (dy > 0) {
        applyLightboxTransform(dy);
        el.style.setProperty("--lb-fade", String(Math.max(0.25, 1 - dy / 400)));
      }
    });
    const end = (e) => {
      if (!start) return;
      const dy = e.clientY - start.y;
      const quick = Date.now() - start.t < 250;
      img.classList.remove("is-dragging");
      el.style.removeProperty("--lb-fade");
      if (lightbox.zoom === 1 && dy > 110) {
        start = null;
        closeLightbox();
        return;
      }
      if (lightbox.zoom === 1) applyLightboxTransform();
      if (!lightbox.dragged && quick && e.target === img) {
        const now = Date.now();
        if (now - lastTap < 300) {
          toggleLightboxZoom(e.clientX, e.clientY);
          lastTap = 0;
        } else {
          lastTap = now;
        }
      }
      start = null;
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    img.addEventListener("dblclick", (e) => e.preventDefault());
  }

  function toggleLightboxZoom(cx, cy) {
    if (lightbox.zoom > 1) {
      Object.assign(lightbox, { zoom: 1, x: 0, y: 0 });
    } else {
      const r = lightbox.img.getBoundingClientRect();
      const z = 2.4;
      lightbox.zoom = z;
      lightbox.x = (r.left + r.width / 2 - cx) * (z - 1);
      lightbox.y = (r.top + r.height / 2 - cy) * (z - 1);
    }
    lightbox.el.classList.toggle("is-zoomed", lightbox.zoom > 1);
    applyLightboxTransform();
  }

  function openLightbox(src, fromEl) {
    const el = ensureLightbox();
    Object.assign(lightbox, { zoom: 1, x: 0, y: 0, dragged: false });
    lightbox.img.src = src;
    lightbox.img.style.transform = "";
    el.querySelector("[data-lb-save]").href = src;
    el.classList.remove("is-zoomed");
    el.classList.add("is-open");
    document.body.classList.add("lightbox-open");
    if (!history.state?.omitelLightbox) history.pushState({ ...(history.state || {}), omitelLightbox: true }, "");
    if (fromEl && !reducedMotion.matches) {
      const a = fromEl.getBoundingClientRect();
      requestAnimationFrame(() => {
        const b = lightbox.img.getBoundingClientRect();
        if (!b.width) return;
        const sx = a.width / b.width;
        const dx = a.left + a.width / 2 - (b.left + b.width / 2);
        const dy = a.top + a.height / 2 - (b.top + b.height / 2);
        lightbox.img.animate(
          [{ transform: `translate(${dx}px, ${dy}px) scale(${sx})`, opacity: 0.6 }, { transform: "none", opacity: 1 }],
          { duration: 260, easing: EASE_DRAWER }
        );
      });
    }
    el.querySelector("[data-lb-close]").focus({ preventScroll: true });
  }

  function closeLightbox(fromHistory = false) {
    if (!lightbox.el?.classList.contains("is-open")) return;
    lightbox.el.classList.remove("is-open");
    document.body.classList.remove("lightbox-open");
    if (!fromHistory && history.state?.omitelLightbox) {
      lightbox.popping = true;
      history.back();
    }
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && lightbox.el?.classList.contains("is-open")) closeLightbox();
  });

  const VOICE_BARS = 34;
  const VOICE_SPEEDS = [1, 1.5, 2];
  const PLAY_ICON = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="currentColor" d="M8 5.5v13a1 1 0 001.5.86l10.4-6.5a1 1 0 000-1.72L9.5 4.64A1 1 0 008 5.5z"/></svg>';
  const PAUSE_ICON = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><rect x="6.5" y="5" width="4" height="14" rx="1.2" fill="currentColor"/><rect x="13.5" y="5" width="4" height="14" rx="1.2" fill="currentColor"/></svg>';
  let playingVoice = null;
  const voicePlayers = new Map();

  function placeholderPeaks(seed) {
    let h = 0;
    for (let i = 0; i < seed.length; i += 97) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
    return Array.from({ length: VOICE_BARS }, (_, i) => {
      h = (h * 1103515245 + 12345) >>> 0;
      return 0.25 + ((h >>> 16) % 1000) / 1000 * 0.6 * Math.sin(Math.PI * (i + 1) / (VOICE_BARS + 1));
    });
  }

  async function decodeVoice(src) {
    const buf = await (await fetch(src)).arrayBuffer();
    const Ctx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!Ctx) throw new Error("sin audio");
    const audio = await new Ctx(1, 1, 44100).decodeAudioData(buf);
    const data = audio.getChannelData(0);
    const step = Math.max(1, Math.floor(data.length / VOICE_BARS));
    const peaks = [];
    for (let b = 0; b < VOICE_BARS; b++) {
      let sum = 0;
      for (let i = b * step; i < Math.min(data.length, (b + 1) * step); i++) sum += data[i] * data[i];
      peaks.push(Math.sqrt(sum / step));
    }
    const max = Math.max(...peaks, 0.0001);
    return { peaks: peaks.map((p) => 0.14 + (p / max) * 0.86), duration: audio.duration };
  }

  function buildVoicePlayer(src) {
    const el = document.createElement("div");
    el.className = "voice-player";
    el.innerHTML = `<button type="button" class="vp-play" aria-label="Reproducir nota de voz">${PLAY_ICON}</button>
      <div class="vp-body">
        <div class="vp-wave" role="slider" aria-label="Avanzar en la nota de voz" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" tabindex="0">
          ${placeholderPeaks(src).map((p) => `<span style="--h:${p.toFixed(3)}"></span>`).join("")}
        </div>
        <div class="vp-meta"><span class="vp-time">0:00</span><button type="button" class="vp-speed" aria-label="Velocidad">1×</button></div>
      </div>`;
    const audio = new Audio();
    audio.preload = "metadata";
    audio.src = src;
    audio.playerEl = el;
    const playBtn = el.querySelector(".vp-play");
    const wave = el.querySelector(".vp-wave");
    const bars = [...wave.children];
    const timeEl = el.querySelector(".vp-time");
    const speedBtn = el.querySelector(".vp-speed");
    let duration = 0;
    let speedIdx = 0;
    let raf = 0;

    const knownDuration = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : duration);
    const paint = () => {
      const total = knownDuration();
      const ratio = total ? Math.min(1, audio.currentTime / total) : 0;
      const lit = Math.round(ratio * VOICE_BARS);
      bars.forEach((b, i) => b.classList.toggle("on", i < lit));
      wave.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
      const playing = !audio.paused;
      timeEl.textContent = formatDuration((playing || audio.currentTime ? audio.currentTime : total) * 1000);
    };
    const loop = () => {
      paint();
      if (!audio.paused) raf = requestAnimationFrame(loop);
    };
    const setPlaying = (on) => {
      el.classList.toggle("is-playing", on);
      playBtn.innerHTML = on ? PAUSE_ICON : PLAY_ICON;
      playBtn.setAttribute("aria-label", on ? "Pausar nota de voz" : "Reproducir nota de voz");
    };

    audio.addEventListener("loadedmetadata", paint);
    audio.addEventListener("play", () => {
      if (playingVoice && playingVoice !== audio) playingVoice.pause();
      playingVoice = audio;
      setPlaying(true);
      cancelAnimationFrame(raf);
      loop();
    });
    audio.addEventListener("pause", () => {
      setPlaying(false);
      paint();
    });
    audio.addEventListener("ended", () => {
      audio.currentTime = 0;
      setPlaying(false);
      paint();
    });
    playBtn.addEventListener("click", () => (audio.paused ? audio.play().catch(() => {}) : audio.pause()));
    speedBtn.addEventListener("click", () => {
      speedIdx = (speedIdx + 1) % VOICE_SPEEDS.length;
      audio.playbackRate = VOICE_SPEEDS[speedIdx];
      speedBtn.textContent = `${String(VOICE_SPEEDS[speedIdx]).replace(".", ",")}×`;
    });
    const seek = (clientX) => {
      const total = knownDuration();
      if (!total) return;
      const rect = wave.getBoundingClientRect();
      audio.currentTime = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) * total;
      paint();
    };
    wave.addEventListener("pointerdown", (e) => {
      seek(e.clientX);
      wave.setPointerCapture?.(e.pointerId);
    });
    wave.addEventListener("pointermove", (e) => {
      if (e.buttons) seek(e.clientX);
    });
    wave.addEventListener("keydown", (e) => {
      const total = knownDuration();
      if (!total || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      e.preventDefault();
      audio.currentTime = Math.max(0, Math.min(total, audio.currentTime + (e.key === "ArrowRight" ? 5 : -5)));
      paint();
    });

    decodeVoice(src)
      .then(({ peaks, duration: d }) => {
        duration = d;
        bars.forEach((b, i) => b.style.setProperty("--h", peaks[i].toFixed(3)));
        paint();
      })
      .catch(() => {});
    return el;
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
      el.addEventListener("click", () => openLightbox(src, el));
    } else if (mt === "video") {
      el = document.createElement("video");
      el.src = src;
      el.controls = true;
      el.className = "media-video";
    } else if (mt === "audio" || mt === "ptt") {
      el = buildVoicePlayer(src);
    } else {
      el = document.createElement("a");
      el.href = src;
      el.download = `archivo.${mimetype.split("/")[1] || "bin"}`;
      el.textContent = "Descargar archivo";
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
      const text = isVoice ? `“${escapeHtml(m.transcript)}”` : `La IA ve: ${escapeHtml(m.transcript)}`;
      const title = isVoice ? "Transcripción de la nota de voz" : "Lo que la IA entendió de la imagen";
      timeHtml = `<p class="media-transcript" title="${title}">${text}</p>${timeHtml}`;
    }

    // Si ya tenemos resultado en caché, mostrar directamente sin spinner
    const cached = mediaCache.get(m.id);
    if (cached) {
      if (cached.ok) {
        const mt = cached.media_type || type;
        const isVoice = mt === "audio" || mt === "ptt";
        const el = (isVoice && voicePlayers.get(m.id)) || _renderMediaElement(mt, cached.base64, cached.mimetype);
        if (isVoice) voicePlayers.set(m.id, el);
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
        if (el.classList.contains("voice-player")) voicePlayers.set(m.id, el);
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
    if (status === "pending") {
      return '<span class="ticks pending" title="Enviando"><svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 4.6V8l2.2 1.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg></span>';
    }
    if (status === "read") return '<span class="ticks read" title="Leído">✓✓</span>';
    if (status === "delivered") return '<span class="ticks delivered" title="Entregado">✓✓</span>';
    if (status === "failed") return '<span class="ticks failed" title="No se envió">!</span>';
    return '<span class="ticks sent" title="Enviado">✓</span>';
  }

  let renderedMsgs = { convId: null, ids: new Set(), bodies: new Set() };
  const pendingOut = [];
  const docFiles = new Map();

  function fileSizeLabel(bytes) {
    const kb = (Number(bytes) || 0) / 1024;
    return kb > 1024 ? `${(kb / 1024).toFixed(1).replace(".", ",")} MB` : `${Math.max(1, Math.round(kb))} KB`;
  }

  function docExtension(name) {
    const match = /\.([a-z0-9]{1,5})$/i.exec(name || "");
    return match ? match[1].toUpperCase() : "DOC";
  }

  const DOC_DOWNLOAD_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/></svg>';

  function buildDocBubble(m, timeHtml) {
    const legacy = (m.body || "").startsWith("📄 ");
    const lines = (legacy ? m.body.slice(2) : mediaCaption(m.body)).trim().split("\n");
    const name = (lines[0] || "").trim() || "Documento";
    const caption = lines.slice(1).join("\n").trim();
    const ext = docExtension(name);
    const local = m.file || docFiles.get(m.id);
    const wrap = document.createElement("div");
    wrap.className = "msg doc-msg " + (m.direction === "in" ? "in" : "out");
    wrap.innerHTML = `<button type="button" class="doc-card" title="Abrir ${escapeHtml(name)}">
        <span class="doc-badge" data-ext="${escapeHtml(ext)}">${escapeHtml(ext.slice(0, 4))}</span>
        <span class="doc-info"><strong>${escapeHtml(name)}</strong><small>${escapeHtml([ext, local && fileSizeLabel(local.size)].filter(Boolean).join(" · "))}</small></span>
        <span class="doc-get">${DOC_DOWNLOAD_ICON}</span>
      </button>${caption ? `<p class="media-caption">${escapeHtml(caption)}</p>` : ""}${timeHtml}`;
    const card = wrap.querySelector(".doc-card");
    card.addEventListener("click", () => openDocument(m, name, card));
    return wrap;
  }

  async function openDocument(m, name, card) {
    let blob = m.file || docFiles.get(m.id);
    if (!blob) {
      if (!m.id || m.pending || card.classList.contains("is-loading")) return;
      card.classList.add("is-loading");
      try {
        const r = await fetch(`${API}/conversations/${m.conversation_id}/messages/${m.id}/media`, { credentials: "include", headers: headers(false) });
        if (!r.ok) throw new Error(String(r.status));
        const { base64, mimetype } = await r.json();
        const bin = atob(base64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        blob = new Blob([bytes], { type: mimetype || "application/octet-stream" });
        docFiles.set(m.id, blob);
        const small = card.querySelector("small");
        if (small) small.textContent = `${docExtension(name)} · ${fileSizeLabel(blob.size)}`;
      } catch {
        toast("No se pudo abrir el archivo. Pídele al cliente que lo vuelva a enviar.", { tone: "error" });
        return;
      } finally {
        card.classList.remove("is-loading");
      }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  function visiblePending(shown) {
    return pendingOut.filter((p) => {
      if (p.conversation_id !== state.activeId) return false;
      const since = new Date(p.created_at).getTime() - 15_000;
      return !shown.some((m) => m.direction === "out" && m.body === p.body && new Date(m.created_at).getTime() >= since);
    });
  }

  function renderMessages() {
    messagesEl.innerHTML = "";
    const sameChat = renderedMsgs.convId === state.activeId && renderedMsgs.ids.size > 0;
    const ids = new Set();
    const bodies = new Set();
    const arrived = [];
    let prevDay = "";
    let prevDir = "";
    const shown = dedupeMessages(state.messages);
    const list = shown.slice();
    for (const p of visiblePending(shown)) {
      const at = Date.parse(p.created_at);
      const i = list.findIndex((m) => !m.pending && Date.parse(m.created_at) > at);
      list.splice(i < 0 ? list.length : i, 0, p);
    }
    for (const m of list) {
      const day = m.created_at ? new Date(m.created_at).toDateString() : prevDay;
      if (day && day !== prevDay) {
        const sep = document.createElement("div");
        sep.className = "day-sep";
        sep.innerHTML = `<span>${escapeHtml(dayLabel(m.created_at))}</span>`;
        messagesEl.appendChild(sep);
        prevDir = "";
      }
      prevDay = day;
      const idKey = m.id || `${m.body}|${m.created_at}`;
      const bodyKey = `${m.direction}|${m.body}`;
      const isNew = sameChat && !renderedMsgs.ids.has(idKey) && !renderedMsgs.bodies.has(bodyKey);
      ids.add(idKey);
      bodies.add(bodyKey);
      const ticks = m.direction === "out" ? outboundTicks(m.status) : "";
      const byAi = m.direction === "out" && (m.source === "ai" || m.source === "bot");
      const timeHtml = `<span class="time" title="${escapeHtml(formatTime(m.created_at))}">${byAi ? '<span class="msg-ai">IA</span>' : ""}${formatClock(m.created_at)}${ticks}</span>`;
      const mediaType = detectMediaType(m.body);

      let div;
      if (mediaType === "document" || (m.body || "").startsWith("📄 ")) {
        div = buildDocBubble(m, timeHtml);
      } else if (mediaType && m.id) {
        div = buildMediaBubble(m, mediaType, timeHtml);
      } else {
        div = document.createElement("div");
        div.className = "msg " + (m.direction === "in" ? "in" : "out");
        const caption = m.body && !detectMediaType(m.body) ? escapeHtml(m.body) : escapeHtml(m.body);
        div.innerHTML = `<span class="msg-text">${caption}</span>${timeHtml}`;
      }
      if (m.pending) {
        div.classList.add(m.status === "failed" ? "is-failed" : "is-pending");
        div.dataset.pending = m.id;
        if (m.status === "failed") div.title = "No se envió. Toca para reintentar.";
      }
      const dir = m.direction === "in" ? "in" : "out";
      if (dir !== prevDir) div.classList.add("tail");
      prevDir = dir;
      messagesEl.appendChild(div);
      if (isNew) arrived.push({ div, inbound: m.direction === "in" });
    }
    renderedMsgs = { convId: state.activeId, ids, bodies };
    if (playingVoice && !messagesEl.contains(playingVoice.playerEl)) playingVoice.pause();
    messagesEl.scrollTop = messagesEl.scrollHeight;
    if (arrived.length <= 4) {
      arrived.forEach(({ div, inbound }, i) =>
        animateIn(div, {
          y: 10,
          scale: 0.96,
          duration: 260,
          delay: i * 50,
          origin: inbound ? "bottom left" : "bottom right",
        })
      );
    }
  }

  // —— Resultados: lo que la IA le generó al negocio ——

  const RESULTS_REFRESH_MS = 60_000;
  let resultsLoadedAt = 0;
  let resultsEditingTicket = false;

  function formatCop(amount) {
    return "$" + Math.round(Number(amount) || 0).toLocaleString("es-CO");
  }

  function pluralEs(n, one, many) {
    return `${Number(n || 0).toLocaleString("es-CO")} ${n === 1 ? one : many}`;
  }

  function formatMultiple(value) {
    return String(value).replace(".", ",").replace(/,0$/, "");
  }

  function resultsHeadlineHtml(month, ticket, hasActivity) {
    if (month.revenue_cop) {
      return `<strong class="results-money">${formatCop(month.revenue_cop)}</strong>
        <span class="results-note">en citas que agendó la IA (${pluralEs(month.ai_appointments, "cita", "citas")} × ${formatCop(ticket)})</span>`;
    }
    if (month.ai_appointments) {
      return `<strong class="results-money">${pluralEs(month.ai_appointments, "cita", "citas")}</strong>
        <span class="results-note">agendadas por la IA este mes</span>`;
    }
    if (hasActivity) {
      return `<strong class="results-money">${pluralEs(month.ai_replies, "mensaje", "mensajes")}</strong>
        <span class="results-note">respondidos por la IA este mes</span>`;
    }
    return `<strong class="results-money results-money-empty">Aquí verás lo que la IA hace por ti</strong>
      <span class="results-note">Citas que agenda, mensajes que responde y cuánta plata te genera, mes a mes.</span>`;
  }

  function resultsHasActivity(data) {
    const month = data?.month || {};
    const total = data?.all_time || {};
    return !!(month.ai_appointments || month.ai_replies || total.ai_appointments || total.ai_replies);
  }

  function resultsTicketHtml(ticket, hasAppointments) {
    const isOwner = state.canManageGlobal;
    if (!hasAppointments && !ticket) return "";
    if (isOwner && (!ticket || resultsEditingTicket)) {
      return `<form class="results-ticket">
        <label>
          <span>¿Cuánto vale en promedio un servicio?</span>
          <span class="results-ticket-hint">Así convertimos las citas de la IA en plata.</span>
          <input name="ticket" inputmode="numeric" maxlength="12" placeholder="30.000" value="${ticket ? escapeHtml(Number(ticket).toLocaleString("es-CO")) : ""}" />
        </label>
        <button type="submit" class="btn primary small">Guardar</button>
        ${resultsEditingTicket ? '<button type="button" class="btn ghost small" data-results-cancel>Cancelar</button>' : ""}
        <p class="results-ticket-status"></p>
      </form>`;
    }
    if (isOwner) {
      return `<button type="button" class="results-link" data-results-edit>Valor promedio del servicio: ${formatCop(ticket)} · Cambiar</button>`;
    }
    if (!ticket) {
      return '<p class="results-fine">El dueño puede poner el valor promedio del servicio para ver la plata generada.</p>';
    }
    return "";
  }

  function resultsCardHtml(data) {
    const month = data.month || {};
    const total = data.all_time || {};
    const ticket = data.avg_ticket_cop;
    const hasActivity = resultsHasActivity(data);
    const hasAppointments = !!(month.ai_appointments || total.ai_appointments);
    const roi = month.roi_multiple >= 1 ? `<span class="results-roi">${formatMultiple(month.roi_multiple)}× tu plan</span>` : "";

    let totalLine = "";
    const sameAsMonth =
      (total.ai_appointments || 0) <= (month.ai_appointments || 0) && (total.ai_replies || 0) <= (month.ai_replies || 0);
    if (sameAsMonth) {
      totalLine = "";
    } else if (total.revenue_cop) {
      totalLine = `Desde que usas Omitel: <strong>${formatCop(total.revenue_cop)}</strong> en ${pluralEs(data.months_active, "mes", "meses")}`;
      if (data.plan_price_cop && data.months_active) {
        const perMonth = total.revenue_cop / data.months_active / data.plan_price_cop;
        if (perMonth >= 1) totalLine += ` · ${formatMultiple(perMonth.toFixed(1))} veces lo que pagas al mes`;
      }
    } else if (total.ai_appointments) {
      totalLine = `Desde que usas Omitel: <strong>${pluralEs(total.ai_appointments, "cita agendada", "citas agendadas")}</strong> por la IA`;
    }
    const hours = month.hours_saved >= 1 ? ` · ≈ ${pluralEs(month.hours_saved, "hora", "horas")} de trabajo ahorradas` : "";

    return `<section class="results-card" aria-label="Resultados de Omitel">
      <div class="results-head">
        <span class="results-kicker">Lo que Omitel hizo por ti en ${escapeHtml(data.month_name || "este mes")}</span>
        ${roi}
      </div>
      <div class="results-headline">${resultsHeadlineHtml(month, ticket, hasActivity)}</div>
      ${hasActivity ? `<div class="results-stats">
        <div><strong>${Number(month.ai_appointments || 0).toLocaleString("es-CO")}</strong><span>citas agendadas por la IA</span></div>
        <div><strong>${Number(month.after_hours_appointments || 0).toLocaleString("es-CO")}</strong><span>fuera de tu horario</span></div>
        <div><strong>${Number(month.ai_replies || 0).toLocaleString("es-CO")}</strong><span>mensajes respondidos</span></div>
        <div><strong>${Number(month.clients_attended || 0).toLocaleString("es-CO")}</strong><span>clientes atendidos</span></div>
      </div>` : ""}
      ${totalLine || hours ? `<p class="results-total">${totalLine}${totalLine ? hours : hours.replace(/^ · /, "")}</p>` : ""}
      ${resultsTicketHtml(ticket, hasAppointments)}
      ${hasActivity ? '<p class="results-fine">Solo contamos las citas que agendó la IA y siguen en tu agenda.</p>' : ""}
    </section>`;
  }

  function renderResults() {
    const data = state.results;
    const show = !!(data && resultsHasActivity(data));
    document.querySelectorAll("#appointments-panel .results-slot").forEach((slot) => {
      slot.classList.toggle("hidden", !show);
      slot.innerHTML = show ? resultsCardHtml(data) : "";
    });
  }

  async function loadResults(force = false) {
    if (!force && state.results && Date.now() - resultsLoadedAt < RESULTS_REFRESH_MS) {
      renderResults();
      return;
    }
    try {
      state.results = await api("/results");
      resultsLoadedAt = Date.now();
    } catch (err) {
      console.warn("No se pudieron cargar los resultados", err);
    }
    renderResults();
  }

  async function saveResultsTicket(form) {
    const status = form.querySelector(".results-ticket-status");
    const digits = String(form.querySelector("input[name=ticket]")?.value || "").replace(/\D/g, "");
    if (!digits) {
      status.textContent = "Escribe el valor, por ejemplo 30.000";
      return;
    }
    status.textContent = "Guardando…";
    try {
      state.results = await api("/results/settings", {
        method: "PUT",
        body: JSON.stringify({ avg_ticket_cop: Number(digits) }),
      });
      resultsLoadedAt = Date.now();
      resultsEditingTicket = false;
      renderResults();
    } catch (err) {
      status.textContent = err.message || "No se pudo guardar";
    }
  }

  document.addEventListener("submit", (e) => {
    const form = e.target.closest?.(".results-ticket");
    if (!form) return;
    e.preventDefault();
    saveResultsTicket(form);
  });

  document.addEventListener("click", (e) => {
    if (e.target.closest?.("[data-results-edit]")) {
      const slot = e.target.closest(".results-slot");
      resultsEditingTicket = true;
      renderResults();
      slot?.querySelector(".results-ticket input")?.focus();
    } else if (e.target.closest?.("[data-results-cancel]")) {
      resultsEditingTicket = false;
      renderResults();
    }
  });

  const MODE_PANELS = {
    ai: "ai-setup-panel",
    appointments: "appointments-panel",
    inventory: "inventory-panel",
    orders: "orders-panel",
  };

  function switchPanelMode(mode) {
    const changed = state.panelMode !== mode;
    state.panelMode = mode;
    const isChats = mode === "chats";
    const isAppointments = mode === "appointments";
    const isInventory = mode === "inventory";
    const isOrders = mode === "orders";
    const isAi = mode === "ai";

    $("mode-chats").classList.toggle("active", isChats);
    $("mode-orders").classList.toggle("active", isOrders);
    $("mode-appointments").classList.toggle("active", isAppointments);
    $("mode-inventory").classList.toggle("active", isInventory);
    $("mode-ai").classList.toggle("active", isAi);
    $("sidebar-chats").classList.toggle("hidden", !isChats);
    $("chat-area").classList.toggle("ai-setup-mode", isAi);
    $("chat-area").classList.toggle("appointments-mode", isAppointments || isInventory || isOrders);
    $("ai-setup-panel").classList.toggle("hidden", !isAi);
    $("appointments-panel").classList.toggle("hidden", !isAppointments);
    $("inventory-panel").classList.toggle("hidden", !isInventory);
    $("orders-panel").classList.toggle("hidden", !isOrders);

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
      if (isInventory) loadInventoryPanel();
      if (isOrders) loadOrders();
      if (isAppointments) {
        loadAppointmentsPanel();
        loadResults();
      }
    }
    renderQuickShortcuts();
    renderStatusStrip();
    if (changed) {
      if (isChats) {
        animateIn($("sidebar-chats"), { y: 6, duration: 220 });
        if (!isPhoneLayout()) animateIn(state.activeId ? activeChat : emptyChat, { y: 6, duration: 220 });
      } else {
        animateIn($(MODE_PANELS[mode]), { y: 8, duration: 240 });
      }
    }
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

  function setScheduleStatus(text) {
    setStatusText("schedule-save-status", text);
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
    state.onboarding.bookingEnabled = !!schedule.ai_booking_enabled;
    state.onboarding.bookingAllowed = schedule.ai_booking_allowed !== false;
    renderOnboarding();
    const bookingBox = $("schedule-ai-booking");
    if (bookingBox) {
      const allowed = schedule.ai_booking_allowed !== false;
      bookingBox.checked = allowed && !!schedule.ai_booking_enabled;
      bookingBox.disabled = !allowed;
      bookingBox.title = allowed ? "" : "Tu plan no incluye que la IA agende citas. Escríbenos para activarlo.";
    }
    if ($("schedule-reminders") && schedule.appointment_reminders !== undefined) {
      $("schedule-reminders").checked = !!schedule.appointment_reminders;
    }
    if ($("schedule-staff-label") && schedule.staff_label !== undefined) {
      $("schedule-staff-label").value = schedule.staff_label || "";
    }
    renderBookingOffHint();
  }

  function renderBookingOffHint() {
    const box = $("schedule-ai-booking");
    const off = !!box && !box.disabled && !box.checked && (state.staff || []).length > 0;
    $("schedule-ai-off")?.classList.toggle("hidden", !off);
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
          m.phone ? '<span class="staff-tag" title="Recibe aviso de sus citas por WhatsApp">Aviso</span>' : "",
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
    renderBookingOffHint();
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
          statusHtml = '<span class="appointments-slot-status">Libre<span class="appointments-slot-add" aria-hidden="true">+ Agendar</span></span>';
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
    const stopSkel = showSkeleton(list, "list", 5);
    setAppointmentsStatus("");

    try {
      const staffParam = state.appointmentsStaffId
        ? `&staff_id=${encodeURIComponent(state.appointmentsStaffId)}`
        : "";
      const day = await api(`/appointments/day?day=${encodeURIComponent(iso)}${staffParam}`);
      stopSkel();
      if (state.appointmentsDate !== iso) return;
      state.appointmentsDay = day;
      state.appointmentsStaffId = day.staff_id || null;
      renderStaffChips(day);
      renderAppointmentsSlots(day);
    } catch (err) {
      stopSkel();
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
    enhancePhoneInput($("staff-phone"));
    setPhoneValue($("staff-phone"), member?.phone || "");
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
      phone: getPhoneValue($("staff-phone")) || null,
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
      const bookingWasOn = !!state.onboarding.bookingEnabled;
      if (!id) fillScheduleForm(await api("/appointments/schedule").catch(() => null));
      await refreshAfterStaffChange();
      const bookingTurnedOn = !id && !bookingWasOn && !!state.onboarding.bookingEnabled;
      if (bookingTurnedOn) refreshReadinessSoon();
      setScheduleStatus(
        id
          ? `✓ ${saved.name} actualizado`
          : bookingTurnedOn
            ? `✓ ${saved.name} agregado · la IA ya agenda citas con tu equipo`
            : `✓ ${saved.name} agregado al equipo`
      );
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
    enhancePhoneInput($("appointment-phone"));
    setPhoneValue($("appointment-phone"), appointment?.client_phone || "");
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
        appointment_reminders: !!$("schedule-reminders")?.checked,
        staff_label: $("schedule-staff-label")?.value?.trim() || "",
      };
      const saved = await api("/appointments/schedule", { method: "PUT", body: JSON.stringify(payload) });
      fillScheduleForm(saved);
      setScheduleStatus(saved.ai_booking_enabled ? "✓ Guardado · la IA agenda citas" : "✓ Horario guardado");
      refreshReadinessSoon();
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
      client_phone: getPhoneValue($("appointment-phone")) || null,
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
      if (err.status === 409) {
        showAppointmentFormError(`Espacio tomado. ${err.message}. Elige otra hora.`);
        loadAppointmentsDay(state.appointmentsDate).catch(() => {});
        return;
      }
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
    "biz-payments",
    "biz-delivery",
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
      payment_methods: $("biz-payments")?.value.trim() || "",
      delivery_info: $("biz-delivery")?.value.trim() || "",
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
    document.querySelector(".topbar")?.classList.toggle("has-placeholder-name", !!isPlaceholder && state.canManageGlobal);
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
    $("biz-payments").value = profile?.payment_methods || "";
    $("biz-delivery").value = profile?.delivery_info || "";
    $("biz-hours").value = profile?.location_hours || "";
    $("biz-tone").value = profile?.tone || "";
    $("biz-restrictions").value = profile?.restrictions || "";
    if (["biz-target", "biz-prices", "biz-tone", "biz-restrictions"].some((id) => $(id).value.trim())) $("biz-more").open = true;
    renderBizSummary(profile);
    renderReadiness();
    updateAiSetupControls();
  }

  function listEs(items) {
    if (items.length < 2) return items.join("");
    return `${items.slice(0, -1).join(", ")} y ${items[items.length - 1]}`;
  }

  function missingReadiness() {
    const r = state.aiProfile?.readiness;
    return r ? r.items.filter((i) => !i.done) : [];
  }

  function renderReadiness() {
    syncOrdersTab();
    syncAutostart();
    const r = state.aiProfile?.readiness;
    const card = $("ai-ready-card");
    card.classList.toggle("hidden", !r);
    if (r) {
      card.classList.toggle("is-ready", r.ready);
      $("ai-ready-title").textContent = r.ready ? "Tu IA está lista para vender" : "¿Tu IA está lista para vender?";
      $("ai-ready-count").textContent = `${r.done} de ${r.total}`;
      $("ai-ready-note").textContent = r.ready
        ? "Tiene lo que necesita para responder y cerrar sin dejar a nadie esperando. Si te preguntan algo nuevo, te lo anotamos abajo para que se lo enseñes."
        : state.tenant?.ai_autostart_pending
          ? "Al completarlos, tu IA se enciende sola y empieza a vender."
          : "Con estos datos tu IA responde y cierra la venta sin decir «te confirmo». Complétalos para poder encenderla.";
      const nextItem = r.items.find((i) => !i.done);
      $("ai-ready-list").innerHTML = r.items
        .map(
          (i) => `
          <li class="ai-ready-item${i.done ? " is-done" : ""}${i === nextItem ? " is-next" : ""}">
            <span class="ai-ready-mark" aria-hidden="true">${i.done ? "✓" : ""}</span>
            <span class="ai-ready-text"><strong>${escapeHtml(i.label)}</strong>${i.done ? "" : `<span>${escapeHtml(i.hint)}</span>`}</span>
            ${i.done ? "" : i.action === "import-catalog"
              ? '<button type="button" class="link-btn ai-ready-import">Importar mi catálogo</button>'
              : `<button type="button" class="link-btn ai-ready-go" data-field="${escapeHtml(i.field)}">Completar</button>`}
          </li>`
        )
        .join("");
    }
    renderSalesTips();
    renderReadyBanner();
  }

  const TIP_ACTIONS = {
    alerts: "Poner mi número",
    inventory: "Ir a Inventario",
    "booking-off": "Apagar citas",
    "try-ai": "Probar ahora",
  };

  function renderSalesTips() {
    const tips = state.canManageGlobal ? state.aiProfile?.tips || [] : [];
    $("ai-tips").classList.toggle("hidden", !tips.length);
    $("ai-tips-list").innerHTML = tips
      .map(
        (t) => `
        <li class="ai-ready-item is-tip">
          <span class="ai-ready-mark" aria-hidden="true">!</span>
          <span class="ai-ready-text"><strong>${escapeHtml(t.label)}</strong><span>${escapeHtml(t.hint)}</span></span>
          <button type="button" class="link-btn ai-tip-go" data-action="${escapeHtml(t.action)}">${escapeHtml(TIP_ACTIONS[t.action] || "Revisar")}</button>
        </li>`
      )
      .join("");
  }

  async function onSalesTip(action, btn) {
    if (action === "alerts") return goToReadinessField("alerts-card");
    if (action === "inventory") return switchPanelMode("inventory");
    if (action === "try-ai") return goToReadinessField("try-ai-input");
    if (action !== "booking-off") return;
    if (!confirm("¿Apagar las citas? La IA dejará de ofrecer horarios. Puedes volver a prenderlas en Citas.")) return;
    btn.disabled = true;
    try {
      const s = await api("/appointments/schedule");
      const saved = await api("/appointments/schedule", {
        method: "PUT",
        body: JSON.stringify({
          open_time: s.open_time,
          close_time: s.close_time,
          slot_minutes: s.slot_minutes,
          ai_booking_enabled: false,
        }),
      });
      fillScheduleForm(saved);
      refreshReadinessSoon();
    } catch (err) {
      btn.disabled = false;
      alert(err.message);
    }
  }

  const tryAi = { turns: [], busy: false };

  function tryAiAttachmentHtml(a) {
    if (!a) return "";
    if (a.type === "image") return `<div class="try-ai-photos"><img src="${escapeHtml(a.url)}" alt="${escapeHtml(a.label)}" loading="lazy" /></div>`;
    if (a.type === "text") return `<div class="msg in try-ai-msg">${escapeHtml(a.text)}</div>`;
    return `<div class="msg in try-ai-msg">📄 ${escapeHtml(a.file_name || a.label)}</div>`;
  }

  function renderTryAi() {
    const box = $("try-ai-messages");
    box.innerHTML =
      tryAi.turns
        .map((t) => {
          if (t.from === "client") return `<div class="msg out try-ai-msg">${escapeHtml(t.text)}</div>`;
          const photos = (t.photos || []).map((u) => `<img src="${escapeHtml(u)}" alt="" loading="lazy" />`).join("");
          return (
            `<div class="msg in try-ai-msg">${escapeHtml(t.text)}</div>` +
            (photos ? `<div class="try-ai-photos">${photos}</div>` : "") +
            tryAiAttachmentHtml(t.attachment) +
            (t.notes || []).map((n) => `<p class="try-ai-note">${escapeHtml(n)}</p>`).join("")
          );
        })
        .join("") + (tryAi.busy ? '<div class="msg in try-ai-msg try-ai-typing">Escribiendo…</div>' : "");
    box.classList.toggle("hidden", !box.innerHTML);
    box.scrollTop = box.scrollHeight;
    $("try-ai-reset").classList.toggle("hidden", !tryAi.turns.length || tryAi.busy);
    $("try-ai-send").disabled = tryAi.busy;
  }

  async function sendTryAi(raw) {
    const text = (raw || "").trim();
    if (!text || tryAi.busy) return;
    const input = $("try-ai-input");
    tryAi.turns.push({ from: "client", text });
    tryAi.busy = true;
    input.value = "";
    $("try-ai-status").textContent = "";
    renderTryAi();
    try {
      const res = await api(
        "/outbound/try-ai",
        { method: "POST", body: JSON.stringify({ turns: tryAi.turns.slice(-20).map((t) => ({ from: t.from, text: t.text })) }) },
        60000
      );
      tryAi.turns.push({ from: "ai", text: res.reply, photos: res.photos, attachment: res.attachment, notes: res.notes });
      if ((state.aiProfile?.tips || []).some((t) => t.key === "try")) refreshReadinessSoon();
    } catch (err) {
      tryAi.turns.pop();
      input.value = text;
      $("try-ai-status").textContent = err.message || "No se pudo probar. Intenta otra vez.";
    } finally {
      tryAi.busy = false;
      renderTryAi();
      input.focus({ preventScroll: true });
    }
  }

  function renderReadyBanner() {
    renderAvisosDot();
    renderSetupGuide();
  }

  function goToReadinessField(fieldId) {
    if (fieldId === "inventory") {
      switchPanelMode("inventory");
      return;
    }
    if (state.panelMode !== "ai") switchPanelMode("ai");
    const target = (fieldId && $(fieldId)) || $("ai-ready-card");
    const folded = target.closest("details");
    if (folded) folded.open = true;
    revealAiGroup(target);
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    if (target.focus) target.focus({ preventScroll: true });
  }

  let readinessTimer = null;
  function refreshReadinessSoon() {
    clearTimeout(readinessTimer);
    readinessTimer = setTimeout(async () => {
      try {
        state.aiProfile = await api("/outbound/business-profile");
        renderReadiness();
      } catch (_) {
        /* se vuelve a revisar al abrir Personalizar IA */
      }
    }, 600);
  }

  function setAiQuestionsStatus(text) {
    setStatusText("ai-questions-status", text);
  }

  function renderAiQuestionsBadge() {
    const open = (state.aiQuestions || []).filter((q) => q.status === "open").length;
    const badge = $("ai-questions-badge");
    badge.textContent = String(open);
    badge.classList.toggle("hidden", !open || !state.canManageGlobal);
    $("ai-respuestas-badge").textContent = String(open);
    $("ai-respuestas-badge").classList.toggle("hidden", !open);
  }

  function renderAvisosDot() {
    const ownNumber = (state.aiProfile?.tips || []).some((t) => t.key === "alerts-own");
    $("ai-avisos-dot").classList.toggle("hidden", !(ownNumber || state.onboarding.alertCount === 0));
  }

  function showAiSection(name, { animate = true } = {}) {
    const panel = $("ai-setup-panel");
    if (!name || panel.dataset.section === name) return;
    panel.dataset.section = name;
    panel.querySelectorAll(".ai-section-tab").forEach((b) => b.classList.toggle("active", b.dataset.aiSection === name));
    panel.scrollTop = 0;
    if (animate) {
      panel.querySelectorAll(`[data-ai-group="${name}"]`).forEach((el, i) =>
        animateIn(el, { y: 8, duration: 240, delay: i * 40 })
      );
    }
  }

  function revealAiGroup(el) {
    showAiSection(el?.closest?.("[data-ai-group]")?.dataset.aiGroup, { animate: false });
  }

  const PHOTO_WORDS = /fot|imag|pic|se ve|luce/i;

  function asksForPhotos(q) {
    return PHOTO_WORDS.test(`${q.topic} ${q.question || ""}`);
  }

  function plainWords(text) {
    return (text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().match(/\w{3,}/g) || [];
  }

  function guessProductFor(q) {
    const said = new Set(plainWords(`${q.topic} ${q.question || ""}`));
    let best = null;
    let bestScore = 0;
    for (const p of state.products || []) {
      const score = plainWords(p.name).filter((w) => said.has(w)).length;
      if (score > bestScore) [best, bestScore] = [p, score];
    }
    return best;
  }

  function aiQuestionPhotoBlock(q) {
    const products = state.products || [];
    if (!products.length) {
      return `<p class="ai-question-photo muted small">¿Pidió fotos? Agrega ese producto en <button type="button" class="link-btn ai-question-inventory">Inventario</button> y súbelas ahí: la IA las mandará sola.</p>`;
    }
    const guess = guessProductFor(q);
    const options = products
      .map((p) => `<option value="${escapeHtml(p.id)}"${guess?.id === p.id ? " selected" : ""}>${escapeHtml(p.name)}</option>`)
      .join("");
    return `
      <div class="ai-question-photo">
        <span class="small">Responde con fotos: quedan guardadas en</span>
        <select class="ai-question-product" aria-label="Producto de la foto">${options}</select>
        <label class="btn ghost small ai-question-photo-btn">Subir foto
          <input type="file" class="ai-question-photo-input" accept="image/jpeg,image/png,image/webp" hidden />
        </label>
      </div>`;
  }

  async function answerQuestionWithPhoto(item, file) {
    const productId = item.querySelector(".ai-question-product")?.value;
    if (!file || !productId) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("product_id", productId);
    item.querySelectorAll("button, select, input").forEach((el) => { el.disabled = true; });
    setAiQuestionsStatus("Subiendo foto…");
    try {
      await api(`/ai-questions/${item.dataset.id}/photo`, { method: "POST", body: fd }, 30000);
      setAiQuestionsStatus("✓ Listo — la próxima vez que la pidan, la IA manda la foto sola");
      state.products = await api("/products").catch(() => state.products);
      await loadAiQuestions();
    } catch (err) {
      setAiQuestionsStatus(err.message);
      item.querySelectorAll("button, select, input").forEach((el) => { el.disabled = false; });
    }
  }

  function aiQuestionRow(q) {
    const ro = !state.canManageGlobal;
    const learned = q.status === "answered";
    const times = q.times_asked > 1 ? `Te lo han preguntado ${q.times_asked} veces` : "";
    return `
      <li class="ai-question${learned ? " is-learned" : ""}" data-id="${escapeHtml(q.id)}">
        <div class="ai-question-head">
          <strong>${escapeHtml(q.topic)}</strong>
          ${times ? `<span class="ai-question-times">${times}</span>` : ""}
        </div>
        ${q.question ? `<p class="ai-question-quote">«${escapeHtml(q.question)}»</p>` : ""}
        ${!learned && !ro && asksForPhotos(q) ? aiQuestionPhotoBlock(q) : ""}
        <textarea rows="2" maxlength="1000" placeholder="Escribe la respuesta como se la darías a un cliente" ${ro ? "disabled" : ""}>${escapeHtml(q.answer || "")}</textarea>
        <div class="ai-question-actions">
          <button type="button" class="btn primary small ai-question-save" ${ro ? "disabled" : ""}>${learned ? "Actualizar" : "Enseñar a la IA"}</button>
          <button type="button" class="btn ghost small ai-question-delete" ${ro ? "disabled" : ""}>${learned ? "Olvidar" : "Descartar"}</button>
        </div>
      </li>`;
  }

  function renderAiQuestions() {
    renderAiQuestionsBadge();
    const rows = state.aiQuestions || [];
    const open = rows.filter((q) => q.status === "open");
    const learned = rows.filter((q) => q.status === "answered");
    $("ai-questions-list").innerHTML = open.length
      ? open.map(aiQuestionRow).join("")
      : '<li class="muted">Por ahora la IA ha podido responder todo lo que le preguntan.</li>';
    $("ai-learned-wrap").classList.toggle("hidden", !learned.length);
    $("ai-learned-summary").textContent = `Lo que la IA ya aprendió (${learned.length})`;
    $("ai-learned-list").innerHTML = learned.map(aiQuestionRow).join("");
  }

  async function loadAiQuestions({ badgeOnly = false } = {}) {
    try {
      state.aiQuestions = await api("/ai-questions");
    } catch (err) {
      if (!badgeOnly) setAiQuestionsStatus(err.message);
      return;
    }
    const needsProducts = state.aiQuestions.some((q) => q.status === "open" && asksForPhotos(q));
    if (!badgeOnly && needsProducts && !state.products && state.canManageGlobal) {
      state.products = await api("/products").catch(() => []);
    }
    if (badgeOnly) renderAiQuestionsBadge();
    else renderAiQuestions();
  }

  async function saveAiQuestion(item) {
    const input = item.querySelector("textarea");
    const answer = input.value.trim();
    if (!answer) {
      input.focus();
      setAiQuestionsStatus("Escribe la respuesta para que la IA la aprenda");
      return;
    }
    item.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    setAiQuestionsStatus("Guardando…");
    try {
      await api(`/ai-questions/${item.dataset.id}`, { method: "PUT", body: JSON.stringify({ answer }) });
      setAiQuestionsStatus("✓ Listo — la IA ya lo sabe y lo usará en todos tus chats");
      await loadAiQuestions();
    } catch (err) {
      setAiQuestionsStatus(err.message);
      item.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  }

  async function deleteAiQuestion(item) {
    const learned = item.classList.contains("is-learned");
    try {
      await api(`/ai-questions/${item.dataset.id}`, { method: "DELETE" });
      setAiQuestionsStatus(learned ? "La IA olvidó esa respuesta" : "Pregunta descartada");
      await loadAiQuestions();
    } catch (err) {
      setAiQuestionsStatus(err.message);
    }
  }

  function renderBizSummary(profile) {
    const list = $("biz-summary-list");
    if (!list) return;
    const sells = Boolean(profile?.payment_methods);
    $("biz-preview-title").textContent = sells ? "Tu IA venderá sola sabiendo esto" : "Tu IA atenderá sabiendo esto";
    $("biz-preview-note").textContent = sells
      ? "Responde, envía tu catálogo, toma el pedido con la dirección y comparte cómo pagarte. Te avisa a los números de «Alertas por WhatsApp» (abajo) cuando entra un pedido y cuando llega el comprobante."
      : "Saluda, responde preguntas, envía atajos (menú, fotos…) y te pasa el chat cuando quieran comprar. Cuéntale cómo te pagan y cerrará la venta sola.";
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
    ["alert-threshold", "alert-save-btn", "alert-test-btn", "followup-hours"].forEach((id) => {
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
    document.querySelectorAll("#alert-recipients .alert-phone").forEach(enhancePhoneInput);
    updateAlertAddButton();
  }

  function readAlertRecipients() {
    return [...document.querySelectorAll("#alert-recipients .alert-recipient")].map((row) => ({
      name: row.querySelector(".alert-name").value.trim(),
      phone: getPhoneValue(row.querySelector(".alert-phone")),
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
    setStatusText("alert-save-status", text);
  }

  const OWN_ALERT_NUMBER =
    "Ese es el WhatsApp de tu negocio: el aviso te llegaría como un mensaje a ti mismo, sin sonar, " +
    "y no te enterarías. Pon tu número personal o el de quien atiende.";

  function isBusinessNumber(phone) {
    const own = phoneTailDigits(state.wa?.phone_number);
    return Boolean(own) && phoneTailDigits(phone) === own;
  }

  function checkAlertRecipients() {
    const clashes = [];
    document.querySelectorAll("#alert-recipients .alert-recipient").forEach((row) => {
      const input = row.querySelector(".alert-phone");
      const own = isBusinessNumber(getPhoneValue(input));
      row.classList.toggle("is-own-number", own);
      input.setAttribute("aria-invalid", own ? "true" : "false");
      if (own) clashes.push(input);
    });
    $("alert-warning").textContent = clashes.length ? OWN_ALERT_NUMBER : "";
    $("alert-warning").classList.toggle("hidden", !clashes.length);
    return clashes;
  }

  function fillInterestAlert(cfg) {
    renderAlertRecipients(cfg?.recipients || []);
    $("alert-warning").textContent = cfg?.warning || "";
    $("alert-warning").classList.toggle("hidden", !cfg?.warning);
    if (state.wa?.phone_number) checkAlertRecipients();
    if (cfg) {
      state.onboarding.alertCount = (cfg.recipients || []).length;
      renderOnboarding();
      renderAvisosDot();
    }
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
    const clashes = checkAlertRecipients();
    if (clashes.length) {
      const field = clashes[0].closest(".phone-field") || clashes[0];
      field.scrollIntoView({ behavior: "smooth", block: "center" });
      clashes[0].focus({ preventScroll: true });
      if (!reducedMotion.matches) {
        field.animate(
          [{ transform: "none" }, { transform: "translateX(-6px)" }, { transform: "translateX(5px)" },
            { transform: "translateX(-3px)" }, { transform: "none" }],
          { duration: 320, easing: "ease-out" }
        );
      }
      setAlertStatus("");
      return;
    }
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
      refreshReadinessSoon();
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

  function setFollowupStatus(text) {
    setStatusText("followup-status", text);
  }

  async function loadFollowup() {
    setFollowupStatus("");
    try {
      const cfg = await api("/tenants/me/followup");
      $("followup-hours").value = String(cfg.followup_hours);
    } catch (err) {
      setFollowupStatus(err.message);
    }
  }

  async function saveFollowup() {
    if (!state.canManageGlobal) return;
    const select = $("followup-hours");
    select.disabled = true;
    setFollowupStatus("Guardando…");
    try {
      const cfg = await api("/tenants/me/followup", {
        method: "PUT",
        body: JSON.stringify({ followup_hours: Number(select.value) }),
      });
      select.value = String(cfg.followup_hours);
      const h = cfg.followup_hours;
      setFollowupStatus(
        h ? `✓ Guardado — le escribe una vez a las ${h} hora${h === 1 ? "" : "s"} sin respuesta` : "✓ Seguimiento apagado"
      );
    } catch (err) {
      setFollowupStatus(err.message);
      await loadFollowup();
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
    setStatusText("biz-save-status", text);
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
    await Promise.all([loadQuickShortcuts(), loadInterestAlert(), loadFollowup(), loadAiQuestions()]);
    renderAiShortcutsSummary();
  }

  async function loadInventoryPanel() {
    setProductsStatus("");
    await Promise.all([loadQuickShortcuts(), loadProducts()]);
  }

  async function loadProducts() {
    let stopSkel = () => {};
    if (!state.productsLoaded) {
      $("products-empty").classList.add("hidden");
      stopSkel = showSkeleton($("products-list"), "list", 4, 0);
    }
    try {
      state.products = await api("/products");
    } catch {
      state.products = [];
    }
    stopSkel();
    state.productsLoaded = true;
    renderProducts();
  }

  async function loadOrders() {
    if (!state.orders) {
      $("orders-empty").classList.add("hidden");
      showSkeleton($("orders-ready"), "cards", 2, 0);
      showSkeleton($("orders-pending"), "cards", 1, 0);
    }
    try {
      state.orders = await api("/orders");
    } catch {
      state.orders = state.orders || [];
    }
    renderOrders();
  }

  function setOrdersStatus(text) {
    setStatusText("orders-status", text);
  }

  function orderCard(o) {
    const who = o.customer || o.contact_name || o.contact_phone || "Cliente";
    const rows = [
      ["Total", o.total],
      ["Entrega", o.delivery],
      ["Pago", o.payment],
      ["Guía", o.tracking],
    ]
      .filter(([, v]) => v)
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${escapeHtml(v)}</dd></div>`)
      .join("");
    const when = o.status === "dispatched" ? `Despachado ${formatTime(o.dispatched_at)}` : formatTime(o.created_at);
    const proof = o.payment_proof && o.status === "ready"
      ? '<span class="order-flag">Comprobante sin verificar</span>'
      : "";
    const chat = o.conversation_id
      ? `<button type="button" class="btn ghost small" data-order-chat="${o.conversation_id}">Ver chat</button>`
      : "";
    let actions = chat;
    if (state.canWrite && o.status === "ready") {
      actions = `<button type="button" class="btn primary small" data-order-dispatch="${o.id}">Marcar despachado</button>${chat}<button type="button" class="btn ghost small" data-order-cancel="${o.id}">Cancelar</button>`;
    } else if (state.canWrite && o.status === "pending") {
      actions = `<button type="button" class="btn ghost small" data-order-paid="${o.id}">Ya pagó</button>${chat}<button type="button" class="btn ghost small" data-order-cancel="${o.id}">Cancelar</button>`;
    }
    return `
      <article class="order-card${o.status === "cancelled" ? " is-cancelled" : ""}" data-order-id="${o.id}">
        <header class="order-head">
          <strong>${escapeHtml(o.items)}</strong>
          <span class="muted small">${escapeHtml(when)}</span>
        </header>
        <p class="order-who">${escapeHtml(who)}${o.contact_phone && o.contact_phone !== who ? ` · ${escapeHtml(o.contact_phone)}` : ""} ${proof}</p>
        ${rows ? `<dl class="order-details">${rows}</dl>` : ""}
        <div class="order-dispatch hidden">
          <input type="text" maxlength="120" placeholder="Número de guía (opcional)" />
          <label><input type="checkbox" checked /> Avisarle al cliente por WhatsApp</label>
          <div class="alert-actions">
            <button type="button" class="btn primary small" data-order-confirm="${o.id}">Confirmar despacho</button>
            <button type="button" class="btn ghost small" data-order-back>Volver</button>
          </div>
        </div>
        <div class="alert-actions order-actions">${actions}</div>
      </article>`;
  }

  function renderOrders() {
    const orders = state.orders || [];
    const groups = {
      ready: orders.filter((o) => o.status === "ready"),
      pending: orders.filter((o) => o.status === "pending"),
      done: orders.filter((o) => o.status === "dispatched" || o.status === "cancelled"),
    };
    for (const [key, list] of Object.entries(groups)) {
      $(`orders-${key}`).innerHTML = list.map(orderCard).join("");
      $(`orders-${key}-empty`).classList.toggle("hidden", list.length > 0);
    }
    renderOrdersEmpty(orders.length === 0);
    $("orders-ready-count").textContent = groups.ready.length ? `(${groups.ready.length})` : "";
    $("orders-pending-count").textContent = groups.pending.length ? `(${groups.pending.length})` : "";
    const badge = $("orders-badge");
    badge.textContent = String(groups.ready.length);
    badge.classList.toggle("hidden", !groups.ready.length);
    syncOrdersTab();
  }

  let ordersEmptyAction = null;
  function renderOrdersEmpty(empty) {
    $("orders-empty").classList.toggle("hidden", !empty);
    document.querySelectorAll("#orders-panel .orders-section").forEach((el) => el.classList.toggle("hidden", empty));
    if (!empty) return;
    const off = aiOffReason();
    const aiOn = !off && !!state.tenant?.ai_global_enabled;
    let text = "Todavía no hay pedidos. En cuanto tu IA cierre la primera venta, aparece aquí.";
    ordersEmptyAction = null;
    if (off) {
      text = `Por ahora no pueden llegar pedidos: ${(off.short || off.title).replace(/\.$/, "").toLowerCase()}.`;
      ordersEmptyAction = off.action || null;
    } else if (!aiOn) {
      text = "Por ahora no pueden llegar pedidos: tu IA está apagada.";
    } else if (state.canManageGlobal) {
      text = "Todavía no hay pedidos. Pruébalo tú: escríbele a tu IA como si fueras un cliente que quiere comprar.";
      ordersEmptyAction = { label: "Probar mi IA", run: () => goToReadinessField("try-ai-input") };
    }
    $("orders-empty-text").textContent = text;
    const btn = $("orders-empty-btn");
    btn.classList.toggle("hidden", !ordersEmptyAction);
    btn.textContent = ordersEmptyAction?.label || "";
  }
  $("orders-empty-btn").addEventListener("click", () => ordersEmptyAction?.run());
  conversationList.addEventListener("click", (e) => {
    if (e.target.closest(".list-empty-btn")) listEmptyAction?.run();
  });

  function syncOrdersTab() {
    const sells = Boolean(state.aiProfile?.payment_methods);
    $("mode-orders").classList.toggle("hidden", !sells && !(state.orders || []).length);
  }

  async function updateOrder(id, payload) {
    setOrdersStatus("Guardando…");
    try {
      const saved = await api(`/orders/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
      state.orders = (state.orders || []).map((o) => (o.id === saved.id ? saved : o));
      renderOrders();
      if (saved.status === "dispatched") {
        setOrdersStatus(saved.customer_notified ? "Despachado. Ya le avisamos al cliente." : "Despachado.");
      } else {
        setOrdersStatus("");
      }
    } catch (err) {
      setOrdersStatus(err.message || "No se pudo guardar. Intenta de nuevo.");
    }
  }

  function onOrdersClick(e) {
    const btn = e.target.closest("button");
    if (!btn) return;
    const card = btn.closest(".order-card");
    const dispatchBox = card?.querySelector(".order-dispatch");
    if (btn.dataset.orderChat) {
      if (!state.conversations.some((c) => c.id === btn.dataset.orderChat)) {
        setOrdersStatus("Ese chat ya no está en tu lista (puede estar archivado).");
        return;
      }
      switchPanelMode("chats");
      selectConversation(btn.dataset.orderChat);
    } else if (btn.dataset.orderDispatch) {
      dispatchBox.classList.remove("hidden");
      card.querySelector(".order-actions").classList.add("hidden");
      dispatchBox.querySelector("input[type=text]").focus();
    } else if (btn.hasAttribute("data-order-back")) {
      dispatchBox.classList.add("hidden");
      card.querySelector(".order-actions").classList.remove("hidden");
    } else if (btn.dataset.orderConfirm) {
      btn.disabled = true;
      updateOrder(btn.dataset.orderConfirm, {
        status: "dispatched",
        tracking: dispatchBox.querySelector("input[type=text]").value.trim(),
        notify_customer: dispatchBox.querySelector("input[type=checkbox]").checked,
      });
    } else if (btn.dataset.orderPaid) {
      updateOrder(btn.dataset.orderPaid, { status: "ready" });
    } else if (btn.dataset.orderCancel) {
      if (!confirm("¿Cancelar este pedido? Las unidades no se devuelven solas al inventario.")) return;
      updateOrder(btn.dataset.orderCancel, { status: "cancelled" });
    }
  }

  function formatThousands(n) {
    return Number(n).toLocaleString("es-CO", { maximumFractionDigits: 0 });
  }

  function stockClass(stock) {
    if (stock === 0) return " is-out";
    return stock != null && stock <= 3 ? " is-low" : "";
  }

  function productRow(p) {
    const ro = state.canManageGlobal ? "" : " disabled";
    return `
      <div class="product-row${stockClass(p.stock)}" data-id="${escapeHtml(p.id || "")}">
        <input type="text" class="product-name" maxlength="120" placeholder="Ej: Tenis blancos" aria-label="Producto" value="${escapeHtml(p.name || "")}"${ro} />
        <input type="text" class="product-price" inputmode="numeric" placeholder="Precio" aria-label="Precio" value="${p.price_cop != null ? "$" + formatThousands(p.price_cop) : ""}"${ro} />
        <input type="number" class="product-stock" min="0" step="1" placeholder="Sin contar" aria-label="Cuántos quedan" value="${p.stock ?? ""}"${ro} />
        <input type="text" class="product-details" maxlength="500" placeholder="Tallas, colores, sabores…" aria-label="Detalles" value="${escapeHtml(p.details || "")}"${ro} />
        <button type="button" class="alert-remove product-remove" aria-label="Quitar producto" title="Quitar"${ro}>×</button>
        <div class="product-photos">${productPhotosHtml(p)}</div>
      </div>`;
  }

  const MAX_PRODUCT_PHOTOS = 4;

  function productPhotoUrl(name) {
    return `${API}/outbound/assets/${encodeURIComponent(name)}`;
  }

  function productPhotosHtml(p, newName = "") {
    const photos = p.photos || [];
    const thumbs = photos
      .map((name) => `
        <span class="product-photo${name === newName ? " is-new" : ""}">
          <button type="button" class="product-photo-view" data-name="${escapeHtml(name)}" aria-label="Ver foto de ${escapeHtml(p.name || "")}" title="Ver en grande">
            <img src="${productPhotoUrl(name)}" alt="" loading="lazy" />
          </button>
          ${state.canManageGlobal ? `<button type="button" class="product-photo-remove" data-name="${escapeHtml(name)}" aria-label="Quitar foto" title="Quitar foto">×</button>` : ""}
        </span>`)
      .join("");
    const canAdd = state.canManageGlobal && photos.length < MAX_PRODUCT_PHOTOS;
    const add = canAdd
      ? `<label class="product-photo-add" title="La IA la manda cuando el cliente pida verlo">${photos.length ? "Otra foto" : "Agregar foto"}
          <input type="file" class="product-photo-input" accept="image/jpeg,image/png,image/webp" multiple hidden />
        </label>`
      : "";
    return thumbs + add;
  }

  function renderProductPhotos(row, product, newName = "") {
    row.querySelector(".product-photos").innerHTML = productPhotosHtml(product, newName);
  }

  function openPhotoViewer(src, caption) {
    let viewer = $("photo-viewer");
    if (!viewer) {
      viewer = document.createElement("div");
      viewer.id = "photo-viewer";
      viewer.className = "photo-viewer hidden";
      viewer.setAttribute("role", "dialog");
      viewer.setAttribute("aria-modal", "true");
      viewer.innerHTML = `
        <figure class="photo-viewer-card">
          <img alt="" />
          <figcaption></figcaption>
        </figure>
        <button type="button" class="photo-viewer-close" aria-label="Cerrar">×</button>`;
      viewer.addEventListener("click", (e) => {
        if (!e.target.closest(".photo-viewer-card img")) viewer.classList.add("hidden");
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") viewer.classList.add("hidden");
      });
      document.body.appendChild(viewer);
    }
    viewer.querySelector("img").src = src;
    viewer.querySelector("figcaption").textContent = caption || "";
    viewer.classList.remove("hidden");
    viewer.querySelector(".photo-viewer-close").focus();
  }

  function upsertProduct(saved) {
    const items = state.products || [];
    const at = items.findIndex((p) => p.id === saved.id);
    state.products = at >= 0 ? items.map((p) => (p.id === saved.id ? saved : p)) : [...items, saved];
  }

  async function uploadProductPhotos(row, files) {
    await row._saving;
    const id = row.dataset.id;
    if (!id) {
      setProductsStatus("Escribe el nombre del producto antes de subir la foto");
      return;
    }
    let product = (state.products || []).find((p) => p.id === id);
    const room = MAX_PRODUCT_PHOTOS - (product?.photos?.length || 0);
    const queue = [...files].slice(0, room);
    for (const [i, file] of queue.entries()) {
      setProductsStatus(queue.length > 1 ? `Subiendo foto ${i + 1} de ${queue.length}…` : "Subiendo foto…");
      const fd = new FormData();
      fd.append("file", file);
      try {
        product = await api(`/products/${id}/photos`, { method: "POST", body: fd }, 30000);
      } catch (err) {
        setProductsStatus(err.message);
        break;
      }
      upsertProduct(product);
      renderProductPhotos(row, product, product.photos[product.photos.length - 1]);
    }
    if (files.length > room) setProductsStatus(`Cada producto puede tener hasta ${MAX_PRODUCT_PHOTOS} fotos`);
    else if (product) setProductsStatus(`✓ Listo — si piden fotos de ${product.name}, la IA las manda sola`);
    renderProductsSummary();
    refreshReadinessSoon();
  }

  async function removeProductPhoto(row, name) {
    try {
      const product = await api(`/products/${row.dataset.id}/photos/${encodeURIComponent(name)}`, { method: "DELETE" });
      upsertProduct(product);
      renderProductPhotos(row, product);
      renderProductsSummary();
      refreshReadinessSoon();
      setProductsStatus("Foto quitada");
    } catch (err) {
      setProductsStatus(err.message);
    }
  }

  function renderProducts() {
    const list = $("products-list");
    if (!list) return;
    const items = state.products || [];
    list.innerHTML = items.map(productRow).join("");
    syncProductsChrome();
    $("products-add-btn").disabled = !state.canManageGlobal;
  }

  function syncProductsChrome() {
    const rows = document.querySelectorAll("#products-list .product-row").length;
    $("products-head").classList.toggle("hidden", !rows);
    $("products-empty").classList.toggle("hidden", rows > 0);
    $("products-add-btn").classList.toggle("primary", rows === 0);
    $("products-add-btn").classList.toggle("ghost", rows > 0);
    renderProductsSummary();
  }

  function renderProductsSummary() {
    const el = $("products-summary");
    if (!el) return;
    const items = state.products || [];
    const out = items.filter((p) => p.stock === 0).length;
    const low = items.filter((p) => p.stock != null && p.stock > 0 && p.stock <= 3).length;
    const unpriced = items.filter((p) => !p.price_cop).length;
    const parts = [`${items.length} producto${items.length === 1 ? "" : "s"}`];
    if (unpriced) {
      parts.push(`<span class="products-out">${unpriced} sin precio: la IA no puede cotizar${unpriced === 1 ? "lo" : "los"}</span>`);
    }
    if (out) parts.push(`<span class="products-out">${out} agotado${out === 1 ? "" : "s"}</span>`);
    if (low) parts.push(`<span class="products-low">${low} con pocas unidades</span>`);
    const noPhoto = items.filter((p) => !(p.photos || []).length).length;
    if (noPhoto) parts.push(`<span class="products-muted">${noPhoto} sin foto</span>`);
    el.innerHTML = parts.join(" · ");
    el.classList.toggle("hidden", !items.length);
  }

  function renderProductsImport() {
    const hasCatalog = (state.quickShortcuts || []).some((s) => s.content);
    $("products-import-btn")?.classList.toggle("hidden", !hasCatalog || !state.canManageGlobal);
  }

  function setProductsStatus(text) {
    setStatusText("products-status", text);
  }

  function addProductRow() {
    const list = $("products-list");
    list.insertAdjacentHTML("beforeend", productRow({}));
    syncProductsChrome();
    list.lastElementChild.querySelector(".product-name").focus();
  }

  function readProductRow(row) {
    const digits = row.querySelector(".product-price").value.replace(/\D/g, "");
    const stockRaw = row.querySelector(".product-stock").value.trim();
    const stock = stockRaw === "" ? null : Math.max(0, Math.floor(Number(stockRaw)) || 0);
    return {
      name: row.querySelector(".product-name").value.trim(),
      price_cop: digits ? Number(digits) : null,
      stock,
      details: row.querySelector(".product-details").value.trim(),
    };
  }

  async function saveProductRow(row) {
    const body = readProductRow(row);
    if (!body.name) {
      if (row.dataset.id) setProductsStatus("Escribe el nombre del producto");
      return;
    }
    setProductsStatus("Guardando…");
    try {
      const id = row.dataset.id;
      const saved = await api(id ? `/products/${id}` : "/products", {
        method: id ? "PATCH" : "POST",
        body: JSON.stringify(body),
      });
      row.dataset.id = saved.id;
      upsertProduct(saved);
      row.classList.toggle("is-out", saved.stock === 0);
      row.classList.toggle("is-low", stockClass(saved.stock) === " is-low");
      renderProductsSummary();
      row.querySelector(".product-price").value = saved.price_cop != null ? "$" + formatThousands(saved.price_cop) : "";
      setProductsStatus(saved.stock === 0 ? `✓ Guardado — ${saved.name} quedó agotado` : "✓ Guardado");
      refreshReadinessSoon();
    } catch (err) {
      setProductsStatus(err.message);
    }
  }

  function queueProductSave(row) {
    // Un producto nuevo se crea en el primer guardado: los cambios siguientes esperan su id.
    row._saving = (row._saving || Promise.resolve()).then(() => saveProductRow(row));
  }

  async function removeProductRow(row) {
    await row._saving;
    const id = row.dataset.id;
    if (id) {
      try {
        await api(`/products/${id}`, { method: "DELETE" });
      } catch (err) {
        setProductsStatus(err.message);
        return;
      }
      state.products = (state.products || []).filter((p) => p.id !== id);
    }
    row.remove();
    syncProductsChrome();
    setProductsStatus(id ? "Producto quitado" : "");
    if (id) refreshReadinessSoon();
  }

  async function importProductsFromCatalog() {
    const btn = $("products-import-btn");
    btn.disabled = true;
    setProductsStatus("Leyendo tu catálogo…");
    try {
      const res = await api("/products/import-catalog", { method: "POST" }, 20000);
      state.products = res.products;
      renderProducts();
      setProductsStatus(
        res.created
          ? `✓ Agregamos ${res.created} producto${res.created === 1 ? "" : "s"} de tu catálogo. Revisa los precios y pon cuántos quedan.`
          : "No encontramos productos nuevos con precio en tu catálogo.",
      );
      refreshReadinessSoon();
    } catch (err) {
      setProductsStatus(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function importCatalogFromReadiness() {
    switchPanelMode("inventory");
    await importProductsFromCatalog();
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
      await syncAutostart();
      const missing = missingReadiness();
      if (missing.length) {
        setBizSaveStatus(`✓ Guardado — aún falta: ${listEs(missing.map((i) => i.label.toLowerCase()))}`);
      } else {
        setBizSaveStatus(
          state.tenant?.ai_global_enabled
            ? "✓ Guardado: tu IA está encendida y ya puede vender sola"
            : "✓ Guardado: tu IA está lista. Enciéndela arriba, en IA",
        );
      }
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
    renderProductsImport();
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
      const icon = s.type === "image" ? "Foto · " : s.type === "document" ? "PDF · " : "";
      return `<button type="button" class="quick-shortcut-pill" data-shortcut-id="${escapeHtml(s.id)}" title="Enviar ${escapeHtml(s.label)}">${icon}${escapeHtml(s.label)}</button>`;
    }).join("");

    const editBtn = state.canManageGlobal
      ? `<button type="button" class="quick-shortcut-pill quick-shortcut-edit" id="edit-shortcuts-btn" title="Configurar atajos (todos los chats)">Atajos</button>`
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
          ? `<span class="small">${escapeHtml(s.file_name || "catalogo.pdf")}</span>`
          : "";
      const uploadLabel = s.uploading
        ? "Leyendo archivo…"
        : type === "image" ? "Subir foto" : "Subir PDF";
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

  function aiAllowedInChat(conv) {
    return !state.tenant || !!state.tenant.ai_global_enabled || !!conv?.ai_override_global;
  }

  function applyTenantAi(fresh) {
    if (!state.tenant || !fresh) return;
    const autostarted = !!state.tenant.ai_autostart_pending && !state.tenant.ai_global_enabled && !!fresh.ai_global_enabled;
    state.tenant.ai_autostart_pending = !!fresh.ai_autostart_pending;
    if (state.tenant.ai_global_enabled === fresh.ai_global_enabled) return;
    state.tenant.ai_global_enabled = fresh.ai_global_enabled;
    $("toggle-ai-global").checked = fresh.ai_global_enabled;
    renderReadyBanner();
    onAiGlobalChanged(fresh.ai_global_enabled);
    if (autostarted) toast("¡Tu IA ya está encendida! Desde ahora les responde a tus clientes.", { tone: "ok", duration: 6000 });
  }

  let autostartChecking = false;
  async function syncAutostart() {
    if (autostartChecking || !state.tenant?.ai_autostart_pending || state.tenant.ai_global_enabled) return;
    if (!state.aiProfile?.readiness?.ready) return;
    autostartChecking = true;
    try {
      applyTenantAi(await api("/tenants/me"));
    } catch {
      /* lo vuelve a intentar en la próxima carga */
    } finally {
      autostartChecking = false;
    }
  }

  function onAiGlobalChanged(enabled) {
    // Al prender la global, el backend borra las excepciones por chat.
    if (enabled) state.conversations.forEach((c) => (c.ai_override_global = false));
    renderAiAlerts();
    renderConversationList();
    if (state.activeId) {
      const conv = state.conversations.find((c) => c.id === state.activeId);
      if (conv) syncChatToggles(conv);
    }
  }

  function setManualMode(on) {
    const input = $("toggle-mode-manual");
    if (!input || input.checked === on || input.disabled) return;
    input.checked = on;
    input.dispatchEvent(new Event("change"));
  }

  function aiBlockReasonForConv(conv) {
    if (!conv) return null;
    const off = aiOffReason();
    if (off && off.tone !== "pause") return { text: off.short || off.title, action: off.action };
    const canEdit = state.canWrite && state.wa.status === "connected";
    if (!aiAllowedInChat(conv)) {
      return {
        text: "IA en pausa: respondes tú.",
        action: canEdit ? { label: "Que responda aquí", run: () => patchConversationAi(true) } : null,
      };
    }
    if (!conv.ai_active) {
      const personal = conv.imported_legacy && !conv.bait_sent;
      return {
        text: personal ? "Chat personal: la IA no responde." : "IA apagada en este chat.",
        action: canEdit ? { label: personal ? "Que responda aquí" : "Encender aquí", run: () => patchConversationAi(true) } : null,
      };
    }
    if (conv.mode === "manual") {
      return {
        text: "Modo manual: respondes tú.",
        action: canEdit ? { label: "Volver a la IA", run: () => setManualMode(false) } : null,
      };
    }
    if (state.canManageGlobal && state.aiProfile?.readiness && !state.aiProfile.readiness.ready) {
      return {
        text: "A tu IA le faltan datos del negocio.",
        action: { label: "Completar", run: () => goToReadinessField(missingReadiness()[0]?.field || null) },
      };
    }
    return null;
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
    renderStatusStrip();
  }

  $("chat-ai-hint").addEventListener("click", (e) => {
    if (e.target.closest(".chat-ai-hint-btn")) chatHintAction?.();
  });

  function syncChatToggles(conv) {
    $("toggle-mode-manual").checked = conv.mode === "manual";
    $("toggle-ai-chat").checked = !!conv.ai_active && aiAllowedInChat(conv);
    const canEdit = state.canWrite && state.wa.status === "connected";
    $("toggle-mode-manual").disabled = !canEdit;
    $("toggle-ai-chat").disabled = !canEdit;
    syncInterestButtons(conv);
    const canRetry = state.canWrite && conv.ai_active && aiAllowedInChat(conv) && conv.mode !== "manual";
    $("ai-retry-btn").classList.toggle("hidden", !canRetry);
    $("ai-retry-btn").disabled = !canRetry;
    const hint = aiBlockReasonForConv(conv);
    const hintEl = $("chat-ai-hint");
    chatHintAction = hint?.action?.run || null;
    if (hint) {
      hintEl.innerHTML = `<span class="chat-ai-hint-text">${escapeHtml(hint.text)}</span>${
        hint.action ? `<button type="button" class="chat-ai-hint-btn">${escapeHtml(hint.action.label)}</button>` : ""
      }`;
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
    if ((pendingImage && pendingImage.conversationId !== id) || (pendingFile && pendingFile.conversationId !== id)) {
      clearPendingImage();
    }
    cancelVoiceNote();
    if (state.activeId && state.activeId !== id) cacheMessages(state.activeId, state.messages);
    state.activeId = id;
    state.messages = [];
    state._messagesSig = "";
    const conv = state.conversations.find((c) => c.id === id);
    if (!conv) return;

    const wasOpen = !activeChat.classList.contains("hidden");
    if (isPhoneLayout() && !history.state?.omitelChat) history.pushState({ omitelChat: true }, "");
    emptyChat.classList.add("hidden");
    activeChat.classList.remove("hidden");
    messagesEl.innerHTML = "";
    if (isPhoneLayout() && !wasOpen) {
      animateIn($("chat-area"), { x: 36, opacity: 0.4, duration: 300, easing: EASE_DRAWER });
    } else if (!wasOpen) {
      animateIn(activeChat, { y: 6, duration: 200 });
    }
    $("chat-title").textContent = convTitle(conv);
    $("chat-avatar").outerHTML = avatarHtml(convTitle(conv), "avatar chat-avatar").replace("<span ", '<span id="chat-avatar" ');
    applyChatContactPhone(conv);
    syncChatToggles(conv);
    renderConversationList();
    renderQuickShortcuts();
    renderStatusStrip();

    const cached = msgCache.get(id);
    let stopSkel = () => {};
    if (cached) {
      state.messages = cached;
      renderMessages();
    } else {
      stopSkel = showSkeleton(messagesEl, "messages", 0, 80);
    }

    try {
      const msgs = dedupeMessages(await api(`/conversations/${id}/messages`));
      stopSkel();
      if (state.activeId !== id) return;
      state.messages = msgs;
      cacheMessages(id, msgs);
      state._messagesSig = msgs
        .map((m) => m.id || `${m.body}|${m.created_at}`)
        .join("\n");
      renderMessages();
      if (!cached) animateIn(messagesEl, { y: 4, duration: 180 });
      if (state.wa.status === "connected") pullActiveChat();
      if (conv.unread_count) {
        conv.unread_count = 0;
        renderConversationList();
      }
    } catch (err) {
      stopSkel();
      console.error(err);
    }
  }

  async function loadInitial() {
    conversationList.innerHTML = skeletonHtml("chats", 7);
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
      loadOnboardingStatus();

      state.tenant = tenant;
      state.aiStatus = aiStatus;
      state.aiProfile = bizProfile;
      state.subscription = subscription;

      if (tenant) {
        setBusinessName(tenant.business_name, !!bizProfile?.business_name_is_placeholder);
      $("toggle-ai-global").checked = tenant.ai_global_enabled;
      }
      renderReadiness();
      loadAiQuestions({ badgeOnly: true });
      loadOrders();

      applyWaSession(wa);
      setChatListTab(state.chatListTab);
      if (!state.syncInProgress) {
        try {
          const all = sortConversations(await api("/conversations?archived=false", {}, 30000));
          state.convCache = all;
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
      maybeShowMorningSummary();
    } catch (err) {
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${escapeHtml(err.message)}</span></li>`;
    } finally {
      renderWaUi();
    }
  }

  /** «OPERADORES» o «laura» → «Operadores», «Laura»: un saludo en mayúsculas se lee como un grito. */
  function firstName() {
    const word = (state.user?.full_name || "").trim().split(/\s+/)[0] || "";
    if (!word || word.includes("@")) return "";
    const rest = word === word.toLocaleUpperCase("es") ? word.slice(1).toLocaleLowerCase("es") : word.slice(1);
    return word.charAt(0).toLocaleUpperCase("es") + rest;
  }

  function plural(n, one, many) {
    return `${n} ${n === 1 ? one : many}`;
  }

  function morningSummaryText(s) {
    const parts = [];
    if (s.attended) {
      const interested = s.interested
        ? ` y ${s.interested === 1 ? "1 quedó interesado" : `${s.interested} quedaron interesados`}`
        : "";
      parts.push(`Anoche tu IA atendió a ${plural(s.attended, "cliente", "clientes")}${interested}.`);
    }
    if (s.appointments_today) {
      const next = s.next_appointment_at ? `; la próxima es a las ${s.next_appointment_at}` : "";
      const line = `Hoy tienes ${plural(s.appointments_today, "cita", "citas")}${next}`;
      parts.push(line.endsWith(".") ? line : `${line}.`);
    }
    return parts.join(" ");
  }

  /** Una vez al día, al abrir el panel en la mañana: lo que pasó mientras el dueño dormía. */
  let morningLoading = false;
  async function maybeShowMorningSummary() {
    const hour = new Date().getHours();
    if (!state.user?.tenant_id || hour < 5 || hour >= 12 || $("morning-card") || morningLoading) return;
    const seenKey = `omitel_morning_${state.user.tenant_id}`;
    let summary;
    morningLoading = true;
    try {
      summary = await api("/results/morning");
    } catch {
      return;
    } finally {
      morningLoading = false;
    }
    if (localStorage.getItem(seenKey) === summary.date) return;
    const text = morningSummaryText(summary);
    if (!text) return;
    localStorage.setItem(seenKey, summary.date);

    const first = firstName();
    const actions = [];
    if (summary.interested) actions.push({ label: "Ver interesados", run: () => { $("mode-chats")?.click(); $("tab-chats-interested")?.click(); } });
    if (summary.appointments_today) actions.push({ label: "Ver citas de hoy", run: () => $("mode-appointments")?.click() });
    const card = document.createElement("div");
    card.id = "morning-card";
    card.className = "morning-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "Resumen de la mañana");
    card.innerHTML = `
      <button type="button" class="morning-card-close" aria-label="Cerrar">×</button>
      <span class="morning-card-sun" aria-hidden="true"></span>
      <p class="morning-card-hello">¡Buen día${first ? `, ${escapeHtml(first)}` : ""}!</p>
      <p class="morning-card-text">${escapeHtml(text)}</p>
      <div class="morning-card-actions">${actions
        .map((a, i) => `<button type="button" class="btn ${i ? "ghost" : "primary"} small" data-i="${i}">${escapeHtml(a.label)}</button>`)
        .join("")}</div>`;
    const close = () => {
      card.classList.remove("is-open");
      setTimeout(() => card.remove(), 260);
    };
    card.addEventListener("click", (e) => {
      if (e.target.closest(".morning-card-close")) return close();
      const btn = e.target.closest("[data-i]");
      if (!btn) return;
      actions[Number(btn.dataset.i)]?.run();
      close();
    });
    document.body.appendChild(card);
    card.getBoundingClientRect();
    card.classList.add("is-open");
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.user) maybeShowMorningSummary();
  });

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
            .then((rows) => {
              state.convCache = sortConversations(rows);
              recalculateInterestedCount(state.convCache);
            })
            .catch(() => {});
          renderOnboarding();
        }
        break;
      case "whatsapp.status":
        applyWaSession({
          status: event.status,
          qr_base64: event.qr_base64,
          pairing_code: event.pairing_code,
          phone_number: event.phone_number,
        });
        break;
      case "tenant.settings":
        if (state.tenant) {
          setBusinessName(event.business_name, event.business_name_is_placeholder);
          applyTenantAi(event);
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
      case "products.updated":
        if (state.panelMode === "inventory" && !document.activeElement?.closest?.("#products-list")) loadProducts();
        refreshReadinessSoon();
        break;
      case "orders.updated":
        if (!document.activeElement?.closest?.("#orders-panel .order-dispatch")) loadOrders();
        break;
      case "ai_questions.updated": {
        const typing = state.panelMode === "ai" && document.activeElement?.closest?.("#ai-questions-card");
        loadAiQuestions({ badgeOnly: state.panelMode !== "ai" || Boolean(typing) });
        break;
      }
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
  $("wa-setup-alt-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    connectWhatsApp(preferredWaMode() === "code" ? "qr" : "code");
  });
  $("qr-mode-switch").addEventListener("click", () => {
    connectWhatsApp(waLink.mode === "code" ? "qr" : "code");
  });
  $("pair-other-number").addEventListener("click", showPairForm);
  $("pair-code").addEventListener("click", copyPairCode);
  $("pair-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const phone = $("pair-phone").value.trim();
    if (phone.replace(/\D/g, "").length < 10) {
      showQrError("Escribe el número completo, ej. 300 123 4567");
      return;
    }
    requestPairCode(phone);
  });
  $("guide-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    startTour();
  });

  /* Instalada desde la pantalla de inicio, el panel abre sin las barras del navegador. */
  const INSTALL_HINT_KEY = "omitel_install_hint";
  let installPrompt = null;
  const isStandalone = () => window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
  const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isPhone = () => window.matchMedia?.("(max-width: 768px) and (pointer: coarse)").matches;

  function canOfferInstall() {
    return !isStandalone() && Boolean(installPrompt || (isIos() && isPhone()));
  }

  function syncInstallButton() {
    $("install-app-btn")?.classList.toggle("hidden", !canOfferInstall());
  }

  function closeInstallCard() {
    const card = $("install-card");
    if (!card) return;
    card.classList.remove("is-open");
    setTimeout(() => card.remove(), 260);
  }

  function showInstallCard({ steps = false } = {}) {
    if ($("install-card") || $("morning-card")) return;
    const text = steps
      ? "Toca el botón Compartir (el cuadrito con la flecha hacia arriba) y elige «Agregar a inicio». Luego abre Omitel desde ese ícono."
      : "Instálalo en tu celular y ábrelo desde su ícono: a pantalla completa, sin las barras del navegador.";
    const card = document.createElement("div");
    card.id = "install-card";
    card.className = "morning-card install-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "Instalar Omitel");
    card.innerHTML = `
      <button type="button" class="morning-card-close" aria-label="Cerrar">×</button>
      <img class="install-card-icon" src="/static/panel/icon-192.png" alt="" width="44" height="44" />
      <p class="morning-card-hello">Usa Omitel como app</p>
      <p class="morning-card-text">${escapeHtml(text)}</p>
      <div class="morning-card-actions">
        ${steps ? "" : '<button type="button" class="btn primary small" data-install>Instalar</button>'}
        <button type="button" class="btn ghost small" data-later>${steps ? "Entendido" : "Ahora no"}</button>
      </div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-install]")) {
        closeInstallCard();
        installApp();
      } else if (e.target.closest(".morning-card-close, [data-later]")) {
        closeInstallCard();
      }
    });
    document.body.appendChild(card);
    card.getBoundingClientRect();
    card.classList.add("is-open");
  }

  async function installApp() {
    if (installPrompt) {
      const prompt = installPrompt;
      installPrompt = null;
      prompt.prompt();
      await prompt.userChoice.catch(() => null);
      syncInstallButton();
      return;
    }
    showInstallCard({ steps: true });
  }

  function maybeOfferInstall() {
    if (!isPhone() || !canOfferInstall() || !state.user) return;
    try {
      if (localStorage.getItem(INSTALL_HINT_KEY)) return;
      localStorage.setItem(INSTALL_HINT_KEY, "1");
    } catch {
      return;
    }
    showInstallCard({ steps: !installPrompt });
  }

  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installPrompt = e;
    syncInstallButton();
  });
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    syncInstallButton();
    closeInstallCard();
    toast("Listo: abre Omitel desde el ícono en tu pantalla de inicio.");
  });
  $("install-app-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    installApp();
  });
  syncThemeColor(document.documentElement.getAttribute("data-theme"));
  syncInstallButton();
  setTimeout(maybeOfferInstall, 6000);
  async function unlinkWhatsApp() {
    if (!confirm("¿Desvincular WhatsApp? Se borrarán todos los chats del panel y dejará de atender hasta que lo vuelvas a conectar.")) return;
    const buttons = ["wa-disconnect-btn", "wa-unlink-ready-btn"].map((id) => $(id)).filter(Boolean);
    buttons.forEach((btn) => {
      btn.disabled = true;
      btn.dataset.prev = btn.textContent;
      btn.textContent = "Desvinculando…";
    });
    try {
      await api("/whatsapp/disconnect", { method: "POST" }, 45000);
      clearConversations();
      await refreshWaStatus();
    } catch (err) {
      await refreshWaStatus();
      if (state.wa.status === "disconnected") {
        clearConversations();
        return;
      }
      alert(err.message || "No se pudo desvincular. Intenta de nuevo.");
    } finally {
      buttons.forEach((btn) => {
        btn.disabled = false;
        btn.textContent = btn.dataset.prev || "Desvincular";
      });
    }
  }
  $("wa-disconnect-btn").addEventListener("click", () => unlinkWhatsApp());
  $("wa-unlink-ready-btn")?.addEventListener("click", () => unlinkWhatsApp());
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
    let stopSkel = () => {};
    if (state.convCache) {
      setConversations(tab === "all" ? state.convCache : state.convCache.filter((c) => matchesInterestTab(c, tab)));
      renderConversationList();
    } else {
      stopSkel = showSkeleton(conversationList, "chats", 7, 0);
    }
    try {
      const rows = await fetchConversations();
      stopSkel();
      if (state.chatListTab !== tab) return;
      setConversations(rows);
      renderConversationList();
    } catch (err) {
      stopSkel();
      conversationList.innerHTML = `<li class="conversation-item"><span class="error">${escapeHtml(err.message)}</span></li>`;
    }
  }

  $("tab-chats-interested").addEventListener("click", () => switchChatTab("interested"));
  $("tab-chats-not-interested").addEventListener("click", () => switchChatTab("not_interested"));
  $("tab-chats-all").addEventListener("click", () => switchChatTab("all"));

  function closeActiveChat() {
    if (!state.activeId) return;
    cancelVoiceNote();
    if (pendingImage) clearPendingImage();
    cacheMessages(state.activeId, state.messages);
    state.activeId = null;
    state.messages = [];
    state._messagesSig = "";
    emptyChat.classList.remove("hidden");
    activeChat.classList.add("hidden");
    renderConversationList();
    renderQuickShortcuts();
    renderStatusStrip();
    if (isPhoneLayout()) animateIn(document.querySelector(".sidebar"), { x: -24, opacity: 0.5, duration: 280, easing: EASE_DRAWER });
  }

  $("chat-back-btn").addEventListener("click", () => {
    if (history.state?.omitelChat) history.back();
    else closeActiveChat();
  });
  window.addEventListener("popstate", () => {
    if (lightbox.popping) {
      lightbox.popping = false;
      return;
    }
    if (lightbox.el?.classList.contains("is-open")) {
      closeLightbox(true);
      return;
    }
    if (!history.state?.omitelChat) closeActiveChat();
  });

  const topbar = document.querySelector(".topbar");
  function setTopbarMenu(open) {
    topbar.classList.toggle("menu-open", open);
    $("topbar-menu-btn").setAttribute("aria-expanded", String(open));
  }
  $("topbar-menu-btn").addEventListener("click", () => setTopbarMenu(!topbar.classList.contains("menu-open")));
  topbar.addEventListener("click", (e) => {
    if (e.target.closest(".topbar-right button, .topbar-right a, .topbar-wa-actions button, .topbar-wa-actions a")) {
      setTopbarMenu(false);
    }
  });
  document.addEventListener("click", (e) => {
    if (topbar.classList.contains("menu-open") && !topbar.contains(e.target)) setTopbarMenu(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && topbar.classList.contains("menu-open")) setTopbarMenu(false);
  });

  $("mode-chats").addEventListener("click", () => switchPanelMode("chats"));
  $("mode-appointments").addEventListener("click", () => switchPanelMode("appointments"));
  $("mode-inventory").addEventListener("click", () => switchPanelMode("inventory"));
  $("mode-orders").addEventListener("click", () => switchPanelMode("orders"));
  $("orders-panel").addEventListener("click", onOrdersClick);
  $("mode-ai").addEventListener("click", () => switchPanelMode("ai"));
  $("schedule-save-btn")?.addEventListener("click", () => saveAppointmentSchedule());
  $("schedule-ai-booking")?.addEventListener("change", () => saveAppointmentSchedule());
  $("schedule-reminders")?.addEventListener("change", () => saveAppointmentSchedule());
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
  $("ai-ready-list").addEventListener("click", (e) => {
    if (e.target.closest(".ai-ready-import")) {
      if (state.canManageGlobal) importCatalogFromReadiness();
      return;
    }
    const btn = e.target.closest(".ai-ready-go");
    if (btn) goToReadinessField(btn.dataset.field);
  });
  $("ai-tips-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".ai-tip-go");
    if (btn) onSalesTip(btn.dataset.action, btn);
  });
  $("try-ai-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendTryAi($("try-ai-input").value);
  });
  $("try-ai-card").addEventListener("click", (e) => {
    const chip = e.target.closest(".try-ai-chip");
    if (chip) sendTryAi(chip.textContent);
  });
  $("try-ai-reset").addEventListener("click", () => {
    tryAi.turns = [];
    $("try-ai-status").textContent = "";
    renderTryAi();
  });
  $("ai-questions-card").addEventListener("click", (e) => {
    const item = e.target.closest(".ai-question");
    if (!item || !state.canManageGlobal) return;
    if (e.target.closest(".ai-question-save")) saveAiQuestion(item);
    else if (e.target.closest(".ai-question-delete")) deleteAiQuestion(item);
    else if (e.target.closest(".ai-question-inventory")) switchPanelMode("inventory");
  });
  $("ai-questions-card").addEventListener("change", (e) => {
    const input = e.target.closest(".ai-question-photo-input");
    if (input && state.canManageGlobal) answerQuestionWithPhoto(input.closest(".ai-question"), input.files[0]);
  });
  $("business-name-cta").addEventListener("click", openBusinessNameSetup);
  $("alert-save-btn").addEventListener("click", () => saveInterestAlert());
  $("alert-test-btn").addEventListener("click", () => testInterestAlert());
  $("followup-hours").addEventListener("change", () => saveFollowup());
  $("alert-add-btn").addEventListener("click", () => addAlertRecipient());
  $("alert-recipients").addEventListener("click", (e) => {
    const btn = e.target.closest(".alert-remove");
    if (btn) {
      removeAlertRecipient(btn);
      checkAlertRecipients();
    }
  });
  $("alert-recipients").addEventListener("input", () => checkAlertRecipients());
  $("alert-recipients").addEventListener("change", () => checkAlertRecipients());
  $("ai-shortcuts-config-btn")?.addEventListener("click", () => openShortcutsModal());
  $("products-add-btn")?.addEventListener("click", addProductRow);
  $("products-import-btn")?.addEventListener("click", importProductsFromCatalog);
  $("products-list")?.addEventListener("change", (e) => {
    const row = e.target.closest(".product-row");
    if (!row || !state.canManageGlobal) return;
    const photoInput = e.target.closest(".product-photo-input");
    if (photoInput) {
      uploadProductPhotos(row, photoInput.files);
      return;
    }
    queueProductSave(row);
  });
  $("products-list")?.addEventListener("click", (e) => {
    const view = e.target.closest(".product-photo-view");
    if (view) {
      const row = view.closest(".product-row");
      const name = row.querySelector(".product-name").value.trim();
      const price = row.querySelector(".product-price").value.trim();
      openPhotoViewer(productPhotoUrl(view.dataset.name), [name, price].filter(Boolean).join(" — "));
      return;
    }
    if (!state.canManageGlobal) return;
    const photo = e.target.closest(".product-photo-remove");
    if (photo) {
      removeProductPhoto(photo.closest(".product-row"), photo.dataset.name);
      return;
    }
    const btn = e.target.closest(".product-remove");
    if (btn) removeProductRow(btn.closest(".product-row"));
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
    pendingFile = null;
    $("composer-attachment").classList.add("hidden");
    $("composer-attachment-img").removeAttribute("src");
    for (const id of ["attach-image-input", "camera-input", "attach-file-input"]) $(id).value = "";
    messageInput.placeholder = "Mensaje";
    syncComposerMode();
  }

  function showAttachmentCard({ title, hint, previewUrl }) {
    $("composer-attachment-img").classList.toggle("hidden", !previewUrl);
    if (previewUrl) $("composer-attachment-img").src = previewUrl;
    $("composer-attachment-doc").classList.toggle("hidden", Boolean(previewUrl));
    $("composer-attachment-title").textContent = title;
    $("composer-attachment-hint").textContent = hint;
    $("composer-attachment").classList.remove("hidden");
    animateIn($("composer-attachment"), { y: 8, duration: 200 });
    messageInput.placeholder = "Agrega un comentario (opcional)";
    messageInput.focus();
    syncComposerMode();
  }

  function canSendFromComposer(what) {
    if (!state.activeId) return false;
    if (!state.canWrite) {
      alert("Tu usuario no puede enviar mensajes. Pide acceso de agente o dueño.");
      return false;
    }
    if (sendForm.classList.contains("disabled")) {
      alert(`Conecta WhatsApp para enviar ${what}.`);
      return false;
    }
    return true;
  }

  const CHAT_FILE_MAX = 16 * 1024 * 1024;
  function setPendingFile(file) {
    if (!file || !canSendFromComposer("archivos")) return;
    if (file.size > CHAT_FILE_MAX) {
      alert("El archivo no puede superar 16 MB.");
      return;
    }
    clearPendingImage();
    pendingFile = { file, conversationId: state.activeId };
    showAttachmentCard({ title: file.name, hint: `${fileSizeLabel(file.size)} · toca enviar para mandarlo` });
  }

  function pickAttachment(file) {
    if (!file) return;
    if (file.type.startsWith("image/") && CHAT_IMAGE_TYPES.concat(["image/heic", "image/heif", "image/gif"]).includes(file.type)) {
      setPendingImage(file);
    } else {
      setPendingFile(file);
    }
  }

  async function setPendingImage(file) {
    if (!file || !canSendFromComposer("fotos")) return;
    if (!file.type.startsWith("image/")) {
      setPendingFile(file);
      return;
    }
    try {
      const prepared = await prepareChatImage(file);
      clearPendingImage();
      pendingImage = { ...prepared, conversationId: state.activeId };
      showAttachmentCard({
        title: "Foto lista para enviar",
        hint: "Escribe un comentario si quieres y toca enviar.",
        previewUrl: prepared.previewUrl,
      });
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

  document.addEventListener("paste", (e) => {
    if (activeChat.classList.contains("hidden")) return;
    if (isEditable(e.target) && e.target !== messageInput) return;
    const file = imageFromClipboard(e);
    if (!file) return;
    e.preventDefault();
    setPendingImage(file);
  });

  function setAttachMenu(open) {
    $("attach-menu").classList.toggle("hidden", !open);
    $("attach-menu-btn").classList.toggle("is-open", open);
    $("attach-menu-btn").setAttribute("aria-expanded", String(open));
    if (open) animateIn($("attach-menu"), { y: 10, scale: 0.96, duration: 200, origin: "bottom left" });
  }
  $("attach-menu-btn").addEventListener("click", () => setAttachMenu($("attach-menu").classList.contains("hidden")));
  $("attach-menu").addEventListener("click", (e) => {
    const opt = e.target.closest("[data-attach]");
    if (!opt) return;
    setAttachMenu(false);
    const input = { document: "attach-file-input", gallery: "attach-image-input", camera: "camera-input" }[opt.dataset.attach];
    $(input).click();
  });
  document.addEventListener("click", (e) => {
    if (!$("attach-menu").classList.contains("hidden") && !e.target.closest("#attach-menu, #attach-menu-btn")) setAttachMenu(false);
  });
  $("camera-btn").addEventListener("click", () => $("camera-input").click());
  $("attach-image-input").addEventListener("change", (e) => pickAttachment(e.target.files?.[0]));
  $("camera-input").addEventListener("change", (e) => setPendingImage(e.target.files?.[0]));
  $("attach-file-input").addEventListener("change", (e) => pickAttachment(e.target.files?.[0]));
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
    pickAttachment(e.dataTransfer.files?.[0]);
  });

  function pushSentMessage(msg) {
    if (msg.conversation_id !== state.activeId) return;
    if (!state.messages.some((m) => m.id === msg.id)) {
      state.messages.push(msg);
      state._messagesSig = state.messages.map((m) => m.id || `${m.body}|${m.created_at}`).join("\n");
    }
    renderMessages();
  }

  let pendingSeq = 0;
  function addPending(conversationId, body, { media = null, file = null } = {}) {
    const tmp = {
      id: `tmp-${++pendingSeq}`,
      conversation_id: conversationId,
      direction: "out",
      body,
      source: "agent",
      created_at: new Date().toISOString(),
      status: "pending",
      pending: true,
      file,
    };
    if (media) mediaCache.set(tmp.id, media);
    pendingOut.push(tmp);
    if (conversationId === state.activeId) renderMessages();
    return tmp;
  }

  const SEND_RETRY_MS = [1500, 3000, 6000, 10000, 15000];
  function sendKey() {
    return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  /** Con la misma Idempotency-Key el servidor no lo envía dos veces aunque el primer intento sí haya salido. */
  async function sendWithRetry(tmp) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await tmp.send({ "Idempotency-Key": tmp.key });
      } catch (err) {
        if (!err.network || attempt >= SEND_RETRY_MS.length) throw err;
        await waitForNetwork(SEND_RETRY_MS[attempt]);
      }
    }
  }

  async function deliverPending(tmp, send) {
    tmp.send = send;
    tmp.key = tmp.key || sendKey();
    try {
      const msg = await sendWithRetry(tmp);
      pendingOut.splice(pendingOut.indexOf(tmp), 1);
      const media = mediaCache.get(tmp.id);
      if (media && msg?.id) mediaCache.set(msg.id, media);
      if (voicePlayers.has(tmp.id) && msg?.id) voicePlayers.set(msg.id, voicePlayers.get(tmp.id));
      if (tmp.file && msg?.id) docFiles.set(msg.id, tmp.file);
      if (msg?.id) pushSentMessage(msg);
      else if (tmp.conversation_id === state.activeId) renderMessages();
    } catch (err) {
      tmp.status = "failed";
      if (tmp.conversation_id === state.activeId) renderMessages();
      toast(`${err.message || "No se pudo enviar."} Toca el mensaje para reintentar.`, { tone: "error" });
    }
  }

  messagesEl.addEventListener("click", (e) => {
    const bubble = e.target.closest(".msg.is-failed");
    const tmp = bubble && pendingOut.find((p) => p.id === bubble.dataset.pending);
    if (!tmp || e.target.closest(".doc-card, .voice-player, .media-img")) return;
    tmp.status = "pending";
    renderMessages();
    deliverPending(tmp, tmp.send);
  });

  window.addEventListener("offline", () => setNetPill(true));
  window.addEventListener("online", async () => {
    setNetPill(false);
    if (!state.user) return;
    if (!state.ws || state.ws.readyState > 1) connectWs();
    pullActiveChat();
    try {
      const rows = await fetchConversations();
      if (rows.length) {
        setConversations(rows);
        renderConversationList();
      }
    } catch {
      /* el sondeo normal lo vuelve a intentar */
    }
  });

  function resetComposerText() {
    messageInput.value = "";
    syncComposerMode();
    fitComposer();
  }

  const canRecordVoice = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  function syncComposerMode() {
    const hasContent = Boolean(messageInput.value.trim() || pendingImage || pendingFile);
    sendForm.classList.toggle("has-content", hasContent || !canRecordVoice);
  }
  messageInput.addEventListener("input", syncComposerMode);
  sendForm.addEventListener("submit", () => setTimeout(syncComposerMode, 0));

  const VOICE_MAX_MS = 5 * 60 * 1000;
  const voice = { rec: null, stream: null, chunks: [], startedAt: 0, timer: null, send: false };

  function voiceMimeType() {
    const options = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
    return options.find((t) => MediaRecorder.isTypeSupported?.(t)) || "";
  }

  function formatDuration(ms) {
    const total = Math.floor(ms / 1000);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  function setVoiceUi(recording) {
    sendForm.classList.toggle("is-recording", recording);
    $("voice-bar").classList.toggle("hidden", !recording);
    if (recording) animateIn($("voice-bar"), { x: 16, duration: 200 });
  }

  async function startVoiceNote() {
    if (voice.rec || !canSendFromComposer("notas de voz")) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      alert("Permite el micrófono en tu navegador para grabar notas de voz.");
      return;
    }
    const mimeType = voiceMimeType();
    const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    Object.assign(voice, { rec, stream, chunks: [], startedAt: Date.now(), send: false, conversationId: state.activeId });
    rec.ondataavailable = (e) => e.data?.size && voice.chunks.push(e.data);
    rec.onstop = () => finishVoiceNote(rec.mimeType || mimeType || "audio/webm");
    rec.start(250);
    $("voice-time").textContent = "0:00";
    voice.timer = setInterval(() => {
      const elapsed = Date.now() - voice.startedAt;
      $("voice-time").textContent = formatDuration(elapsed);
      if (elapsed >= VOICE_MAX_MS) stopVoiceNote(true);
    }, 250);
    setVoiceUi(true);
  }

  function stopVoiceNote(send) {
    if (!voice.rec) return;
    voice.send = send;
    clearInterval(voice.timer);
    if (voice.rec.state !== "inactive") voice.rec.stop();
    setVoiceUi(false);
  }

  function cancelVoiceNote() {
    stopVoiceNote(false);
  }

  async function finishVoiceNote(mimetype) {
    const { stream, chunks, send, startedAt, conversationId } = voice;
    stream?.getTracks().forEach((t) => t.stop());
    Object.assign(voice, { rec: null, stream: null, chunks: [] });
    if (!send) return;
    if (Date.now() - startedAt < 900) {
      toast("Nota muy corta. Toca el micrófono, habla y luego toca enviar.", { tone: "error" });
      return;
    }
    const blob = new Blob(chunks, { type: mimetype });
    const ext = mimetype.includes("mp4") ? "m4a" : mimetype.includes("ogg") ? "ogg" : "webm";
    const fd = new FormData();
    fd.append("file", blob, `nota.${ext}`);
    let base64 = "";
    try {
      base64 = await blobToBase64(blob);
    } catch {
      /* se descargará de WhatsApp */
    }
    const tmp = addPending(conversationId, "[audio]", {
      media: base64 ? { ok: true, base64, media_type: "audio", mimetype: mimetype.split(";")[0] } : null,
    });
    deliverPending(tmp, (h) => api(`/conversations/${conversationId}/messages/audio`, { method: "POST", body: fd, headers: h }, 60000));
  }

  $("voice-btn").addEventListener("click", startVoiceNote);
  $("voice-send").addEventListener("click", () => stopVoiceNote(true));
  $("voice-cancel").addEventListener("click", cancelVoiceNote);
  syncComposerMode();

  sendForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.activeId || !state.canWrite) return;
    const text = messageInput.value.trim();
    if (pendingFile) {
      const { file, conversationId } = pendingFile;
      const fd = new FormData();
      fd.append("file", file, file.name);
      fd.append("caption", text);
      clearPendingImage();
      resetComposerText();
      const tmp = addPending(conversationId, ["[document]", file.name, text].filter(Boolean).join("\n"), { file });
      deliverPending(tmp, (h) => api(`/conversations/${conversationId}/messages/file`, { method: "POST", body: fd, headers: h }, 60000));
      return;
    }
    if (pendingImage) {
      const image = pendingImage;
      const fd = new FormData();
      fd.append("file", image.blob, `foto.${image.mimetype.split("/")[1]}`);
      fd.append("caption", text);
      clearPendingImage();
      resetComposerText();
      let base64 = "";
      try {
        base64 = await blobToBase64(image.blob);
      } catch {
        /* se descargará de WhatsApp como cualquier otra foto */
      }
      const tmp = addPending(image.conversationId, text ? `[image]\n${text}` : "[image]", {
        media: base64 ? { ok: true, base64, media_type: "image", mimetype: image.mimetype } : null,
      });
      deliverPending(tmp, (h) => api(`/conversations/${image.conversationId}/messages/image`, { method: "POST", body: fd, headers: h }, 30000));
      return;
    }
    if (!text) return;

    const conversationId = state.activeId;
    resetComposerText();
    const tmp = addPending(conversationId, text);
    deliverPending(tmp, (h) =>
      api(`/conversations/${conversationId}/messages`, { method: "POST", body: JSON.stringify({ text }), headers: h })
    );
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
    if (e.target.checked && missingReadiness().length) {
      e.target.checked = false;
      goToReadinessField(null);
      return;
    }
    try {
      const tenant = await api("/tenants/me", {
        method: "PATCH",
        body: JSON.stringify({ ai_global_enabled: e.target.checked }),
      });
      state.tenant = tenant;
      onAiGlobalChanged(tenant.ai_global_enabled);
    } catch (err) {
      alert(err.message);
      e.target.checked = !e.target.checked;
    }
  });

  function fitComposer() {
    messageInput.style.height = "auto";
    messageInput.style.height = `${Math.min(messageInput.scrollHeight, 140)}px`;
  }
  messageInput.addEventListener("input", fitComposer);
  sendForm.addEventListener("submit", () => requestAnimationFrame(fitComposer));

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
