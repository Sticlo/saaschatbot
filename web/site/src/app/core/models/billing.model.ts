export interface BillingConfig {
  enabled: boolean;
  public_key: string | null;
  sandbox?: boolean;
  sync_enabled?: boolean;
  auto_renew_enabled?: boolean;
  api_base?: string | null;
}

export interface CheckoutSession {
  reference: string;
  amount_in_cents: number;
  currency: string;
  public_key: string;
  integrity_signature: string;
  redirect_url: string;
  plan_slug: string;
  plan_name: string;
  customer_email: string;
  customer_name: string;
}

export interface CheckoutStatus {
  reference: string;
  status: string;
  plan_slug: string | null;
  plan_name: string | null;
  paid_at: string | null;
  wompi_transaction_id: string | null;
  failure_reason?: string | null;
}

export interface SubscriptionSummary {
  status: string;
  tenant_plan: string;
  is_trial: boolean;
  is_paid: boolean;
  needs_payment: boolean;
  plan: {
    slug: string;
    name: string;
    price_cop?: number;
  };
  trial_ends_at?: string | null;
  trial_days_left?: number | null;
  trial_expired?: boolean;
  current_period_end?: string | null;
  auto_renew?: boolean;
  payment_method_type?: 'CARD' | 'NEQUI' | null;
  payment_method_label?: string | null;
  next_charge_at?: string | null;
  renewal_failing?: boolean;
  cancel_at_period_end?: boolean;
  cancelled_at?: string | null;
}

/** Lo que la IA hizo por el negocio en un periodo (GET /results). */
export interface ResultsPeriod {
  ai_appointments: number;
  after_hours_appointments: number;
  ai_replies: number;
  clients_attended: number;
  revenue_cop: number | null;
  roi_multiple: number | null;
}

export interface BusinessResults {
  month: ResultsPeriod;
  last_30_days: ResultsPeriod;
  all_time: ResultsPeriod;
  months_active: number;
  avg_ticket_cop: number | null;
  plan_price_cop: number | null;
}

export interface WompiTerms {
  acceptance_permalink: string;
  personal_data_permalink: string;
}

export interface PaymentMethodRequest {
  type: 'CARD' | 'NEQUI';
  token: string;
  accept_auto_renew: boolean;
  accept_wompi_terms: boolean;
  plan_slug?: string | null;
  brand?: string | null;
  last_four?: string | null;
  phone_last_four?: string | null;
}

export interface PaymentMethodResult {
  auto_renew: boolean;
  payment_method_label: string | null;
  charge: CheckoutStatus | null;
  charge_error: string | null;
}

export interface UserMe {
  id: string;
  email: string;
  full_name: string | null;
  role: string;
  tenant_id: string;
}
