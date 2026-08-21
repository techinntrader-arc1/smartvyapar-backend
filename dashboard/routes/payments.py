"""Payment analysis route handlers."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date
from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import payment_service

router = APIRouter()


@router.get("/summary")
def payment_summary(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return payment_service.get_payment_method_summary(db, start, end)


@router.get("/trend")
def payment_trend(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "last30")
    return payment_service.get_payment_trend(db, start, end)


@router.get("/reconciliation")
def payment_reconciliation(
    date_str: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    if not date_str:
        date_str = date.today().isoformat()
    return payment_service.get_payment_reconciliation(db, date_str)


@router.get("/outstanding")
def outstanding_payments(
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return payment_service.get_outstanding_payments(db, limit)
