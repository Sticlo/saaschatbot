/** Same-origin OAuth start (via dev proxy or production reverse proxy). */
export function oauthStartUrl(provider: 'google' | 'github', nextPath: string): string {
  const next = encodeURIComponent(nextPath);
  return `/api/v1/auth/${provider}/start?next=${next}`;
}
