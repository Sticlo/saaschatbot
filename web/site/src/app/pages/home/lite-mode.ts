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

export function markLiteGpu(): void {
  document.documentElement.classList.add(LITE_GPU_CLASS);
  try {
    localStorage.setItem(LITE_GPU_KEY, '1');
  } catch {
    /* Ídem. */
  }
}
