import { isPlatformBrowser, NgIf } from '@angular/common';
import { Component, OnInit, PLATFORM_ID, inject } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { combineLatest, filter } from 'rxjs';

import { ShellComponent } from '../../layout/shell/shell.component';
import { oauthStartUrl } from '../../core/oauth-url';
import { AuthService } from '../../core/services/auth.service';
import { SessionService } from '../../core/services/session.service';

type AuthStep = 'start' | 'login' | 'register';

@Component({
  selector: 'app-login',
  imports: [ShellComponent, ReactiveFormsModule, RouterLink, NgIf],
  templateUrl: './login.component.html',
  styleUrl: './login.component.scss',
})
export class LoginComponent implements OnInit {
  private readonly fb = inject(FormBuilder);
  private readonly auth = inject(AuthService);
  private readonly session = inject(SessionService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly platformId = inject(PLATFORM_ID);

  step: AuthStep = 'start';
  submitting = false;
  oauthLoading = false;
  backendOnline = true;
  error = '';
  signupMode = false;
  providers = { google: false, github: false };
  nextPath = '/precios';
  googleOAuthUrl = oauthStartUrl('google', '/precios');
  githubOAuthUrl = oauthStartUrl('github', '/precios');

  readonly emailForm = this.fb.nonNullable.group({
    email: ['', [Validators.required, Validators.email]],
  });

  readonly loginForm = this.fb.nonNullable.group({
    password: ['', [Validators.required, Validators.minLength(1)]],
  });

  readonly registerForm = this.fb.nonNullable.group({
    business_name: ['', [Validators.required, Validators.minLength(2)]],
    owner_name: ['', [Validators.required, Validators.minLength(2)]],
    password: ['', [Validators.required, Validators.minLength(8)]],
  });

  ngOnInit(): void {
    this.signupMode = this.route.snapshot.routeConfig?.path === 'registro';
    const oauthError = this.route.snapshot.queryParamMap.get('error');
    if (oauthError) {
      this.error = oauthError;
    }

    const plan = this.route.snapshot.queryParamMap.get('plan');
    this.nextPath = '/precios';
    if (plan) {
      this.nextPath = `/precios?plan=${encodeURIComponent(plan)}`;
    }
    this.googleOAuthUrl = oauthStartUrl('google', this.nextPath);
    this.githubOAuthUrl = oauthStartUrl('github', this.nextPath);

    if (!isPlatformBrowser(this.platformId)) {
      return;
    }

    combineLatest([this.session.ready$, this.session.user$])
      .pipe(filter(([ready]) => ready))
      .subscribe(([, user]) => {
        if (user && this.session.isLoggedIn()) {
          this.router.navigate(['/precios']);
        }
      });

    this.auth.getProviders().subscribe({
      next: (providers) => {
        this.providers = providers;
        this.backendOnline = true;
      },
      error: () => {
        this.providers = { google: false, github: false };
        this.backendOnline = false;
        if (!this.error) {
          this.error =
            'No pudimos contactar el servidor. Inicia el backend (puerto 8000) e intenta de nuevo.';
        }
      },
    });
  }

  canUseOAuth(provider: 'google' | 'github'): boolean {
    return this.backendOnline && this.providers[provider] && !this.oauthLoading;
  }

  startOAuth(provider: 'google' | 'github', event: Event): void {
    event.preventDefault();
    if (this.oauthLoading) {
      return;
    }
    if (!this.backendOnline) {
      this.error =
        'No pudimos contactar el servidor. Inicia el backend (puerto 8000) e intenta de nuevo.';
      return;
    }
    if (!this.providers[provider]) {
      this.error =
        provider === 'google'
          ? 'Google no está configurado. Añade GOOGLE_CLIENT_ID al .env del backend.'
          : 'GitHub no está configurado en el servidor.';
      return;
    }
    this.oauthLoading = true;
    this.error = '';
    this.session.markLoggedIn();
    window.location.assign(oauthStartUrl(provider, this.nextPath));
  }

  get title(): string {
    if (this.step === 'login') return 'Bienvenido de nuevo';
    if (this.step === 'register') return 'Crea tu cuenta';
    return this.signupMode ? 'Crear cuenta' : 'Entrar al panel';
  }

  get subtitle(): string {
    if (this.step === 'login') return 'Ingresa tu contraseña para continuar.';
    if (this.step === 'register') return 'Completa los datos de tu negocio.';
    return 'Elige cómo quieres continuar.';
  }

  get email(): string {
    return this.emailForm.controls.email.value.trim().toLowerCase();
  }

  startGoogle(): void {
    this.error = '';
    this.auth.startGoogleOAuth();
  }

  startGithub(): void {
    this.error = '';
    this.auth.startGithubOAuth();
  }

  continueWithEmail(): void {
    if (this.emailForm.invalid || this.submitting) {
      this.emailForm.markAllAsTouched();
      return;
    }

    this.submitting = true;
    this.error = '';

    this.auth.lookupEmail({ email: this.email }).subscribe({
      next: ({ exists }) => {
        this.submitting = false;
        this.step = exists ? 'login' : 'register';
      },
      error: () => {
        this.submitting = false;
        this.error =
          'No pudimos contactar el servidor. Verifica que el backend esté corriendo e intenta de nuevo.';
      },
    });
  }

  backToStart(): void {
    this.step = 'start';
    this.error = '';
    this.loginForm.reset();
    this.registerForm.reset();
  }

  submitLogin(): void {
    if (this.loginForm.invalid || this.submitting) {
      this.loginForm.markAllAsTouched();
      return;
    }

    this.submitting = true;
    this.error = '';

    this.auth
      .login({ email: this.email, password: this.loginForm.controls.password.value })
      .subscribe({
        error: (err) => {
          this.submitting = false;
          this.error = err?.error?.detail || 'Email o contraseña incorrectos.';
        },
      });
  }

  submitRegister(): void {
    if (this.registerForm.invalid || this.submitting) {
      this.registerForm.markAllAsTouched();
      return;
    }

    this.submitting = true;
    this.error = '';

    const { business_name, owner_name, password } = this.registerForm.getRawValue();
    this.auth
      .register({
        business_name,
        owner_name,
        email: this.email,
        password,
      })
      .subscribe({
        error: (err) => {
          this.submitting = false;
          this.error =
            err?.error?.detail ||
            'No pudimos crear la cuenta. Revisa los datos e intenta de nuevo.';
        },
      });
  }
}
