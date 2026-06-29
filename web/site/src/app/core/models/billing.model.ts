export interface BillingConfig {
  enabled: boolean;
  public_key: string | null;
  sandbox?: boolean;
  sync_enabled?: boolean;
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
  };
}

export interface UserMe {
  id: string;
  email: string;
  full_name: string | null;
  role: string;
  tenant_id: string;
}
