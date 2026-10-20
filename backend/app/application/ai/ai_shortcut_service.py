from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_knowledge_service import clean_topic
from app.application.outbound.quick_shortcut_service import find_shortcut
from app.application.products.product_service import find_by_ref, list_products, photo_caption, product_ref
from app.application.whatsapp.whatsapp_service import (
    send_document_message,
    send_image_message,
    send_text_message,
)
from app.domain.entities import Conversation, Message, Tenant, WhatsAppSession
from app.infrastructure.cache.redis_client import get_redis

if TYPE_CHECKING:
    from app.application.ai.ai_appointment_service import BookSlotRequest

log = logging.getLogger(__name__)

_SHORTCUT_PREVIEW_LEN = 120

_CATALOG_KNOWLEDGE_CHARS = 6000
SHORTCUT_SENT_TTL_SECONDS = 7 * 24 * 3600
SHORTCUT_RESEND_GUARD_SECONDS = 30 * 60

_JSON_REPLY_INSTRUCTION = """
Atajos rápidos que puedes enviar al cliente (botones con contenido listo):
{shortcut_lines}
{catalog_block}
Cuando el cliente pida menú, carta, fotos, precios, catálogo o algo que coincida con un atajo,
responde con un mensaje breve y envía ese atajo (shortcut_id).
Si un atajo tiene "enviarlo cuando", el dueño te dijo cuándo usarlo: si el cliente pregunta o habla de
eso (aunque lo diga con otras palabras), envía ese atajo en esa respuesta.
Si un atajo dice "ya enviado en este chat", NO lo vuelvas a enviar: responde con la
información que conoces o recuérdale que se lo compartiste arriba. Solo reenvíalo si el
cliente lo pide otra vez de forma explícita (por ejemplo "mándamelo de nuevo").
- Si en message dices que envías, compartes o pasas algo ("te lo envío", "aquí tienes el menú"),
  pon su shortcut_id en esa misma respuesta. Nunca prometas un envío sin shortcut_id.
- Usa solo shortcut_id de la lista de arriba. No inventes ids.
- No repitas en message el contenido completo del atajo; el atajo se envía aparte.
- Si ningún atajo aplica, shortcut_id debe ser null.
"""

STAGE_CHATTING = "conversando"
STAGE_INTERESTED = "interesado"
STAGE_CLOSING = "cierre"
STAGE_RANK = {STAGE_CHATTING: 0, STAGE_INTERESTED: 1, STAGE_CLOSING: 2}

_REPLY_FORMAT = """

Responde SOLO con JSON válido (sin markdown), exactamente con estas claves:
{{"message":"tu respuesta al cliente","shortcut_id":null,{photo_key}"book_slot":null,"stage":"conversando","missing":"","chosen":""{order_key}}}
{key_rules}missing = si el cliente preguntó algo que NO está en la información del negocio y por eso no pudiste
responderlo, escribe en pocas palabras qué dato te faltó (ej. "costo del envío a Cali", "si hay talla 42").
Si pudiste responder todo, "". No lo uses para cosas que el cliente debe decirte (su dirección, su talla).

chosen = si en este mensaje el cliente elige un producto, paquete o servicio concreto (por ejemplo manda
un pantallazo o foto del catálogo y dice «me gustó este», «quiero este»), su nombre tal como aparece en
«Productos del negocio» (o en la imagen, si no está en la lista); si no eligió nada, "".
Cuando elige así: confírmale cuál es y su precio, solo si está en la información del negocio, y pídele el
siguiente dato para dejarlo listo (ej. fecha y ciudad del evento, talla, cantidad). Si en la imagen se ven
varios y no está claro cuál le gustó, chosen = "" y pregúntale cuál fue, nombrando las opciones que se ven.

stage = en qué punto está el cliente, mirando toda la conversación:
- "conversando": saluda, charla o pregunta en general; aún no muestra intención de compra.
- "interesado": muestra interés real: pide el catálogo, la carta o fotos; pregunta precio, talla,
  color o disponibilidad de algo concreto; pregunta por envíos o domicilios.
- "cierre": ya decidió: quiere pedir, pagar, apartar o reservar, pide datos para pagar o la
  dirección para ir, o confirma que lo lleva.
{closing_rules}"""

