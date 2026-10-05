import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, of, timeout } from 'rxjs';

import { API_BASE_URL } from '../tokens';
import {
  BillingConfig,
  CheckoutSession,
  CheckoutStatus,
  PaymentMethodRequest,
  PaymentMethodResult,
  SubscriptionSummary,
  WompiTerms,
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

  getWompiTerms(): Observable<WompiTerms> {
    return this.http
      .get<WompiTerms>(`${this.apiBase}/api/v1/billing/wompi-terms`, { withCredentials: true })
      .pipe(timeout(API_TIMEOUT_MS));
  }

  savePaymentMethod(body: PaymentMethodRequest): Observable<PaymentMethodResult> {
    return this.http
      .post<PaymentMethodResult>(`${this.apiBase}/api/v1/billing/payment-method`, body, {
        withCredentials: true,
      })
      .pipe(timeout(40_000));
  }

  removePaymentMethod(): Observable<void> {
    return this.http
      .delete<void>(`${this.apiBase}/api/v1/billing/payment-method`, { withCredentials: true })
      .pipe(timeout(API_TIMEOUT_MS * 2));
  }

  cancelSubscription(reason: string, feedback: string): Observable<void> {
    return this.http
      .post<void>(
        `${this.apiBase}/api/v1/billing/subscription/cancel`,
        { reason, feedback },
        { withCredentials: true },
      )
      .pipe(timeout(API_TIMEOUT_MS * 2));
  }

  resumeSubscription(): Observable<void> {
    return this.http
      .post<void>(`${this.apiBase}/api/v1/billing/subscription/resume`, {}, { withCredentials: true })
      .pipe(timeout(API_TIMEOUT_MS));
  }
}
