export const SESSION_REVOKED_KEY = 'omitel_session_revoked';

export function isSessionRevoked(): boolean {
  return typeof sessionStorage !== 'undefined' && sessionStorage.getItem(SESSION_REVOKED_KEY) === '1';
}

export function setSessionRevoked(revoked: boolean): void {
  if (typeof sessionStorage === 'undefined') {
    return;
  }
  if (revoked) {
    sessionStorage.setItem(SESSION_REVOKED_KEY, '1');
  } else {
    sessionStorage.removeItem(SESSION_REVOKED_KEY);
  }
}

export function clearSessionRevoked(): void {
  setSessionRevoked(false);
}
