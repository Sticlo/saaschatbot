import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LEGAL } from '../../../core/legal';
import { CookieConsentService } from '../../../core/services/cookie-consent.service';
import { ShellComponent } from '../../../layout/shell/shell.component';

@Component({
  selector: 'app-cookies',
  imports: [ShellComponent, RouterLink],
  templateUrl: './cookies.component.html',
  styleUrl: '../legal.scss',
})
export class CookiesComponent {
  readonly legal = LEGAL;
  readonly cookies = inject(CookieConsentService);
}
