import { AsyncPipe, NgIf } from '@angular/common';
import { Component, HostListener, OnInit, inject } from '@angular/core';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter } from 'rxjs/operators';

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

  readonly appName = environment.appName;
  readonly panelUrl = environment.panelUrl;
  readonly year = new Date().getFullYear();
  readonly user$ = this.session.user$;
  readonly subscription$ = this.session.subscription$;

  userMenuOpen = false;
  loggingOut = false;

  ngOnInit(): void {
    this.router.events
      .pipe(filter((event) => event instanceof NavigationEnd))
      .subscribe(() => {
        this.userMenuOpen = false;
        this.session.refresh();
      });
  }

  @HostListener('document:click')
  closeUserMenu(): void {
    this.userMenuOpen = false;
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
