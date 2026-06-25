import { NgFor, NgIf } from '@angular/common';
import { Component, OnInit, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ShellComponent } from '../../layout/shell/shell.component';
import { Plan } from '../../core/models/plan.model';
import { PlansService } from '../../core/services/plans.service';

@Component({
  selector: 'app-pricing',
  imports: [ShellComponent, RouterLink, NgFor, NgIf],
  templateUrl: './pricing.component.html',
  styleUrl: './pricing.component.scss',
})
export class PricingComponent implements OnInit {
  private readonly plansService = inject(PlansService);

  plans: Plan[] = [];
  loading = true;
  error = '';

  ngOnInit(): void {
    this.plansService.listPublicPlans().subscribe({
      next: (plans) => {
        this.plans = plans;
        this.loading = false;
      },
      error: () => {
        this.error = 'No pudimos cargar los precios. Intenta de nuevo en unos segundos.';
        this.loading = false;
      },
    });
  }

  formatCop(value: number): string {
    return new Intl.NumberFormat('es-CO', {
      style: 'currency',
      currency: 'COP',
      maximumFractionDigits: 0,
    }).format(value);
  }

  featureList(plan: Plan): string[] {
    const features = plan.features ?? {};
    const items: string[] = [
      `${plan.trial_bait_limit} carnadas de prueba`,
      `${plan.daily_bait_limit} carnadas/día en plan pago`,
      `Hasta ${plan.max_team_members} miembros del equipo`,
    ];
    if (features['ai_on_reply']) items.push('Respuestas automáticas con IA');
    if (features['realtime_panel']) items.push('Panel en tiempo real');
    if (features['maps_scraper']) items.push('Extractor de leads (Maps)');
    return items;
  }
}
