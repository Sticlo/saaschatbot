import { isPlatformBrowser, NgFor, NgIf } from '@angular/common';
import { Component, OnDestroy, OnInit, PLATFORM_ID, inject } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subscription, combineLatest } from 'rxjs';

import { ShellComponent } from '../../layout/shell/shell.component';
import { Plan } from '../../core/models/plan.model';
import { CheckoutStatus, SubscriptionSummary } from '../../core/models/billing.model';
import { PlansService } from '../../core/services/plans.service';
import { appUrl } from '../../core/oauth-url';
import { BillingService } from '../../core/services/billing.service';
import { SessionService } from '../../core/services/session.service';

export interface PlanFeature {
  text: string;
  tag?: string;
  highlight?: boolean;
}

/** Página de pago oficial de Wompi (sandbox o producción según la llave pública). */
const WOMPI_WEB_CHECKOUT = 'https://checkout.wompi.co/p/';
const CHECKOUT_POLL_MS = 3000;
const CHECKOUT_POLL_ATTEMPTS = 40;

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

  get panelUrl(): string {
    return appUrl('/panel');
  }

  private sessionSub?: Subscription;

  plans: Plan[] = [];
  loggedIn = false;
  billingEnabled = false;
  billingSandbox = false;
  subscription: SubscriptionSummary | null = null;
  checkoutLoading = false;
  checkoutError = '';
  checkoutSuccess = '';
  checkoutInfo = '';
  activePlanSlug: string | null = null;

  readonly trialFeatures: string[] = [
    '3 días con todo el plan incluido',
    'Conecta WhatsApp y ve tus chats',
    'La IA responde y te avisa quién quiere comprar',
    'Sin tarjeta de crédito',
  ];

  ngOnInit(): void {
    this.plans = this.fallbackPlans();

    if (!isPlatformBrowser(this.platformId)) {
      return;
    }
    window.addEventListener('pageshow', this.onPageShow);

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
        this.autoRenewEnabled = !!cfg.auto_renew_enabled;
        this.configLoaded = true;
        this.maybeStartPlanCheckout();
      },
      error: () => {
        this.configLoaded = true;
        this.maybeStartPlanCheckout();
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
          queryParams: { checkout: null, ref: null, id: null, env: null },
          queryParamsHandling: 'merge',
          replaceUrl: true,
        });
        return;
      }
      const planSlug = params.get('plan');
      if (planSlug) {
        this.pendingPlanSlug = planSlug;
        this.payOnce = params.get('once') === '1';
        this.maybeStartPlanCheckout();
      }
    });
  }

  ngOnDestroy(): void {
    this.sessionSub?.unsubscribe();
    if (isPlatformBrowser(this.platformId)) {
      window.removeEventListener('pageshow', this.onPageShow);
    }
  }

  /** Volver con «atrás» desde Wompi restaura la página congelada con el botón en «Abriendo…». */
  private readonly onPageShow = (event: PageTransitionEvent): void => {
    if (event.persisted) {
      this.checkoutLoading = false;
      this.activePlanSlug = null;
    }
  };

  private pendingPlanSlug: string | null = null;
  /** Pago único en la página de Wompi (PSE, etc.) en vez de guardar la tarjeta para cobro automático. */
  private payOnce = false;
  private autoRenewEnabled = false;
  private configLoaded = false;

  /** Por defecto se guarda la tarjeta o Nequi con cobro automático (menos bajas por olvido). */
  private goToPlan(planSlug: string): void {
    if (this.payOnce || !this.autoRenewEnabled) {
      this.startCheckout(planSlug);
    } else {
      this.router.navigate(['/mi-plan'], { queryParams: { plan: planSlug } });
    }
  }

  private maybeStartPlanCheckout(): void {
    if (!this.loggedIn || !this.pendingPlanSlug || this.checkoutLoading || !this.configLoaded) {
      return;
    }
    const plan = this.plans.find((p) => p.slug === this.pendingPlanSlug);
    if (!plan || this.isCurrentPlan(plan)) {
      return;
    }
    this.pendingPlanSlug = null;
    this.goToPlan(plan.slug);
  }

  formatCop(value: number): string {
    return new Intl.NumberFormat('es-CO', {
      style: 'currency',
      currency: 'COP',
      maximumFractionDigits: 0,
    }).format(value);
  }

  ribbonLabel(_plan: Plan): string | null {
    return 'Todo incluido';
  }

  referencePrice(_plan: Plan): number | null {
    return 250_000;
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

    this.goToPlan(plan.slug);
  }

  private async startCheckout(planSlug: string): Promise<void> {
    this.checkoutError = '';
    this.checkoutSuccess = '';
    this.checkoutLoading = true;
    this.activePlanSlug = planSlug;

    this.billing.createCheckout(planSlug).subscribe({
      next: (session) => {
        const params = new URLSearchParams({
          'public-key': session.public_key,
          currency: session.currency,
          'amount-in-cents': String(session.amount_in_cents),
          reference: session.reference,
          'signature:integrity': session.integrity_signature,
          'redirect-url': session.redirect_url,
          'customer-data:email': session.customer_email,
          'customer-data:full-name': session.customer_name,
        });
        window.location.assign(`${WOMPI_WEB_CHECKOUT}?${params.toString()}`);
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
    this.checkoutInfo = 'Confirmando tu pago con Wompi…';
    this.billing.syncCheckout(reference, transactionId).subscribe({
      next: (status) => {
        if (!this.showCheckoutResult(status)) this.pollCheckout(reference);
      },
      error: () => this.pollCheckout(reference),
    });
  }

  private pollCheckout(reference: string, attempt = 0): void {
    this.checkoutInfo = 'Confirmando tu pago con Wompi…';
    this.billing.getCheckoutStatus(reference).subscribe({
      next: (status) => {
        if (this.showCheckoutResult(status)) return;
        if (attempt < CHECKOUT_POLL_ATTEMPTS) {
          window.setTimeout(() => this.pollCheckout(reference, attempt + 1), CHECKOUT_POLL_MS);
          return;
        }
        this.checkoutInfo =
          'Wompi todavía está procesando tu pago (con PSE puede tardar unos minutos). Tu plan se activa solo apenas se confirme; recarga esta página más tarde.';
      },
      error: () => {
        this.checkoutInfo = '';
        this.checkoutError = 'No pudimos consultar tu pago. Recarga la página en un momento.';
      },
    });
  }

  /** true si el pago ya terminó (aprobado o rechazado). */
  private showCheckoutResult(status: CheckoutStatus): boolean {
    if (status.status === 'approved') {
      this.checkoutInfo = '';
      this.checkoutSuccess = `¡Listo! Tu ${status.plan_name || 'plan'} está activo.`;
      this.session.refreshSubscription();
      this.session.refresh();
      return true;
    }
    if (status.status === 'declined' || status.status === 'error') {
      this.checkoutInfo = '';
      this.checkoutError = `Wompi no aprobó el pago${status.failure_reason ? ': ' + status.failure_reason : ''}. No se te cobró nada; puedes intentarlo de nuevo.`;
      return true;
    }
    return false;
  }

  readonly features: PlanFeature[] = [
    { text: 'Panel de chats en tiempo real' },
    { text: 'IA que saluda, responde dudas y conoce tus productos' },
    { text: 'Respuestas de IA ilimitadas', highlight: true },
    { text: 'Te avisa quién quiere comprar — tú cierras' },
    { text: 'Envía tu catálogo o menú en PDF o foto', highlight: true },
    { text: 'Agenda citas y reservas por ti (si la activas)', highlight: true },
    { text: 'Personalizar la IA con tu negocio' },
    { text: 'Atajos: menú, precios, fotos' },
    { text: 'Clasificación de clientes ilimitada' },
    { text: 'Hasta 5 usuarios · 1 WhatsApp' },
    { text: 'Onboarding guiado y soporte prioritario' },
  ];

  private fallbackPlans(): Plan[] {
    return [
      {
        id: 'pro',
        slug: 'pro',
        name: 'Plan Pro',
        description:
          'Todo Omitel incluido: IA sin límite que atiende tu WhatsApp, catálogo, citas, equipo de hasta 5 personas y soporte prioritario.',
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
        sort_order: 0,
      },
    ];
  }
}
