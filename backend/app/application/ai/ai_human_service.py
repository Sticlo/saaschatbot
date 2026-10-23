"""Que la IA escriba y se comporte como una persona del equipo: sin frases de robot, sin alargar
despedidas, sin quedarse conversando con otro bot."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

HUMAN_STYLE = """

Escribe como una persona real del equipo chateando por WhatsApp, no como un asistente virtual:
- Frases cortas y naturales, como un vendedor colombiano amable. Nada de tono de call center ni de correo formal.
- Si ya saludaste en este chat, no vuelvas a saludar. No repitas el nombre del cliente en cada mensaje.
- Máximo un emoji por mensaje, y no en todos.
- No empieces siempre igual («¡Claro que sí!», «¡Perfecto!», «¡Qué alegría!»): varía o ve directo al punto.
- Sin viñetas, negritas ni títulos, salvo que el cliente pida comparar varias opciones o precios.
- Nunca escribas: «entiendo perfectamente», «quedo atento», «no dudes en», «estoy aquí para ayudarte», «será un placer», «excelente pregunta», «¿en qué más te puedo ayudar?», «sin ningún compromiso», «como asistente virtual».
- No anuncies que eres un asistente virtual. Si el cliente te pregunta directamente si eres un bot o una persona, no mientas: dile con naturalidad que eres el asistente del negocio y que el equipo también está pendiente del chat."""

_ROBOT_RE = re.compile(
    r"\b(?:entiendo|comprendo)\s+perfectamente"
    r"|\bquedo\s+(?:atent[oa]|a\s+tu\s+disposicion|pendiente)"
    r"|\bno\s+dudes?\s+en\b"
    r"|\bestoy\s+aqui\s+para\s+(?:ayudar|servir|lo\s+que)"
    r"|\b(?:sera|es)\s+un\s+placer\s+(?:ayudar|atender|asistir)"
    r"|\b(?:excelente|buena|gran)\s+pregunta"
    r"|\ben\s+que\s+mas\s+(?:te|le)\s+(?:puedo|podemos)\s+(?:ayudar|servir|colaborar)"
    r"|\b(?:algo\s+mas|alguna\s+otra\s+cosa)\s+en\s+(?:lo\s+)?que\s+(?:te|le)\s+(?:pueda|puedo|podamos)\s+(?:ayudar|servir)"
    r"|\bsin\s+ningun\s+compromiso"
    r"|\bcomo\s+(?:tu\s+)?(?:asistente\s+virtual|inteligencia\s+artificial|modelo\s+de\s+lenguaje)"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", _plain(text))


def robot_phrases(text: str) -> list[str]:
    found = [m.group(0) for m in _ROBOT_RE.finditer(_plain(text))]
    if "!!" in (text or "") or "¡¡" in (text or ""):
        found.append("!!")
    return found


def _last_question(text: str) -> str:
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text or "") if p.strip()]
    return parts[-1] if parts and parts[-1].rstrip().endswith("?") else ""


def repeats_last_question(reply: str, previous: str) -> bool:
    """La misma pregunta del mensaje anterior otra vez: así no habla una persona."""
    new, old = _last_question(reply), _last_question(previous)
    if not new or not old:
        return False
    a = {w for w in _words(new) if len(w) > 2}
    b = {w for w in _words(old) if len(w) > 2}
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.6


def style_correction(reply: str, previous_reply: str = "") -> str:
    """Nota para que la IA reescriba su respuesta; vacío si suena a persona."""
    problems = []
    phrases = robot_phrases(reply)
    if phrases:
        listed = ", ".join(f"«{p}»" for p in dict.fromkeys(phrases))
        problems.append(f"usaste frases que suenan a robot o a call center ({listed}): quítalas")
    if repeats_last_question(reply, previous_reply):
        problems.append("terminas con la misma pregunta de tu mensaje anterior: formúlala distinto o pregunta otra cosa")
    if not problems:
        return ""
    return (
        "Tu respuesta anterior no sonó a una persona: " + "; ".join(problems)
        + ". Escríbela otra vez con la misma información, natural y corta, en el mismo formato JSON."
    )


def without_robot_phrases(text: str) -> str:
    """Último recurso si la IA insiste: se quitan las frases de robot sin dejar el mensaje vacío."""
    cleaned = re.sub(r"!{2,}", "!", re.sub(r"¡{2,}", "¡", text or ""))
    parts = [p for p in _SENTENCE_SPLIT_RE.split(cleaned) if p.strip()]
    kept = [p for p in parts if not _ROBOT_RE.search(_plain(p))]
    if not kept or len(kept) == len(parts):
        return cleaned
    return " ".join(kept)


# --- Despedidas que no se acaban -----------------------------------------------------------

_FAREWELL_RE = re.compile(
    r"\bque\s+(?:tengas|tenga|pases|pase)\s+(?:un\s+)?(?:lindo|buen|bonito|feliz|excelente|muy\s+buen)"
    r"|\b(?:feliz|lindo|buen)\s+(?:dia|tarde|noche|fin\s+de\s+semana)"
    r"|\bhasta\s+(?:luego|pronto|manana)|\bun\s+abrazo|\bque\s+te\s+vaya\s+(?:bien|super)"
    r"|\bfue\s+un\s+gusto|\bcuando\s+quieras\b|\ba\s+la\s+orden\b|\bpor\s+aca\s+estamos|\baqui\s+estamos"
)
_COURTESY_WORDS = frozenset(
    "gracias muchas muchisimas mil igualmente igual bueno buen buena buenas dia dias tarde tardes noche noches "
    "ok okey okay oki listo dale perfecto bendiciones bendicion feliz ti a vos usted con gusto chao chau adios "
    "vale genial excelente que tengas tenga tambien amen super claro si de nada para hasta luego pronto un una "
    "abrazo saludos muy amable amables quedo atenta atento aqui estoy estamos lo la le te y por todo ya va "
    "senor senora senorita mi mis hermosa hermoso linda lindo bonito bonita feliz finde semana fin el los las "
    "ustedes tu igualmente jaja jajaja jeje".split()
)


def is_endless_goodbye(last_reply: str, client_text: str) -> bool:
    """Ya nos despedimos sin preguntar nada y el cliente solo devuelve la cortesía: una persona no
    contestaría otra vez. Si agrega algo real («gracias, ¿y el sábado?»), sí se contesta."""
    reply = _plain(last_reply)
    text = (client_text or "").strip()
    if not reply or "?" in last_reply or not _FAREWELL_RE.search(reply):
        return False
    if not text or "?" in text or len(text) >= 160:
        return False
    return all(w in _COURTESY_WORDS for w in _words(text))


# --- Del otro lado también puede haber un bot -------------------------------------------------


@dataclass(frozen=True)
class Turn:
    inbound: bool
    body: str
    at: datetime


_SAYS_BOT_RE = re.compile(
    r"\bsoy\s+(?:un|una)\s+(?:bot|robot|chatbot|asistente\s+(?:virtual|automatic[oa]|digital))"
    r"|\bsoy\s+[\w ]{1,25}?,?\s+(?:tu|la|el|su)\s+(?:asistente|agente)\s+(?:virtual|automatic[oa]|digital)"
    r"|\b(?:esta\s+es\s+una|este\s+es\s+un)\s+(?:respuesta|mensaje)\s+automatic[oa]"
    r"|\bmensaje\s+(?:generado|enviado)\s+automaticamente"
)
_FAST_MIN_CHARS = 80
_FAST_MAX_SECONDS = 25
_FAST_CHARS_PER_SECOND = 6
_LOOP_MIN_CHARS = 20
_LOOP_LONG_CHARS = 80


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def says_it_is_a_bot(text: str) -> bool:
    """«Soy Sol, la asistente virtual» sí; «¿eres un bot?» o «soy la asistente de gerencia» no."""
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        plain = _plain(sentence)
        if "?" in sentence or "¿" in sentence:
            continue
        if _SAYS_BOT_RE.search(plain):
            return True
    return False


def other_side_bot_reason(turns: Sequence[Turn]) -> Optional[str]:
    """Motivo para apartarse si el que escribe parece un bot; None si es una persona.
    `turns`: el chat del más viejo al más nuevo."""
    if not turns or not turns[-1].inbound:
        return None
    last = turns[-1]
    if says_it_is_a_bot(last.body):
        return f"dijo que es un bot: «{last.body.strip()[:160]}»"

    # Solo cuenta si repite lo mismo DESPUÉS de que le respondimos: un cliente que insiste porque
    # nadie le contesta no es un bot.
    key = _plain(last.body)
    if len(key) >= _LOOP_MIN_CHARS:
        copies = [i for i, t in enumerate(turns) if t.inbound and _plain(t.body) == key]
        answered = sum(
            1 for a, b in zip(copies, copies[1:]) if any(not t.inbound for t in turns[a + 1: b])
        )
        if answered >= 2 or (answered >= 1 and len(key) >= _LOOP_LONG_CHARS):
            return "repite el mismo mensaje automático aunque ya le respondimos"

    fast = 0
    for prev, cur in zip(turns, turns[1:]):
        if prev.inbound or not cur.inbound:
            continue
        seconds = (_as_utc(cur.at) - _as_utc(prev.at)).total_seconds()
        chars = len(cur.body or "")
        quick = chars >= _FAST_MIN_CHARS and 0 < seconds < _FAST_MAX_SECONDS and chars / seconds > _FAST_CHARS_PER_SECOND
        fast = fast + 1 if quick else 0
    if fast >= 2:
        return "contesta textos largos en segundos, más rápido de lo que escribe una persona"
    return None


# --- Topes por chat ----------------------------------------------------------------------------

CHAT_REPLY_CAPS = ((timedelta(minutes=10), 8), (timedelta(hours=1), 20), (timedelta(days=1), 50))
_SAME_REPLY_GAP = timedelta(seconds=20)


def count_replies(bot_times: Sequence[datetime]) -> list[datetime]:
    """Una respuesta puede salir partida en varios mensajes: cuenta respuestas, no mensajes."""
    replies: list[datetime] = []
    for at in sorted(_as_utc(t) for t in bot_times):
        if not replies or at - replies[-1] > _SAME_REPLY_GAP:
            replies.append(at)
    return replies


def chat_cap_reason(bot_times: Sequence[datetime], now: Optional[datetime] = None) -> Optional[str]:
    now = _as_utc(now or datetime.now(timezone.utc))
    replies = count_replies(bot_times)
    for window, cap in CHAT_REPLY_CAPS:
        if sum(1 for t in replies if now - t <= window) >= cap:
            minutes = int(window.total_seconds() // 60)
            span = f"{minutes} min" if minutes < 60 else ("1 hora" if minutes == 60 else "24 horas")
            return f"la IA ya respondió {cap} veces en {span} en este chat"
    return None
