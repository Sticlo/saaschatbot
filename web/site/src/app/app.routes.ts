import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./pages/home/home.component').then((m) => m.HomeComponent),
    title: 'Omitel — WhatsApp con IA',
  },
  {
    path: 'precios',
    loadComponent: () => import('./pages/pricing/pricing.component').then((m) => m.PricingComponent),
    title: 'Precios — Omitel',
  },
  {
    path: 'extras',
    loadComponent: () => import('./pages/extras/extras.component').then((m) => m.ExtrasComponent),
    title: 'Extras — Omitel',
  },
  {
    path: 'login',
    loadComponent: () => import('./pages/login/login.component').then((m) => m.LoginComponent),
    title: 'Entrar — Omitel',
  },
  {
    path: 'registro',
    loadComponent: () => import('./pages/login/login.component').then((m) => m.LoginComponent),
    title: 'Crear cuenta — Omitel',
  },
  {
    path: 'auth/entrar',
    loadComponent: () =>
      import('./pages/magic-verify/magic-verify.component').then((m) => m.MagicVerifyComponent),
    title: 'Entrando — Omitel',
  },
  {
    path: 'recuperar',
    loadComponent: () =>
      import('./pages/password-reset/password-reset.component').then((m) => m.PasswordResetComponent),
    title: 'Recuperar contraseña — Omitel',
  },
  {
    path: 'restablecer',
    loadComponent: () =>
      import('./pages/password-reset/password-reset.component').then((m) => m.PasswordResetComponent),
    title: 'Nueva contraseña — Omitel',
  },
  {
    path: 'privacidad',
    loadComponent: () => import('./pages/legal/privacy/privacy.component').then((m) => m.PrivacyComponent),
    title: 'Política de Tratamiento de Datos — Omitel',
  },
  {
    path: 'terminos',
    loadComponent: () => import('./pages/legal/terms/terms.component').then((m) => m.TermsComponent),
    title: 'Términos y Condiciones — Omitel',
  },
  {
    path: 'cookies',
    loadComponent: () => import('./pages/legal/cookies/cookies.component').then((m) => m.CookiesComponent),
    title: 'Política de Cookies — Omitel',
  },
  { path: '**', redirectTo: '' },
];
