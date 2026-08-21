"""
Executive Overview route handler — single comprehensive dashboard summary.
Provides executive KPIs, payment splits, top products, stock snapshots, period comparisons, forecasts, and CSV exports.
"""

import logging
import csv
import io
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from auth import get_current_user, require_admin
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import overview_service
from dashboard.utils.aggregation_helpers import top_n_products, category_revenue_breakdown
import models

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.overview")
logger.setLevel(logging.INFO)

router = APIRouter()

ALLOWED_PRESETS = {"today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last7", "last30", "last90", "this_year", "custom"}


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class KPIData(BaseModel):
    gross_sales: float = 0.0
    net_sales: float = 0.0
    total_orders: int = 0
    avg_order_value: float = 0.0
    total_discount: float = 0.0
    total_expenses: float = 0.0
    gross_profit: float = 0.0
    net_profit: float = 0.0
    tax_collected: float = 0.0
    refund_count: int = 0
    refund_amount: float = 0.0


class PaymentSplitData(BaseModel):
    cash: float = 0.0
    card: float = 0.0
    credit: float = 0.0
    other: float = 0.0


class ShiftSummary(BaseModel):
    open_shifts_count: int = 0
    closed_shifts_count: int = 0
    total_cash_drawer: float = 0.0


class StockSnapshot(BaseModel):
    total_skus: int = 0
    healthy_stock_count: int = 0
    low_stock_count: int = 0
    out_of_stock_count: int = 0
    total_stock_value: float = 0.0


class ChartData(BaseModel):
    dates: List[str] = Field(default_factory=list)
    sales: List[float] = Field(default_factory=list)
    expenses: List[float] = Field(default_factory=list)


class ReceivablesPayables(BaseModel):
    total_receivable: float = 0.0
    total_payable: float = 0.0


class TopProductItem(BaseModel):
    product_id: int
    product_name: str
    qty_sold: float = 0.0
    total_revenue: float = 0.0


class TopCategoryItem(BaseModel):
    category_id: Optional[int] = None
    category_name: str
    total_revenue: float = 0.0


class ExecutiveOverviewResponse(BaseModel):
    gross_sales_period: float = 0.0
    net_sales_period: float = 0.0
    total_orders_period: int = 0
    gross_profit_period: float = 0.0
    net_profit_period: float = 0.0
    avg_order_value_period: float = 0.0
    total_expenses_period: float = 0.0

    gross_sales_today: float = 0.0
    net_sales_today: float = 0.0
    gross_sales_month: float = 0.0
    net_sales_month: float = 0.0
    total_orders_today: int = 0
    total_orders_month: int = 0
    avg_order_value_today: float = 0.0
    avg_order_value_month: float = 0.0
    total_expenses_today: float = 0.0
    total_expenses_month: float = 0.0
    gross_profit_today: float = 0.0
    gross_profit_month: float = 0.0
    net_profit_today: float = 0.0
    net_profit_month: float = 0.0
    total_discount_today: float = 0.0
    total_discount_month: float = 0.0
    tax_collected_today: float = 0.0
    tax_collected_month: float = 0.0
    refund_count_month: int = 0
    refund_amount_month: float = 0.0
    return_count_month: int = 0
    total_receivable: float = 0.0
    total_payable: float = 0.0

    payment_split_today: Any = None
    top_products_today: Any = None
    top_categories_month: Any = None
    shift_summary: Any = None
    stock_snapshot: Any = None
    chart_7day: Any = None
    month_vs_prev_month: Optional[float] = None


class PeriodComparisonResponse(BaseModel):
    current_period: str
    previous_period: str
    current_gross_sales: float
    previous_gross_sales: float
    sales_growth_pct: float
    current_total_orders: int
    previous_total_orders: int
    orders_growth_pct: float


class ExecutiveForecastResponse(BaseModel):
    forecast_days: int
    historical_avg_daily_sales: float
    projected_sales_next_period: float
    trend_direction: str = Field(..., description="upward, downward, stable")


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

