import { isPlatformBrowser } from '@angular/common';
import { Component, OnDestroy, OnInit, PLATFORM_ID, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subscription, combineLatest, filter, take } from 'rxjs';

import { BillingConfig, CheckoutStatus, PaymentMethodResult, SubscriptionSummary, WompiTerms } from '../../core/models/billing.model';
import { Plan } from '../../core/models/plan.model';
import { BillingService } from '../../core/services/billing.service';
import { PlansService } from '../../core/services/plans.service';
import { SessionService } from '../../core/services/session.service';
import {
  WompiTokenError,
  startNequiSubscription,
  tokenizeCard,
  waitForNequiApproval,
} from '../../core/wompi-tokenizer';
import { ShellComponent } from '../../layout/shell/shell.component';

type MethodTab = 'CARD' | 'NEQUI';
type Phase = 'idle' | 'tokenizing' | 'nequi_waiting' | 'charging' | 'polling';

const CANCEL_REASONS: { value: string; label: string }[] = [
  { value: 'precio', label: 'Es muy caro' },
  { value: 'resultados', label: 'No vi resultados' },
  { value: 'tecnico', label: 'Problemas técnicos' },
  { value: 'no_necesito', label: 'Ya no lo necesito' },
  { value: 'otra_herramienta', label: 'Me cambio a otra herramienta' },
  { value: 'otro', label: 'Otro motivo' },
];

