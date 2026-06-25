export interface Plan {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  price_cop: number;
  price_usd_cents: number | null;
  trial_bait_limit: number;
  daily_bait_limit: number;
  max_team_members: number;
  features: Record<string, unknown> | null;
  is_active: boolean;
  is_public: boolean;
  sort_order: number;
}
