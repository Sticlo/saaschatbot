import { isPlatformBrowser } from '@angular/common';
import { Injectable, PLATFORM_ID, computed, inject, signal } from '@angular/core';

import { readStorage, writeStorage } from '../safe-storage';

export type CookieCategory = 'analytics' | 'marketing';

export interface CookieConsent {
  version: number;
  necessary: true;
  analytics: boolean;
  marketing: boolean;
  decidedAt: string;
}

const STORAGE_KEY = 'omitel_cookie_consent';
/** Subir cuando cambien las categorías o los proveedores: vuelve a preguntar. */
const CONSENT_VERSION = 1;

/**
 * Hoy el sitio solo usa cookies necesarias. Cualquier script de analítica o publicidad que se
 * agregue debe cargarse únicamente si `allows('analytics' | 'marketing')` es true.
 */
@Injectable({ providedIn: 'root' })
export class CookieConsentService {
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));
  private readonly state = signal<CookieConsent | null>(this.load());
  private readonly settingsOpen = signal(false);

  readonly consent = this.state.asReadonly();
  readonly needsDecision = computed(() => this.isBrowser && this.state() === null);
  readonly showSettings = this.settingsOpen.asReadonly();

  allows(category: CookieCategory): boolean {
    return this.state()?.[category] === true;
  }

  acceptAll(): void {
    this.save({ analytics: true, marketing: true });
  }

  rejectOptional(): void {
    this.save({ analytics: false, marketing: false });
  }

  save(choice: { analytics: boolean; marketing: boolean }): void {
    const consent: CookieConsent = {
      version: CONSENT_VERSION,
      necessary: true,
      analytics: choice.analytics,
      marketing: choice.marketing,
      decidedAt: new Date().toISOString(),
    };
    writeStorage(STORAGE_KEY, JSON.stringify(consent));
    this.state.set(consent);
    this.settingsOpen.set(false);
  }

  openSettings(): void {
    this.settingsOpen.set(true);
  }

  closeSettings(): void {
    this.settingsOpen.set(false);
  }

  private load(): CookieConsent | null {
    if (!this.isBrowser) {
      return null;
    }
    try {
      const parsed = JSON.parse(readStorage(STORAGE_KEY) || 'null') as CookieConsent | null;
      return parsed?.version === CONSENT_VERSION ? parsed : null;
    } catch {
      return null;
    }
  }
}
