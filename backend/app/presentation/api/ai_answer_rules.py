from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.application.ai.ai_answer_rules_service import AnswerRulesError, get_rules, normalize_rules, save_rules
from app.application.billing.tenant_service import log_audit
from app.infrastructure.persistence.database import get_db
from app.shared.core.deps import RequireOwner, RequireViewer

router = APIRouter(prefix="/ai-answer-rules", tags=["ai-answer-rules"])

PREVIEW = 8


class RulesBody(BaseModel):
    rules: Any


def _summary(rules: list[dict]) -> dict:
    return {
        "count": len(rules),
        "review": sum(1 for r in rules if r.get("review")),
        "preview": [{"name": r.get("name") or "", "response": r.get("response") or ""} for r in rules[:PREVIEW]],
    }


@router.get("")
def list_rules(current: RequireViewer, db: Session = Depends(get_db)):
    return _summary(get_rules(db, current.tenant_id))


@router.put("")
def replace_rules(body: RulesBody, request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    try:
        rules = normalize_rules(body.rules)
    except AnswerRulesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_rules(db, current.tenant_id, rules)
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="ai_answer_rules.updated",
        details={"count": len(rules)},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return _summary(rules)


@router.delete("")
def delete_rules(request: Request, current: RequireOwner, db: Session = Depends(get_db)):
    save_rules(db, current.tenant_id, [])
    log_audit(
        db,
        tenant_id=current.tenant_id,
        user_id=current.id,
        action="ai_answer_rules.deleted",
        details={},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return _summary([])
