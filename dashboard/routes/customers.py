"""
Customer analytics route handlers.
Provides segment summaries, customer growth trends, top customer metrics, purchase frequency, CLV, and CSV exports.
"""

import logging
import csv
import io
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from database import get_db
from auth import get_current_user
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import customer_service
from services.sale_return_service import POSTED_SALE_STATUSES, paid_refund_amount
import models

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.customers")
logger.setLevel(logging.INFO)

router = APIRouter()

ALLOWED_PRESETS = {"today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last7", "last30", "this_year", "custom"}


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class CustomerSummaryResponse(BaseModel):
    total_customers: int = Field(0, description="Total count of active registered customers")
    new_customers: int = Field(0, description="New customers registered in period")
    new_this_month: int = Field(0, description="New customers registered this month")
    returning_customers: int = Field(0, description="Repeat customers who bought in period")
    repeat_customers: int = Field(0, description="Repeat customers count")
    avg_spend_per_customer: float = Field(0.0, description="Average spend per active customer")
    avg_lifetime_value: float = Field(0.0, description="Average lifetime value")
    total_receivables: float = Field(0.0, description="Total receivables balance")
    repeat_rate_pct: float = Field(0.0, description="Repeat customer percentage")


class CustomerGrowthItem(BaseModel):
    date: Optional[str] = Field(None, description="Date string YYYY-MM-DD")
    label: Optional[str] = Field(None, description="Date or Month label")
    new_customers: int = Field(0, description="New customer registrations on date")
    cumulative: int = Field(0, description="Cumulative customer base up to date")


class TopCustomerItem(BaseModel):
    customer_id: int = Field(..., description="Customer ID")
    name: str = Field(..., description="Customer full name")
    phone: Optional[str] = Field(None, description="Customer phone number")
    total_spent: float = Field(0.0, description="Total spend amount")
    total_spend: float = Field(0.0, description="Total spend amount (alias)")
    order_count: int = Field(0, description="Total completed order count")
    total_orders: int = Field(0, description="Total order count (alias)")
    last_order_date: Optional[str] = Field(None, description="Last purchase date")
    last_purchase_date: Optional[str] = Field(None, description="Last purchase date (alias)")


class FrequencyDistributionItem(BaseModel):
    frequency_bracket: Optional[str] = Field(None, description="Bracket name")
    bucket: Optional[str] = Field(None, description="Bucket name")
    customer_count: int = Field(0, description="Number of customers in bracket")
    count: int = Field(0, description="Count alias")
    percentage: float = Field(0.0, description="Percentage of total customer base")
    pct: float = Field(0.0, description="Percentage alias")


class CustomerListItem(BaseModel):
    id: int
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    due_balance: float = 0.0
    created_at: Optional[str] = None


class CustomerListResponse(BaseModel):
    customers: List[CustomerListItem]
    count: int


class CustomerPurchaseHistoryItem(BaseModel):
    sale_id: int
    invoice_no: str
    date: str
    total: float
    paid_amount: float
    items_count: int


class CustomerDetailsResponse(BaseModel):
    customer_id: int
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    due_balance: float = 0.0
    total_orders: int = 0
    total_spent: float = 0.0
    first_purchase_date: Optional[str] = None
    last_purchase_date: Optional[str] = None
    recent_purchases: List[CustomerPurchaseHistoryItem] = Field(default_factory=list)


class CustomerSegmentResponse(BaseModel):
    segment: str = Field(..., description="Segment name: loyal, returning, new, at_risk, inactive")
    count: int = Field(0, description="Customer count in segment")
    percentage: float = Field(0.0, description="Segment percentage")


class CustomerRetentionResponse(BaseModel):
    period_start: str
    period_end: str
    total_active_customers: int
    repeat_purchase_rate_pct: float
    churn_rate_pct: float


class CohortItem(BaseModel):
    cohort_month: str
    initial_customers: int
    retained_month_1: int
    retained_month_2: int
    retention_rate_pct: float


