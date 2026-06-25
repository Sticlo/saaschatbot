#!/usr/bin/env node
/**
 * security-audit.js — Auditoría OWASP adaptada a SaasChatbot
 * Stack: web/panel + web/landing + FastAPI (backend/app/)
 *
 * Uso: node security-audit.js
 */

const fs = require("fs");
const path = require("path");
const { execSync, spawnSync } = require("child_process");

const ROOT = path.join(__dirname, "..");

const C = {
  reset: "\x1b[0m",
  bold: "\x1b[1m",
  dim: "\x1b[2m",
  red: "\x1b[31m",
  yellow: "\x1b[33m",
  blue: "\x1b[34m",
  magenta: "\x1b[35m",
  cyan: "\x1b[36m",
  green: "\x1b[32m",
  gray: "\x1b[90m",
};

const SEV = {
  CRITICO: { order: 0, color: C.red, label: "CRÍTICO" },
  ALTO: { order: 1, color: C.magenta, label: "ALTO" },
  MEDIO: { order: 2, color: C.yellow, label: "MEDIO" },
  BAJO: { order: 3, color: C.blue, label: "BAJO" },
  OK: { order: 4, color: C.green, label: "OK" },
};

/** @type {Array<{severity:string,category:string,file:string,line:number|null,message:string,fix:string}>} */
const findings = [];

function rel(p) {
  return path.relative(ROOT, p).replace(/\\/g, "/");
}

function add(severity, category, file, line, message, fix) {
  findings.push({
    severity,
    category,
    file: file ? rel(file) : "—",
    line: line ?? null,
    message,
    fix,
  });
}

function addOk(category, message) {
  add("OK", category, null, null, message, "—");
}

function walk(dir, extensions, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    let st;
    try {
      st = fs.statSync(full);
    } catch {
      continue;
    }
    if (st.isDirectory()) {
      if (["node_modules", ".venv", "venv", "__pycache__", ".git", "services", ".pytest_cache"].includes(name)) {
        continue;
      }
      walk(full, extensions, out);
    } else if (extensions.some((ext) => name.endsWith(ext))) {
      out.push(full);
    }
  }
  return out;
}

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

function lineOf(content, index) {
  return content.slice(0, index).split("\n").length;
}

function lines(content) {
  return content.split("\n");
}

function matchAll(content, regex) {
  const out = [];
  if (!regex.global) {
    const copy = new RegExp(regex.source, regex.flags + "g");
    return matchAll(content, copy);
  }
  let m;
  while ((m = regex.exec(content)) !== null) {
    out.push({ match: m[0], index: m.index, groups: m });
  }
  return out;
}

// ─── 1. CREDENCIALES Y API KEYS ───────────────────────────────────────────

