"""
Sales analytics route handlers.
Provides sales trend time-series, hourly heatmaps, basket size trends, and recent sales queries.
"""

import logging
from datetime import datetime, date, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import sales_service
import models

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.sales")
logger.setLevel(logging.INFO)

router = APIRouter()


@router.get("/trend", summary="Get sales revenue trend time-series")
def sales_trend(
    period: str = Query("daily", pattern="^(daily|weekly|monthly|yearly)$"),
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    cashier: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves sales revenue trend aggregated by daily, weekly, or monthly periods."""
    try:
        start, end = resolve_date_range(start_date, end_date, preset or "last30")
        logger.info(f"[Sales] User '{current_user.username}' requested sales trend (period={period})")
        csh = cashier if cashier and isinstance(cashier, str) else None
        pym = payment_method if payment_method and isinstance(payment_method, str) else None
        return sales_service.get_sales_trend(db, start, end, period, csh, pym)
    except Exception as e:
        logger.error(f"[Sales Error] Failed to get sales trend: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving sales trend: {str(e)}"
        )


@router.get("/heatmap", summary="Get hourly sales heatmap")
def hourly_heatmap(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves hourly sales distribution matrix for heatmaps."""
    try:
        start, end = resolve_date_range(start_date, end_date, preset or "last30")
        logger.info(f"[Sales] User '{current_user.username}' requested hourly heatmap")
        return sales_service.get_hourly_heatmap(db, start, end)
    except Exception as e:
        logger.error(f"[Sales Error] Failed to get hourly heatmap: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving hourly heatmap: {str(e)}"
        )


@router.get("/basket-trend", summary="Get average basket size trend")
def basket_trend(
    period: str = Query("daily"),
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves average basket size trend over time."""
    try:
        start, end = resolve_date_range(start_date, end_date, preset or "last30")
        logger.info(f"[Sales] User '{current_user.username}' requested basket trend")
        return sales_service.get_basket_trend(db, start, end, period)
    except Exception as e:
        logger.error(f"[Sales Error] Failed to get basket trend: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving basket trend: {str(e)}"
        )


@router.get("/recent", summary="Get recent sales transactions list")
def recent_sales(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    cashier: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves recent completed sales list for table rendering."""
    try:
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Sales] User '{current_user.username}' requested recent sales")
        lim = limit if isinstance(limit, int) else 100
        off = offset if isinstance(offset, int) else 0
        csh = cashier if cashier and isinstance(cashier, str) else None
        pym = payment_method if payment_method and isinstance(payment_method, str) else None
        st = status_filter if status_filter and isinstance(status_filter, str) else None
        
        return sales_service.get_recent_sales(db, start, end, csh, pym, st, lim, off)
    except Exception as e:
        logger.error(f"[Sales Error] Failed to get recent sales: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving recent sales: {str(e)}"
        )
