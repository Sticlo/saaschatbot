import { environment } from '../../environments/environment';

/** URL absoluta para navegar: las rutas del panel viven en el dominio de la app. */
export function appUrl(path: string): string {
  if (path.startsWith('/panel') && environment.appOrigin) {
    return `${environment.appOrigin}${path}`;
  }
  return typeof window !== 'undefined' ? `${window.location.origin}${path}` : path;
}

/** El OAuth arranca en la API: allí queda la cookie de sesión. */
export function oauthStartUrl(provider: 'google' | 'github', nextPath: string): string {
  const next = encodeURIComponent(nextPath);
  return `${environment.apiUrl}/api/v1/auth/${provider}/start?next=${next}`;
}
