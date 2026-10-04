import { isPlatformBrowser, NgIf, NgTemplateOutlet } from '@angular/common';
import { Component, OnInit, PLATFORM_ID, inject } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { ShellComponent } from '../../layout/shell/shell.component';
import { appUrl, oauthStartUrl } from '../../core/oauth-url';
import { environment } from '../../../environments/environment';
import { AuthService } from '../../core/services/auth.service';
import { SessionService } from '../../core/services/session.service';

const LEGAL_REQUIRED_MESSAGE =
  'Para crear la cuenta debes aceptar los Términos y Condiciones y la Política de Tratamiento de Datos Personales.';

type AuthStep = 'start' | 'signup-details' | 'sent';

@Component({
  selector: 'app-login',
  imports: [ShellComponent, ReactiveFormsModule, RouterLink, NgIf, NgTemplateOutlet],
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
  successMessage = '';
  devLink: string | null = null;
  signupMode = false;
  providers = { google: false, github: false };
  nextPath = '/panel?welcome=1';
  googleOAuthUrl = oauthStartUrl('google', '/panel?welcome=1');
  githubOAuthUrl = oauthStartUrl('github', '/panel?welcome=1');

  readonly authForm = this.fb.nonNullable.group({
    email: ['', [Validators.required, Validators.email]],
    business_name: [''],
    owner_name: [''],
    accept_legal: [false],
    accept_marketing: [false],
  });

  private resolveNextPath(): string {
    const fromQuery = this.route.snapshot.queryParamMap.get('next')?.trim();
    if (fromQuery && fromQuery.startsWith('/') && !fromQuery.startsWith('//')) {
      return fromQuery;
    }
    const plan = this.route.snapshot.queryParamMap.get('plan');
    if (plan) {
      return `/precios?plan=${encodeURIComponent(plan)}`;
    }
    return `${environment.panelUrl}?welcome=1`;
  }

  ngOnInit(): void {
    this.signupMode = this.route.snapshot.routeConfig?.path === 'registro';
    if (this.signupMode) {
      this.applySignupValidators();
    }
    const oauthError = this.route.snapshot.queryParamMap.get('error');
    if (oauthError) {
      this.error = oauthError;
    }

    this.nextPath = this.resolveNextPath();
    this.googleOAuthUrl = oauthStartUrl('google', this.nextPath);
    this.githubOAuthUrl = oauthStartUrl('github', this.nextPath);

    if (!isPlatformBrowser(this.platformId)) {
      return;
    }

    // Si ya hay sesión válida en el servidor, ir directo al destino (no volver a pedir login).
    if (this.session.snapshot()) {
      window.location.replace(appUrl(this.nextPath));
      return;
    }
    this.auth.getMe().subscribe({
      next: (user) => {
        if (user) {
          this.session.markLoggedIn();
          window.location.replace(appUrl(this.nextPath));
        }
      },
      error: () => {
        /* Sin sesión: mostrar el formulario de login. */
      },
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
    return (
      this.backendOnline &&
      this.providers[provider] &&
      !this.oauthLoading &&
      (!this.signupMode || this.authForm.controls.accept_legal.value)
    );
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
    if (this.signupMode && !this.authForm.controls.accept_legal.value) {
      this.authForm.controls.accept_legal.markAsTouched();
      this.error = LEGAL_REQUIRED_MESSAGE;
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
    if (this.step === 'sent') return 'Revisa tu correo';
    if (this.step === 'signup-details') return 'Crea tu cuenta';
    return this.signupMode ? 'Crear cuenta' : 'Entrar al panel';
  }

  get subtitle(): string {
    if (this.step === 'sent') {
      return 'Te enviamos un enlace seguro para entrar sin contraseña.';
    }
    if (this.step === 'signup-details') {
      return 'Completa los datos de tu negocio y te enviamos el enlace.';
    }
    if (this.signupMode) {
      return 'Te enviaremos un enlace a tu correo para activar la cuenta.';
    }
    return 'Ingresa tu correo y te enviamos un enlace para entrar.';
  }

  get email(): string {
    return this.authForm.controls.email.value.trim().toLowerCase();
  }

  submitEmail(): void {
    if (this.authForm.controls.email.invalid || this.submitting) {
      this.authForm.controls.email.markAsTouched();
      return;
    }

    const withSignup = this.signupMode || this.step === 'signup-details';
    if (withSignup && this.authForm.invalid) {
      this.authForm.markAllAsTouched();
      return;
    }

    this.sendMagicLink(withSignup);
  }

  submitSignupDetails(): void {
    this.submitEmail();
  }

  private applySignupValidators(): void {
    const validators = [Validators.required, Validators.minLength(2)];
    this.authForm.controls.business_name.setValidators(validators);
    this.authForm.controls.owner_name.setValidators(validators);
    this.authForm.controls.business_name.updateValueAndValidity();
    this.authForm.controls.owner_name.updateValueAndValidity();
    this.authForm.controls.accept_legal.setValidators(Validators.requiredTrue);
    this.authForm.controls.accept_legal.updateValueAndValidity();
  }

  private sendMagicLink(withSignup: boolean): void {
    this.submitting = true;
    this.error = '';
    this.devLink = null;

    const body = {
      email: this.email,
      ...(withSignup
        ? {
            business_name: this.authForm.controls.business_name.value.trim(),
            owner_name: this.authForm.controls.owner_name.value.trim(),
            accept_legal: this.authForm.controls.accept_legal.value,
            accept_marketing: this.authForm.controls.accept_marketing.value,
          }
        : {}),
    };

    this.auth.requestMagicLink(body).subscribe({
      next: (res) => {
        this.submitting = false;
        if (res.needs_signup) {
          if (this.step === 'signup-details' && res.message) {
            this.error = res.message;
          }
          this.applySignupValidators();
          this.step = 'signup-details';
          return;
        }
        if (res.sent) {
          this.successMessage = res.message;
          this.devLink = res.dev_link ?? null;
          this.step = 'sent';
        }
      },
      error: (err) => {
        this.submitting = false;
        this.error =
          err?.error?.detail ||
          'No pudimos enviar el enlace. Verifica el correo e intenta de nuevo.';
      },
    });
  }

  backToStart(): void {
    this.step = 'start';
    this.error = '';
    this.successMessage = '';
    this.devLink = null;
    this.authForm.controls.business_name.reset('');
    this.authForm.controls.owner_name.reset('');
    if (!this.signupMode) {
      this.authForm.controls.business_name.clearValidators();
      this.authForm.controls.owner_name.clearValidators();
      this.authForm.controls.accept_legal.clearValidators();
      this.authForm.controls.accept_legal.updateValueAndValidity();
      this.authForm.controls.business_name.updateValueAndValidity();
      this.authForm.controls.owner_name.updateValueAndValidity();
    }
  }
}
