"""Revenue analytics route handlers."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import revenue_service

router = APIRouter()


@router.get("/breakdown")
def revenue_breakdown(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    cashier: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return revenue_service.get_revenue_breakdown(db, start, end, cashier)


@router.get("/discounts")
def discount_analysis(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return revenue_service.get_discount_analysis(db, start, end)


@router.get("/tax")
def tax_analysis(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return revenue_service.get_tax_analysis(db, start, end)


@router.get("/period-comparison")
def period_comparison(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    preset: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return revenue_service.get_period_comparison(db, start, end)
