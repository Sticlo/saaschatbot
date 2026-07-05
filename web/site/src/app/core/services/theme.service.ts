import { Injectable, PLATFORM_ID, inject } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { BehaviorSubject } from 'rxjs';

export type ThemeMode = 'light' | 'dark';

const STORAGE_KEY = 'omitel.theme';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly platformId = inject(PLATFORM_ID);
  private readonly themeSubject = new BehaviorSubject<ThemeMode>('light');

  readonly theme$ = this.themeSubject.asObservable();

  constructor() {
    if (!isPlatformBrowser(this.platformId)) {
      return;
    }

    const current = document.documentElement.getAttribute('data-theme');
    if (current === 'dark' || current === 'light') {
      this.themeSubject.next(current);
      return;
    }

    this.setTheme(this.readPreferredTheme(), false);
  }

  get theme(): ThemeMode {
    return this.themeSubject.value;
  }

  toggle(): void {
    this.setTheme(this.theme === 'dark' ? 'light' : 'dark');
  }

  setTheme(theme: ThemeMode, persist = true): void {
    this.themeSubject.next(theme);

    if (!isPlatformBrowser(this.platformId)) {
      return;
    }

    document.documentElement.setAttribute('data-theme', theme);

    if (persist) {
      localStorage.setItem(STORAGE_KEY, theme);
    }
  }

  private readPreferredTheme(): ThemeMode {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'dark' || saved === 'light') {
      return saved;
    }

    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
}