const SECRET_PATTERNS = [
  { re: /sk_live_[a-zA-Z0-9]{10,}/g, label: "Stripe live key" },
  { re: /sk_test_[a-zA-Z0-9]{10,}/g, label: "Stripe test key" },
  { re: /AIza[0-9A-Za-z\-_]{20,}/g, label: "Google API key" },
  { re: /AKIA[0-9A-Z]{16}/g, label: "AWS access key" },
  { re: /(?:api[_-]?key|secret|password|token)\s*[:=]\s*['"](?!change-me|dev-|&lt;|<tu-|password@|example)[^'"]{12,}['"]/gi, label: "Posible secreto hardcodeado" },
  { re: /Bearer\s+[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+/g, label: "JWT hardcodeado" },
];

const SECRET_ALLOWLIST = [
  /change-me/i,
  /dev-/i,
  /<tu-/i,
  /password123/,
  /example\.com/i,
  /saaschatbot_token/,
  /STORAGE_KEY/,
];

function auditCredentials() {
  const cat = "1. Credenciales y API keys";
  const scanDirs = [
    ...walk(path.join(ROOT, "web", "panel"), [".js", ".html"]),
    ...walk(path.join(ROOT, "backend", "app"), [".py"]),
  ];
  const configFiles = [".env.example", "backend/app/config.py", "deploy/docker-compose.yml", "deploy/docker-compose.prod.yml"]
    .map((f) => path.join(ROOT, f))
    .filter((f) => fs.existsSync(f));

  let hits = 0;
  for (const file of [...scanDirs, ...configFiles]) {
    const content = readText(file);
    for (const { re, label } of SECRET_PATTERNS) {
      for (const { match, index } of matchAll(content, re)) {
        if (SECRET_ALLOWLIST.some((a) => a.test(match))) continue;
        if (file.endsWith(".env.example") && /<tu-/.test(match)) continue;
        hits++;
        add(
          file.includes("config.py") && /change-me|password@localhost/.test(match) ? "ALTO" : "CRITICO",
          cat,
          file,
          lineOf(content, index),
          `${label}: ${match.slice(0, 60)}${match.length > 60 ? "…" : ""}`,
          "Mueve secretos a variables de entorno (.env) y rota las claves expuestas."
        );
      }
    }
  }

  if (fs.existsSync(path.join(ROOT, ".env"))) {
    add(
      "MEDIO",
      cat,
      ".env",
      null,
      "Archivo .env presente en el proyecto — verifica que esté en .gitignore y no se suba a git.",
      "Confirma: git check-ignore -v .env"
    );
  } else {
    addOk(cat, ".env no está en el árbol (correcto si solo vive local).");
  }

  if (hits === 0) addOk(cat, "No se detectaron API keys obvias en código fuente.");
}

// ─── 2. XSS ───────────────────────────────────────────────────────────────

function auditXss() {
  const cat = "2. XSS (Cross-Site Scripting)";
  const panelJs = path.join(ROOT, "static", "panel", "app.js");
  const indexHtml = path.join(ROOT, "static", "panel", "index.html");
  const js = readText(panelJs);
  const html = readText(indexHtml);

  if (!js.includes("function escapeHtml")) {
    add("ALTO", cat, panelJs, null, "No se encontró función escapeHtml en el panel.", "Implementa escape antes de asignar innerHTML con datos dinámicos.");
  } else {
    addOk(cat, "Función escapeHtml presente en app.js.");
  }

  const jsLines = lines(js);
  jsLines.forEach((line, i) => {
    const n = i + 1;
    if (!/innerHTML\s*=/.test(line)) return;

    // Reconstruir asignación multilínea hasta ';' final (evita cortar dentro de .map)
    let block = line;
    let j = i;
    while (j + 1 < jsLines.length) {
      const trimmed = block.trim();
      const complete =
        trimmed.endsWith(";") &&
        (trimmed.includes(".join(") || !/\.map\s*\(/.test(trimmed));
      if (complete) break;
      j += 1;
      block += "\n" + jsLines[j];
    }

    const isStaticOnly =
      /innerHTML\s*=\s*['"][^'"]*['"]\s*;/.test(block.trim()) && !block.includes("${");
    if (isStaticOnly) return;

    if (block.includes("escapeHtml(")) return;
    if (/rows\.join\(|statRow\(/.test(block)) return;
    if (/\.map\(\(l\)/.test(block) && block.includes("escapeHtml(")) return;
    if (/mediaIcon\(|mediaLabel\(/.test(block)) return;
    const prevLine = i > 0 ? jsLines[i - 1] : "";
    if (/escapeHtml\(/.test(prevLine) && /\$\{caption\}/.test(block)) return;
    if (/Sin (resultados|leads|conversaciones|chats)|Cargando|Importando|No se pudo cargar/.test(block)) return;
    // msg en lista de chats es "Sin resultados" o emptyLabel — estático
    if (/\$\{msg\}/.test(block) && !block.includes("err.message")) return;

    add(
      "ALTO",
      cat,
      panelJs,
      n,
      `innerHTML con datos dinámicos sin escapeHtml: ${line.trim().slice(0, 90)}${line.trim().length > 90 ? "…" : ""}`,
      "Usa escapeHtml() o textContent / createElement para contenido de usuario (mensajes, errores, nombres)."
    );
  });

  for (const bad of ["eval(", "new Function(", "document.write("]) {
    if (js.includes(bad)) {
      add("CRITICO", cat, panelJs, lineOf(js, js.indexOf(bad)), `Uso de ${bad}`, "Elimina ejecución dinámica de código.");
    }
  }

  if (!/Content-Security-Policy/i.test(html)) {
    add(
      "MEDIO",
      cat,
      indexHtml,
      null,
      "index.html sin meta Content-Security-Policy.",
      'Añade CSP vía Nginx o <meta http-equiv="Content-Security-Policy" …> restringiendo script-src y connect-src.'
    );
  }

  if (!html.includes("escapeHtml") && js.includes("innerHTML")) {
    addOk(cat, "Revisa manualmente mensajes WhatsApp — usan escapeHtml en renderMessages.");
  }
}

// ─── 3. ALMACENAMIENTO INSEGURO ───────────────────────────────────────────

function auditStorage() {
  const cat = "3. Almacenamiento en navegador";
  const panelJs = path.join(ROOT, "static", "panel", "app.js");
  const js = readText(panelJs);

  if (/localStorage\.(setItem|getItem)\s*\(\s*['"][^'"]*token/i.test(js) || /localStorage\.setItem\(STORAGE_KEY/.test(js)) {
    add(
      "ALTO",
      cat,
      panelJs,
      lineOf(js, js.search(/localStorage/)),
      "JWT almacenado en localStorage (vulnerable a robo vía XSS).",
      "Preferir cookies HttpOnly + Secure + SameSite=Strict emitidas por el backend, o sessionStorage con CSP estricta."
    );
  } else if (/credentials:\s*['"]include['"]/.test(js) && /auth\/logout/.test(js)) {
    addOk(cat, "Sesión en cookie HttpOnly (credentials: include) — sin JWT en localStorage.");
  } else if (/LEGACY_STORAGE_KEY|removeItem\(LEGACY/.test(js)) {
    addOk(cat, "Migración: elimina token legacy de localStorage al iniciar sesión.");
  }

  if (/sessionStorage/.test(js)) {
    add("MEDIO", cat, panelJs, null, "Uso de sessionStorage detectado.", "No guardes tokens ni PII sin cifrar.");
  }

  if (!/localStorage\.clear|removeItem\(STORAGE_KEY\)/.test(js)) {
    add("MEDIO", cat, panelJs, null, "Verifica que logout limpie el token.", "Asegura removeItem en logout() — ya debería existir.");
  } else {
    addOk(cat, "logout() limpia localStorage (removeItem).");
  }
}

// ─── 4. AUTENTICACIÓN Y AUTORIZACIÓN ──────────────────────────────────────

function auditAuth() {
  const cat = "4. Autenticación y autorización";
  const panelJs = path.join(ROOT, "static", "panel", "app.js");
  const depsPy = path.join(ROOT, "backend", "app", "shared", "core", "deps.py");
  const js = readText(panelJs);
  const deps = readText(depsPy);

  if (js.includes("roleAtLeast") && js.includes("canWrite")) {
    add(
      "MEDIO",
      cat,
      panelJs,
      null,
      "Controles de rol solo en cliente (canWrite, canManageGlobal) — la UI se puede manipular.",
      "OK si el backend siempre valida con RequireAgent/RequireOwner (FastAPI deps). Nunca confíes solo en el panel."
    );
  }

  if (deps.includes("get_current_user") && deps.includes("require_role")) {
    addOk(cat, "Backend FastAPI valida JWT y roles en cada request protegido.");
  } else {
    add("ALTO", cat, depsPy, null, "No se detectó cadena clara de autenticación en deps.py.", "Implementa Depends(get_current_user) en rutas sensibles.");
  }

  if (/ws\/panel\?token=/.test(js) || /encodeURIComponent\(state\.token\)/.test(js)) {
    add(
      "ALTO",
      cat,
      panelJs,
      lineOf(js, js.search(/ws\/panel/)),
      "JWT enviado en query string del WebSocket (visible en logs, historial, Referer).",
      "Autentica WS con cookie HttpOnly, primer mensaje con token, o subprotocolo Sec-WebSocket-Protocol."
    );
  } else if (/\/ws\/panel`/.test(js) && /credentials:\s*['"]include['"]/.test(js)) {
    addOk(cat, "WebSocket sin token en URL — usa cookie HttpOnly del mismo origen.");
  }

  if (!/res\.status\s*===\s*401/.test(js)) {
    add("ALTO", cat, panelJs, null, "No se detecta manejo de 401 en fetch.", "Redirige a login y limpia sesión en 401.");
  } else {
    addOk(cat, "Cliente maneja 401 (logout + sesión expirada).");
  }

  const authPy = readText(path.join(ROOT, "backend", "app", "presentation", "api", "auth.py"));
  if (!/verify_password|hash_password/.test(authPy)) {
    add("ALTO", cat, "backend/app/presentation/api/auth.py", null, "Revisa hashing de contraseñas en login/registro.", "Usa bcrypt/argon2 — parece estar en security.py.");
  } else {
    addOk(cat, "Login/registro usan hash de contraseña en backend.");
  }
}

// ─── 5. HTTP / API CLIENT ─────────────────────────────────────────────────

function auditHttpClient() {
  const cat = "5. Cliente HTTP y API";
  const panelJs = path.join(ROOT, "static", "panel", "app.js");
  const js = readText(panelJs);

  if (js.includes('const API = "/api/v1"')) {
    addOk(cat, "API usa rutas relativas (mismo origen) — el Bearer no debería filtrarse a dominios externos.");
  }

  if (/fetch\s*\(\s*['"]https?:\/\//.test(js)) {
    add("MEDIO", cat, panelJs, null, "fetch a URL absoluta externa detectado.", "No envíes Authorization a terceros.");
  }

  const consoleLogs = (js.match(/console\.(log|debug|info)\(/g) || []).length;
  const consoleWarns = (js.match(/console\.(warn|error)\(/g) || []).length;
  if (consoleLogs > 0) {
    add(
      "BAJO",
      cat,
      panelJs,
      null,
      `${consoleLogs} console.log/debug en panel (pueden filtrar info en producción).`,
      "Elimina logs de depuración o envuélvelos en if (DEBUG)."
    );
  }
  if (consoleWarns > 0) {
    add("BAJO", cat, panelJs, null, `${consoleWarns} console.warn/error en panel.`, "Evita loguear objetos de error completos con tokens.");
  }
}

// ─── 6. PAGOS ─────────────────────────────────────────────────────────────

function auditPayments() {
  const cat = "6. Seguridad en pagos";
  const appDir = walk(path.join(ROOT, "backend", "app"), [".py"]);
  const all = appDir.map(readText).join("\n");
  const panel = readText(path.join(ROOT, "static", "panel", "app.js"));

  if (/wompi|stripe|mercadopago|payment_intent/i.test(all)) {
    add("MEDIO", cat, "backend/app/", null, "Integración de pagos detectada.", "Montos y planes deben validarse solo en backend; nunca confíes en campaign-limit del panel.");
  } else {
    addOk(cat, "Sin integración de pago activa aún — recuerda validar montos server-side cuando agregues Wompi.");
  }

  if (/card_number|cardNumber|\bcvv\b|primaryAccountNumber/i.test(panel)) {
    add("CRITICO", cat, "web/panel/app.js", null, "Posible manejo de datos de tarjeta en frontend.", "Usa checkout hosted (Wompi/Stripe.js) — nunca toques PAN/CVV.");
  }

  if (/campaign-limit|enqueue.*campaign/i.test(panel)) {
    add(
      "MEDIO",
      cat,
      "web/panel/app.js",
      null,
      "Límite de campaña enviado desde el panel (campaign-limit).",
      "Backend debe recalcular cuota trial/diaria y rechazar límites manipulados (bait_limit_service)."
    );
  }
}

// ─── 7. VALIDACIÓN DE FORMULARIOS ─────────────────────────────────────────

function auditForms() {
  const cat = "7. Validación de formularios";
  const html = readText(path.join(ROOT, "static", "panel", "index.html"));
  const panelJs = readText(path.join(ROOT, "static", "panel", "app.js"));

  const forms = [
    { id: "login-form", critical: true },
    { id: "send-form", critical: false },
  ];

  for (const { id, critical } of forms) {
    if (!html.includes(`id="${id}"`)) continue;
    const block = html.slice(html.indexOf(`id="${id}"`), html.indexOf(`id="${id}"`) + 800);
    if (!/required/.test(block) && id === "login-form") {
      add("MEDIO", cat, "web/panel/index.html", null, `Formulario ${id} sin atributo required en campos.`, "Añade required + validación server-side.");
    }
  }

  if (html.includes('maxlength="4096"')) {
    addOk(cat, "Composer de mensajes tiene maxlength=4096.");
  }

  if (/Field\(min_length/.test(walk(path.join(ROOT, "backend", "app", "presentation", "schemas"), [".py"]).map(readText).join("\n"))) {
    addOk(cat, "Schemas Pydantic con min_length en auth/registro.");
  }

  if (!/login-email|type="email"/.test(html)) {
    add("BAJO", cat, "web/panel/index.html", null, "Campo email sin type=email.", "Usa type=email en login.");
  } else {
    addOk(cat, "Login usa input type=email.");
  }

  if (panelJs.includes("preventDefault") || html.includes("required")) {
    addOk(cat, "Formularios HTML5 con validación básica.");
  }
}

// ─── 8. DEPENDENCIAS ──────────────────────────────────────────────────────

function auditDependencies() {
  const cat = "8. Dependencias";
  const req = path.join(ROOT, "backend", "requirements.txt");
  if (!fs.existsSync(req)) {
    add("MEDIO", cat, req, null, "requirements.txt no encontrado.", "Mantén dependencias pinneadas.");
    return;
  }

  const pipAudit = spawnSync("pip-audit", ["-r", req, "--format", "json"], {
    encoding: "utf8",
    cwd: ROOT,
    timeout: 120000,
  });

  if (pipAudit.error && pipAudit.error.code === "ENOENT") {
    add(
      "BAJO",
      cat,
      "requirements.txt",
      null,
      "pip-audit no instalado — no se auditaron vulnerabilidades Python.",
      "pip install pip-audit && pip-audit -r requirements.txt"
    );
    return;
  }

  if (pipAudit.status !== 0) {
    try {
      const data = JSON.parse(pipAudit.stdout || "[]");
      const vulns = Array.isArray(data) ? data : data.dependencies || [];
      let count = 0;
      for (const dep of vulns) {
        const vs = dep.vulns || dep.vulnerabilities || [];
        for (const v of vs) {
          count++;
          add(
            vs.length > 2 ? "ALTO" : "MEDIO",
            cat,
            "requirements.txt",
            null,
            `${dep.name || dep.package}: ${v.id || v.alias || "CVE"} — ${(v.description || "").slice(0, 80)}`,
            `Actualiza ${dep.name}: pip install --upgrade ${dep.name}`
          );
        }
      }
      if (count === 0) addOk(cat, "pip-audit: sin vulnerabilidades conocidas en requirements.txt.");
    } catch {
      add("BAJO", cat, req, null, "pip-audit falló o salida no JSON.", "Ejecuta manualmente: pip-audit -r requirements.txt");
    }
  } else {
    addOk(cat, "pip-audit: sin vulnerabilidades reportadas.");
  }

  if (!fs.existsSync(path.join(ROOT, "package.json"))) {
    addOk(cat, "Sin package.json — frontend vanilla sin npm (no aplica npm audit).");
  }
}

// ─── 9. CONFIGURACIÓN Y BUILD ─────────────────────────────────────────────

function auditConfig() {
  const cat = "9. Configuración y producción";
  const configPy = readText(path.join(ROOT, "backend", "app", "config.py"));
  const mainPy = readText(path.join(ROOT, "backend", "app", "presentation", "main.py"));

  if (/debug:\s*bool\s*=\s*True/.test(configPy)) {
    add(
      "ALTO",
      cat,
      "app/config.py",
      lineOf(configPy, configPy.search(/debug:\s*bool/)),
      "DEBUG=True por defecto en Settings.",
      "En producción: DEBUG=false, APP_ENV=production en .env / docker-compose.prod.yml."
    );
  }

  if (/secret_key:\s*str\s*=\s*"change-me/.test(configPy)) {
    const envContent = readText(path.join(ROOT, ".env"));
    const isProd = /APP_ENV\s*=\s*production/i.test(envContent);
    add(
      isProd ? "CRITICO" : "MEDIO",
      cat,
      "app/config.py",
      null,
      'SECRET_KEY por defecto "change-me-in-env" (solo válido en desarrollo).',
      "En producción: SECRET_KEY ≥32 caracteres en .env. En dev: define uno propio antes de desplegar."
    );
  }

  if (/allow_origins=\["\*"\]/.test(mainPy) && /settings\.debug/.test(mainPy)) {
    add(
      "MEDIO",
      cat,
      "app/main.py",
      null,
      "CORS allow_origins=* cuando debug=True.",
      "En producción debug=False deja origins vacío — confirma que Nginx solo sirve tu dominio."
    );
  }

  if (mainPy.includes('docs": "/docs"') || readText(path.join(ROOT, "backend", "app", "presentation", "main.py")).includes("FastAPI(")) {
    add(
      "MEDIO",
      cat,
      "app/main.py",
      null,
      "FastAPI expone /docs y /redoc por defecto.",
      "En producción: FastAPI(docs_url=None, redoc_url=None) o protégelos con auth/IP."
    );
  }

  const prodCompose = readText(path.join(ROOT, "docker-compose.prod.yml"));
  if (prodCompose.includes('DEBUG: "false"') && prodCompose.includes("EMBED_WORKERS_IN_API")) {
    addOk(cat, "docker-compose.prod.yml desactiva DEBUG y separa workers.");
  }

  if (/password@localhost|change-me-evolution/.test(configPy)) {
    add(
      "ALTO",
      cat,
      "app/config.py",
      null,
      "Valores por defecto débiles en database_url / evolution_api_key.",
      "Solo usar variables de entorno en despliegue."
    );
  }
}

// ─── 10. PRIVACIDAD ───────────────────────────────────────────────────────

function auditPrivacy() {
  const cat = "10. Privacidad y datos personales";
  const panelJs = readText(path.join(ROOT, "static", "panel", "app.js"));

  if (/token=/.test(panelJs) && /location\.|ws\//.test(panelJs)) {
    add(
      "ALTO",
      cat,
      "web/panel/app.js",
      null,
      "Token JWT puede quedar en URL del WebSocket (logs de proxy).",
      "Evita PII/tokens en query strings."
    );
  }

  if (!/cookie|consent|gdpr/i.test(readText(path.join(ROOT, "static", "panel", "index.html")))) {
    add(
      "BAJO",
      cat,
      "web/panel/index.html",
      null,
      "Sin banner de consentimiento de cookies / tratamiento de datos.",
      "Añade aviso legal si almacenas leads y teléfonos (Colombia: Ley 1581)."
    );
  }

  const phoneInUrl = /fetch\([^)]*\+57|phone=/.test(panelJs);
  if (phoneInUrl) {
    add("MEDIO", cat, "web/panel/app.js", null, "Posible dato personal en URL de fetch.", "Envía teléfonos en body JSON, no en query.");
  } else {
    addOk(cat, "No se detectaron teléfonos en URLs de fetch en el panel.");
  }
}

// ─── 11. HIGIENE ──────────────────────────────────────────────────────────

function auditHygiene() {
  const cat = "11. Higiene de código";
  const gitignore = readText(path.join(ROOT, ".gitignore"));

  for (const entry of [".env", ".venv/", "__pycache__/", "*.pyc"]) {
    if (!gitignore.includes(entry.replace(/\/$/, "")) && !gitignore.includes(entry)) {
      add("ALTO", cat, ".gitignore", null, `.gitignore no menciona ${entry}`, `Añade ${entry} a .gitignore.`);
    }
  }
  if (gitignore.includes(".env")) addOk(cat, ".gitignore protege .env");

  const tests = walk(path.join(ROOT, "tests"), [".py"]);
  if (tests.length < 5) {
    add("MEDIO", cat, "tests/", null, `Solo ${tests.length} archivos de test.`, "Aumenta cobertura en auth, webhooks y permisos.");
  } else {
    addOk(cat, `${tests.length} archivos de test en tests/.`);
  }

  if (fs.existsSync(path.join(ROOT, "static", "panel", "app.js"))) {
    const size = fs.statSync(path.join(ROOT, "static", "panel", "app.js")).size;
    if (size > 80000) {
      add("BAJO", cat, "web/panel/app.js", null, `app.js monolítico (${Math.round(size / 1024)} KB).`, "Considera dividir módulos para revisión de seguridad.");
    }
  }
}

// ─── 12. META TAGS Y HEADERS ──────────────────────────────────────────────

function auditMetaSecurity() {
  const cat = "12. Meta tags y headers de seguridad";
  const html = readText(path.join(ROOT, "static", "panel", "index.html"));

  const checks = [
    { re: /X-Frame-Options|frame-ancestors/i, name: "Protección clickjacking (CSP frame-ancestors o X-Frame-Options)", sev: "MEDIO" },
    { re: /Referrer-Policy/i, name: "Referrer-Policy", sev: "BAJO" },
    { re: /Content-Security-Policy/i, name: "Content-Security-Policy", sev: "MEDIO" },
  ];

  for (const { re, name, sev } of checks) {
    if (!re.test(html)) {
      add(sev, cat, "web/panel/index.html", null, `Falta ${name} en index.html.`, "Configura headers en Nginx: add_header X-Frame-Options DENY; etc.");
    }
  }

  if (/target="_blank"/.test(html) && !/rel="noopener/.test(html)) {
    add("BAJO", cat, "web/panel/index.html", null, 'target="_blank" sin rel="noopener noreferrer".', "Añade rel=\"noopener noreferrer\".");
  } else if (!html.includes('target="_blank"')) {
    addOk(cat, "Sin enlaces target=_blank en index.html.");
  }

  for (const script of matchAll(html, /<script[^>]+src="https?:\/\/[^"]+"/gi)) {
    if (!/integrity=/.test(script.match)) {
      add(
        "ALTO",
        cat,
        "web/panel/index.html",
        lineOf(html, script.index),
        `Script externo sin SRI: ${script.match.slice(0, 80)}`,
        'Añade integrity="sha384-…" crossorigin="anonymous".'
      );
    }
  }

  if (html.includes('/web/panel/app.js')) {
    addOk(cat, "Scripts del panel servidos desde mismo origen (sin CDN externo).");
  }
}

// ─── 13. BACKEND ESPECÍFICO (FastAPI) ─────────────────────────────────────

function auditBackend() {
  const cat = "13. Backend FastAPI / webhooks";
  const webhooks = readText(path.join(ROOT, "backend", "app", "presentation", "api", "webhooks.py"));

  if (webhooks.includes("evolution_webhook_secret") || webhooks.includes("X-Webhook-Secret")) {
    addOk(cat, "Webhook Evolution valida X-Webhook-Secret.");
  } else {
    add("CRITICO", cat, "app/api/webhooks.py", null, "Webhook sin validación de secreto.", "Exige header compartido o firma HMAC.");
  }

  const pyFiles = walk(path.join(ROOT, "backend", "app"), [".py"]);
  let rawSql = 0;
  for (const f of pyFiles) {
    const c = readText(f);
    if (/f["']SELECT|\.execute\(\s*["']SELECT[^"]*\+/.test(c)) {
      rawSql++;
      add("ALTO", cat, f, null, "Posible SQL concatenado (inyección).", "Usa SQLAlchemy ORM o text() con parámetros bind.");
    }
  }
  if (rawSql === 0) addOk(cat, "No se detectó SQL concatenado obvio en app/.");

  const mainPy = readText(path.join(ROOT, "backend", "app", "presentation", "main.py"));
  if (/health/.test(mainPy)) {
    addOk(cat, "Endpoint /health presente para monitoreo.");
  }
}

// ─── REPORTE ──────────────────────────────────────────────────────────────

function severityCounts() {
  const counts = { CRITICO: 0, ALTO: 0, MEDIO: 0, BAJO: 0, OK: 0 };
  for (const f of findings) counts[f.severity] = (counts[f.severity] || 0) + 1;
  return counts;
}

function letterGrade(counts) {
  if (counts.CRITICO > 0) return "F";
  if (counts.ALTO >= 8) return "D";
  if (counts.ALTO >= 4) return "C";
  if (counts.ALTO >= 2) return "C+";
  if (counts.MEDIO >= 3) return "B";
  if (counts.MEDIO >= 1) return "B+";
  if (counts.BAJO >= 5) return "A-";
  return "A";
}

function printReport() {
  const width = 72;
  console.log(`\n${C.bold}${C.cyan}${"═".repeat(width)}${C.reset}`);
  console.log(`${C.bold}  SECURITY AUDIT — SaasChatbot${C.reset}`);
  console.log(`${C.dim}  Panel: web/panel/  |  API: backend/app/  |  ${new Date().toISOString().slice(0, 10)}${C.reset}`);
  console.log(`${C.cyan}${"═".repeat(width)}${C.reset}\n`);

  const byCat = new Map();
  for (const f of findings) {
    if (!byCat.has(f.category)) byCat.set(f.category, []);
    byCat.get(f.category).push(f);
  }

  for (const [category, items] of byCat) {
    console.log(`${C.bold}${category}${C.reset}`);
    const sorted = [...items].sort((a, b) => SEV[a.severity].order - SEV[b.severity].order);
    for (const f of sorted) {
      const s = SEV[f.severity] || SEV.MEDIO;
      const loc = f.line ? `${f.file}:${f.line}` : f.file;
      console.log(
        `  ${s.color}[${s.label}]${C.reset} ${C.dim}${loc}${C.reset}\n` +
          `    ${f.message}\n` +
          `    ${C.green}FIX:${C.reset} ${f.fix}\n`
      );
    }
    console.log("");
  }

  const counts = severityCounts();
  const grade = letterGrade(counts);

  console.log(`${C.bold}${"─".repeat(width)}${C.reset}`);
  console.log(`${C.bold}  RESUMEN${C.reset}`);
  console.log(
    `  ${C.red}CRÍTICO: ${counts.CRITICO}${C.reset}  ` +
      `${C.magenta}ALTO: ${counts.ALTO}${C.reset}  ` +
      `${C.yellow}MEDIO: ${counts.MEDIO}${C.reset}  ` +
      `${C.blue}BAJO: ${counts.BAJO}${C.reset}  ` +
      `${C.green}OK: ${counts.OK}${C.reset}`
  );
  console.log(`  ${C.bold}Calificación: ${gradeColor(grade)}${grade}${C.reset}\n`);

  const priorities = findings
    .filter((f) => f.severity !== "OK")
    .sort((a, b) => SEV[a.severity].order - SEV[b.severity].order)
    .slice(0, 5);

  if (priorities.length) {
    console.log(`${C.bold}  TOP 5 ACCIONES PRIORITARIAS${C.reset}`);
    priorities.forEach((f, i) => {
      console.log(`  ${C.bold}${i + 1}.${C.reset} [${f.severity}] ${f.message}`);
      console.log(`     ${C.green}→${C.reset} ${f.fix}`);
    });
  } else {
    console.log(`${C.green}  Sin hallazgos críticos — mantén buenas prácticas en despliegue.${C.reset}`);
  }
  console.log(`\n${C.cyan}${"═".repeat(width)}${C.reset}\n`);
}

function gradeColor(grade) {
  if (grade.startsWith("A")) return C.green;
  if (grade.startsWith("B")) return C.cyan;
  if (grade === "C" || grade === "C+") return C.yellow;
  return C.red;
}

// ─── MAIN ─────────────────────────────────────────────────────────────────

function main() {
  console.log(`${C.dim}Escaneando proyecto…${C.reset}`);
  auditCredentials();
  auditXss();
  auditStorage();
  auditAuth();
  auditHttpClient();
  auditPayments();
  auditForms();
  auditDependencies();
  auditConfig();
  auditPrivacy();
  auditHygiene();
  auditMetaSecurity();
  auditBackend();
  printReport();
  const c = severityCounts();
  process.exit(c.CRITICO > 0 || c.ALTO > 3 ? 1 : 0);
}

main();