class CohortAnalysisResponse(BaseModel):
    cohorts: List[CohortItem]


class CustomerLifetimeValueResponse(BaseModel):
    avg_customer_lifespan_days: float
    avg_order_value: float
    avg_purchase_frequency: float
    estimated_clv: float


class ChurnRiskCustomer(BaseModel):
    customer_id: int
    name: str
    phone: Optional[str] = None
    days_since_last_order: int
    churn_risk_score: float = Field(..., description="0.0 to 1.0 risk score")
    risk_level: str = Field(..., description="low, medium, high, critical")


class ChurnPredictionResponse(BaseModel):
    at_risk_customers_count: int
    high_risk_customers: List[ChurnRiskCustomer]


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

@router.get("/health", response_model=HealthCheckResponse, summary="Customers module health check")
def customers_health():
    """Health check endpoint for the customers module."""
    return HealthCheckResponse(
        status="ok",
        module="customers",
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@router.get("/summary", response_model=CustomerSummaryResponse, summary="Get customer summary metrics")
def customer_summary(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves high-level customer metrics (total, new, returning, avg spend)."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Customers] User '{current_user.username}' requested summary for period {start} to {end}")
        
        raw = customer_service.get_customer_segment_summary(db, start, end)
        
        # Populate unified fields for 100% compatibility
        tot = raw.get("total_customers", 0)
        new_c = raw.get("new_customers", raw.get("new_this_month", 0))
        ret_c = raw.get("returning_customers", raw.get("repeat_customers", 0))
        avg_s = float(raw.get("avg_spend_per_customer", raw.get("avg_lifetime_value", 0.0)))
        
        data = {
            "total_customers": tot,
            "new_customers": new_c,
            "new_this_month": new_c,
            "returning_customers": ret_c,
            "repeat_customers": ret_c,
            "avg_spend_per_customer": avg_s,
            "avg_lifetime_value": avg_s,
            "total_receivables": float(raw.get("total_receivables", 0.0)),
            "repeat_rate_pct": float(raw.get("repeat_rate_pct", 0.0)),
        }
        return CustomerSummaryResponse(**data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Customers Error] Failed to get customer summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving customer summary: {str(e)}"
        )


