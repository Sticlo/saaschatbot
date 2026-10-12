"""Preguntas que la IA no supo responder. El dueño contesta una vez y la IA lo aprende."""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.domain.entities import AiQuestion

STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
MAX_OPEN_QUESTIONS = 100
TOPIC_MAX_CHARS = 160
ANSWER_MAX_CHARS = 1000
_LEARNED_LIMIT = 60
_LEARNED_MAX_CHARS = 4000

_NOTHING = {"", "null", "none", "ninguno", "ninguna", "nada", "n/a", "-"}


def clean_topic(raw) -> str:
    if not isinstance(raw, str):
        return ""
    topic = " ".join(raw.split()).strip(" .,:;«»\"'")[:TOPIC_MAX_CHARS]
    return "" if topic.lower() in _NOTHING or len(topic) < 3 else topic


def topic_key(topic: str) -> str:
    plain = unicodedata.normalize("NFKD", topic.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", plain).split())[:TOPIC_MAX_CHARS]


def record_missing_info(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: Optional[uuid.UUID],
    topic: str,
    question: str,
) -> Optional[tuple[AiQuestion, bool]]:
    """Devuelve (pregunta, es_nueva). Si ya estaba pendiente solo suma una vez más."""
    topic = clean_topic(topic)
    key = topic_key(topic)
    if not key:
        return None
    question = " ".join((question or "").split())[:500] or None
    now = datetime.now(timezone.utc)
    row = (
        db.query(AiQuestion)
        .filter(AiQuestion.tenant_id == tenant_id, AiQuestion.topic_key == key)
        .first()
    )
    if row is not None:
        row.times_asked += 1
        row.last_asked_at = now
        if row.status == STATUS_OPEN:
            row.question = question or row.question
            row.conversation_id = conversation_id or row.conversation_id
        return row, False
    pending = (
        db.query(AiQuestion)
        .filter(AiQuestion.tenant_id == tenant_id, AiQuestion.status == STATUS_OPEN)
        .count()
    )
    if pending >= MAX_OPEN_QUESTIONS:
        return None
    row = AiQuestion(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        topic=topic,
        topic_key=key,
        question=question,
        status=STATUS_OPEN,
        times_asked=1,
        last_asked_at=now,
    )
    db.add(row)
    db.flush()
    return row, True


def list_questions(db: Session, tenant_id: uuid.UUID) -> list[AiQuestion]:
    """Pendientes primero (las más preguntadas arriba), luego lo ya aprendido."""
    rows = db.query(AiQuestion).filter(AiQuestion.tenant_id == tenant_id).all()
    pending = sorted(
        (r for r in rows if r.status == STATUS_OPEN),
        key=lambda r: (-r.times_asked, -r.last_asked_at.timestamp()),
    )
    learned = sorted(
        (r for r in rows if r.status == STATUS_ANSWERED),
        key=lambda r: -(r.answered_at or r.created_at).timestamp(),
    )
    return [*pending, *learned]


def get_question(db: Session, tenant_id: uuid.UUID, question_id: uuid.UUID) -> AiQuestion:
    row = (
        db.query(AiQuestion)
        .filter(AiQuestion.tenant_id == tenant_id, AiQuestion.id == question_id)
        .first()
    )
    if row is None:
        raise LookupError("Pregunta no encontrada")
    return row


def answer_question(row: AiQuestion, answer: str) -> AiQuestion:
    text = " ".join((answer or "").split())[:ANSWER_MAX_CHARS]
    if not text:
        raise ValueError("Escribe la respuesta")
    row.answer = text
    row.status = STATUS_ANSWERED
    row.answered_at = datetime.now(timezone.utc)
    return row


def question_to_dict(row: AiQuestion) -> dict:
    return {
        "id": str(row.id),
        "topic": row.topic,
        "question": row.question,
        "answer": row.answer,
        "status": row.status,
        "times_asked": row.times_asked,
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "last_asked_at": row.last_asked_at.isoformat() if row.last_asked_at else None,
    }


def learned_answers_block(db: Session, tenant_id: uuid.UUID) -> str:
    rows = (
        db.query(AiQuestion)
        .filter(AiQuestion.tenant_id == tenant_id, AiQuestion.status == STATUS_ANSWERED)
        .order_by(AiQuestion.answered_at.desc())
        .limit(_LEARNED_LIMIT)
        .all()
    )
    lines: list[str] = []
    used = 0
    for row in rows:
        line = f"- {row.topic}: {row.answer}"
        if used + len(line) > _LEARNED_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return (
        "\n\nRespuestas que te dio el dueño del negocio (úsalas tal cual cuando pregunten por esto):\n"
        + "\n".join(lines)
    )
