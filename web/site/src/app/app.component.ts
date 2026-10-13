import { DOCUMENT } from '@angular/common';
import { Component, inject } from '@angular/core';
import { NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';

import { CookieBannerComponent } from './layout/cookie-banner/cookie-banner.component';

const SITE_ORIGIN = 'https://www.omitel.net';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, CookieBannerComponent],
  template: '<router-outlet /><app-cookie-banner />',
  styles: ':host { display: block; min-height: 100vh; }',
})
export class AppComponent {
  private readonly document = inject(DOCUMENT);

  constructor() {
    inject(Router)
      .events.pipe(filter((e): e is NavigationEnd => e instanceof NavigationEnd))
      .subscribe((e) => this.setCanonical(e.urlAfterRedirects));
  }

  private setCanonical(url: string): void {
    const path = url.split(/[?#]/)[0] || '/';
    let link = this.document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]');
    if (!link) {
      link = this.document.createElement('link');
      link.rel = 'canonical';
      this.document.head.appendChild(link);
    }
    link.href = SITE_ORIGIN + (path === '/' ? '/' : path);
  }
}