_HANDOFF_CLOSING = """Si el stage es "cierre": responde cálido y natural, como alguien del negocio. Si aún falta un dato
para dejar el pedido listo (qué, cuándo, talla, dirección), pídelo. Cuando lo tengas, resume en una
frase lo que quedó acordado para que sienta que quedó anotado, y dile que por este chat le
confirmas lo que falte (ej. «¡Listo, Juan! Te dejo anotado el post para Facebook el lunes 12 ✅
Por aquí mismo te confirmo el precio y los detalles»). Nunca digas «dame un momentico», «ya te
confirmo» ni nada que lo haga esperar una respuesta en minutos: la confirmación la da una
persona del negocio y puede tardar. No digas que lo pasas con un asesor u otra persona ni que
eres un bot. No pidas datos de pago."""

_SELLING_CLOSING = """Si el stage es "cierre": cierra tú la venta, cálido y natural, como alguien del negocio:
1. Confirma qué lleva (producto o servicio, variante o talla, cantidad) y el total, solo con precios
   que estén en la información del negocio.
2. Si hay que entregárselo, pide en un solo mensaje lo que falte: nombre, dirección, barrio y ciudad
   (o si prefiere recoger, cuando el negocio tenga local).
3. Compártele cómo pagar, tal cual está en «Cómo pagarle al negocio». Nunca inventes números de
   cuenta, links ni medios de pago que no estén ahí.
4. Cuando mande el comprobante, agradécele y dile que su pedido quedó registrado y qué sigue.
Si algo no está en la información del negocio (un precio, si hay existencias, el costo del envío),
no lo inventes ni lo adivines: dile «te confirmo por aquí apenas lo revise», ponlo en missing y sigue
con lo demás.
No digas que lo pasas con un asesor u otra persona ni que eres un bot.

order = null mientras el cliente no haya confirmado qué lleva. Cuando lo confirme y tengas cómo
entregárselo, llénalo (y repítelo igual en las respuestas siguientes, actualizando lo que cambie):
{"items":"2 camisetas negras talla M","total":"$120.000","customer":"Laura Gómez","delivery":"Cra 45 #10-20, Laureles, Medellín","payment":"Nequi","paid":false}
- total: solo si sale de los precios del negocio; si no lo sabes, "".
- delivery: dirección completa, o "recoge en el local".
- payment: el medio que eligió; "" si aún no lo dice.
- paid: true solo si ya mandó el comprobante o la foto de la transferencia.
Para citas y reservas no uses order."""


def reply_format_instruction(
    *, has_shortcuts: bool, booking_enabled: bool, selling: bool = False, has_photos: bool = False
) -> str:
    rules = []
    if not has_shortcuts:
        rules.append("- shortcut_id siempre null (no hay atajos).")
    if has_photos:
        rules.append("- photo_ref = ref de un producto con 📷 cuando el cliente quiere verlo; si no, null.")
    if not booking_enabled:
        rules.append(
            "- book_slot siempre null. Si piden una cita o reserva, no la confirmes tú: es stage \"cierre\"."
        )
    key_rules = ("\n".join(rules) + "\n") if rules else ""
    return _REPLY_FORMAT.format(
        key_rules=key_rules,
        photo_key='"photo_ref":null,' if has_photos else "",
        order_key=',"order":null' if selling else "",
        closing_rules=_SELLING_CLOSING if selling else _HANDOFF_CLOSING,
    )

_CATALOG_BLOCK = """
Lo que sabes de los productos (sacado del catálogo/carta del negocio):
{catalog_text}

Usa esta información para responder preguntas concretas (precios, tallas, sabores,
disponibilidad). No inventes productos ni precios que no estén aquí. Si un producto también está en
«Productos del negocio», su precio y disponibilidad mandan sobre lo que diga el catálogo.
"""


