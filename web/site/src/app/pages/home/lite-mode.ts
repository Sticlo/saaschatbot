/**
 * Teléfonos y tablets: la GPU de un Android de gama media no sostiene el fondo WebGL ni los
 * desenfoques animados encima. Debe coincidir con la media query «modo liviano» de home.component.scss.
 */
export const LITE_QUERY = '(max-width: 900px), (pointer: coarse)';

/** Clase en <html> para PCs cuya GPU no sostiene la landing completa; activa el mismo modo liviano. */
export const LITE_GPU_CLASS = 'is-lite-gpu';
const LITE_GPU_KEY = 'omitel:lite-gpu';

export function isLiteDevice(): boolean {
  const connection = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection;
  return window.matchMedia(LITE_QUERY).matches || !!connection?.saveData || isLiteGpu();
}

/** Marcado en una visita anterior (se recuerda) o en esta, tras medir los fps. */
export function isLiteGpu(): boolean {
  if (document.documentElement.classList.contains(LITE_GPU_CLASS)) {
    return true;
  }
  try {
    if (localStorage.getItem(LITE_GPU_KEY) === '1') {
      document.documentElement.classList.add(LITE_GPU_CLASS);
      return true;
    }
  } catch {
    /* Sin localStorage (modo privado estricto): se vuelve a medir en cada visita. */
  }
  return false;
}

/**
 * Corre `fn` cuando la página ya cargó y el navegador quedó libre: los efectos no compiten con
 * el texto, los botones ni la hidratación. Devuelve una función para cancelarlo.
 */
export function whenPageIdle(fn: () => void, maxWaitMs = 2500): () => void {
  let cancelled = false;
  let idleId = 0;
  let timer = 0;
  const w = window as Window & {
    requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
    cancelIdleCallback?: (id: number) => void;
  };
  const run = () => {
    if (!cancelled) {
      fn();
    }
  };
  const schedule = () => {
    if (cancelled) {
      return;
    }
    if (w.requestIdleCallback) {
      idleId = w.requestIdleCallback(run, { timeout: maxWaitMs });
    } else {
      timer = window.setTimeout(run, 300);
    }
  };
  if (document.readyState === 'complete') {
    schedule();
  } else {
    window.addEventListener('load', schedule, { once: true });
  }
  return () => {
    cancelled = true;
    window.removeEventListener('load', schedule);
    if (idleId) {
      w.cancelIdleCallback?.(idleId);
    }
    clearTimeout(timer);
  };
}

export function markLiteGpu(): void {
  document.documentElement.classList.add(LITE_GPU_CLASS);
  try {
    localStorage.setItem(LITE_GPU_KEY, '1');
  } catch {
    /* Ídem. */
  }
}
