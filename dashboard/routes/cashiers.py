"""
Cashier performance route handlers.
Provides performance summaries, daily trends, shift reports, comparison metrics, and CSV exports.
"""

import logging
import csv
import io
from datetime import datetime, date, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import cashier_service
from dashboard.filters.query_builder import get_distinct_cashiers
import models
from services.sale_return_service import POSTED_SALE_STATUSES

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.cashiers")
logger.setLevel(logging.INFO)

router = APIRouter()

ALLOWED_PRESETS = {"today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last7", "last30", "this_year", "custom"}


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class CashierSummaryItem(BaseModel):
    username: str = Field(..., description="Cashier username")
    total_orders: int = Field(0, description="Total number of completed orders")
    gross_revenue: float = Field(0.0, description="Gross total sales before discounts")
    net_revenue: float = Field(0.0, description="Net revenue after discounts")
    avg_basket: float = Field(0.0, description="Average order value")
    total_discount_given: float = Field(0.0, description="Total discount given by cashier")


class CashierDailyTrendItem(BaseModel):
    date: str = Field(..., description="Date string YYYY-MM-DD")
    cashier: str = Field(..., description="Cashier username")
    total_sales: float = Field(0.0, description="Total sales value on date")
    order_count: int = Field(0, description="Order count on date")


class ShiftSummaryItem(BaseModel):
    id: Optional[int] = None
    cashier: str = Field(..., description="Cashier username")
    date: str = Field(..., description="Closing or shift date")
    status: str = Field("closed", description="Shift status: open, closed")
    opening_cash: float = Field(0.0, description="Opening cash in drawer")
    closing_cash: float = Field(0.0, description="Recorded closing cash")
    expected_cash: float = Field(0.0, description="Expected cash based on transactions")
    difference: float = Field(0.0, description="Over/Short variance")


class CashierListResponse(BaseModel):
    cashiers: List[str]
    count: int


class CashierDetailResponse(BaseModel):
    username: str
    period_start: str
    period_end: str
    summary: CashierSummaryItem
    first_sale_at: Optional[str] = None
    last_sale_at: Optional[str] = None


class CashierComparisonItem(BaseModel):
    username: str
    gross_revenue: float
    order_count: int
    avg_basket: float
    market_share_pct: float


class CashierComparisonResponse(BaseModel):
    period_start: str
    period_end: str
    total_gross_revenue: float
    total_orders: int
    comparison: List[CashierComparisonItem]


class HealthCheckResponse(BaseModel):
    status: str
    module: str
    timestamp: str


# ── Helper Functions ──────────────────────────────────────────────────────────
def _validate_date_inputs(preset: Optional[str], start_date: Optional[str], end_date: Optional[str]):
    if preset and preset.lower() not in ALLOWED_PRESETS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid preset '{preset}'. Allowed presets: {', '.join(sorted(ALLOWED_PRESETS))}"
        )
    if start_date and isinstance(start_date, str):
        try:
            date.fromisoformat(start_date)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid start_date '{start_date}'. Must be YYYY-MM-DD format."
            )
    if end_date and isinstance(end_date, str):
        try:
            date.fromisoformat(end_date)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid end_date '{end_date}'. Must be YYYY-MM-DD format."
            )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthCheckResponse, summary="Cashiers module health check")
