"""Product performance route handlers."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import product_service

router = APIRouter()


@router.get("/top-sellers")
def top_sellers(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    order_by: str = Query("revenue", regex="^(revenue|qty)$"),
    category_id: Optional[int] = Query(None),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return product_service.get_top_products(db, start, end, order_by, limit, category_id)


@router.get("/slow-movers")
def slow_movers(
    period_days: int = Query(30, ge=7, le=365),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return product_service.get_slow_movers(db, period_days, limit=limit)


@router.get("/revenue-ranking")
def revenue_ranking(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(30, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return product_service.get_product_revenue_ranking(db, start, end, limit)


@router.get("/movement")
def product_movement(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    product_id: Optional[int] = Query(None),
    limit: int = Query(30, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    return product_service.get_product_movement_summary(db, start, end, product_id, limit)
