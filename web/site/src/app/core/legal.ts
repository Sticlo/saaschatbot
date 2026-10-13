import { CONTACT } from './contact';

/**
 * Datos del responsable que aparecen en Términos, Privacidad y Cookies.
 * Completar antes de publicar. `version` debe coincidir con TERMS_VERSION / PRIVACY_VERSION
 * de backend/app/application/auth/legal_consent_service.py.
 */
export const LEGAL = {
  version: '2026-10-12',
  updatedLabel: '12 de octubre de 2026',
  brand: 'Omitel',
  companyName: 'OMITEL',
  taxId: '1025523061-6',
  address: 'Calle 59 B Sur # 22 G - 19',
  city: 'Bogotá D.C.',
  privacyEmail: 'privacidad@omitel.net',
  supportEmail: 'soporte@omitel.net',
  phoneDisplay: CONTACT.phoneDisplay,
  whatsappDisplay: CONTACT.whatsappDisplay,
  siteUrl: 'https://www.omitel.net',
  appDomain: 'app.omitel.net',
  /** Días tras cancelar la cuenta en que se borran los datos. */
  deletionDays: 30,
  graceDays: 3,
  /** Debe coincidir con REMINDER_LEAD de backend/app/application/billing/auto_renew_service.py. */
  renewalNoticeDays: 3,
} as const;
