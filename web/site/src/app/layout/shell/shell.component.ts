import { AsyncPipe, NgIf } from '@angular/common';
import { Component, HostBinding, HostListener, OnInit, inject } from '@angular/core';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter } from 'rxjs/operators';

import { CONTACT, phoneUrl, whatsappUrl } from '../../core/contact';
import { environment } from '../../../environments/environment';
import { UserMe } from '../../core/models/auth.model';
import { SubscriptionSummary } from '../../core/models/billing.model';
import { SessionService } from '../../core/services/session.service';

@Component({
  selector: 'app-shell',
  imports: [RouterLink, RouterLinkActive, NgIf, AsyncPipe],
  templateUrl: './shell.component.html',
  styleUrl: './shell.component.scss',
})
export class ShellComponent implements OnInit {
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);

  @HostBinding('class.shell-home')
  isHome = false;

  readonly appName = environment.appName;
  readonly year = new Date().getFullYear();
  readonly whatsappHref = whatsappUrl();
  readonly whatsappDisplay = CONTACT.whatsappDisplay;
  readonly phoneHref = phoneUrl();
  readonly phoneDisplay = CONTACT.phoneDisplay;

  /** Mismo origen que la landing (evita perder la cookie entre localhost y 127.0.0.1). */
  get panelUrl(): string {
    if (typeof window !== 'undefined') {
      return `${window.location.origin}${environment.panelUrl}`;
    }
    return environment.panelUrl;
  }
  readonly user$ = this.session.user$;
  readonly subscription$ = this.session.subscription$;

  userMenuOpen = false;
  mobileNavOpen = false;
  loggingOut = false;

  ngOnInit(): void {
    this.syncHomeRoute();
    this.router.events
      .pipe(filter((event) => event instanceof NavigationEnd))
      .subscribe(() => {
        this.userMenuOpen = false;
        this.mobileNavOpen = false;
        this.syncHomeRoute();
        this.session.refresh();
      });
  }

  private syncHomeRoute(): void {
    const url = this.router.url.split('?')[0];
    this.isHome = url === '/' || url === '';
  }

  @HostListener('document:click')
  closeUserMenu(): void {
    this.userMenuOpen = false;
    if (this.mobileNavOpen) {
      this.mobileNavOpen = false;
      this.syncBodyScrollLock();
    }
  }

  toggleMobileNav(event: MouseEvent): void {
    event.stopPropagation();
    this.mobileNavOpen = !this.mobileNavOpen;
    if (this.mobileNavOpen) {
      this.userMenuOpen = false;
    }
    this.syncBodyScrollLock();
  }

  closeMobileNav(): void {
    this.mobileNavOpen = false;
    this.syncBodyScrollLock();
  }

  private syncBodyScrollLock(): void {
    if (typeof document === 'undefined') {
      return;
    }
    document.body.style.overflow = this.mobileNavOpen ? 'hidden' : '';
  }

  toggleUserMenu(event: MouseEvent): void {
    event.stopPropagation();
    this.userMenuOpen = !this.userMenuOpen;
  }

  displayName(user: UserMe): string {
    const name = user.full_name?.trim();
    return name || user.email.split('@')[0];
  }

  userInitials(user: UserMe): string {
    const name = user.full_name?.trim();
    if (name) {
      const parts = name.split(/\s+/).filter(Boolean);
      if (parts.length >= 2) {
        return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
      }
      return parts[0].slice(0, 2).toUpperCase();
    }
    return user.email.slice(0, 2).toUpperCase();
  }

  planCtaLabel(subscription: SubscriptionSummary | null): string {
    if (subscription?.is_paid) {
      return 'Mi plan';
    }
    return 'Activar plan';
  }

  logout(): void {
    if (this.loggingOut) {
      return;
    }
    this.loggingOut = true;
    this.userMenuOpen = false;
    this.session.logout().subscribe({
      complete: () => {
        this.loggingOut = false;
        window.location.assign('/login');
      },
      error: () => {
        this.loggingOut = false;
        window.location.assign('/login');
      },
    });
  }
}