@dataclass(frozen=True)
class AiGeneratedReply:
    message: str
    shortcut_id: Optional[str] = None
    book_slot: Optional["BookSlotRequest"] = None
    stage: str = STAGE_CHATTING
    order: Optional["OrderSummary"] = None
    # Lo que el cliente preguntó y no está en la información del negocio.
    missing: str = ""
    # Producto (ref) cuyas fotos se envían después del mensaje.
    photo_ref: Optional[str] = None
    # Producto o paquete que el cliente acaba de elegir (ej. «me gustó este» con un pantallazo).
    chosen: str = ""


@dataclass(frozen=True)
class OrderSummary:
    items: str
    total: str = ""
    customer: str = ""
    delivery: str = ""
    payment: str = ""
    paid: bool = False
    # (ref del producto, cantidad): con esto se descuenta lo vendido.
    lines: tuple[tuple[str, int], ...] = ()


_ORDER_FIELD_MAX = 300
_REF_RE = re.compile(r"^[0-9a-f]{8}$")


def _order_lines(raw: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, int]] = []
    for item in raw[:30]:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip().lower()
        try:
            qty = int(item.get("qty", 1))
        except (TypeError, ValueError):
            continue
        if _REF_RE.match(ref) and 0 < qty <= 999:
            out.append((ref, qty))
    return tuple(out)


def parse_order(raw: Any) -> Optional[OrderSummary]:
    if not isinstance(raw, dict):
        return None

    def field(key: str) -> str:
        value = raw.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v)
        if value is None or isinstance(value, (dict, bool)):
            return ""
        text = " ".join(str(value).split())
        return "" if text.lower() in {"null", "none"} else text[:_ORDER_FIELD_MAX]

    items = field("items")
    if not items:
        return None
    paid = raw.get("paid")
    return OrderSummary(
        items=items,
        total=field("total"),
        customer=field("customer"),
        delivery=field("delivery"),
        payment=field("payment"),
        paid=paid is True or str(paid).strip().lower() == "true",
        lines=_order_lines(raw.get("lines")),
    )