def cashiers_health():
    """Health check endpoint for the cashiers module."""
    return HealthCheckResponse(
        status="ok",
        module="cashiers",
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@router.get("/summaries", response_model=List[CashierSummaryItem], summary="Get performance summaries for cashiers")
def cashier_summaries(
    preset: Optional[str] = Query(None, description="Date filter preset: this_month, last30, etc."),
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    query: Optional[str] = Query(None, description="Search cashier name"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Retrieves performance metrics (orders, gross revenue, net revenue, avg basket, discounts)
    grouped by cashier for the specified period.
    """
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Cashiers] User '{current_user.username}' requested summaries for period {start} to {end}")
        
        raw = cashier_service.get_cashier_summaries(db, start, end)
        
        if query:
            q_lower = query.lower().strip()
            raw = [c for c in raw if q_lower in str(c.get("username", "")).lower()]

        items = [CashierSummaryItem(**item) for item in raw]
        return items
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to get cashier summaries: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing cashier summaries: {str(e)}"
        )


@router.get("/daily-trend", response_model=List[CashierDailyTrendItem], summary="Get daily sales trend per cashier")
def cashier_daily_trend(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves daily breakdown of sales and order count per cashier for charting."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "last30")
        logger.info(f"[Cashiers] User '{current_user.username}' requested daily trend for period {start} to {end}")
        
        raw = cashier_service.get_cashier_daily_trend(db, start, end)
        items = [CashierDailyTrendItem(**item) for item in raw]
        return items
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to get daily trend: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving daily trend: {str(e)}"
        )


@router.get("/shift-summaries", response_model=List[ShiftSummaryItem], summary="Get cashier shift reports")
def shift_summaries(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves shift closing reports, drawer totals, and cash variances per cashier."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "last7")
        logger.info(f"[Cashiers] User '{current_user.username}' requested shift summaries for period {start} to {end}")
        
        raw = cashier_service.get_cashier_shift_summaries(db, start, end)
        items = [ShiftSummaryItem(**item) for item in raw]
        return items
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to get shift summaries: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving shift summaries: {str(e)}"
        )


@router.get("/list", response_model=CashierListResponse, summary="List distinct cashier usernames")
def list_cashiers(
    query: Optional[str] = Query(None, description="Search cashier by name"),
    limit: int = Query(100, ge=1, le=1000, description="Pagination limit"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    sort_order: str = Query("asc", description="Sort order: asc, desc"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Returns the list of distinct active cashiers with search, pagination, and sorting."""
    try:
        logger.info(f"[Cashiers] User '{current_user.username}' requested cashiers list")
        all_cashiers = get_distinct_cashiers(db)
        
        # Filter by search query
        if query:
            q_lower = query.lower().strip()
            all_cashiers = [c for c in all_cashiers if q_lower in c.lower()]

        # Sort
        all_cashiers.sort(reverse=(sort_order.lower() == "desc"))

        # Paginate
        paginated = all_cashiers[offset : offset + limit]

        return CashierListResponse(
            cashiers=paginated,
            count=len(all_cashiers)
        )
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to list cashiers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching cashier list: {str(e)}"
        )


@router.get("/details/{cashier_name}", response_model=CashierDetailResponse, summary="Get details for single cashier")
def get_cashier_details(
    cashier_name: str,
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves detailed performance metrics and activity timestamps for an individual cashier."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        all_sums = cashier_service.get_cashier_summaries(db, start, end)
        target = next((c for c in all_sums if c.get("username", "").lower() == cashier_name.lower()), None)
        
        if not target:
            # Return empty baseline if no sales in period
            target = {
                "username": cashier_name,
                "total_orders": 0,
                "gross_revenue": 0.0,
                "net_revenue": 0.0,
                "avg_basket": 0.0,
                "total_discount_given": 0.0
            }

        # First and last sale timestamp query
        sale_bounds = db.query(
            models.Sale.created_at
        ).filter(
            models.Sale.cashier.ilike(cashier_name),
            models.Sale.status.in_(POSTED_SALE_STATUSES)
        ).order_by(models.Sale.created_at.asc()).all()

        first_at = sale_bounds[0][0].isoformat() if sale_bounds else None
        last_at = sale_bounds[-1][0].isoformat() if sale_bounds else None

        return CashierDetailResponse(
            username=cashier_name,
            period_start=start,
            period_end=end,
            summary=CashierSummaryItem(**target),
            first_sale_at=first_at,
            last_sale_at=last_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to get cashier details for '{cashier_name}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting cashier details: {str(e)}"
        )


@router.get("/performance/comparison", response_model=CashierComparisonResponse, summary="Compare cashier performance")
def cashier_performance_comparison(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Compares gross revenue market share and basket sizes across all cashiers for a period."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        summaries = cashier_service.get_cashier_summaries(db, start, end)
        total_rev = sum(c.get("gross_revenue", 0.0) for c in summaries)
        total_ord = sum(c.get("total_orders", 0) for c in summaries)

        comparison_items = []
        for c in summaries:
            g_rev = c.get("gross_revenue", 0.0)
            m_share = round((g_rev / total_rev * 100.0), 1) if total_rev > 0 else 0.0
            comparison_items.append(CashierComparisonItem(
                username=c.get("username", "Unknown"),
                gross_revenue=g_rev,
                order_count=c.get("total_orders", 0),
                avg_basket=c.get("avg_basket", 0.0),
                market_share_pct=m_share
            ))

        return CashierComparisonResponse(
            period_start=start,
            period_end=end,
            total_gross_revenue=round(total_rev, 2),
            total_orders=total_ord,
            comparison=comparison_items
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to generate cashier comparison: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating comparison report: {str(e)}"
        )


@router.get("/export", summary="Export cashier performance metrics as CSV")
def export_cashier_report(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Generates and downloads a CSV export file of cashier performance summaries."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        summaries = cashier_service.get_cashier_summaries(db, start, end)

        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV Header
        writer.writerow(["Cashier Username", "Total Orders", "Gross Revenue (PKR)", "Net Revenue (PKR)", "Avg Basket (PKR)", "Discounts Given (PKR)"])
        
        for c in summaries:
            writer.writerow([
                c.get("username", ""),
                c.get("total_orders", 0),
                f"{c.get('gross_revenue', 0.0):.2f}",
                f"{c.get('net_revenue', 0.0):.2f}",
                f"{c.get('avg_basket', 0.0):.2f}",
                f"{c.get('total_discount_given', 0.0):.2f}"
            ])

        output.seek(0)
        filename = f"cashier_performance_{start}_to_{end}.csv"
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Cashiers Error] Failed to export CSV: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error exporting cashier report CSV: {str(e)}"
        )