@router.get("/health", response_model=HealthCheckResponse, summary="Overview module health check")
def overview_health():
    """Health check endpoint for executive overview module."""
    return HealthCheckResponse(
        status="ok",
        module="overview",
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@router.get("/", response_model=ExecutiveOverviewResponse, summary="Get comprehensive executive overview payload")
def get_executive_overview(
    preset: Optional[str] = Query(None, description="Date preset filter: today, this_month, etc."),
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    top_n: int = Query(5, ge=1, le=20, description="Top N products/categories count"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Comprehensive executive dashboard summary payload.
    Provides 20+ KPIs, payment method split, top products, top categories, stock snapshot, and monthly comparison growth %.
    """
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "today")

        logger.info(f"[Overview] User '{current_user.username}' requested executive overview (preset={preset or 'today'})")

        top_limit = top_n if isinstance(top_n, int) else 5

        # KPIs calculations
        period_kpis = overview_service.get_kpis_for_range(db, start, end)
        today_kpis = overview_service.get_today_kpis(db)
        month_kpis = overview_service.get_month_kpis(db)
        
        # Calculate month vs previous month growth %
        prev_month_start, prev_month_end = resolve_date_range(None, None, "last_month")
        prev_month_kpis = overview_service.get_kpis_for_range(db, prev_month_start, prev_month_end)
        
        month_sales = month_kpis.get("gross_sales", 0.0)
        prev_sales = prev_month_kpis.get("gross_sales", 0.0)
        
        if prev_sales > 0:
            month_vs_prev_growth = round(((month_sales - prev_sales) / prev_sales * 100.0), 1)
        else:
            month_vs_prev_growth = 100.0 if month_sales > 0 else 0.0

        payment_split = overview_service.get_payment_split(db, start, end)
        shift = overview_service.get_shift_summary(db, start)
        stock = overview_service.get_stock_snapshot(db)
        chart = overview_service.get_7day_chart(db)
        bal = overview_service.get_receivables_payables(db)
        
        top_prods = top_n_products(db, start, end, top_limit)
        top_cats = category_revenue_breakdown(db, start, end)[:top_limit]

        return ExecutiveOverviewResponse(
            gross_sales_period=float(period_kpis.get("gross_sales", 0.0)),
            net_sales_period=float(period_kpis.get("net_sales", 0.0)),
            total_orders_period=int(period_kpis.get("total_orders", 0)),
            gross_profit_period=float(period_kpis.get("gross_profit", 0.0)),
            net_profit_period=float(period_kpis.get("net_profit", 0.0)),
            avg_order_value_period=float(period_kpis.get("avg_order_value", 0.0)),
            total_expenses_period=float(period_kpis.get("total_expenses", 0.0)),

            gross_sales_today=float(today_kpis.get("gross_sales", 0.0)),
            net_sales_today=float(today_kpis.get("net_sales", 0.0)),
            gross_sales_month=float(month_kpis.get("gross_sales", 0.0)),
            net_sales_month=float(month_kpis.get("net_sales", 0.0)),
            total_orders_today=int(today_kpis.get("total_orders", 0)),
            total_orders_month=int(month_kpis.get("total_orders", 0)),
            avg_order_value_today=float(today_kpis.get("avg_order_value", 0.0)),
            avg_order_value_month=float(month_kpis.get("avg_order_value", 0.0)),
            total_expenses_today=float(today_kpis.get("total_expenses", 0.0)),
            total_expenses_month=float(month_kpis.get("total_expenses", 0.0)),
            gross_profit_today=float(today_kpis.get("gross_profit", 0.0)),
            gross_profit_month=float(month_kpis.get("gross_profit", 0.0)),
            net_profit_today=float(today_kpis.get("net_profit", 0.0)),
            net_profit_month=float(month_kpis.get("net_profit", 0.0)),
            total_discount_today=float(today_kpis.get("total_discount", 0.0)),
            total_discount_month=float(month_kpis.get("total_discount", 0.0)),
            tax_collected_today=float(today_kpis.get("tax_collected", 0.0)),
            tax_collected_month=float(today_kpis.get("tax_collected", 0.0)),
            refund_count_month=int(month_kpis.get("refund_count", 0)),
            refund_amount_month=float(month_kpis.get("refund_amount", 0.0)),
            return_count_month=int(month_kpis.get("refund_count", 0)),
            total_receivable=float(bal.get("total_receivable", 0.0)),
            total_payable=float(bal.get("total_payable", 0.0)),

            payment_split_today=payment_split,
            top_products_today=top_prods,
            top_categories_month=top_cats,
            shift_summary=shift,
            stock_snapshot=stock,
            chart_7day=chart,
            month_vs_prev_month=month_vs_prev_growth
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Overview Error] Failed to generate executive overview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving executive overview: {str(e)}"
        )


@router.get("/compare", response_model=PeriodComparisonResponse, summary="Compare sales performance against previous period")
def compare_periods(
    preset: Optional[str] = Query("this_month", description="Date preset filter: this_month, last30, etc."),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Compares sales and order volume performance of the current period against the preceding period."""
    try:
        cur_start, cur_end = resolve_date_range(None, None, preset or "this_month")
        
        # Resolve previous period equivalent
        prev_preset = "last_month" if preset == "this_month" else "last_week" if preset == "this_week" else "yesterday"
        prev_start, prev_end = resolve_date_range(None, None, prev_preset)

        cur_kpis = overview_service.get_kpis_for_range(db, cur_start, cur_end)
        prev_kpis = overview_service.get_kpis_for_range(db, prev_start, prev_end)

        c_sales = float(cur_kpis.get("gross_sales", 0.0))
        p_sales = float(prev_kpis.get("gross_sales", 0.0))

        c_orders = int(cur_kpis.get("total_orders", 0))
        p_orders = int(prev_kpis.get("total_orders", 0))

        s_growth = round(((c_sales - p_sales) / p_sales * 100.0), 1) if p_sales > 0 else 0.0
        o_growth = round(((c_orders - p_orders) / p_orders * 100.0), 1) if p_orders > 0 else 0.0

        return PeriodComparisonResponse(
            current_period=f"{cur_start} to {cur_end}",
            previous_period=f"{prev_start} to {prev_end}",
            current_gross_sales=c_sales,
            previous_gross_sales=p_sales,
            sales_growth_pct=s_growth,
            current_total_orders=c_orders,
            previous_total_orders=p_orders,
            orders_growth_pct=o_growth
        )
    except Exception as e:
        logger.error(f"[Overview Error] Failed to compare periods: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating period comparison: {str(e)}"
        )


@router.get("/forecast", response_model=ExecutiveForecastResponse, summary="Get sales trend forecast")
def executive_forecast(
    forecast_days: int = Query(7, ge=1, le=30, description="Number of days to forecast"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates basic sales forecast based on recent 30-day daily averages."""
    try:
        days_limit = forecast_days if isinstance(forecast_days, int) else 7
        start, end = resolve_date_range(None, None, "last30")

        kpis_30d = overview_service.get_kpis_for_range(db, start, end)
        tot_sales = float(kpis_30d.get("gross_sales", 0.0))
        daily_avg = tot_sales / 30.0

        projected = daily_avg * days_limit

        # Compare 7-day trend to previous 7 days
        start_7, end_7 = resolve_date_range(None, None, "last7")
        kpis_7d = overview_service.get_kpis_for_range(db, start_7, end_7)
        sales_7d = float(kpis_7d.get("gross_sales", 0.0))
        avg_7d = sales_7d / 7.0

        direction = "upward" if avg_7d > (daily_avg * 1.05) else "downward" if avg_7d < (daily_avg * 0.95) else "stable"

        return ExecutiveForecastResponse(
            forecast_days=days_limit,
            historical_avg_daily_sales=round(daily_avg, 2),
            projected_sales_next_period=round(projected, 2),
            trend_direction=direction
        )
    except Exception as e:
        logger.error(f"[Overview Error] Failed to calculate forecast: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating executive forecast: {str(e)}"
        )


@router.get("/export", summary="Export executive summary as CSV")
def export_executive_summary(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Generates and downloads a CSV export of high-level executive KPIs."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        kpis = overview_service.get_kpis_for_range(db, start, end)

        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV Header
        writer.writerow(["Metric Name", "Value (PKR / Count)"])
        writer.writerow(["Period Start", start])
        writer.writerow(["Period End", end])
        writer.writerow(["Gross Sales", f"{float(kpis.get('gross_sales', 0.0)):.2f}"])
        writer.writerow(["Net Sales", f"{float(kpis.get('net_sales', 0.0)):.2f}"])
        writer.writerow(["Total Orders", kpis.get("total_orders", 0)])
        writer.writerow(["Average Order Value", f"{float(kpis.get('avg_order_value', 0.0)):.2f}"])
        writer.writerow(["Total Discounts Given", f"{float(kpis.get('total_discount', 0.0)):.2f}"])
        writer.writerow(["Total Operating Expenses", f"{float(kpis.get('total_expenses', 0.0)):.2f}"])
        writer.writerow(["Gross Profit", f"{float(kpis.get('gross_profit', 0.0)):.2f}"])
        writer.writerow(["Net Profit", f"{float(kpis.get('net_profit', 0.0)):.2f}"])

        output.seek(0)
        filename = f"executive_summary_{start}_to_{end}.csv"
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Overview Error] Failed to export CSV: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error exporting executive summary CSV: {str(e)}"
        )
