"""
Orders analytics route handlers.
Provides order status breakdowns, volume trends, cashier performance, payment method distributions, order timeline, peak patterns, and CSV exports.
"""

import logging
import csv
import io
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, extract

from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import orders_service
from services.sale_return_service import POSTED_SALE_STATUSES
import models

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.orders")
logger.setLevel(logging.INFO)

router = APIRouter()

ALLOWED_PRESETS = {"today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last7", "last30", "last90", "this_year", "custom"}
ALLOWED_PERIODS = {"daily", "weekly", "monthly", "yearly"}


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class OrderStatusBreakdown(BaseModel):
    total: int = Field(0, description="Total order count")
    completed: int = Field(0, description="Completed order count")
    held: int = Field(0, description="Held order count")
    returned: int = Field(0, description="Fully returned order count")
    partially_returned: int = Field(0, description="Partially returned order count")
    cancelled: int = Field(0, description="Cancelled/voided order count")


class OrderVolumeTrendItem(BaseModel):
    label: str = Field(..., description="Date or period label")
    order_count: int = Field(0, description="Total order count")
    total_sales: float = Field(0.0, description="Gross sales revenue")


class OrdersByCashierItem(BaseModel):
    cashier: str = Field(..., description="Cashier name")
    count: int = Field(0, description="Order count handled")
    total_sales: float = Field(0.0, description="Total sales handled")
    avg_order_value: float = Field(0.0, description="Average order value")


class OrdersByPaymentMethodItem(BaseModel):
    method: str = Field(..., description="Payment method name: Cash, Card, Credit, etc.")
    count: int = Field(0, description="Order count")
    total_amount: float = Field(0.0, description="Total sales value")
    pct: float = Field(0.0, description="Percentage of total orders")


class OrderTimelineItem(BaseModel):
    id: int
    invoice_no: str
    date: Optional[str] = None
    created_at: Optional[str] = None
    customer_name: Optional[str] = "Walk-in Customer"
    cashier: Optional[str] = "Admin"
    payment_method: Optional[str] = "Cash"
    total: float = 0.0
    status: str = "completed"
    items_count: int = 1


class PeakPatternResponse(BaseModel):
    peak_hour: int = Field(12, description="Busiest hour of the day (0-23)")
    busiest_weekday: int = Field(0, description="Busiest day of the week (0=Sun, 6=Sat)")
    peak_hour_orders: int = Field(0, description="Order count during peak hour")
    hourly_distribution: List[Dict[str, Any]] = Field(default_factory=list)


class OrderItemDetail(BaseModel):
    product_id: int
    product_name: str
    qty: float
    unit_price: float
    subtotal: float
    discount: float = 0.0


class OrderDetailsResponse(BaseModel):
    order_id: int
    invoice_no: str
    date: str
    customer_name: Optional[str] = None
    cashier_name: Optional[str] = None
    payment_method: str
    subtotal: float
    discount: float
    tax: float
    total: float
    paid_amount: float
    change_due: float
    status: str
    items: List[OrderItemDetail] = Field(default_factory=list)


class OrderListItem(BaseModel):
    id: int
    invoice_no: str
    date: str
    customer_name: Optional[str] = None
    cashier: Optional[str] = None
    payment_method: str
    total: float
    status: str


class OrderListResponse(BaseModel):
    orders: List[OrderListItem]
    count: int


class HourlyDistributionItem(BaseModel):
    hour: int
    order_count: int
    total_sales: float


class HourlyDistributionResponse(BaseModel):
    period_start: str
    period_end: str
    peak_hour: int
    hourly_data: List[HourlyDistributionItem]


class AOVTrendItem(BaseModel):
    period_label: str
    total_revenue: float
    total_orders: int
    average_order_value: float


class AOVTrendResponse(BaseModel):
    period_start: str
    period_end: str
    overall_aov: float
    trend: List[AOVTrendItem]


class CancellationRateResponse(BaseModel):
    period_start: str
    period_end: str
    total_orders: int
    cancelled_orders: int
    cancellation_rate_pct: float


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

