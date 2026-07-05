import { readSessionStorage, removeSessionStorage, writeSessionStorage } from './safe-storage';

export const SESSION_REVOKED_KEY = 'omitel_session_revoked';

export function isSessionRevoked(): boolean {
  return readSessionStorage(SESSION_REVOKED_KEY) === '1';
}

export function setSessionRevoked(revoked: boolean): void {
  if (revoked) {
    writeSessionStorage(SESSION_REVOKED_KEY, '1');
    return;
  }
  removeSessionStorage(SESSION_REVOKED_KEY);
}

export function clearSessionRevoked(): void {
  setSessionRevoked(false);
}
