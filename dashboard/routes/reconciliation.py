"""End-of-Day Reconciliation route handlers."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date
from database import get_db
from auth import get_current_user, require_admin
from dashboard.services import reconciliation_service

router = APIRouter()


@router.get("/summary")
def eod_summary(
    date_str: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    if not date_str:
        date_str = date.today().isoformat()
    return reconciliation_service.get_eod_summary(db, date_str)


@router.get("/cashier-breakdown")
def eod_cashier_breakdown(
    date_str: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    if not date_str:
        date_str = date.today().isoformat()
    return reconciliation_service.get_eod_cashier_breakdown(db, date_str)


@router.get("/closing-summary")
def closing_summary(
    date_str: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    if not date_str:
        date_str = date.today().isoformat()
    return reconciliation_service.get_closing_summary(db, date_str)


@router.get("/print-report")
def eod_print_report(
    date_str: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    if not date_str:
        date_str = date.today().isoformat()
    text = reconciliation_service.get_eod_printable_report(db, date_str)
    return {"report_text": text, "date": date_str}
