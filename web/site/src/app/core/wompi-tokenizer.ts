/**
 * Tokenización en el navegador contra Wompi: los datos de la tarjeta o el celular Nequi
 * van directo a Wompi con la llave pública y nunca pasan por el servidor de Omitel.
 */

export interface CardInput {
  number: string;
  holder: string;
  expMonth: string;
  expYear: string;
  cvc: string;
}

export interface CardToken {
  id: string;
  brand: string;
  lastFour: string;
}

export type NequiStatus = 'APPROVED' | 'DECLINED' | 'TIMEOUT';

export class WompiTokenError extends Error {}

async function wompiRequest<T>(
  apiBase: string,
  publicKey: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${publicKey}`,
        'Content-Type': 'application/json',
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new WompiTokenError('No pudimos conectar con Wompi. Revisa tu internet e intenta de nuevo.');
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new WompiTokenError(describeError(body));
  }
  return (body?.data ?? body) as T;
}

function describeError(body: { error?: { messages?: Record<string, string[]>; reason?: string } }): string {
  const fields = Object.keys(body?.error?.messages ?? {});
  if (fields.some((f) => f.includes('number'))) return 'El número de la tarjeta no es válido.';
  if (fields.some((f) => f.includes('exp'))) return 'La fecha de vencimiento no es válida.';
  if (fields.some((f) => f.includes('cvc'))) return 'El código de seguridad (CVC) no es válido.';
  if (fields.some((f) => f.includes('phone'))) return 'El número de celular Nequi no es válido.';
  return 'Wompi no aceptó los datos. Revísalos e intenta de nuevo.';
}

async function encryptCard(apiBase: string, publicKey: string, card: Record<string, string>): Promise<string> {
  const keyData = await wompiRequest<{ publicKey?: string }>(apiBase, publicKey, '/tokens/keys/tokenization');
  if (!keyData?.publicKey) {
    throw new WompiTokenError('Wompi no entregó la llave de cifrado.');
  }
  const { EncryptJWT, importSPKI } = await import('jose');
  const key = await importSPKI(keyData.publicKey, 'RSA-OAEP-256');
  return new EncryptJWT(card).setProtectedHeader({ alg: 'RSA-OAEP-256', enc: 'A256GCM' }).encrypt(key);
}

export async function tokenizeCard(apiBase: string, publicKey: string, input: CardInput): Promise<CardToken> {
  const card = {
    number: input.number.replace(/\D/g, ''),
    cvc: input.cvc.replace(/\D/g, ''),
    exp_month: input.expMonth.replace(/\D/g, '').padStart(2, '0'),
    exp_year: input.expYear.replace(/\D/g, '').slice(-2),
    card_holder: input.holder.trim(),
  };

  let body: unknown;
  try {
    body = { payload: await encryptCard(apiBase, publicKey, card) };
  } catch {
    // Sin llave de cifrado disponible (algunos entornos de prueba): tokenización simple por HTTPS.
    body = card;
  }

  const data = await wompiRequest<{ id: string; brand?: string; last_four?: string }>(
    apiBase,
    publicKey,
    '/tokens/cards',
    { method: 'POST', body: JSON.stringify(body) },
  );
  if (!data?.id) {
    throw new WompiTokenError('Wompi no pudo validar la tarjeta.');
  }
  return { id: data.id, brand: data.brand ?? 'Tarjeta', lastFour: data.last_four ?? card.number.slice(-4) };
}

export async function startNequiSubscription(apiBase: string, publicKey: string, phone: string): Promise<string> {
  const data = await wompiRequest<{ id: string }>(apiBase, publicKey, '/tokens/nequi', {
    method: 'POST',
    body: JSON.stringify({ phone_number: phone.replace(/\D/g, '') }),
  });
  if (!data?.id) {
    throw new WompiTokenError('Wompi no pudo iniciar la suscripción con Nequi.');
  }
  return data.id;
}

export async function waitForNequiApproval(
  apiBase: string,
  publicKey: string,
  tokenId: string,
  {
    timeoutMs = 180_000,
    intervalMs = 3_000,
    isCancelled = (): boolean => false,
  }: { timeoutMs?: number; intervalMs?: number; isCancelled?: () => boolean } = {},
): Promise<NequiStatus> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !isCancelled()) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    try {
      const data = await wompiRequest<{ status?: string }>(apiBase, publicKey, `/tokens/nequi/${tokenId}`);
      if (data?.status === 'APPROVED') return 'APPROVED';
      if (data?.status === 'DECLINED') return 'DECLINED';
    } catch {
      /* reintenta hasta el límite */
    }
  }
  return 'TIMEOUT';
}
