"""Avisos al desarrollador (Telegram y/o correo) cuando algo falla en producción.

El cliente nunca ve estos avisos: la app degrada en silencio y el dev se entera en segundos.
Cada problema se agrupa por `key` y se avisa como máximo una vez por ventana, con el conteo
de repeticiones, para que una caída no se convierta en cien mensajes.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

SEVERITY_ICON = {"critical": "🚨", "error": "🔴", "warning": "🟠", "info": "🔵"}
_IGNORED_LOGGERS = ("uvicorn.access", "httpx", "httpcore", __name__)

_queue: "queue.Queue[DevAlert]" = queue.Queue(maxsize=200)
_sender: Optional[threading.Thread] = None
_state_lock = threading.Lock()
_local_last_sent: dict[str, float] = {}
_local_repeats: dict[str, int] = {}
_guard = threading.local()
_handler_installed = False


@dataclass
class DevAlert:
    key: str
    title: str
    detail: str
    severity: str
    repeated: int
    throttle_seconds: int


def channels_configured() -> bool:
    telegram = settings.ops_telegram_bot_token.strip() and settings.ops_telegram_chat_id.strip()
    return bool(telegram or settings.ops_alert_email.strip())


def _claim(key: str, throttle_seconds: int) -> Optional[int]:
    """None = ya se avisó hace poco (se suma al conteo). Si toca avisar, devuelve cuántas
    veces se repitió el problema desde el último aviso."""
    try:
        from app.infrastructure.cache.redis_client import get_redis

        client = get_redis()
        count_key = f"ops:alert:n:{key}"
        if client.set(f"ops:alert:{key}", "1", nx=True, ex=throttle_seconds):
            pipe = client.pipeline()
            pipe.get(count_key)
            pipe.delete(count_key)
            repeated, _ = pipe.execute()
            return int(repeated or 0)
        pipe = client.pipeline()
        pipe.incr(count_key)
        pipe.expire(count_key, throttle_seconds * 4)
        pipe.execute()
        return None
    except Exception:
        # Sin Redis (que puede ser justo lo que se cayó): agrupar en memoria del proceso.
        now = time.time()
        with _state_lock:
            if now - _local_last_sent.get(key, 0.0) < throttle_seconds:
                _local_repeats[key] = _local_repeats.get(key, 0) + 1
                return None
            _local_last_sent[key] = now
            return _local_repeats.pop(key, 0)


def alert_dev(
    key: str,
    title: str,
    detail: str = "",
    *,
    severity: str = "error",
    throttle_seconds: Optional[int] = None,
    log_it: bool = True,
) -> bool:
    """Avisa al dev. True = el aviso salió (no estaba agrupado con uno reciente)."""
    if log_it:
        level = logging.ERROR if severity in ("critical", "error") else logging.WARNING
        log.log(level, "[ALERTA DEV] %s — %s", title, (detail or "")[:500], extra={"dev_alert": True})
    # En local un error mientras programas no debe sonar en tu celular.
    if not settings.ops_alerts_enabled or not settings.is_production():
        return False
    throttle = int(throttle_seconds or settings.ops_alert_throttle_seconds)
    repeated = _claim(key, throttle)
    if repeated is None:
        return False
    _dispatch(
        DevAlert(
            key=key,
            title=title[:200],
            detail=(detail or "")[:1500],
            severity=severity,
            repeated=repeated,
            throttle_seconds=throttle,
        )
    )
    return True


def count_in_window(key: str, window_seconds: int) -> int:
    """Cuenta eventos en una ventana fija (p. ej. fallos de DeepSeek en 5 min)."""
    try:
        from app.infrastructure.cache.redis_client import get_redis

        client = get_redis()
        redis_key = f"ops:burst:{key}"
        count = int(client.incr(redis_key))
        if count == 1:
            client.expire(redis_key, window_seconds)
        return count
    except Exception:
        return 1


def format_alert(alert: DevAlert) -> str:
    icon = SEVERITY_ICON.get(alert.severity, "🔴")
    host = socket.gethostname()
    lines = [f"{icon} Omitel [{settings.app_env} · {host}]", alert.title]
    if alert.detail:
        lines += ["", alert.detail]
    if alert.repeated:
        minutes = max(1, alert.throttle_seconds // 60)
        lines += ["", f"Se repitió {alert.repeated} vez/veces más en los últimos ~{minutes} min."]
    return "\n".join(lines)


def _send_telegram(text: str) -> None:
    token = settings.ops_telegram_bot_token.strip()
    chat_id = settings.ops_telegram_chat_id.strip()
    if not token or not chat_id:
        return
    with httpx.Client(timeout=10.0) as client:
        res = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
        )
    if res.status_code >= 400:
        raise RuntimeError(f"Telegram HTTP {res.status_code}")


def _send_email(alert: DevAlert, text: str) -> None:
    to_email = settings.ops_alert_email.strip()
    if not to_email:
        return
    from app.infrastructure.email.email_service import send_ops_alert_email

    icon = SEVERITY_ICON.get(alert.severity, "🔴")
    send_ops_alert_email(to_email=to_email, subject=f"{icon} Omitel: {alert.title}"[:150], text=text)


def deliver(alert: DevAlert) -> list[str]:
    """Envía por todos los canales configurados. Devuelve los canales que fallaron."""
    text = format_alert(alert)
    failed: list[str] = []
    for name, send in (("telegram", lambda: _send_telegram(text)), ("email", lambda: _send_email(alert, text))):
        try:
            send()
        except Exception as exc:
            failed.append(name)
            log.warning("No se pudo enviar alerta dev por %s: %s", name, exc, extra={"dev_alert": True})
    return failed


def _sender_loop() -> None:
    _guard.active = True
    while True:
        alert = _queue.get()
        try:
            deliver(alert)
        finally:
            _queue.task_done()


def _ensure_sender() -> None:
    global _sender
    with _state_lock:
        if _sender and _sender.is_alive():
            return
        _sender = threading.Thread(target=_sender_loop, name="dev-alert-sender", daemon=True)
        _sender.start()


def _dispatch(alert: DevAlert) -> None:
    if not channels_configured():
        return
    _ensure_sender()
    try:
        _queue.put_nowait(alert)
    except queue.Full:
        log.warning("Cola de alertas dev llena; se descarta %s", alert.key, extra={"dev_alert": True})


def _exception_summary(record: logging.LogRecord) -> tuple[str, str]:
    if not record.exc_info or record.exc_info[1] is None:
        return "", ""
    exc = record.exc_info[1]
    where = ""
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        last = frames[-1]
        where = f"{last.filename.rsplit('/', 1)[-1]}:{last.lineno} en {last.name}"
    return type(exc).__name__, f"{type(exc).__name__}: {str(exc)[:400]}" + (f"\n{where}" if where else "")


class DevAlertLogHandler(logging.Handler):
    """Todo log.error / log.exception del backend llega al dev, agrupado por tipo de error."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, "active", False) or getattr(record, "dev_alert", False):
            return
        if record.name.startswith(_IGNORED_LOGGERS):
            return
        _guard.active = True
        try:
            exc_name, exc_detail = _exception_summary(record)
            fingerprint = f"{record.name}:{record.msg}:{exc_name}"
            key = "log:" + hashlib.sha1(fingerprint.encode("utf-8", "replace")).hexdigest()[:16]
            detail = record.getMessage()[:600]
            if exc_detail:
                detail += f"\n\n{exc_detail}"
            alert_dev(key, f"Error en {record.name}", detail, severity="error", log_it=False)
        except Exception:
            pass
        finally:
            _guard.active = False


def install_log_alert_handler() -> None:
    global _handler_installed
    if _handler_installed:
        return
    root = logging.getLogger()
    if not root.handlers:
        # Con un handler en root Python deja de imprimir por consola (lastResort): sin esto
        # los errores de la app desaparecerían de los logs de Docker.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root.addHandler(DevAlertLogHandler())
    _handler_installed = True


def _send_test_alert() -> int:
    if not channels_configured():
        print("Configura OPS_TELEGRAM_BOT_TOKEN + OPS_TELEGRAM_CHAT_ID y/o OPS_ALERT_EMAIL en .env")
        return 1
    failed = deliver(
        DevAlert(
            key="test",
            title="Prueba de alertas",
            detail="Si lees esto, las alertas de Omitel llegan bien.",
            severity="info",
            repeated=0,
            throttle_seconds=60,
        )
    )
    if failed:
        print(f"Falló el envío por: {', '.join(failed)}")
        return 1
    print("Alerta de prueba enviada.")
    return 0


if __name__ == "__main__":
    sys.exit(_send_test_alert())
