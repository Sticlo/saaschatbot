export interface AuthProviders {
  google: boolean;
  github: boolean;
}

export interface EmailLookupRequest {
  email: string;
}

export interface EmailLookupResponse {
  email: string;
  exists: boolean;
}

export interface MagicLinkRequest {
  email: string;
  business_name?: string;
  owner_name?: string;
}

export interface MagicLinkResponse {
  sent: boolean;
  needs_signup: boolean;
  message: string;
  dev_link?: string | null;
}

export interface ForgotPasswordRequest {
  email: string;
}

export interface ForgotPasswordResponse {
  message: string;
  dev_link?: string | null;
}

export interface ResetPasswordRequest {
  token: string;
  password: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  business_name: string;
  owner_name: string;
  email: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  tenant_id: string;
  role: string;
  email: string;
}

export interface UserMe {
  id: string;
  email: string;
  full_name: string | null;
  role: string;
  tenant_id: string;
}
