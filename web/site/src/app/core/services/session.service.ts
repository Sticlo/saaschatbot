import { Injectable, inject } from '@angular/core';
import {
  BehaviorSubject,
  Observable,
  catchError,
  forkJoin,
  map,
  of,
  switchMap,
  tap,
} from 'rxjs';

import { UserMe } from '../models/auth.model';
import { SubscriptionSummary } from '../models/billing.model';
import { clearSessionRevoked, isSessionRevoked } from '../session-revocation';
import { AuthService } from './auth.service';
import { BillingService } from './billing.service';

@Injectable({ providedIn: 'root' })
export class SessionService {
  private readonly auth = inject(AuthService);
  private readonly billing = inject(BillingService);

  private readonly userSubject = new BehaviorSubject<UserMe | null>(null);
  private readonly subscriptionSubject = new BehaviorSubject<SubscriptionSummary | null>(null);
  private readonly billingEnabledSubject = new BehaviorSubject(false);
  private readonly readySubject = new BehaviorSubject(false);

  readonly user$ = this.userSubject.asObservable();
  readonly subscription$ = this.subscriptionSubject.asObservable();
  readonly billingEnabled$ = this.billingEnabledSubject.asObservable();
  readonly ready$ = this.readySubject.asObservable();

  initialize(): Observable<void> {
    if (isSessionRevoked()) {
      this.clear();
      return of(undefined);
    }
    return this.loadSession().pipe(map(() => undefined));
  }

  refresh(): void {
    if (isSessionRevoked()) {
      this.clear();
      return;
    }
    this.loadSession().subscribe();
  }

  refreshSubscription(): void {
    this.billing.getMySubscription().subscribe((sub) => {
      this.subscriptionSubject.next(sub);
    });
  }

  snapshot(): UserMe | null {
    return this.userSubject.value;
  }

  markLoggedIn(): void {
    clearSessionRevoked();
  }

  clear(): void {
    this.userSubject.next(null);
    this.subscriptionSubject.next(null);
    this.billingEnabledSubject.next(false);
    this.readySubject.next(true);
  }

  logout(): Observable<void> {
    return this.auth.logout().pipe(tap(() => this.clear()));
  }

  private loadSession(): Observable<void> {
    return forkJoin({
      user: this.auth.getMe(),
      billing: this.billing.getConfig(),
    }).pipe(
      switchMap(({ user, billing }) => {
        this.userSubject.next(user);
        this.billingEnabledSubject.next(!!billing.enabled);
        if (!user) {
          this.subscriptionSubject.next(null);
          this.readySubject.next(true);
          return of(undefined);
        }
        return this.billing.getMySubscription().pipe(
          tap((sub) => {
            this.subscriptionSubject.next(sub);
            this.readySubject.next(true);
          }),
          map(() => undefined),
        );
      }),
      catchError(() => {
        this.readySubject.next(true);
        return of(undefined);
      }),
    );
  }
}
