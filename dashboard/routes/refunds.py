"""Refund and returns analytics route handlers."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import refund_service

router = APIRouter()


@router.get("/summary")
def refund_summary(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return refund_service.get_refund_summary(db, start, end)


@router.get("/trend")
def refund_trend(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "last30")
    return refund_service.get_refund_trend(db, start, end)


@router.get("/by-product")
def refunds_by_product(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return refund_service.get_refunds_by_product(db, start, end, limit)


@router.get("/by-category")
def refunds_by_category(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return refund_service.get_refunds_by_category(db, start, end)


@router.get("/recent")
def recent_returns(
    limit: int = Query(30, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return refund_service.get_recent_returns(db, limit)
