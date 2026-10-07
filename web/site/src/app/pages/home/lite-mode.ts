/**
 * Teléfonos y tablets: la GPU de un Android de gama media no sostiene el fondo WebGL ni los
 * desenfoques animados encima. Debe coincidir con la media query «modo liviano» de home.component.scss.
 */
export const LITE_QUERY = '(max-width: 900px), (pointer: coarse)';

export function isLiteDevice(): boolean {
  const connection = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection;
  return window.matchMedia(LITE_QUERY).matches || !!connection?.saveData;
}