def _preview_text(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= _SHORTCUT_PREVIEW_LEN:
        return cleaned
    return cleaned[: _SHORTCUT_PREVIEW_LEN - 1] + "…"


def format_shortcut_line(shortcut: dict[str, Any]) -> str:
    sid = str(shortcut.get("id") or "")
    label = str(shortcut.get("label") or "").strip()
    kind = str(shortcut.get("type") or "text").lower()
    if kind == "image":
        content = f"imagen ({label})"
    elif kind == "document":
        content = f"archivo PDF ({shortcut.get('file_name') or label})"
    else:
        content = _preview_text(str(shortcut.get("text") or ""))
    line = f'- id="{sid}" | botón="{label}" | tipo={kind} | contenido: {content}'
    send_when = " ".join(str(shortcut.get("send_when") or "").split())
    if send_when:
        line += f" | enviarlo cuando: {send_when}"
    if shortcut.get("already_sent"):
        line += " | ya enviado en este chat"
    return line


def _catalog_text(shortcuts: list[dict]) -> str:
    parts: list[str] = []
    remaining = _CATALOG_KNOWLEDGE_CHARS
    for shortcut in shortcuts:
        if shortcut.get("type") not in ("image", "document"):
            continue
        content = str(shortcut.get("content") or "").strip()
        if not content or remaining <= 0:
            continue
        label = str(shortcut.get("label") or "").strip() or "Catálogo"
        chunk = f"[{label}]\n{content[:remaining]}"
        parts.append(chunk)
        remaining -= len(chunk)
    return "\n\n".join(parts)


def append_shortcuts_instructions(base_prompt: str, shortcuts: list[dict]) -> str:
    if not shortcuts:
        return base_prompt
    lines = [format_shortcut_line(s) for s in shortcuts if s.get("id")]
    if not lines:
        return base_prompt
    catalog_text = _catalog_text(shortcuts)
    catalog_block = _CATALOG_BLOCK.format(catalog_text=catalog_text) if catalog_text else ""
    block = _JSON_REPLY_INSTRUCTION.format(
        shortcut_lines="\n".join(lines), catalog_block=catalog_block
    )
    return base_prompt.rstrip() + "\n" + block


def _sent_key(conversation_id, shortcut_id: str) -> str:
    return f"ai:shortcut_sent:{conversation_id}:{shortcut_id}"


def mark_shortcut_sent(conversation_id, shortcut_id: str) -> None:
    try:
        get_redis().set(
            _sent_key(conversation_id, shortcut_id),
            str(int(time.time())),
            ex=SHORTCUT_SENT_TTL_SECONDS,
        )
    except Exception:
        log.warning("No se pudo marcar atajo enviado conv=%s", conversation_id, exc_info=True)


def shortcut_sent_at(conversation_id, shortcut_id: str) -> Optional[float]:
    try:
        raw = get_redis().get(_sent_key(conversation_id, shortcut_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def annotate_sent_shortcuts(shortcuts: list[dict], conversation_id) -> list[dict]:
    annotated = []
    for shortcut in shortcuts:
        item = dict(shortcut)
        sid = str(item.get("id") or "")
        if sid and shortcut_sent_at(conversation_id, sid) is not None:
            item["already_sent"] = True
        annotated.append(item)
    return annotated


def shortcut_ids(shortcuts: list[dict]) -> frozenset[str]:
    return frozenset(str(s.get("id") or "") for s in shortcuts if s.get("id"))


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


_PROMISE_RE = re.compile(
    r"\b(?:te\s+(?:lo|la|los|las)\s+(?:envio|mando|comparto|paso|dejo|adjunto)"
    r"|te\s+(?:envio|mando|comparto|paso|dejo|adjunto)\s+(?:el|la|los|las|nuestr[oa]s?|mi|una|un)\b"
    r"|aqui\s+(?:tienes|te\s+va|va|esta)"
    r"|ahi\s+(?:te\s+va|va|tienes))"
)
_FILE_WORDS = ("menu", "carta", "catalogo", "precio", "servicio", "lista", "foto", "portafolio", "tarifa")
_RESEND_RE = re.compile(
    r"no\s+(?:me\s+)?(?:ha\s+)?(?:lleg|aparec|veo|carg|abr|sale)|de\s+nuevo|otra\s+vez|reenv|nuevamente"
    r"|^[\s?¿!.]*\?[\s?¿!.]*$"
)


def infer_promised_shortcut(message: str, client_text: str, shortcuts: list[dict]) -> Optional[str]:
    """La IA a veces dice «te lo envío» y olvida el shortcut_id: el cliente se queda esperando.
    Si promete un envío, adjuntamos el atajo del que se está hablando (si no hay duda de cuál)."""
    if not shortcuts or not _PROMISE_RE.search(_plain(message)):
        return None
    text = f" {_plain(message)} {_plain(client_text)} "
    labeled = []
    for shortcut in shortcuts:
        words = [w for w in re.split(r"\W+", _plain(str(shortcut.get("label") or ""))) if len(w) >= 3]
        if shortcut.get("id") and words and any(f" {w} " in text or f" {w}s " in text for w in words):
            labeled.append(shortcut)
    if len(labeled) == 1:
        return str(labeled[0]["id"])
    if labeled:
        return None
    files = [s for s in shortcuts if s.get("id") and s.get("type") in ("image", "document")]
    if len(files) == 1 and any(w in text for w in _FILE_WORDS):
        return str(files[0]["id"])
    return None


_ASKS_PHOTO_RE = re.compile(r"\bfotos?\b|\bfoticos?\b|\bimagen(?:es)?\b|\bpics?\b")
_SHOWS_RE = re.compile(r"\bmira\b|\baqui\s+(?:estan|esta|las|la)\b|\bte\s+(?:las?|los?)\s+(?:paso|mando|envio|comparto)\b")


def shows_photos(message: str, client_text: str) -> bool:
    """El cliente pidió fotos (o reclama las que no le llegaron) y la respuesta dice que ahí van."""
    reply = _plain(message)
    asked = _plain(client_text)
    wants = _ASKS_PHOTO_RE.search(asked) or (asks_to_resend(client_text) and _ASKS_PHOTO_RE.search(reply))
    return bool(wants and (_SHOWS_RE.search(reply) or _PROMISE_RE.search(reply)))


def _best_product(text: str, products: list):
    text = " " + " ".join(re.findall(r"\w+", _plain(text))) + " "
    scored = []
    for product in products:
        words = [w for w in re.split(r"\W+", _plain(product.name)) if len(w) >= 3]
        score = sum(1 for w in words if f" {w} " in text or f" {w}s " in text)
        if score:
            scored.append((score, product))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def infer_photo_ref(
    message: str, client_text: str, products: list, recent: tuple[str, ...] = ()
) -> Optional[str]:
    """«¡Claro! Mira 👇» sin photo_ref: el cliente se queda sin las fotos que pidió.
    `recent`: lo último que se habló, del más nuevo al más viejo. Un «fotos» a secas es del
    producto del que se venía hablando."""
    if not shows_photos(message, client_text):
        return None
    with_photos = [p for p in products if p.photos]
    if not with_photos:
        return None
    named = _ref_in_text(message, with_photos) or _best_product(f"{client_text} {message}", with_photos)
    if named is None:
        for text in recent:
            named = _ref_in_text(text, with_photos) or _best_product(text, with_photos)
            if named is not None:
                break
    if named is None and len(with_photos) == 1:
        named = with_photos[0]
    return product_ref(named) if named is not None else None


_REF_RE = re.compile(r"\b(?:ref(?:erencia)?\.?\s*[:=#]?\s*)?([0-9a-f]{8})\b", re.IGNORECASE)


def _ref_in_text(text: str, products: list):
    refs = {product_ref(p): p for p in products}
    for match in _REF_RE.finditer(text or ""):
        product = refs.get(match.group(1).lower())
        if product is not None:
            return product
    return None


def without_internal_refs(message: str, products: list) -> str:
    """La ref es para la IA: «Ropa Unisex (ref df984011)» no le dice nada al cliente."""
    refs = {product_ref(p) for p in products}
    if not refs:
        return message
    cleaned = re.sub(
        r"\s*\(\s*(?:ref(?:erencia)?\.?\s*[:=#]?\s*)?([0-9a-f]{8})\s*\)|\s*\bref(?:erencia)?\.?\s*[:=#]?\s*([0-9a-f]{8})\b",
        lambda m: "" if (m.group(1) or m.group(2) or "").lower() in refs else m.group(0),
        message,
        flags=re.IGNORECASE,
    )
    return re.sub(r" +([,.;:!?])", r"\1", cleaned)


def photo_choice_message(products: list) -> str:
    """Pidió fotos y no queda claro de cuál: preguntar antes que decir «Mira 👇» y no mandar nada."""
    names = [p.name for p in products if p.photos][:6]
    listed = ", ".join(names[:-1]) + f" o {names[-1]}" if len(names) > 1 else names[0]
    return f"¡Claro! ¿De cuál quieres ver fotos: {listed}? 📸"


def attach_promised_media(
    reply: "AiGeneratedReply",
    *,
    client_text: str,
    products: list,
    shortcuts: list[dict],
    recent: tuple[str, ...] = (),
) -> "AiGeneratedReply":
    """«Mira 👇» o «te lo envío» sin adjunto: se adjunta lo prometido o se pregunta cuál."""
    if reply.photo_ref is None and reply.shortcut_id is None and shows_photos(reply.message, client_text):
        shown = infer_photo_ref(reply.message, client_text, products, recent)
        if shown:
            log.info("IA dijo que mostraba fotos sin photo_ref; se adjuntan")
            return replace(reply, photo_ref=shown)
        if any(p.photos for p in products):
            log.info("IA dijo que mostraba fotos sin decir de cuál; se pregunta")
            return replace(reply, message=photo_choice_message(products))
    if reply.shortcut_id is None and reply.photo_ref is None:
        promised = infer_promised_shortcut(reply.message, client_text, shortcuts)
        if promised:
            log.info("IA prometió un atajo sin adjuntarlo; se adjunta")
            return replace(reply, shortcut_id=promised)
    return reply


def asks_to_resend(client_text: str) -> bool:
    """«No me llegó», «mándamelo de nuevo», «???»: el cliente no tiene lo que le prometimos."""
    return bool(_RESEND_RE.search(_plain(client_text)))


def wants_photos_again(client_text: str, reply: "AiGeneratedReply", *, conversation_id, asked_at: float) -> bool:
    """«No me llegó», o vuelve a pedir las fotos después de recibirlas: salen otra vez.
    Un reintento de la misma respuesta no las repite."""
    if asks_to_resend(client_text):
        return True
    if not (reply.photo_ref and _ASKS_PHOTO_RE.search(_plain(client_text))):
        return False
    sent_at = photos_sent_at(conversation_id, reply.photo_ref)
    return sent_at is not None and sent_at < asked_at


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_ai_reply(
    raw: str,
    *,
    valid_ids: frozenset[str],
    valid_book_keys: frozenset[tuple[str, str, str]] | None = None,
    valid_photo_refs: frozenset[str] = frozenset(),
) -> AiGeneratedReply:
    from app.application.ai.ai_appointment_service import parse_book_slot

    text = (raw or "").strip()
    if not text:
        return AiGeneratedReply(message="")

    book_keys = valid_book_keys or frozenset()
    data = _load_reply_json(_strip_json_fence(text))
    if data is None:
        # JSON roto: nunca mandarle llaves y comillas al cliente, solo el texto del mensaje.
        return AiGeneratedReply(message=_salvage_message(text) or text)

    message = str(data.get("message") or data.get("reply") or "").strip()
    if not message:
        return AiGeneratedReply(message=text)

    raw_sid = data.get("shortcut_id")
    shortcut_id: Optional[str] = None
    if raw_sid is not None:
        sid = str(raw_sid).strip()
        if sid and sid.lower() not in {"null", "none"} and sid in valid_ids:
            shortcut_id = sid

    book_slot = parse_book_slot(data.get("book_slot"), valid_keys=book_keys)
    stage = str(data.get("stage") or "").strip().lower()
    if stage not in STAGE_RANK:
        stage = STAGE_CHATTING

    return AiGeneratedReply(
        message=message,
        shortcut_id=shortcut_id,
        book_slot=book_slot,
        stage=stage,
        order=parse_order(data.get("order")),
        missing=clean_topic(data.get("missing")),
        photo_ref=_photo_ref(data.get("photo_ref"), valid_photo_refs),
        chosen=_chosen(data.get("chosen")),
    )


def _chosen(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    return "" if text.lower() in {"null", "none", "ninguno"} else text[:120]


def _photo_ref(raw: Any, valid: frozenset[str]) -> Optional[str]:
    ref = str(raw or "").strip().lower().removeprefix("ref=")
    return ref if ref in valid else None


def _load_reply_json(candidate: str) -> Optional[dict]:
    attempts = [candidate]
    start, end = candidate.find("{"), candidate.rfind("}")
    if 0 <= start < end:
        attempts.append(candidate[start : end + 1])
    for attempt in attempts:
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _salvage_message(text: str) -> Optional[str]:
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"').strip() or None
    except json.JSONDecodeError:
        return None


def send_shortcut_content(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    shortcut: dict,
    source: str,
) -> Message:
    kind = shortcut.get("type")
    if kind == "image":
        message = send_image_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            image_path=str(shortcut["image_path"]),
            caption="",
            source=source,
        )
    elif kind == "document":
        message = send_document_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            file_path=str(shortcut["file_path"]),
            file_name=str(shortcut.get("file_name") or "catalogo.pdf"),
            source=source,
        )
    else:
        message = send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=str(shortcut["text"]),
            source=source,
        )
    if shortcut.get("id"):
        mark_shortcut_sent(message.conversation_id, str(shortcut["id"]))
    return message


def send_bot_shortcut(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    shortcut: dict,
) -> None:
    from app.domain.entities.enums import MessageSource

    send_shortcut_content(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
        shortcut=shortcut,
        source=MessageSource.BOT.value,
    )


def send_reply_with_shortcut(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    reply: AiGeneratedReply,
    allow_resend: bool = False,
) -> None:
    """Envía mensaje de la IA y atajo opcional — sin cerrar venta ni agendar."""
    from app.domain.entities.enums import MessageSource

    if not reply.message.strip():
        return

    send_text_message(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
        text=reply.message.strip(),
        source=MessageSource.BOT.value,
    )
    if reply.shortcut_id:
        _send_reply_shortcut(
            db, tenant=tenant, session=session, conversation=conversation, reply=reply, allow_resend=allow_resend
        )
    if reply.photo_ref:
        send_product_photos(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            ref=reply.photo_ref,
            allow_resend=allow_resend,
        )


def _photos_sent_key(conversation_id, ref: str) -> str:
    return f"ai:product_photos_sent:{conversation_id}:{ref}"


def photos_sent_at(conversation_id, ref: str) -> Optional[float]:
    try:
        raw = get_redis().get(_photos_sent_key(conversation_id, ref))
        return float(raw) if raw is not None else None
    except Exception:
        return None


def send_product_photos(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    ref: str,
    allow_resend: bool = False,
) -> int:
    """Las fotos del producto, la primera con nombre y precio. Devuelve cuántas salieron."""
    from app.domain.entities.enums import MessageSource

    product = find_by_ref(list_products(db, tenant.id), ref)
    if product is None or not product.photos:
        return 0
    key = _photos_sent_key(conversation.id, ref)
    try:
        recently = get_redis().exists(key)
    except Exception:
        recently = False
    if recently and not allow_resend:
        log.info("Fotos ya enviadas hace poco, no se reenvían conv=%s ref=%s", conversation.id, ref)
        return 0
    sent = 0
    for index, path in enumerate(product.photos):
        try:
            send_image_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                image_path=str(path),
                caption=photo_caption(product) if index == 0 else "",
                source=MessageSource.BOT.value,
            )
            sent += 1
        except Exception:
            log.exception("Error enviando foto de producto conv=%s ref=%s", conversation.id, ref)
    if sent:
        try:
            get_redis().set(key, str(time.time()), ex=SHORTCUT_RESEND_GUARD_SECONDS)
        except Exception:
            pass
        log.info("IA envió %s fotos de %s conv=%s", sent, product.name, conversation.id)
    return sent


def _send_reply_shortcut(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    reply: AiGeneratedReply,
    allow_resend: bool,
) -> None:
    shortcut = find_shortcut(db, tenant.id, reply.shortcut_id)
    if shortcut is None:
        log.warning(
            "IA pidió atajo inexistente tenant=%s shortcut_id=%s",
            tenant.id,
            reply.shortcut_id,
        )
        return

    sent_at = shortcut_sent_at(conversation.id, reply.shortcut_id)
    if not allow_resend and sent_at is not None and time.time() - sent_at < SHORTCUT_RESEND_GUARD_SECONDS:
        log.info(
            "Atajo ya enviado hace poco, no se reenvía conv=%s shortcut=%s",
            conversation.id,
            reply.shortcut_id,
        )
        return

    try:
        send_bot_shortcut(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            shortcut=shortcut,
        )
        log.info(
            "IA envió atajo tenant=%s conv=%s label=%s",
            tenant.id,
            conversation.id,
            shortcut.get("label"),
        )
    except Exception:
        log.exception(
            "Error enviando atajo IA conv=%s shortcut=%s",
            conversation.id,
            reply.shortcut_id,
        )
