import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, of, timeout } from 'rxjs';

import { API_BASE_URL } from '../tokens';
import {
  BillingConfig,
  CheckoutSession,
  CheckoutStatus,
  SubscriptionSummary,
} from '../models/billing.model';

const API_TIMEOUT_MS = 8000;

@Injectable({ providedIn: 'root' })
export class BillingService {
  private readonly http = inject(HttpClient);
  private readonly apiBase = inject(API_BASE_URL);

  getConfig(): Observable<BillingConfig> {
    return this.http.get<BillingConfig>(`${this.apiBase}/api/v1/billing/config`).pipe(
      timeout(API_TIMEOUT_MS),
      catchError(() =>
        of({ enabled: false, public_key: null, sandbox: false, sync_enabled: false }),
      ),
    );
  }

  getMySubscription(): Observable<SubscriptionSummary | null> {
    return this.http
      .get<SubscriptionSummary>(`${this.apiBase}/api/v1/subscriptions/me`, {
        withCredentials: true,
      })
      .pipe(timeout(API_TIMEOUT_MS), catchError(() => of(null)));
  }

  createCheckout(planSlug: string): Observable<CheckoutSession> {
    return this.http
      .post<CheckoutSession>(
        `${this.apiBase}/api/v1/billing/checkout`,
        { plan_slug: planSlug },
        { withCredentials: true },
      )
      .pipe(timeout(API_TIMEOUT_MS));
  }

  getCheckoutStatus(reference: string): Observable<CheckoutStatus> {
    return this.http
      .get<CheckoutStatus>(`${this.apiBase}/api/v1/billing/checkout/${reference}`, {
        withCredentials: true,
      })
      .pipe(timeout(API_TIMEOUT_MS));
  }

  syncCheckout(reference: string, transactionId: string): Observable<CheckoutStatus> {
    return this.http
      .post<CheckoutStatus>(
        `${this.apiBase}/api/v1/billing/checkout/${reference}/sync`,
        { transaction_id: transactionId },
        { withCredentials: true },
      )
      .pipe(timeout(API_TIMEOUT_MS));
  }
}
