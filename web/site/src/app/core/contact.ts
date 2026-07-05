/** Teléfonos de contacto Omitel (Colombia). */
export const CONTACT = {
  whatsappE164: '573337341031',
  whatsappDisplay: '333 734 1031',
  phoneE164: '573017453703',
  phoneDisplay: '301 745 3703',
} as const;

export function whatsappUrl(message?: string): string {
  const base = `https://wa.me/${CONTACT.whatsappE164}`;
  if (!message?.trim()) {
    return base;
  }
  return `${base}?text=${encodeURIComponent(message.trim())}`;
}

export function phoneUrl(): string {
  return `tel:+${CONTACT.phoneE164}`;
}
