import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, map, of, tap, timeout } from 'rxjs';

import { environment } from '../../../environments/environment';
import { API_BASE_URL } from '../tokens';
import { clearSessionRevoked } from '../session-revocation';
import {
  AuthProviders,
  ForgotPasswordRequest,
  ForgotPasswordResponse,
  LoginRequest,
  MagicLinkRequest,
  MagicLinkResponse,
  RegisterRequest,
  ResetPasswordRequest,
  TokenResponse,
  UserMe,
} from '../models/auth.model';

const API_TIMEOUT_MS = 8000;

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);
  private readonly apiBase = inject(API_BASE_URL);

  getMe(): Observable<UserMe | null> {
    return this.http
      .get<UserMe>(`${this.apiBase}/api/v1/auth/me`, { withCredentials: true })
      .pipe(timeout(API_TIMEOUT_MS), catchError(() => of(null)));
  }

  getProviders(): Observable<AuthProviders> {
    return this.http.get<AuthProviders>(`${this.apiBase}/api/v1/auth/providers`).pipe(
      timeout(API_TIMEOUT_MS),
      catchError(() => of({ google: false, github: false })),
    );
  }

  requestMagicLink(body: MagicLinkRequest): Observable<MagicLinkResponse> {
    return this.http
      .post<MagicLinkResponse>(`${this.apiBase}/api/v1/auth/magic-link`, body)
      .pipe(timeout(API_TIMEOUT_MS));
  }

  verifyMagicLink(token: string): Observable<TokenResponse> {
    return this.http
      .post<TokenResponse>(
        `${this.apiBase}/api/v1/auth/magic-link/verify`,
        { token },
        { withCredentials: true },
      )
      .pipe(timeout(API_TIMEOUT_MS), tap(() => this.goAfterAuth()));
  }

  register(body: RegisterRequest): Observable<TokenResponse> {
    return this.http
      .post<TokenResponse>(`${this.apiBase}/api/v1/auth/register`, body, {
        withCredentials: true,
      })
      .pipe(timeout(API_TIMEOUT_MS), tap(() => this.goToPanelWelcome()));
  }

  login(body: LoginRequest): Observable<TokenResponse> {
    return this.http
      .post<TokenResponse>(`${this.apiBase}/api/v1/auth/login`, body, {
        withCredentials: true,
      })
      .pipe(timeout(API_TIMEOUT_MS), tap(() => this.goAfterAuth()));
  }

  forgotPassword(body: ForgotPasswordRequest): Observable<ForgotPasswordResponse> {
    return this.http
      .post<ForgotPasswordResponse>(`${this.apiBase}/api/v1/auth/forgot-password`, body)
      .pipe(timeout(API_TIMEOUT_MS));
  }

  resetPassword(body: ResetPasswordRequest): Observable<TokenResponse> {
    return this.http
      .post<TokenResponse>(`${this.apiBase}/api/v1/auth/reset-password`, body, {
        withCredentials: true,
      })
      .pipe(timeout(API_TIMEOUT_MS), tap(() => this.goToPanelWelcome()));
  }

  logout(): Observable<void> {
    return this.http
      .post<void>(`${this.apiBase}/api/v1/auth/logout`, null, { withCredentials: true })
      .pipe(
        timeout(API_TIMEOUT_MS),
        catchError(() => of(undefined)),
        map(() => undefined),
      );
  }

  goAfterAuth(): void {
    clearSessionRevoked();
    if (typeof window !== 'undefined') {
      window.location.href = `${environment.panelUrl}?welcome=1`;
    }
  }

  goToPanelWelcome(): void {
    clearSessionRevoked();
    if (typeof window !== 'undefined') {
      window.location.href = `${environment.panelUrl}?welcome=1`;
    }
  }

  goToPanel(): void {
    if (typeof window !== 'undefined') {
      window.location.href = environment.panelUrl;
    }
  }
}
