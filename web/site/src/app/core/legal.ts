import { CONTACT } from './contact';

/**
 * Datos del responsable que aparecen en Términos, Privacidad y Cookies.
 * Completar antes de publicar. `version` debe coincidir con TERMS_VERSION / PRIVACY_VERSION
 * de backend/app/application/auth/legal_consent_service.py.
 */
export const LEGAL = {
  version: '2026-10-04',
  updatedLabel: '4 de octubre de 2026',
  brand: 'Omitel',
  companyName: '[RAZÓN SOCIAL — POR COMPLETAR]',
  taxId: '[NIT — POR COMPLETAR]',
  address: '[DIRECCIÓN — POR COMPLETAR]',
  city: '[CIUDAD — POR COMPLETAR]',
  privacyEmail: 'privacidad@omitel.net',
  supportEmail: 'soporte@omitel.net',
  phoneDisplay: CONTACT.phoneDisplay,
  whatsappDisplay: CONTACT.whatsappDisplay,
  siteUrl: 'https://www.omitel.net',
  appDomain: 'app.omitel.net',
  /** Días tras cancelar la cuenta en que se borran los datos. */
  deletionDays: 30,
  graceDays: 3,
} as const;
