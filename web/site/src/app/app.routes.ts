import { Routes } from '@angular/router';

import { ExtrasComponent } from './pages/extras/extras.component';
import { HomeComponent } from './pages/home/home.component';
import { LoginComponent } from './pages/login/login.component';
import { MagicVerifyComponent } from './pages/magic-verify/magic-verify.component';
import { PasswordResetComponent } from './pages/password-reset/password-reset.component';
import { PricingComponent } from './pages/pricing/pricing.component';

export const routes: Routes = [
  { path: '', component: HomeComponent, title: 'Omitel — WhatsApp con IA' },
  { path: 'precios', component: PricingComponent, title: 'Precios — Omitel' },
  { path: 'extras', component: ExtrasComponent, title: 'Extras — Omitel' },
  { path: 'login', component: LoginComponent, title: 'Entrar — Omitel' },
  { path: 'registro', component: LoginComponent, title: 'Crear cuenta — Omitel' },
  { path: 'auth/entrar', component: MagicVerifyComponent, title: 'Entrando — Omitel' },
  { path: 'recuperar', component: PasswordResetComponent, title: 'Recuperar contraseña — Omitel' },
  { path: 'restablecer', component: PasswordResetComponent, title: 'Nueva contraseña — Omitel' },
  { path: '**', redirectTo: '' },
];