@Component({
  selector: 'app-my-plan',
  imports: [ShellComponent, FormsModule, RouterLink],
  templateUrl: './my-plan.component.html',
  styleUrl: './my-plan.component.scss',
})
export class MyPlanComponent implements OnInit, OnDestroy {
  private readonly billing = inject(BillingService);
  private readonly plansService = inject(PlansService);
  private readonly session = inject(SessionService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly platformId = inject(PLATFORM_ID);
  private sub?: Subscription;
  private destroyed = false;

  readonly cancelReasons = CANCEL_REASONS;

  loading = true;
  subscription: SubscriptionSummary | null = null;
  config: BillingConfig | null = null;
  plans: Plan[] = [];
  wompiTerms: WompiTerms | null = null;

  showMethodForm = false;
  selectedPlanSlug = '';
  tab: MethodTab = 'CARD';
  cardNumber = '';
  cardHolder = '';
  cardExpiry = '';
  cardCvc = '';
  nequiPhone = '';
  acceptAutoRenew = false;
  acceptWompi = false;

  phase: Phase = 'idle';
  error = '';
  success = '';

  cancelOpen = false;
  cancelReason = '';
  cancelFeedback = '';
  busy = false;

  ngOnInit(): void {
    if (!isPlatformBrowser(this.platformId)) {
      return;
    }
    const requestedPlan = this.route.snapshot.queryParamMap.get('plan');

    this.sub = combineLatest([this.session.ready$, this.session.user$])
      .pipe(
        filter(([ready]) => ready),
        take(1),
      )
      .subscribe(([, user]) => {
        if (!user) {
          this.router.navigate(['/login'], { queryParams: { next: '/mi-plan' } });
          return;
        }
        this.load(requestedPlan);
      });
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.sub?.unsubscribe();
  }

  private load(requestedPlan: string | null): void {
    this.billing.getConfig().subscribe((cfg) => (this.config = cfg));
    this.plansService.listPublicPlans().subscribe((plans) => {
      this.plans = plans;
      this.applyRequestedPlan(requestedPlan);
    });
    this.reloadSubscription(() => this.applyRequestedPlan(requestedPlan));
  }

  private applyRequestedPlan(requestedPlan: string | null): void {
    if (!this.subscription || !this.plans.length) return;
    const fallback = this.subscription.plan?.slug || this.plans[0]?.slug || '';
    const wanted = requestedPlan && this.plans.some((p) => p.slug === requestedPlan) ? requestedPlan : fallback;
    if (!this.selectedPlanSlug) {
      this.selectedPlanSlug = wanted;
    }
    if (requestedPlan && (!this.subscription.is_paid || requestedPlan !== this.subscription.plan.slug)) {
      this.openMethodForm();
    }
    if (!this.subscription.is_paid && !this.subscription.payment_method_label) {
      this.openMethodForm();
    }
  }

  /** Plan pagado a mano (sin cobro automático) en su última semana: renovar en la página de Wompi. */
  get canRenewInWompi(): boolean {
    const sub = this.subscription;
    if (!sub?.is_paid || sub.auto_renew || sub.cancel_at_period_end || !sub.current_period_end) return false;
    return new Date(sub.current_period_end).getTime() - Date.now() <= 7 * 86_400_000;
  }

  private reloadSubscription(after?: () => void): void {
    this.billing.getMySubscription().subscribe((sub) => {
      this.subscription = sub;
      this.loading = false;
      this.session.refreshSubscription();
      after?.();
    });
  }

  // —— Presentación ——

  get autoRenewAvailable(): boolean {
    return !!this.config?.auto_renew_enabled && !!this.config?.public_key && !!this.config?.api_base;
  }

  get selectedPlan(): Plan | undefined {
    return this.plans.find((p) => p.slug === this.selectedPlanSlug);
  }

  get chargesNow(): boolean {
    const sub = this.subscription;
    if (!sub) return false;
    return !sub.is_paid || this.selectedPlanSlug !== sub.plan.slug;
  }

  get priceForAuthorization(): number {
    return this.selectedPlan?.price_cop ?? this.subscription?.plan.price_cop ?? 0;
  }

  get statusLabel(): string {
    const sub = this.subscription;
    if (!sub) return '';
    if (sub.cancel_at_period_end) {
      return sub.current_period_end ? `Termina el ${this.formatDate(sub.current_period_end, true)}` : 'Cancelada';
    }
    if (sub.is_trial) return 'Prueba gratis';
    if (sub.trial_expired) return 'Prueba terminada';
    if (sub.is_paid) return 'Activo';
    if (sub.status === 'cancelled') return 'Cancelado';
    return 'Pago pendiente';
  }

  get statusTone(): 'ok' | 'warn' | 'off' {
    const sub = this.subscription;
    if (!sub) return 'off';
    if (sub.cancel_at_period_end || sub.renewal_failing || sub.needs_payment) return 'warn';
    if (sub.is_paid || sub.is_trial) return 'ok';
    return 'off';
  }

  get canCancel(): boolean {
    const sub = this.subscription;
    return (
      !!sub && !sub.is_trial && !sub.trial_expired && sub.status !== 'cancelled' && !sub.cancel_at_period_end
    );
  }

  get processing(): boolean {
    return this.phase !== 'idle';
  }

  get submitLabel(): string {
    switch (this.phase) {
      case 'tokenizing':
        return 'Validando con Wompi…';
      case 'nequi_waiting':
        return 'Esperando aprobación en Nequi…';
      case 'charging':
      case 'polling':
        return this.chargesNow ? 'Procesando pago…' : 'Guardando…';
    }
    return this.chargesNow
      ? `Pagar ${this.formatCop(this.priceForAuthorization)} y activar cobro automático`
      : 'Guardar medio de pago';
  }

  formatCop(value: number | undefined | null): string {
    return new Intl.NumberFormat('es-CO', { style: 'currency', currency: 'COP', maximumFractionDigits: 0 }).format(
      value ?? 0,
    );
  }

  formatDate(value: string | null | undefined, short = false): string {
    if (!value) return '';
    return new Intl.DateTimeFormat('es-CO', {
      day: 'numeric',
      month: short ? 'short' : 'long',
      ...(short ? {} : { year: 'numeric' as const }),
      timeZone: 'America/Bogota',
    }).format(new Date(value));
  }

  // —— Medio de pago ——

  openMethodForm(): void {
    this.showMethodForm = true;
    this.error = '';
    if (!this.wompiTerms && this.config?.enabled !== false) {
      this.billing.getWompiTerms().subscribe({
        next: (terms) => (this.wompiTerms = terms),
        error: () => (this.wompiTerms = null),
      });
    }
  }

  closeMethodForm(): void {
    if (this.processing) return;
    this.showMethodForm = false;
    this.error = '';
  }

  formatCardNumber(): void {
    const digits = this.cardNumber.replace(/\D/g, '').slice(0, 19);
    this.cardNumber = digits.replace(/(.{4})/g, '$1 ').trim();
  }

  formatExpiry(): void {
    const digits = this.cardExpiry.replace(/\D/g, '').slice(0, 4);
    this.cardExpiry = digits.length > 2 ? `${digits.slice(0, 2)}/${digits.slice(2)}` : digits;
  }

  private validateForm(): string | null {
    if (!this.acceptAutoRenew) return 'Debes autorizar el cobro automático para continuar.';
    if (!this.acceptWompi) return 'Debes aceptar los términos de Wompi para continuar.';
    if (this.chargesNow && !this.selectedPlan) return 'Elige un plan.';
    if (this.tab === 'CARD') {
      const digits = this.cardNumber.replace(/\D/g, '');
      if (digits.length < 13) return 'Revisa el número de la tarjeta.';
      if (this.cardHolder.trim().length < 3) return 'Escribe el nombre como aparece en la tarjeta.';
      const [month, year] = this.cardExpiry.split('/');
      if (!month || !year || Number(month) < 1 || Number(month) > 12 || year.length !== 2) {
        return 'Revisa la fecha de vencimiento (MM/AA).';
      }
      if (!/^\d{3,4}$/.test(this.cardCvc)) return 'Revisa el código de seguridad (CVC).';
    } else if (!/^3\d{9}$/.test(this.nequiPhone.replace(/\D/g, ''))) {
      return 'Escribe tu número Nequi de 10 dígitos.';
    }
    return null;
  }

  async submitMethod(): Promise<void> {
    if (this.processing || !this.config?.api_base || !this.config.public_key) return;
    const invalid = this.validateForm();
    if (invalid) {
      this.error = invalid;
      return;
    }
    this.error = '';
    this.success = '';
    const apiBase = this.config.api_base;
    const publicKey = this.config.public_key;

    try {
      let token: string;
      let hint: { brand?: string; last_four?: string; phone_last_four?: string };
      if (this.tab === 'CARD') {
        this.phase = 'tokenizing';
        const [month, year] = this.cardExpiry.split('/');
        const card = await tokenizeCard(apiBase, publicKey, {
          number: this.cardNumber,
          holder: this.cardHolder,
          expMonth: month,
          expYear: year,
          cvc: this.cardCvc,
        });
        token = card.id;
        hint = { brand: card.brand, last_four: card.lastFour };
      } else {
        this.phase = 'tokenizing';
        token = await startNequiSubscription(apiBase, publicKey, this.nequiPhone);
        this.phase = 'nequi_waiting';
        const status = await waitForNequiApproval(apiBase, publicKey, token, { isCancelled: () => this.destroyed });
        if (status !== 'APPROVED') {
          this.phase = 'idle';
          this.error =
            status === 'DECLINED'
              ? 'La suscripción fue rechazada en Nequi.'
              : 'No recibimos la aprobación en Nequi a tiempo. Intenta de nuevo.';
          return;
        }
        hint = { phone_last_four: this.nequiPhone.replace(/\D/g, '').slice(-4) };
      }

      this.phase = 'charging';
      this.billing
        .savePaymentMethod({
          type: this.tab,
          token,
          accept_auto_renew: this.acceptAutoRenew,
          accept_wompi_terms: this.acceptWompi,
          plan_slug: this.chargesNow ? this.selectedPlanSlug : null,
          ...hint,
        })
        .subscribe({
          next: (result) => this.handleSaveResult(result),
          error: (err) => {
            this.phase = 'idle';
            this.error = err?.error?.detail || 'No pudimos guardar el medio de pago. Intenta de nuevo.';
          },
        });
    } catch (err) {
      this.phase = 'idle';
      this.error = err instanceof WompiTokenError ? err.message : 'No pudimos validar el medio de pago.';
    }
  }

  private handleSaveResult(result: PaymentMethodResult): void {
    const charge = result.charge;
    if (!charge) {
      this.finishSuccess('Listo: guardamos tu medio de pago. El próximo cobro será automático.');
      return;
    }
    this.followCharge(charge, 0);
  }

  private followCharge(charge: CheckoutStatus, attempt: number): void {
    if (charge.status === 'approved') {
      this.finishSuccess(`¡Pago aprobado! Tu ${charge.plan_name || 'plan'} está activo y se renovará solo cada 30 días.`);
      return;
    }
    if (charge.status === 'declined') {
      this.phase = 'idle';
      this.error = `El pago fue rechazado${charge.failure_reason ? `: ${charge.failure_reason}` : ''}. Prueba con otro medio de pago.`;
      this.reloadSubscription();
      return;
    }
    if (attempt >= 20 || this.destroyed) {
      this.phase = 'idle';
      this.success = 'Tu pago está en proceso. Te avisaremos por correo cuando Wompi lo confirme.';
      this.showMethodForm = false;
      this.reloadSubscription();
      return;
    }
    this.phase = 'polling';
    window.setTimeout(() => {
      this.billing.getCheckoutStatus(charge.reference).subscribe({
        next: (next) => this.followCharge(next, attempt + 1),
        error: () => this.followCharge(charge, attempt + 1),
      });
    }, 3000);
  }

  private finishSuccess(message: string): void {
    this.phase = 'idle';
    this.success = message;
    this.showMethodForm = false;
    this.cardNumber = this.cardHolder = this.cardExpiry = this.cardCvc = this.nequiPhone = '';
    this.acceptAutoRenew = this.acceptWompi = false;
    this.reloadSubscription();
    this.session.refresh();
  }

  removeMethod(): void {
    if (this.busy) return;
    const ok = window.confirm(
      'Si quitas el medio de pago se desactiva el cobro automático. Tu plan sigue activo hasta su fecha y luego tendrás que pagar manualmente. ¿Continuar?',
    );
    if (!ok) return;
    this.busy = true;
    this.billing.removePaymentMethod().subscribe({
      next: () => {
        this.busy = false;
        this.success = 'Quitamos tu medio de pago. No se harán cobros automáticos.';
        this.reloadSubscription();
      },
      error: (err) => {
        this.busy = false;
        this.error = err?.error?.detail || 'No pudimos quitar el medio de pago.';
      },
    });
  }

  // —— Cancelación ——

  openCancel(): void {
    this.cancelOpen = true;
    this.cancelReason = '';
    this.cancelFeedback = '';
    this.error = '';
  }

  closeCancel(): void {
    if (!this.busy) this.cancelOpen = false;
  }

  confirmCancel(): void {
    if (this.busy || !this.cancelReason) return;
    this.busy = true;
    this.billing.cancelSubscription(this.cancelReason, this.cancelFeedback.trim()).subscribe({
      next: () => {
        this.busy = false;
        this.cancelOpen = false;
        this.success = 'Cancelaste tu suscripción. Te enviamos la confirmación por correo.';
        this.reloadSubscription();
      },
      error: (err) => {
        this.busy = false;
        this.error = err?.error?.detail || 'No pudimos cancelar. Intenta de nuevo o escríbenos.';
        this.cancelOpen = false;
      },
    });
  }

  resume(): void {
    if (this.busy) return;
    this.busy = true;
    this.billing.resumeSubscription().subscribe({
      next: () => {
        this.busy = false;
        this.success = '¡Qué bien que te quedas! Tu suscripción sigue activa.';
        this.reloadSubscription();
      },
      error: (err) => {
        this.busy = false;
        this.error = err?.error?.detail || 'No pudimos reactivar la suscripción.';
      },
    });
  }
}