@router.get("/growth", response_model=List[CustomerGrowthItem], summary="Get customer registration growth trend")
def customer_growth(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves daily/monthly registration growth trend and cumulative customer base."""
    try:
        logger.info(f"[Customers] User '{current_user.username}' requested customer growth trend")
        raw = customer_service.get_customer_growth_trend(db)
        items = []
        for item in raw:
            d = dict(item)
            d_str = d.get("date") or d.get("label") or ""
            d["date"] = d_str
            d["label"] = d_str
            items.append(CustomerGrowthItem(**d))
        return items
    except Exception as e:
        logger.error(f"[Customers Error] Failed to get customer growth: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving customer growth: {str(e)}"
        )


@router.get("/top", response_model=List[TopCustomerItem], summary="Get top spending customers")
def top_customers(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves top customers by total spend amount for the selected period."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Customers] User '{current_user.username}' requested top customers (limit={limit})")
        
        raw = customer_service.get_top_customers(db, start, end, limit)
        items = []
        for item in raw:
            d = dict(item)
            spend = float(d.get("total_spent", d.get("total_spend", 0.0)))
            orders = int(d.get("order_count", d.get("total_orders", 0)))
            l_date = d.get("last_order_date") or d.get("last_purchase_date")
            d["total_spent"] = spend
            d["total_spend"] = spend
            d["order_count"] = orders
            d["total_orders"] = orders
            d["last_order_date"] = l_date
            d["last_purchase_date"] = l_date
            items.append(TopCustomerItem(**d))
        return items
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Customers Error] Failed to get top customers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving top customers: {str(e)}"
        )


@router.get("/frequency", response_model=List[FrequencyDistributionItem], summary="Get customer purchase frequency distribution")
def customer_frequency(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves frequency distribution of orders per customer (1 order, 2-5 orders, etc.)."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        logger.info(f"[Customers] User '{current_user.username}' requested customer frequency distribution")
        
        raw = customer_service.get_customer_frequency_distribution(db, start, end)
        items = []
        for item in raw:
            d = dict(item)
            brk = d.get("frequency_bracket") or d.get("bucket") or ""
            cnt = int(d.get("customer_count", d.get("count", 0)))
            pct = float(d.get("percentage", d.get("pct", 0.0)))
            d["frequency_bracket"] = brk
            d["bucket"] = brk
            d["customer_count"] = cnt
            d["count"] = cnt
            d["percentage"] = pct
            d["pct"] = pct
            items.append(FrequencyDistributionItem(**d))
        return items
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Customers Error] Failed to get frequency distribution: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving frequency distribution: {str(e)}"
        )


@router.get("/list", response_model=CustomerListResponse, summary="Get paginated list of customers")
def list_customers(
    query: Optional[str] = Query(None, description="Search customer by name, phone, or email"),
    limit: int = Query(50, ge=1, le=1000, description="Pagination limit"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Returns a paginated list of customers with optional search filtering."""
    try:
        logger.info(f"[Customers] User '{current_user.username}' requested customer list")
        q = db.query(models.Customer)
        
        if query and isinstance(query, str):
            q_str = f"%{query.strip()}%"
            q = q.filter(
                (models.Customer.name.ilike(q_str)) |
                (models.Customer.phone.ilike(q_str))
            )

        lim = limit if isinstance(limit, int) else 50
        off = offset if isinstance(offset, int) else 0

        total_count = q.count()
        customers = q.order_by(models.Customer.name.asc()).offset(off).limit(lim).all()

        items = [
            CustomerListItem(
                id=c.id,
                name=c.name,
                phone=c.phone,
                email=getattr(c, 'email', None),
                due_balance=float(getattr(c, 'due_balance', getattr(c, 'credit_balance', 0.0)) or 0.0),
                created_at=c.created_at.isoformat() if hasattr(c, 'created_at') and c.created_at else None
            )
            for c in customers
        ]

        return CustomerListResponse(customers=items, count=total_count)
    except Exception as e:
        logger.error(f"[Customers Error] Failed to list customers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving customer list: {str(e)}"
        )


@router.get("/details/{customer_id}", response_model=CustomerDetailsResponse, summary="Get details and purchase history for a single customer")
def get_customer_details(
    customer_id: int,
    limit_history: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Retrieves deep-dive details and recent purchase history for an individual customer."""
    try:
        c = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
        if not c:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer ID {customer_id} not found")

        sales = db.query(models.Sale).options(
            joinedload(models.Sale.items),
            joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction),
        ).filter(
            models.Sale.customer_id == customer_id,
            models.Sale.status.in_(POSTED_SALE_STATUSES)
        ).order_by(models.Sale.created_at.desc()).all()

        total_orders = len(sales)
        def net_total(sale):
            return max(0.0, float(sale.total or 0.0) - sum(float(r.refund_amount or 0) for r in sale.returns))

        def net_paid(sale):
            return max(0.0, float(sale.paid_amount or 0.0) - sum(float(paid_refund_amount(r)) for r in sale.returns))

        total_spent = sum(net_total(s) for s in sales)
        first_date = sales[-1].created_at.isoformat() if sales else None
        last_date = sales[0].created_at.isoformat() if sales else None

        recent_items = [
            CustomerPurchaseHistoryItem(
                sale_id=s.id,
                invoice_no=s.invoice_no,
                date=s.created_at.isoformat() if hasattr(s.created_at, 'isoformat') else str(s.created_at),
                total=net_total(s),
                paid_amount=net_paid(s),
                items_count=len(s.items) if hasattr(s, 'items') and s.items else 0
            )
            for s in sales[:limit_history]
        ]

        return CustomerDetailsResponse(
            customer_id=c.id,
            name=c.name,
            phone=c.phone,
            email=getattr(c, 'email', None),
            address=getattr(c, 'address', None),
            due_balance=float(getattr(c, 'due_balance', getattr(c, 'credit_balance', 0.0)) or 0.0),
            total_orders=total_orders,
            total_spent=round(total_spent, 2),
            first_purchase_date=first_date,
            last_purchase_date=last_date,
            recent_purchases=recent_items
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Customers Error] Failed to get customer details for ID {customer_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching customer details: {str(e)}"
        )


@router.get("/segments", response_model=List[CustomerSegmentResponse], summary="Get customer segmentation breakdown")
def get_customer_segments(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Categorizes customer base into segments (Loyal, Returning, New, At-Risk, Inactive)."""
    try:
        total_c = db.query(models.Customer).count()
        if total_c == 0:
            return []

        now = datetime.now()
        thirty_days_ago = now - timedelta(days=30)
        ninety_days_ago = now - timedelta(days=90)

        # Query sales stats per customer
        stats = db.query(
            models.Sale.customer_id,
            func.count(models.Sale.id).label("order_count"),
            func.max(models.Sale.created_at).label("last_order")
        ).filter(
            models.Sale.customer_id.isnot(None),
            models.Sale.status.in_(POSTED_SALE_STATUSES)
        ).group_by(models.Sale.customer_id).all()

        loyal_c = sum(1 for s in stats if s.order_count >= 5)
        returning_c = sum(1 for s in stats if 2 <= s.order_count < 5)
        new_c = sum(1 for s in stats if s.order_count == 1)
        at_risk_c = sum(1 for s in stats if s.last_order and s.last_order < thirty_days_ago)
        inactive_c = total_c - len(stats)

        segments = [
            ("Loyal Customers (5+ orders)", loyal_c),
            ("Returning Customers (2-4 orders)", returning_c),
            ("New Customers (1 order)", new_c),
            ("At-Risk (No order in 30 days)", at_risk_c),
            ("Inactive / Walk-in", max(0, inactive_c))
        ]

        return [
            CustomerSegmentResponse(
                segment=name,
                count=count,
                percentage=round((count / total_c * 100.0), 1)
            )
            for name, count in segments
        ]
    except Exception as e:
        logger.error(f"[Customers Error] Failed to generate customer segments: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating customer segments: {str(e)}"
        )


@router.get("/retention", response_model=CustomerRetentionResponse, summary="Get customer retention & churn metrics")
def customer_retention(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates customer repeat purchase rate and churn metrics for period."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")

        active_custs = db.query(
            models.Sale.customer_id,
            func.count(models.Sale.id).label("cnt")
        ).filter(
            models.Sale.customer_id.isnot(None),
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            func.date(models.Sale.created_at) >= start,
            func.date(models.Sale.created_at) <= end
        ).group_by(models.Sale.customer_id).all()

        tot_active = len(active_custs)
        repeats = sum(1 for c in active_custs if c.cnt > 1)

        repeat_rate = round((repeats / tot_active * 100.0), 1) if tot_active > 0 else 0.0
        churn_rate = round(100.0 - repeat_rate, 1) if tot_active > 0 else 0.0

        return CustomerRetentionResponse(
            period_start=start,
            period_end=end,
            total_active_customers=tot_active,
            repeat_purchase_rate_pct=repeat_rate,
            churn_rate_pct=churn_rate
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Customers Error] Failed to generate retention metrics: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating customer retention: {str(e)}"
        )


@router.get("/lifetime-value", response_model=CustomerLifetimeValueResponse, summary="Get Customer Lifetime Value (CLV) metrics")
def customer_lifetime_value(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates overall estimated Customer Lifetime Value (CLV)."""
    try:
        sales = db.query(models.Sale).options(joinedload(models.Sale.returns)).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES)
        ).all()
        if not sales:
            return CustomerLifetimeValueResponse(
                avg_customer_lifespan_days=0.0,
                avg_order_value=0.0,
                avg_purchase_frequency=0.0,
                estimated_clv=0.0
            )

        tot_revenue = sum(
            max(0.0, float(s.total or 0.0) - sum(float(r.refund_amount or 0) for r in s.returns))
            for s in sales
        )
        tot_orders = len(sales)
        avg_aov = tot_revenue / tot_orders if tot_orders > 0 else 0.0

        unique_custs = len(set(s.customer_id for s in sales if s.customer_id))
        avg_freq = tot_orders / max(1, unique_custs)

        # Estimated CLV = Average Order Value * Average Purchase Frequency
        clv = avg_aov * avg_freq

        return CustomerLifetimeValueResponse(
            avg_customer_lifespan_days=180.0,
            avg_order_value=round(avg_aov, 2),
            avg_purchase_frequency=round(avg_freq, 2),
            estimated_clv=round(clv, 2)
        )
    except Exception as e:
        logger.error(f"[Customers Error] Failed to calculate CLV: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating Customer Lifetime Value: {str(e)}"
        )


@router.get("/churn-prediction", response_model=ChurnPredictionResponse, summary="Get churn risk prediction scoring")
def churn_prediction(
    inactivity_days: int = Query(30, ge=7, le=365, description="Days of inactivity threshold"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Identifies customers at risk of churning based on inactivity window."""
    try:
        cutoff = datetime.now() - timedelta(days=inactivity_days)

        stats = db.query(
            models.Customer.id,
            models.Customer.name,
            models.Customer.phone,
            func.max(models.Sale.created_at).label("last_sale")
        ).join(models.Sale, models.Sale.customer_id == models.Customer.id).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES)
        ).group_by(models.Customer.id).all()

        at_risk = []
        now = datetime.now()

        for s in stats:
            if s.last_sale:
                days_absent = (now - s.last_sale).days
                if days_absent >= inactivity_days:
                    score = min(1.0, round(days_absent / 180.0, 2))
                    risk = "critical" if score >= 0.8 else "high" if score >= 0.5 else "medium"
                    at_risk.append(ChurnRiskCustomer(
                        customer_id=s.id,
                        name=s.name,
                        phone=s.phone,
                        days_since_last_order=days_absent,
                        churn_risk_score=score,
                        risk_level=risk
                    ))

        return ChurnPredictionResponse(
            at_risk_customers_count=len(at_risk),
            high_risk_customers=sorted(at_risk, key=lambda x: x.days_since_last_order, reverse=True)[:50]
        )
    except Exception as e:
        logger.error(f"[Customers Error] Failed to generate churn predictions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating churn risk prediction: {str(e)}"
        )


@router.get("/export", summary="Export customer analytics report as CSV")
def export_customer_report(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Generates and downloads a CSV export of top customer analytics."""
    try:
        _validate_date_inputs(preset, start_date, end_date)
        start, end = resolve_date_range(start_date, end_date, preset or "this_month")
        
        raw_top = customer_service.get_top_customers(db, start, end, limit)

        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV Header
        writer.writerow(["Customer ID", "Customer Name", "Phone", "Total Spent (PKR)", "Order Count", "Last Order Date"])
        
        for c in raw_top:
            writer.writerow([
                c.get("customer_id", ""),
                c.get("name", ""),
                c.get("phone", ""),
                f"{c.get('total_spent', c.get('total_spend', 0.0)):.2f}",
                c.get("order_count", c.get("total_orders", 0)),
                c.get("last_order_date", c.get("last_purchase_date", ""))
            ])

        output.seek(0)
        filename = f"customer_analytics_{start}_to_{end}.csv"
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Customers Error] Failed to export CSV: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error exporting customer report CSV: {str(e)}"
        )
