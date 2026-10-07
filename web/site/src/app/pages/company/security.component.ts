import { AsyncPipe } from '@angular/common';
import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { whatsappUrl } from '../../core/contact';
import { LEGAL } from '../../core/legal';
import { appUrl } from '../../core/oauth-url';
import { SessionService } from '../../core/services/session.service';
import { environment } from '../../../environments/environment';
import { ShellComponent } from '../../layout/shell/shell.component';

@Component({
  selector: 'app-security',
  imports: [ShellComponent, RouterLink, AsyncPipe],
  templateUrl: './security.component.html',
  styleUrl: './company.scss',
})
export class SecurityComponent {
  private readonly session = inject(SessionService);

  readonly user$ = this.session.user$;
  readonly legal = LEGAL;
  readonly officialApiHref = whatsappUrl(
    'Hola, me interesa Omitel con la API oficial de WhatsApp para mi empresa.',
  );

  get panelUrl(): string {
    return appUrl(environment.panelUrl);
  }
}
