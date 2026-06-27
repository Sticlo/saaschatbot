from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)


def _magic_link_html(url: str, *, signup: bool) -> str:
    action = "crear tu cuenta y entrar" if signup else "entrar al panel"
    return f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:520px;margin:0 auto;color:#1d1e20">
  <h1 style="font-size:1.35rem;margin:0 0 0.75rem">Omitel</h1>
  <p style="line-height:1.55;color:#6d7080">Haz clic para {action}. El enlace expira en {settings.magic_link_expire_minutes} minutos.</p>
  <p style="margin:1.5rem 0">
    <a href="{url}" style="display:inline-block;background:#ff6b00;color:#fff;text-decoration:none;padding:0.75rem 1.25rem;border-radius:8px;font-weight:700">
      {action.capitalize()}
    </a>
  </p>
  <p style="font-size:0.82rem;color:#9aa0ae;word-break:break-all">Si el botón no funciona: {url}</p>
</div>
"""


def send_magic_link_email(*, to_email: str, url: str, signup: bool) -> str | None:
    """Envía el enlace mágico. En dev sin API key, devuelve la URL para mostrarla."""
    subject = "Tu enlace para Omitel" if not signup else "Crea tu cuenta en Omitel"
    html = _magic_link_html(url, signup=signup)
    return _send_html_email(to_email=to_email, subject=subject, html=html, log_label="Magic link", url=url)


def _password_reset_html(url: str) -> str:
    return f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:520px;margin:0 auto;color:#1d1e20">
  <h1 style="font-size:1.35rem;margin:0 0 0.75rem">Omitel</h1>
  <p style="line-height:1.55;color:#6d7080">Recibimos una solicitud para restablecer tu contraseña. El enlace expira en {settings.password_reset_expire_minutes} minutos.</p>
  <p style="margin:1.5rem 0">
    <a href="{url}" style="display:inline-block;background:#ff6b00;color:#fff;text-decoration:none;padding:0.75rem 1.25rem;border-radius:8px;font-weight:700">
      Restablecer contraseña
    </a>
  </p>
  <p style="font-size:0.82rem;color:#9aa0ae">Si no pediste esto, ignora este correo.</p>
  <p style="font-size:0.82rem;color:#9aa0ae;word-break:break-all">Si el botón no funciona: {url}</p>
</div>
"""


def send_password_reset_email(*, to_email: str, url: str) -> str | None:
    """Envía enlace de recuperación. En dev sin API key, devuelve la URL."""
    html = _password_reset_html(url)
    return _send_html_email(
        to_email=to_email,
        subject="Restablece tu contraseña en Omitel",
        html=html,
        log_label="Password reset",
        url=url,
    )


def _send_html_email(
    *,
    to_email: str,
    subject: str,
    html: str,
    log_label: str,
    url: str,
) -> str | None:
    api_key = (settings.resend_api_key or "").strip()
    if not api_key:
        log.warning("%s para %s (sin RESEND_API_KEY): %s", log_label, to_email, url)
        return url if settings.debug else None

    try:
        with httpx.Client(timeout=15.0) as client:
            res = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.email_from,
                    "to": [to_email],
                    "subject": subject,
                    "html": html,
                },
            )
        if res.status_code >= 400:
            log.error("Resend error %s: %s", res.status_code, res.text)
            raise RuntimeError("No pudimos enviar el correo. Intenta más tarde.")
    except httpx.HTTPError as exc:
        log.exception("Resend request failed")
        raise RuntimeError("No pudimos enviar el correo. Intenta más tarde.") from exc

    return None
