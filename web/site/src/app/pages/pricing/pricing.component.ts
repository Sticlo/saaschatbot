import { isPlatformBrowser, NgFor, NgIf } from '@angular/common';
import { Component, OnDestroy, OnInit, PLATFORM_ID, inject } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subscription, combineLatest } from 'rxjs';

import { ShellComponent } from '../../layout/shell/shell.component';
import { Plan } from '../../core/models/plan.model';
import { SubscriptionSummary } from '../../core/models/billing.model';
import { PlansService } from '../../core/services/plans.service';
import { BillingService } from '../../core/services/billing.service';
import { SessionService } from '../../core/services/session.service';

export interface PlanFeature {
  text: string;
  tag?: string;
  highlight?: boolean;
}

interface WompiWidgetCheckout {
  open: (callback: (result: { transaction?: { id?: string; status?: string } }) => void) => void;
}

interface WompiWidgetConstructor {
  new (config: Record<string, unknown>): WompiWidgetCheckout;
}

@Component({
  selector: 'app-pricing',
  imports: [ShellComponent, RouterLink, NgFor, NgIf],
  templateUrl: './pricing.component.html',
  styleUrl: './pricing.component.scss',
})
export class PricingComponent implements OnInit, OnDestroy {
  private readonly plansService = inject(PlansService);
  private readonly session = inject(SessionService);
  private readonly billing = inject(BillingService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly platformId = inject(PLATFORM_ID);

  private sessionSub?: Subscription;

  plans: Plan[] = [];
  loggedIn = false;
  billingEnabled = false;
  billingSandbox = false;
  subscription: SubscriptionSummary | null = null;
  checkoutLoading = false;
  checkoutError = '';
  checkoutSuccess = '';
  activePlanSlug: string | null = null;

  readonly trialFeatures: string[] = [
    '7 días de acceso al panel',
    'Conecta WhatsApp y ve tus chats',
    'IA qualify con límites de prueba',
    'Sin tarjeta de crédito',
  ];

  ngOnInit(): void {
    this.plans = this.fallbackPlans();

    if (!isPlatformBrowser(this.platformId)) {
      return;
    }

    this.plansService.listPublicPlans().subscribe({
      next: (plans) => {
        if (plans.length) {
          this.plans = plans;
        }
        this.maybeStartPlanCheckout();
      },
    });

    this.sessionSub = combineLatest([
      this.session.user$,
      this.session.subscription$,
      this.session.billingEnabled$,
      this.session.ready$,
    ]).subscribe(([user, subscription, billingEnabled, ready]) => {
      if (!ready) {
        return;
      }
      this.loggedIn = !!user;
      this.subscription = subscription;
      this.billingEnabled = billingEnabled;
      if (this.loggedIn) {
        this.maybeStartPlanCheckout();
      }
    });

    this.billing.getConfig().subscribe({
      next: (cfg) => {
        this.billingSandbox = !!cfg.sandbox;
      },
    });

    this.route.queryParamMap.subscribe((params) => {
      if (params.get('checkout') === 'done') {
        const ref = params.get('ref');
        const wompiId = params.get('id');
        if (ref) {
          this.finishCheckoutReturn(ref, wompiId);
        }
        this.router.navigate([], {
          relativeTo: this.route,
          queryParams: { checkout: null, ref: null },
          queryParamsHandling: 'merge',
          replaceUrl: true,
        });
        return;
      }
      const planSlug = params.get('plan');
      if (planSlug) {
        this.pendingPlanSlug = planSlug;
        this.maybeStartPlanCheckout();
      }
    });
  }

  ngOnDestroy(): void {
    this.sessionSub?.unsubscribe();
  }

  private pendingPlanSlug: string | null = null;

  private maybeStartPlanCheckout(): void {
    if (!this.loggedIn || !this.pendingPlanSlug || this.checkoutLoading) {
      return;
    }
    const plan = this.plans.find((p) => p.slug === this.pendingPlanSlug);
    if (!plan || this.isCurrentPlan(plan)) {
      return;
    }
    this.pendingPlanSlug = null;
    this.startCheckout(plan.slug);
  }

  formatCop(value: number): string {
    return new Intl.NumberFormat('es-CO', {
      style: 'currency',
      currency: 'COP',
      maximumFractionDigits: 0,
    }).format(value);
  }

  isFeatured(plan: Plan): boolean {
    return plan.slug === 'pro';
  }

  isPremium(plan: Plan): boolean {
    return plan.slug === 'premium';
  }

  ribbonLabel(plan: Plan): string | null {
    if (plan.slug === 'pro') return 'Más vendido';
    if (plan.slug === 'premium') return 'Máximo volumen';
    return null;
  }

  referencePrice(plan: Plan): number | null {
    if (plan.slug === 'pro') return 150_000;
    if (plan.slug === 'premium') return 250_000;
    return null;
  }

  discountLabel(plan: Plan): string | null {
    const ref = this.referencePrice(plan);
    if (!ref || ref <= plan.price_cop) return null;
    const pct = Math.round((1 - plan.price_cop / ref) * 100);
    return `${pct}% dto.`;
  }

  ctaLabel(plan: Plan): string {
    if (this.subscription?.is_paid && this.subscription.plan.slug === plan.slug) {
      return 'Plan activo';
    }
    if (this.loggedIn) {
      return this.subscription?.is_trial ? 'Activar plan' : 'Cambiar a este plan';
    }
    return 'Elegir plan';
  }

  isCurrentPlan(plan: Plan): boolean {
    return (
      !!this.subscription?.is_paid && this.subscription.plan.slug === plan.slug
    );
  }

  canSelectPlan(plan: Plan): boolean {
    if (this.checkoutLoading) return false;
    if (this.isCurrentPlan(plan)) return false;
    if (this.loggedIn && !this.billingEnabled) return false;
    return true;
  }

  onPlanSelect(plan: Plan): void {
    if (!this.canSelectPlan(plan)) return;

    if (!this.loggedIn) {
      this.router.navigate(['/registro'], { queryParams: { plan: plan.slug } });
      return;
    }

    this.startCheckout(plan.slug);
  }

  private async startCheckout(planSlug: string): Promise<void> {
    this.checkoutError = '';
    this.checkoutSuccess = '';
    this.checkoutLoading = true;
    this.activePlanSlug = planSlug;

    this.billing.createCheckout(planSlug).subscribe({
      next: async (session) => {
        try {
          await this.loadWompiScript();
          const WidgetCheckout = (window as unknown as { WidgetCheckout: WompiWidgetConstructor })
            .WidgetCheckout;
          const widget = new WidgetCheckout({
            currency: session.currency,
            amountInCents: session.amount_in_cents,
            reference: session.reference,
            publicKey: session.public_key,
            redirectUrl: session.redirect_url,
            signature: { integrity: session.integrity_signature },
            customerData: {
              email: session.customer_email,
              fullName: session.customer_name,
            },
          });
          widget.open((result) => {
            const tx = result.transaction;
            if (tx?.id) {
              this.confirmCheckout(session.reference, tx.id);
            } else if (tx?.status === 'APPROVED') {
              this.pollCheckout(session.reference);
            }
          });
        } catch {
          this.checkoutError =
            'No pudimos abrir el checkout de Wompi. Recarga e intenta de nuevo.';
        } finally {
          this.checkoutLoading = false;
          this.activePlanSlug = null;
        }
      },
      error: (err) => {
        this.checkoutLoading = false;
        this.activePlanSlug = null;
        this.checkoutError =
          err?.error?.detail || 'No pudimos iniciar el pago. Intenta de nuevo.';
      },
    });
  }

  private finishCheckoutReturn(reference: string, wompiTransactionId: string | null): void {
    if (wompiTransactionId) {
      this.confirmCheckout(reference, wompiTransactionId);
      return;
    }
    this.pollCheckout(reference);
  }

  private confirmCheckout(reference: string, transactionId: string): void {
    this.checkoutLoading = true;
    this.billing.syncCheckout(reference, transactionId).subscribe({
      next: (status) => {
        this.checkoutLoading = false;
        if (status.status === 'approved') {
          this.checkoutSuccess = `¡Listo! Tu ${status.plan_name || 'plan'} está activo.`;
          this.session.refreshSubscription();
          this.session.refresh();
          return;
        }
        this.pollCheckout(reference);
      },
      error: () => {
        this.checkoutLoading = false;
        this.pollCheckout(reference);
      },
    });
  }

  private pollCheckout(reference: string, attempt = 0): void {
    this.billing.getCheckoutStatus(reference).subscribe({
      next: (status) => {
        if (status.status === 'approved') {
          this.checkoutSuccess = `¡Listo! Tu ${status.plan_name || 'plan'} está activo.`;
          this.session.refreshSubscription();
          this.session.refresh();
          return;
        }
        if (attempt < 8) {
          window.setTimeout(() => this.pollCheckout(reference, attempt + 1), 2000);
        }
      },
    });
  }

  private loadWompiScript(): Promise<void> {
    if ((window as unknown as { WidgetCheckout?: WompiWidgetConstructor }).WidgetCheckout) {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://checkout.wompi.co/widget.js';
      script.async = true;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('wompi_script'));
      document.body.appendChild(script);
    });
  }

  /** Lista compacta Pro — lo esencial. */
  proFeatures(_plan: Plan): PlanFeature[] {
    return [
      { text: 'Panel de chats en tiempo real' },
      { text: 'IA que saluda y responde dudas' },
      { text: 'Pestaña Interesados — tú cierras' },
      { text: 'Personalizar IA con tu negocio' },
      { text: 'Atajos: menú, precios, fotos' },
      { text: 'Clasificación de leads ilimitada' },
      { text: 'Hasta 400 respuestas IA al día' },
      { text: '50 contactos fríos por día' },
      { text: '2 usuarios · 1 WhatsApp' },
      { text: 'Onboarding guiado incluido' },
    ];
  }

  /** Premium: todo Pro + extras — lista más larga = más valor percibido. */
  premiumFeatures(_plan: Plan): PlanFeature[] {
    return [
      { text: 'Panel de chats en tiempo real' },
      { text: 'IA que saluda y responde dudas' },
      { text: 'Pestaña Interesados — tú cierras' },
      { text: 'Personalizar IA con tu negocio' },
      { text: 'Atajos: menú, precios, fotos' },
      { text: 'Clasificación de leads ilimitada' },
      { text: 'Respuestas IA ilimitadas', highlight: true, tag: 'Nuevo' },
      { text: '100 contactos fríos por día', highlight: true },
      { text: '5 usuarios en el panel', highlight: true },
      { text: 'Onboarding prioritario', highlight: true },
      { text: 'Soporte preferente', highlight: true },
      { text: 'Prospectos Google Maps', highlight: true, tag: 'Pronto' },
    ];
  }

  featuresFor(plan: Plan): PlanFeature[] {
    return this.isPremium(plan)
      ? this.premiumFeatures(plan)
      : this.proFeatures(plan);
  }

  featuresTitle(plan: Plan): string {
    return this.isPremium(plan) ? 'Todo incluido' : 'Beneficios incluidos';
  }

  private fallbackPlans(): Plan[] {
    return [
      {
        id: 'pro',
        slug: 'pro',
        name: 'Plan Pro',
        description: 'Todo lo que necesitas para ordenar WhatsApp y no perder ventas.',
        price_cop: 120_000,
        price_usd_cents: 3000,
        trial_bait_limit: 10,
        daily_bait_limit: 50,
        max_team_members: 2,
        features: {
          ai_on_reply: true,
          ai_daily_replies: 400,
          ai_daily_classifications: 0,
          realtime_panel: true,
        },
        is_active: true,
        is_public: true,
        sort_order: 0,
      },
      {
        id: 'premium',
        slug: 'premium',
        name: 'Plan Premium',
        description: 'Para equipos y mucho volumen. IA sin tope y soporte prioritario.',
        price_cop: 200_000,
        price_usd_cents: 5000,
        trial_bait_limit: 10,
        daily_bait_limit: 100,
        max_team_members: 5,
        features: {
          ai_on_reply: true,
          ai_daily_replies: 0,
          ai_daily_classifications: 0,
          maps_scraper: true,
          priority_support: true,
          realtime_panel: true,
        },
        is_active: true,
        is_public: true,
        sort_order: 1,
      },
    ];
  }
}
