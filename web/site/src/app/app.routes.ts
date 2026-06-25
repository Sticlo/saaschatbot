import { Routes } from '@angular/router';

import { HomeComponent } from './pages/home/home.component';
import { LoginComponent } from './pages/login/login.component';
import { PricingComponent } from './pages/pricing/pricing.component';
import { RegisterComponent } from './pages/register/register.component';

export const routes: Routes = [
  { path: '', component: HomeComponent, title: 'SaasChatbot — WhatsApp con IA' },
  { path: 'precios', component: PricingComponent, title: 'Precios — SaasChatbot' },
  { path: 'login', component: LoginComponent, title: 'Entrar — SaasChatbot' },
  { path: 'registro', component: RegisterComponent, title: 'Crear cuenta — SaasChatbot' },
  { path: '**', redirectTo: '' },
];
