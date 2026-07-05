export function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Safari private mode and blocked storage should not break the app.
  }
}

export function readSessionStorage(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeSessionStorage(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // Ignore blocked session storage.
  }
}

export function removeSessionStorage(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // Ignore blocked session storage.
  }
}