@router.get("/health", response_model=HealthCheckResponse, summary="Orders module health check")
def orders_health():
    """Health check endpoint for the orders module."""
    return HealthCheckResponse(
        status="ok",
        module="orders",
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@router.get("/status", response_model=OrderStatusBreakdown, summary="Get order status breakdown")
def order_status(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves count breakdown of orders by status (completed, held, returned, cancelled)."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Orders] User '{current_user.username}' requested status breakdown for period {start} to {end}")
        
        raw = orders_service.get_order_status_breakdown(db, start, end)
        return OrderStatusBreakdown(**raw)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get order status breakdown: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving order status breakdown: {str(e)}"
        )


@router.get("/volume", summary="Get order volume trend")
def order_volume(
    period: str = Query("daily", description="Grouping period: daily, weekly, monthly, yearly"),
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves order count and sales volume trend over time."""
    try:
        if period.lower() not in ALLOWED_PERIODS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid period '{period}'. Allowed periods: {', '.join(sorted(ALLOWED_PERIODS))}"
            )
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "last30")
        logger.info(f"[Orders] User '{current_user.username}' requested volume trend (period={period})")
        
        vol_data = orders_service.get_orders_volume_trend(db, start, end, period)
        return vol_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get order volume trend: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving order volume trend: {str(e)}"
        )


@router.get("/by-cashier", summary="Get orders breakdown by cashier")
def orders_by_cashier(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves order count and sales volume metrics per cashier."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Orders] User '{current_user.username}' requested orders by cashier")
        
        raw = orders_service.get_orders_by_cashier(db, start, end)
        return raw
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get orders by cashier: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving orders by cashier: {str(e)}"
        )


@router.get("/by-payment-method", summary="Get orders breakdown by payment method")
def orders_by_payment(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves order count and percentage distribution by payment method."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Orders] User '{current_user.username}' requested orders by payment method")
        
        raw = orders_service.get_orders_by_payment_method(db, start, end)
        return raw
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get orders by payment method: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving orders by payment method: {str(e)}"
        )


@router.get("/timeline", response_model=List[OrderTimelineItem], summary="Get recent orders timeline")
def order_timeline(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    cashier: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves recent orders timeline with pagination and cashier filtering."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "today")
        
        lim = limit if isinstance(limit, int) else 50
        off = offset if isinstance(offset, int) else 0

        logger.info(f"[Orders] User '{current_user.username}' requested order timeline (limit={lim}, offset={off})")
        csh = cashier if cashier and isinstance(cashier, str) else None
        raw = orders_service.get_recent_orders_timeline(db, start, end, lim + off, csh)
        
        paginated = raw[off : off + lim]

        items = []
        for o in paginated:
            d = dict(o)
            d_str = str(d.get("date") or d.get("created_at") or "")
            d["date"] = d_str
            d["created_at"] = d_str
            d["total"] = float(d.get("total", 0.0))
            items.append(OrderTimelineItem(**d))

        return items
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get order timeline: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving order timeline: {str(e)}"
        )


@router.get("/peak-patterns", response_model=PeakPatternResponse, summary="Get peak order hours and busiest weekdays")
def peak_patterns(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Calculates busiest hours of the day and peak weekdays for store staffing optimization."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "last30")
        logger.info(f"[Orders] User '{current_user.username}' requested peak patterns")
        
        raw = orders_service.get_peak_patterns(db, start, end)
        return PeakPatternResponse(**raw)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get peak patterns: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving peak patterns: {str(e)}"
        )


@router.get("/list", response_model=OrderListResponse, summary="Get paginated list of orders")
def list_orders(
    query: Optional[str] = Query(None, description="Search by invoice number or customer name"),
    cashier: Optional[str] = Query(None, description="Filter by cashier"),
    status_filter: Optional[str] = Query(None, description="Filter by order status"),
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns a paginated list of orders with optional search and filters."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        q = db.query(models.Sale).filter(
            func.date(models.Sale.created_at) >= start,
            func.date(models.Sale.created_at) <= end
        )

        if query and isinstance(query, str):
            q_str = f"%{query.strip()}%"
            q = q.filter(models.Sale.invoice_no.ilike(q_str))

        if cashier and isinstance(cashier, str):
            q = q.filter(models.Sale.cashier.ilike(f"%{cashier.strip()}%"))

        if status_filter and isinstance(status_filter, str):
            q = q.filter(models.Sale.status == status_filter.lower())

        lim = limit if isinstance(limit, int) else 50
        off = offset if isinstance(offset, int) else 0

        total_count = q.count()
        sales = q.order_by(models.Sale.created_at.desc()).offset(off).limit(lim).all()

        items = [
            OrderListItem(
                id=s.id,
                invoice_no=s.invoice_no,
                date=s.created_at.isoformat() if hasattr(s.created_at, 'isoformat') else str(s.created_at),
                customer_name=s.customer.name if s.customer else "Walk-in Customer",
                cashier=s.cashier or "Admin",
                payment_method=s.payment_method or "Cash",
                total=float(s.total or 0.0),
                status=s.status or "completed"
            )
            for s in sales
        ]

        return OrderListResponse(orders=items, count=total_count)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to list orders: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing orders: {str(e)}"
        )


@router.get("/details/{order_id}", response_model=OrderDetailsResponse, summary="Get details for a single order")
def get_order_details(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves deep-dive details and itemized list for an individual order."""
    try:
        s = db.query(models.Sale).filter(models.Sale.id == order_id).first()
        if not s:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order ID {order_id} not found")

        item_details = []
        if hasattr(s, 'items') and s.items:
            for item in s.items:
                p_name = item.product.name if hasattr(item, 'product') and item.product else "Item"
                subt = float(item.total if hasattr(item, 'total') else (item.qty * item.price))
                item_details.append(OrderItemDetail(
                    product_id=item.product_id,
                    product_name=p_name,
                    qty=float(item.qty or 0.0),
                    unit_price=float(item.price or 0.0),
                    subtotal=round(subt, 2),
                    discount=float(item.discount or 0.0) if hasattr(item, 'discount') else 0.0
                ))

        ts = s.created_at.isoformat() if hasattr(s.created_at, 'isoformat') else str(s.created_at)

        return OrderDetailsResponse(
            order_id=s.id,
            invoice_no=s.invoice_no,
            date=ts,
            customer_name=s.customer.name if s.customer else "Walk-in Customer",
            cashier_name=s.cashier or "Admin",
            payment_method=s.payment_method or "Cash",
            subtotal=float(s.subtotal or 0.0),
            discount=float(s.discount or 0.0),
            tax=float(s.tax or 0.0),
            total=float(s.total or 0.0),
            paid_amount=float(s.paid_amount or 0.0),
            change_due=float(s.change_due or 0.0),
            status=s.status or "completed",
            items=item_details
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get order details for ID {order_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching order details: {str(e)}"
        )


@router.get("/hourly-distribution", response_model=HourlyDistributionResponse, summary="Get hourly order distribution")
def hourly_distribution(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Aggregates order counts and sales by hour of the day (0 to 23)."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "last30")

        hourly_results = db.query(
            extract("hour", models.Sale.created_at).label("hr"),
            func.count(models.Sale.id).label("cnt"),
            func.sum(models.Sale.total).label("tot")
        ).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            func.date(models.Sale.created_at) >= start,
            func.date(models.Sale.created_at) <= end
        ).group_by("hr").all()

        h_map = {int(r.hr): (int(r.cnt or 0), float(r.tot or 0.0)) for r in hourly_results}
        hourly_returns = db.query(
            extract("hour", models.SaleReturn.posted_at).label("hr"),
            func.sum(models.SaleReturn.refund_amount).label("tot"),
        ).filter(
            func.date(models.SaleReturn.posted_at) >= start,
            func.date(models.SaleReturn.posted_at) <= end,
        ).group_by("hr").all()
        for row in hourly_returns:
            hour = int(row.hr)
            count, amount = h_map.get(hour, (0, 0.0))
            h_map[hour] = (count, amount - float(row.tot or 0.0))

        data_items = []
        peak_h = 12
        max_cnt = 0

        for h in range(24):
            cnt, tot = h_map.get(h, (0, 0.0))
            if cnt > max_cnt:
                max_cnt = cnt
                peak_h = h
            data_items.append(HourlyDistributionItem(
                hour=h,
                order_count=cnt,
                total_sales=round(tot, 2)
            ))

        return HourlyDistributionResponse(
            period_start=start,
            period_end=end,
            peak_hour=peak_h,
            hourly_data=data_items
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to get hourly distribution: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating hourly distribution: {str(e)}"
        )


@router.get("/average-order-value", response_model=AOVTrendResponse, summary="Get Average Order Value (AOV) trend")
def average_order_value(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates overall and daily Average Order Value (AOV) trends."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")

        daily_stats = db.query(
            func.date(models.Sale.created_at).label("d_label"),
            func.sum(models.Sale.total).label("tot"),
            func.count(models.Sale.id).label("cnt")
        ).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            func.date(models.Sale.created_at) >= start,
            func.date(models.Sale.created_at) <= end
        ).group_by("d_label").order_by("d_label").all()

        return_stats = db.query(
            func.date(models.SaleReturn.posted_at).label("d_label"),
            func.sum(models.SaleReturn.refund_amount).label("tot"),
        ).filter(
            func.date(models.SaleReturn.posted_at) >= start,
            func.date(models.SaleReturn.posted_at) <= end,
        ).group_by("d_label").all()
        sale_map = {
            str(row.d_label): {"revenue": float(row.tot or 0.0), "count": int(row.cnt or 0)}
            for row in daily_stats
        }
        return_map = {str(row.d_label): float(row.tot or 0.0) for row in return_stats}
        days = sorted(set(sale_map) | set(return_map))
        tot_rev = sum(row["revenue"] for row in sale_map.values()) - sum(return_map.values())
        tot_cnt = sum(int(r.cnt or 0) for r in daily_stats)
        overall_aov = tot_rev / tot_cnt if tot_cnt > 0 else 0.0

        trend_items = [
            AOVTrendItem(
                period_label=day,
                total_revenue=round(sale_map.get(day, {"revenue": 0.0})["revenue"] - return_map.get(day, 0.0), 2),
                total_orders=sale_map.get(day, {"count": 0})["count"],
                average_order_value=round(
                    (sale_map.get(day, {"revenue": 0.0})["revenue"] - return_map.get(day, 0.0))
                    / sale_map.get(day, {"count": 0})["count"],
                    2,
                ) if sale_map.get(day, {"count": 0})["count"] else 0.0,
            )
            for day in days
        ]

        return AOVTrendResponse(
            period_start=start,
            period_end=end,
            overall_aov=round(overall_aov, 2),
            trend=trend_items
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to calculate AOV trend: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating Average Order Value: {str(e)}"
        )


@router.get("/cancellation-rate", response_model=CancellationRateResponse, summary="Get order cancellation/void rate")
def cancellation_rate(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates order cancellation rate percentage."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")

        tot_orders = db.query(func.count(models.Sale.id)).filter(
            func.date(models.Sale.created_at) >= start,
            func.date(models.Sale.created_at) <= end
        ).scalar() or 0

        cancelled_orders = db.query(func.count(models.Sale.id)).filter(
            models.Sale.status == "cancelled",
            func.date(models.Sale.created_at) >= start,
            func.date(models.Sale.created_at) <= end
        ).scalar() or 0

        rate = round((cancelled_orders / tot_orders * 100.0), 1) if tot_orders > 0 else 0.0

        return CancellationRateResponse(
            period_start=start,
            period_end=end,
            total_orders=tot_orders,
            cancelled_orders=cancelled_orders,
            cancellation_rate_pct=rate
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to calculate cancellation rate: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating cancellation rate: {str(e)}"
        )


@router.get("/export", summary="Export orders analytics report as CSV")
def export_orders_report(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Generates and downloads a CSV export of order sales and statuses."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        lim = limit if isinstance(limit, int) else 200
        raw = orders_service.get_recent_orders_timeline(db, start, end, lim)

        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV Header
        writer.writerow(["Order ID", "Invoice No", "Date", "Customer Name", "Cashier", "Payment Method", "Total Amount (PKR)", "Status"])
        
        for r in raw:
            writer.writerow([
                r.get("id", ""),
                r.get("invoice_no", ""),
                r.get("date", ""),
                r.get("customer_name", "Walk-in Customer"),
                r.get("cashier", "Admin"),
                r.get("payment_method", "Cash"),
                f"{float(r.get('total', 0.0)):.2f}",
                r.get("status", "completed")
            ])

        output.seek(0)
        filename = f"orders_analytics_{start}_to_{end}.csv"
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Orders Error] Failed to export CSV: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error exporting orders CSV report: {str(e)}"
        )
