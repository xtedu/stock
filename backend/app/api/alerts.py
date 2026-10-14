from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AlertLog, AlertRule
from ..schemas import AlertLogOut, AlertRuleIn, AlertRuleOut

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/logs", response_model=list[AlertLogOut])
def list_logs(limit: int = 50, db: Session = Depends(get_db)):
    return (db.query(AlertLog).order_by(AlertLog.notified_at.desc())
            .limit(limit).all())


@router.get("/rules", response_model=list[AlertRuleOut])
def list_rules(db: Session = Depends(get_db)):
    return db.query(AlertRule).order_by(AlertRule.id.desc()).all()


@router.post("/rules", response_model=AlertRuleOut)
def create_rule(payload: AlertRuleIn, db: Session = Depends(get_db)):
    rule = AlertRule(**payload.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404, "rule not found")
    db.delete(rule)
    db.commit()
    return {"ok": True}
