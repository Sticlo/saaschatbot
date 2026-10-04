import { Component, effect, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { CookieConsentService } from '../../core/services/cookie-consent.service';

@Component({
  selector: 'app-cookie-banner',
  imports: [RouterLink],
  templateUrl: './cookie-banner.component.html',
  styleUrl: './cookie-banner.component.scss',
})
export class CookieBannerComponent {
  readonly cookies = inject(CookieConsentService);
  readonly analytics = signal(false);
  readonly marketing = signal(false);

  constructor() {
    effect(() => {
      if (this.cookies.showSettings()) {
        const current = this.cookies.consent();
        this.analytics.set(current?.analytics ?? false);
        this.marketing.set(current?.marketing ?? false);
      }
    });
  }

  saveSelection(): void {
    this.cookies.save({ analytics: this.analytics(), marketing: this.marketing() });
  }
}
