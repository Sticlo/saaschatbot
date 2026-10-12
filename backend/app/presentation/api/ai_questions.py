from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.application.ai.ai_knowledge_service import (
    ANSWER_MAX_CHARS,
    answer_question,
    get_question,
    list_questions,
    question_to_dict,
)
from app.application.products.product_service import attach_uploaded_photo, get_product
from app.application.realtime.realtime_service import publish_panel_event
from app.infrastructure.persistence.database import get_db
from app.presentation.api.products import enforce_photo_rate_limit
from app.shared.core.deps import RequireOwner, RequireViewer

router = APIRouter(prefix="/ai-questions", tags=["ai"])


class AiQuestionResponse(BaseModel):
    id: str
    topic: str
    question: Optional[str] = None
    answer: Optional[str] = None
    status: str
    times_asked: int
    conversation_id: Optional[str] = None
    last_asked_at: Optional[str] = None


class AiQuestionAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=ANSWER_MAX_CHARS)


@router.get("", response_model=list[AiQuestionResponse])
def get_ai_questions(current: RequireViewer, db: Session = Depends(get_db)):
    return [AiQuestionResponse(**question_to_dict(r)) for r in list_questions(db, current.tenant_id)]


@router.put("/{question_id}", response_model=AiQuestionResponse)
def put_ai_question_answer(
    question_id: uuid.UUID, body: AiQuestionAnswer, current: RequireOwner, db: Session = Depends(get_db)
):
    try:
        row = answer_question(get_question(db, current.tenant_id, question_id), body.answer)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    publish_panel_event(current.tenant_id, {"type": "ai_questions.updated"})
    return AiQuestionResponse(**question_to_dict(row))


@router.post("/{question_id}/photo", response_model=AiQuestionResponse)
def answer_ai_question_with_photo(
    question_id: uuid.UUID,
    current: RequireOwner,
    product_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """«¿Tienes fotos de la suite?»: la foto queda en ese producto y la IA la manda las próximas veces."""
    enforce_photo_rate_limit(current.tenant_id)
    try:
        row = get_question(db, current.tenant_id, question_id)
        product = attach_uploaded_photo(db, get_product(db, current.tenant_id, product_id), file)
        answer_question(row, f"Sí, hay fotos de «{product.name}» en Inventario: envíalas cuando las pidan.")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    publish_panel_event(current.tenant_id, {"type": "ai_questions.updated"})
    publish_panel_event(current.tenant_id, {"type": "products.updated"})
    return AiQuestionResponse(**question_to_dict(row))


@router.delete("/{question_id}", status_code=204)
def delete_ai_question(question_id: uuid.UUID, current: RequireOwner, db: Session = Depends(get_db)):
    """Descartar una pendiente o hacer que la IA olvide una respuesta."""
    try:
        db.delete(get_question(db, current.tenant_id, question_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    publish_panel_event(current.tenant_id, {"type": "ai_questions.updated"})
    return Response(status_code=204)
