/** De dónde llegó el visitante (UTM, referido y lead de siochat): se guarda al entrar y viaja al registrarse. */
const STORAGE_KEY = 'omitel_attribution';
const KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'ref', 'lead'] as const;
const MAX_AGE_MS = 30 * 24 * 3600 * 1000;

export type Attribution = Partial<Record<(typeof KEYS)[number], string>>;

/** El último link con parámetros gana: es el que lo trajo a registrarse. */
export function captureAttribution(search: string = window.location.search): void {
  const params = new URLSearchParams(search);
  const found: Attribution = {};
  for (const key of KEYS) {
    const value = params.get(key)?.trim().slice(0, 200);
    if (value) found[key] = value;
  }
  if (!Object.keys(found).length) return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ at: Date.now(), values: found }));
  } catch {
    /* Navegación privada o almacenamiento lleno: el registro sigue sin atribución. */
  }
}

export function storedAttribution(): Attribution | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { at?: number; values?: Attribution };
    if (!parsed.values || !parsed.at || Date.now() - parsed.at > MAX_AGE_MS) return null;
    return parsed.values;
  } catch {
    return null;
  }
}

export function attributionQuery(): string {
  const values = storedAttribution();
  if (!values) return '';
  return Object.entries(values)
    .map(([key, value]) => `&${encodeURIComponent(key)}=${encodeURIComponent(value ?? '')}`)
    .join('');
}
