"""
Reports Router - Handles all business reports:
Dashboard Summary, Sales, Purchases, Profit & Loss, Stock, Product Sales, Product Ledger, Daily Performance, Audit Details, Executive Summary, Export & Health Check.
"""

import logging
import io
import csv
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case, or_
from sqlalchemy.sql import text
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user, require_admin
import models
from services.sale_return_service import (
    POSTED_SALE_STATUSES,
    allocate_sale_item_amounts,
    paid_refund_amount,
)
from dashboard.services.accounting_helpers import salary_reversal_total

# Safe import for rate limiting (slowapi)
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address)
except ImportError:
    class DummyLimiter:
        def limit(self, limit_value: str):
            def decorator(func):
                return func
            return decorator
    limiter = DummyLimiter()

logger = logging.getLogger("smartvyapar.reports")
logger.setLevel(logging.INFO)

router = APIRouter()

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_PERIOD = "90days"
SALE_STATUS_ACTIVE = list(POSTED_SALE_STATUSES)
FF_STATUS_ACTIVE = ["pending", "ready", "completed"]
CASH_OUT_EXPENSE_TYPES = ["expense", "salary"]


# ── Schemas ──────────────────────────────────────────────────────────────

class DashboardChartPoint(BaseModel):
    date: str
    total: float


class DashboardSummaryResponse(BaseModel):
    period_sales: float
    period_orders: int
    today_sales: float
    month_sales: float
    month_profit: float
    total_receivable: float
    total_payable: float
    low_stock_count: int
    chart_data: List[DashboardChartPoint]


class SalesReportItem(BaseModel):
    invoice_no: str
    customer: str
    total: float
    paid: float
    method: str
    date: datetime
    cost: float
    profit: float


class SalesReportResponse(BaseModel):
    count: int
    total: float
    total_cost: float
    total_profit: float
    paid: float
    due: float
    items: List[SalesReportItem]


class PurchaseReportItem(BaseModel):
    bill_no: str
    supplier: str
    supplier_balance: float
    total: float
    paid: float
    date: datetime


class PurchasesReportResponse(BaseModel):
    count: int
    total: float
    items: List[PurchaseReportItem]


class ProfitLossResponse(BaseModel):
    period: str
    total_sales: float
    total_purchase_cost: float
    gross_profit: float
    total_expenses: float
    net_profit: float


class StockReportItem(BaseModel):
    id: int
    name: str
    category: str
    barcode: Optional[str] = None
    code: Optional[str] = None
    stock: float
    unit: str
    buy_price: float
    sell_price: float
    stock_value: float
    sale_value: float
    low: bool


class StockReportResponse(BaseModel):
    count: int
    total_products: int
    total_stock_value: float
    total_sale_value: float
    categories: List[str]
    items: List[StockReportItem]


class ProductSalesItem(BaseModel):
    product_id: Optional[int] = None
    product_name: str
    total_qty: float
    total_amount: float
    total_cost: float
    total_profit: float


class ProductLedgerResponse(BaseModel):
    product_name: str
    product_code: Optional[str] = None
    current_stock: float
    total_purchased: float
    total_sold: float
    count: int
    items: List[Dict[str, Any]]


class DailyPerformanceItem(BaseModel):
    date: str
    gross_sales: float
    discount: float
    sales: float
    cash_collected: float
    purchases: float
    expenses: float
    cogs: float
    received: float
    paid: float
    net_cash: float
    profit: float


class DailyPerformanceResponse(BaseModel):
    count: int
    items: List[DailyPerformanceItem]
    totals: Dict[str, float]


class AuditSummary(BaseModel):
    gross_sales: float
    total_expenses: float
    net_profit: float
    sales_cash_collected: float
    customer_payments_total: float
    supplier_payments_total: float
    expected_cash: float
    period: str


class AuditDetailsResponse(BaseModel):
    summary: AuditSummary
    customer_payments: List[Dict[str, Any]]
    expenses: List[Dict[str, Any]]
    supplier_payments: List[Dict[str, Any]]
    credit_sales: List[Dict[str, Any]]


class ExecutiveSummaryResponse(BaseModel):
    period: str
    sales: float
    cogs: float
    gross_profit: float
    expenses: float
    net_profit: float
    total_receivable: float
    total_payable: float
    low_stock_alerts: int
    top_selling_product: Optional[str] = None


class ReportsHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Helper Functions ──────────────────────────────────────────────────────────

def parse_report_dates(start_date: Optional[str], end_date: Optional[str]) -> tuple:
    if not start_date:
        start_date = date.today().replace(day=1).isoformat()
    if not end_date:
        end_date = date.today().isoformat()
    return start_date, end_date


def _period_return_amount(db: Session, start_date: str, end_date: str) -> float:
    return float(db.query(func.sum(models.SaleReturn.refund_amount)).filter(
        models.SaleReturn.posted_at >= start_date + " 00:00:00",
        models.SaleReturn.posted_at <= end_date + " 23:59:59",
    ).scalar() or 0)


def _period_return_cost(db: Session, start_date: str, end_date: str) -> float:
    return float(db.query(func.sum(models.SaleReturnItem.cost_amount)).join(models.SaleReturn).filter(
        models.SaleReturn.posted_at >= start_date + " 00:00:00",
        models.SaleReturn.posted_at <= end_date + " 23:59:59",
    ).scalar() or 0)


def _sale_return_amount(sale: models.Sale) -> float:
    return sum(float(posted.refund_amount or 0) for posted in sale.returns)


def _sale_paid_return_amount(sale: models.Sale, through=None) -> float:
    return sum(
        float(paid_refund_amount(posted))
        for posted in sale.returns
        if through is None or posted.posted_at <= through
    )


def _period_paid_return_amount(db: Session, start_date: str, end_date: str) -> float:
    posted_returns = db.query(models.SaleReturn).options(
        joinedload(models.SaleReturn.cash_transaction)
    ).filter(
        models.SaleReturn.posted_at >= start_date + " 00:00:00",
        models.SaleReturn.posted_at <= end_date + " 23:59:59",
    ).all()
    return sum(float(paid_refund_amount(posted)) for posted in posted_returns)


# ── Health & Executive Endpoints ──────────────────────────────────────────────

@router.get("/health", response_model=ReportsHealthCheckResponse)
@limiter.limit("1200/minute")
def reports_health(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Health check endpoint for reports module."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "reports",
        "endpoints": [
            "/dashboard",
            "/sales",
            "/purchases",
            "/profit-loss",
            "/stock",
            "/product-sales",
            "/product-ledger/{product_id}",
            "/daily-performance",
            "/audit-details",
            "/executive-summary",
            "/export",
            "/health"
        ]
    }


@router.get("/executive-summary", response_model=ExecutiveSummaryResponse)
@limiter.limit("1200/minute")
def executive_summary(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Compressed executive overview of financial performance and inventory status."""
    try:
        start_d, end_d = parse_report_dates(start_date, end_date)
        logger.info(f"Admin '{current_user.username}' requesting executive summary for {start_d} to {end_d}")

        sales_raw = db.query(func.sum(models.Sale.total)).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).scalar() or 0

        returned = _period_return_amount(db, start_d, end_d)

        total_sales = float(sales_raw) - float(returned)

        cogs_sold = db.query(
            func.sum(models.SaleItem.qty * models.SaleItem.buy_price)
        ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).scalar() or 0
        cogs = float(cogs_sold) - _period_return_cost(db, start_d, end_d)

        exp_raw = db.query(func.sum(models.Expense.amount)).filter(
            models.Expense.date >= start_d + " 00:00:00",
            models.Expense.date <= end_d + " 23:59:59"
        ).scalar() or 0

        cash_exp = db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type.in_(CASH_OUT_EXPENSE_TYPES),
            (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
            models.CashTransaction.created_at >= start_d + " 00:00:00",
            models.CashTransaction.created_at <= end_d + " 23:59:59"
        ).scalar() or 0

        expenses = float(exp_raw) + float(cash_exp) - salary_reversal_total(
            db, start_d + " 00:00:00", end_d + " 23:59:59"
        )
        gross_profit = total_sales - float(cogs)
        net_profit = gross_profit - expenses

        receivable = db.query(func.sum(models.Customer.credit_balance)).scalar() or 0
        payable = db.query(func.sum(models.Supplier.due_balance)).scalar() or 0
        low_stock = db.query(func.count(models.Product.id)).filter(
            models.Product.is_active == True,
            models.Product.stock <= models.Product.min_stock
        ).scalar() or 0

        sold_product_rows = db.query(
            models.SaleItem.product_name,
            func.sum(models.SaleItem.qty).label("qty")
        ).join(models.Sale).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).group_by(models.SaleItem.product_name).all()
        returned_product_rows = db.query(
            models.SaleReturnItem.product_name,
            func.sum(models.SaleReturnItem.qty).label("qty"),
        ).join(models.SaleReturn).filter(
            models.SaleReturn.posted_at >= start_d + " 00:00:00",
            models.SaleReturn.posted_at <= end_d + " 23:59:59",
        ).group_by(models.SaleReturnItem.product_name).all()
        product_qty = {row.product_name: float(row.qty or 0) for row in sold_product_rows}
        for row in returned_product_rows:
            product_qty[row.product_name] = product_qty.get(row.product_name, 0.0) - float(row.qty or 0)
        top_prod_name = max(product_qty, key=product_qty.get) if product_qty else None

        return {
            "period": f"{start_d} to {end_d}",
            "sales": round(total_sales, 2),
            "cogs": round(float(cogs), 2),
            "gross_profit": round(gross_profit, 2),
            "expenses": round(expenses, 2),
            "net_profit": round(net_profit, 2),
            "total_receivable": round(float(receivable), 2),
            "total_payable": round(float(payable), 2),
            "low_stock_alerts": low_stock,
            "top_selling_product": top_prod_name
        }
    except Exception as e:
        logger.error(f"Error building executive summary: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Executive summary failed: {str(e)}")


@router.get("/export")
@limiter.limit("10/minute")
def export_report(
    request: Request,
    report_type: str = Query("sales", pattern="^(sales|purchases|stock|product_sales)$"),
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Export report dataset in CSV or Excel format."""
    try:
        start_d, end_d = parse_report_dates(start_date, end_date)
        logger.info(f"Admin '{current_user.username}' exporting '{report_type}' report in {format} format")

        data = []
        if report_type == "sales":
            sales = db.query(models.Sale).options(
                joinedload(models.Sale.customer),
                joinedload(models.Sale.employee),
            ).filter(
                models.Sale.status.in_(SALE_STATUS_ACTIVE),
                models.Sale.created_at >= start_d + " 00:00:00",
                models.Sale.created_at <= end_d + " 23:59:59"
            ).order_by(models.Sale.created_at.desc()).all()

            for s in sales:
                data.append({
                    "Transaction Type": "SALE",
                    "Return Ref": "",
                    "Invoice No": s.invoice_no,
                    "Customer": (
                        f"Employee: {s.employee.full_name}"
                        if getattr(s, "employee_id", None) and s.employee
                        else (s.customer.name if s.customer else "Walk-in")
                    ),
                    "Total Amount": s.total,
                    "Paid Amount": s.paid_amount,
                    "Refund Amount": 0,
                    "Payment Method": s.payment_method,
                    "Date": s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else ""
                })

            posted_returns = db.query(models.SaleReturn).join(models.Sale).options(
                joinedload(models.SaleReturn.sale).joinedload(models.Sale.customer),
                joinedload(models.SaleReturn.sale).joinedload(models.Sale.employee),
                joinedload(models.SaleReturn.cash_transaction),
            ).filter(
                models.SaleReturn.posted_at >= start_d + " 00:00:00",
                models.SaleReturn.posted_at <= end_d + " 23:59:59",
            ).order_by(models.SaleReturn.posted_at.desc()).all()
            for posted in posted_returns:
                sale = posted.sale
                refund = float(posted.refund_amount or 0)
                payment_method = str(posted.payment_method or sale.payment_method or "")
                data.append({
                    "Transaction Type": "RETURN",
                    "Return Ref": f"RTN-{posted.id}",
                    "Invoice No": sale.invoice_no,
                    "Customer": (
                        f"Employee: {sale.employee.full_name}"
                        if getattr(sale, "employee_id", None) and sale.employee
                        else (sale.customer.name if sale.customer else "Walk-in")
                    ),
                    "Total Amount": -refund,
                    "Paid Amount": -float(paid_refund_amount(posted)),
                    "Refund Amount": refund,
                    "Payment Method": payment_method,
                    "Date": posted.posted_at.strftime("%Y-%m-%d %H:%M:%S") if posted.posted_at else "",
                })

            data.sort(key=lambda row: row["Date"], reverse=True)

        elif report_type == "purchases":
            purchases = db.query(models.Purchase).options(
                joinedload(models.Purchase.supplier)
            ).filter(
                models.Purchase.created_at >= start_d + " 00:00:00",
                models.Purchase.created_at <= end_d + " 23:59:59"
            ).order_by(models.Purchase.created_at.desc()).all()

            for p in purchases:
                data.append({
                    "Bill No": p.bill_no,
                    "Supplier": p.supplier.name if p.supplier else "",
                    "Total Amount": p.total,
                    "Paid Amount": p.paid_amount,
                    "Date": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else ""
                })

        elif report_type == "stock":
            products = db.query(models.Product).options(
                joinedload(models.Product.category)
            ).filter(models.Product.is_active == True).order_by(models.Product.name).all()

            for pr in products:
                data.append({
                    "Product ID": pr.id,
                    "Name": pr.name,
                    "Category": pr.category.name if pr.category else "Uncategorized",
                    "Barcode": pr.barcode or "",
                    "Stock": pr.stock,
                    "Buy Price": pr.buy_price,
                    "Sell Price": pr.sell_price,
                    "Stock Value": round(pr.stock * (pr.buy_price or 0.0), 2)
                })

        elif report_type == "product_sales":
            product_totals: Dict[str, Dict[str, Decimal]] = {}
            period_sales = db.query(models.Sale).options(
                joinedload(models.Sale.items)
            ).filter(
                models.Sale.status.in_(SALE_STATUS_ACTIVE),
                models.Sale.created_at >= start_d + " 00:00:00",
                models.Sale.created_at <= end_d + " 23:59:59"
            ).all()

            for sale in period_sales:
                sale_items = sorted(sale.items, key=lambda item: item.id or 0)
                sale_allocations = allocate_sale_item_amounts(sale)
                for item in sale_items:
                    allocated = sale_allocations.get(item.id, Decimal("0.00"))
                    values = product_totals.setdefault(
                        item.product_name,
                        {"qty": Decimal("0"), "amount": Decimal("0")},
                    )
                    values["qty"] += Decimal(str(item.qty or 0))
                    values["amount"] += allocated

            returned_items = db.query(
                models.SaleReturnItem.product_name,
                func.sum(models.SaleReturnItem.qty).label("total_qty"),
                func.sum(models.SaleReturnItem.allocated_amount).label("total_amount"),
            ).join(models.SaleReturn).filter(
                models.SaleReturn.posted_at >= start_d + " 00:00:00",
                models.SaleReturn.posted_at <= end_d + " 23:59:59",
            ).group_by(models.SaleReturnItem.product_name).all()
            for returned in returned_items:
                values = product_totals.setdefault(
                    returned.product_name,
                    {"qty": Decimal("0"), "amount": Decimal("0")},
                )
                values["qty"] -= Decimal(str(returned.total_qty or 0))
                values["amount"] -= Decimal(str(returned.total_amount or 0))

            for product_name, values in sorted(product_totals.items()):
                data.append({
                    "Product Name": product_name,
                    "Total Qty Sold": float(values["qty"]),
                    "Total Amount": float(values["amount"].quantize(Decimal("0.01"))),
                })

        if not data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No data found for report export")

        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

        if format == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
            output.seek(0)
            return StreamingResponse(
                io.BytesIO(output.getvalue().encode('utf-8')),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename={report_type}_report_{timestamp_str}.csv"
                }
            )
        else:
            try:
                import pandas as pd
                df = pd.DataFrame(data)
                excel_output = io.BytesIO()
                with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, sheet_name='Report', index=False)
                excel_output.seek(0)
                return StreamingResponse(
                    excel_output,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": f"attachment; filename={report_type}_report_{timestamp_str}.xlsx"
                    }
                )
            except ImportError:
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
                writer.writeheader()
                writer.writerows(data)
                output.seek(0)
                return StreamingResponse(
                    io.BytesIO(output.getvalue().encode('utf-8')),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f"attachment; filename={report_type}_report_{timestamp_str}.csv"
                    }
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting report '{report_type}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Export failed: {str(e)}")


# ── Core Reports Endpoints ───────────────────────────────────────────────────

@router.get("/dashboard", response_model=DashboardSummaryResponse)
@limiter.limit("1200/minute")
def dashboard_summary(
    request: Request,
    period: str = Query("90days", description="Period window: today | 7days | 30days | 90days | month | year"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Calculates dashboard KPIs, today's metrics, monthly totals, and chart trend points."""
    try:
        logger.info(f"Admin '{current_user.username}' fetching dashboard summary (period={period})")
        today_dt = date.today()
        today_str = today_dt.isoformat()
        
        if period == "today":
            start_dt = today_dt
        elif period == "7days":
            start_dt = today_dt - timedelta(days=7)
        elif period == "30days":
            start_dt = today_dt - timedelta(days=30)
        elif period == "90days":
            start_dt = today_dt - timedelta(days=90)
        elif period == "month":
            start_dt = today_dt.replace(day=1)
        elif period == "year":
            start_dt = today_dt.replace(month=1, day=1)
        else:
            start_dt = today_dt - timedelta(days=30)
            
        start_str = start_dt.isoformat()
        month_start_str = today_dt.replace(day=1).isoformat()

        # 1. Sales Totals for Selected Period
        period_sales_raw = db.query(func.sum(models.Sale.total)).filter(
            models.Sale.created_at >= start_str,
            models.Sale.status.in_(SALE_STATUS_ACTIVE)
        ).scalar() or 0

        period_ff_sales = db.query(func.sum(models.FFOrder.total)).filter(
            models.FFOrder.created_at >= start_str,
            models.FFOrder.status.in_(FF_STATUS_ACTIVE)
        ).scalar() or 0

        period_returned = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
            models.SaleReturn.posted_at >= start_str
        ).scalar() or 0

        period_sales = (period_sales_raw - period_returned) + float(period_ff_sales)

        period_retail_orders = db.query(func.count(models.Sale.id)).filter(
            models.Sale.created_at >= start_str,
            models.Sale.status.in_(SALE_STATUS_ACTIVE)
        ).scalar() or 0

        period_ff_orders = db.query(func.count(models.FFOrder.id)).filter(
            models.FFOrder.created_at >= start_str,
            models.FFOrder.status.in_(FF_STATUS_ACTIVE)
        ).scalar() or 0

        period_orders = period_retail_orders + period_ff_orders

        # 2. Today's Performance
        today_sales_raw = db.query(func.sum(models.Sale.total)).filter(
            models.Sale.created_at >= today_str,
            models.Sale.status.in_(SALE_STATUS_ACTIVE)
        ).scalar() or 0

        today_ff_sales = db.query(func.sum(models.FFOrder.total)).filter(
            models.FFOrder.created_at >= today_str,
            models.FFOrder.status.in_(FF_STATUS_ACTIVE)
        ).scalar() or 0

        today_returned = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
            models.SaleReturn.posted_at >= today_str
        ).scalar() or 0

        today_sales = (today_sales_raw - today_returned) + float(today_ff_sales)

        # 3. Monthly Performance
        month_sales_raw = db.query(func.sum(models.Sale.total)).filter(
            models.Sale.created_at >= month_start_str,
            models.Sale.status.in_(SALE_STATUS_ACTIVE)
        ).scalar() or 0

        month_ff_sales = db.query(func.sum(models.FFOrder.total)).filter(
            models.FFOrder.created_at >= month_start_str,
            models.FFOrder.status.in_(FF_STATUS_ACTIVE)
        ).scalar() or 0

        month_returned = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
            models.SaleReturn.posted_at >= month_start_str
        ).scalar() or 0

        month_sales = (month_sales_raw - month_returned) + float(month_ff_sales)

        month_purchases = db.query(func.sum(models.Purchase.total)).filter(
            models.Purchase.created_at >= month_start_str
        ).scalar() or 0

        month_expenses_raw = db.query(func.sum(models.Expense.amount)).filter(
            models.Expense.date >= month_start_str
        ).scalar() or 0

        month_cash_expenses = db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type.in_(CASH_OUT_EXPENSE_TYPES),
            (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
            models.CashTransaction.created_at >= month_start_str
        ).scalar() or 0

        month_expenses = float(month_expenses_raw) + float(month_cash_expenses) - salary_reversal_total(
            db, month_start_str + " 00:00:00", today_str + " 23:59:59"
        )

        # 4. Global Receivables/Payables & Stock Alert Count
        total_receivable = db.query(func.sum(models.Customer.credit_balance)).scalar() or 0
        total_payable = db.query(func.sum(models.Supplier.due_balance)).scalar() or 0
        low_stock_count = db.query(func.count(models.Product.id)).filter(
            models.Product.is_active == True,
            models.Product.stock <= models.Product.min_stock
        ).scalar() or 0

        # 5. Chart Trend Points (Last 7 Days)
        chart_sales_raw = db.query(
            func.strftime("%Y-%m-%d", models.Sale.created_at).label("d"),
            func.sum(models.Sale.total).label("val")
        ).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_str
        ).group_by(func.strftime("%Y-%m-%d", models.Sale.created_at)).all()
        
        chart_returned_raw = db.query(
            func.strftime("%Y-%m-%d", models.SaleReturn.posted_at).label("d"),
            func.sum(models.SaleReturn.refund_amount).label("val")
        ).filter(
            models.SaleReturn.posted_at >= start_str
        ).group_by(func.strftime("%Y-%m-%d", models.SaleReturn.posted_at)).all()
        
        chart_sales_map = {str(r.d): r.val for r in chart_sales_raw}
        chart_returned_map = {str(r.d): r.val for r in chart_returned_raw}
        chart_map = {}
        for d in set(list(chart_sales_map.keys()) + list(chart_returned_map.keys())):
            chart_map[d] = float(chart_sales_map.get(d, 0) or 0) - float(chart_returned_map.get(d, 0) or 0)
        
        chart_data = []
        for i in range(6, -1, -1):
            d_str = (today_dt - timedelta(days=i)).isoformat()
            chart_data.append({"date": d_str, "total": round(chart_map.get(d_str, 0), 2)})

        return {
            "period_sales": round(period_sales, 2),
            "period_orders": period_orders,
            "today_sales": round(today_sales, 2),
            "month_sales": round(month_sales, 2),
            "month_profit": round(month_sales - month_purchases - month_expenses, 2),
            "total_receivable": round(total_receivable, 2),
            "total_payable": round(total_payable, 2),
            "low_stock_count": low_stock_count,
            "chart_data": chart_data,
        }
    except Exception as e:
        logger.error(f"Error compiling dashboard summary: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Dashboard summary failed: {str(e)}")


@router.get("/sales", response_model=SalesReportResponse)
@limiter.limit("1200/minute")
def sales_report(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Detailed Sales Report with revenues, costs, profits, and paginated invoice items."""
    try:
        start_d, end_d = parse_report_dates(start_date, end_date)
        logger.info(f"Admin '{current_user.username}' fetching sales report ({start_d} to {end_d})")

        query = db.query(models.Sale).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        )

        summary = db.query(
            func.count(models.Sale.id).label("count"),
            func.sum(models.Sale.total).label("total_revenue_raw"),
            func.sum(models.Sale.paid_amount).label("total_paid_raw")
        ).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).first()

        total_returned_full = _period_return_amount(db, start_d, end_d)

        total_cost_sold = db.query(
            func.sum(models.SaleItem.qty * models.SaleItem.buy_price)
        ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).scalar() or 0
        total_cost_full = float(total_cost_sold) - _period_return_cost(db, start_d, end_d)

        total_count = summary.count or 0
        total_rev_raw = summary.total_revenue_raw or 0
        total_paid_raw = summary.total_paid_raw or 0
        
        total_rev = total_rev_raw - total_returned_full
        total_paid = float(total_paid_raw) - _period_paid_return_amount(db, start_d, end_d)

        due_rows = query.options(
            joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction)
        ).all()
        total_due = 0.0
        for row in due_rows:
            if row.payment_method == "employee_credit":
                continue
            returned_for_sale = _sale_return_amount(row)
            net_total_for_sale = max(0.0, float(row.total or 0) - returned_for_sale)
            net_paid_for_sale = max(0.0, float(row.paid_amount or 0) - _sale_paid_return_amount(row))
            total_due += max(0.0, net_total_for_sale - net_paid_for_sale)

        sales = query.options(
            joinedload(models.Sale.customer),
            joinedload(models.Sale.employee),
            joinedload(models.Sale.items).joinedload(models.SaleItem.product),
        ).order_by(models.Sale.created_at.desc()).offset(offset).limit(limit).all()

        sales_data = []
        for s in sales:
            cost = sum(float(i.buy_price or 0) * float(i.qty or 0) for i in s.items)
            sale_total = float(s.total or 0)
            sales_data.append({
                "invoice_no": s.invoice_no, 
                "customer": (
                    f"Employee: {s.employee.full_name}"
                    if getattr(s, "employee_id", None) and s.employee
                    else (s.customer.name if s.customer else "Walk-in")
                ),
                "total": round(sale_total, 2),
                "paid": round(float(s.paid_amount or 0), 2),
                "method": s.payment_method,
                "date": s.created_at,
                "cost": round(cost, 2),
                "profit": round(sale_total - cost, 2)
            })

        # Return documents are separate accounting events, so a return posted
        # in this period remains visible even when its source sale is older.
        posted_returns = db.query(models.SaleReturn).options(
            joinedload(models.SaleReturn.sale).joinedload(models.Sale.customer),
            joinedload(models.SaleReturn.sale).joinedload(models.Sale.employee),
            joinedload(models.SaleReturn.items),
            joinedload(models.SaleReturn.cash_transaction),
        ).filter(
            models.SaleReturn.posted_at >= start_d + " 00:00:00",
            models.SaleReturn.posted_at <= end_d + " 23:59:59",
        ).order_by(models.SaleReturn.posted_at.desc()).all()
        for posted in posted_returns:
            source_sale = posted.sale
            refund = float(posted.refund_amount or 0)
            returned_cost = sum(float(line.cost_amount or 0) for line in posted.items)
            method = str(posted.payment_method or source_sale.payment_method or "")
            sales_data.append({
                "invoice_no": f"{source_sale.invoice_no} / RTN-{posted.id}",
                "customer": (
                    f"Employee: {source_sale.employee.full_name}"
                    if getattr(source_sale, "employee_id", None) and source_sale.employee
                    else (source_sale.customer.name if source_sale.customer else "Walk-in")
                ),
                "total": round(-refund, 2),
                "paid": round(-float(paid_refund_amount(posted)), 2),
                "method": method,
                "date": posted.posted_at,
                "cost": round(-returned_cost, 2),
                "profit": round(-refund + returned_cost, 2),
            })
        sales_data.sort(key=lambda item: item["date"] or datetime.min, reverse=True)

        return {
            "count": total_count,
            "total": round(total_rev, 2),
            "total_cost": round(total_cost_full, 2),
            "total_profit": round(total_rev - total_cost_full, 2),
            "paid": round(total_paid, 2),
            "due": round(total_due, 2),
            "items": sales_data
        }
    except Exception as e:
        logger.error(f"Error fetching sales report: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Sales report failed: {str(e)}")


@router.get("/purchases", response_model=PurchasesReportResponse)
@limiter.limit("1200/minute")
def purchases_report(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Purchases report with total supplier expenditures and paginated bill entries."""
    try:
        start_d, end_d = parse_report_dates(start_date, end_date)
        logger.info(f"Admin '{current_user.username}' fetching purchases report ({start_d} to {end_d})")

        query = db.query(models.Purchase).filter(
            models.Purchase.created_at >= start_d + " 00:00:00",
            models.Purchase.created_at <= end_d + " 23:59:59"
        )

        total_count = query.count()
        total_revenue = db.query(func.sum(models.Purchase.total)).filter(
            models.Purchase.created_at >= start_d + " 00:00:00",
            models.Purchase.created_at <= end_d + " 23:59:59"
        ).scalar() or 0

        purchases = query.options(joinedload(models.Purchase.supplier))\
                         .order_by(models.Purchase.created_at.desc())\
                         .offset(offset).limit(limit).all()

        return {
            "count": total_count,
            "total": round(total_revenue, 2),
            "items": [
                {
                    "bill_no": p.bill_no, 
                    "supplier": p.supplier.name if p.supplier else "",
                    "supplier_balance": p.supplier.due_balance if p.supplier else 0,
                    "total": p.total, 
                    "paid": p.paid_amount, 
                    "date": p.created_at
                }
                for p in purchases
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching purchases report: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Purchases report failed: {str(e)}")


@router.get("/profit-loss", response_model=ProfitLossResponse)
@limiter.limit("1200/minute")
def profit_loss(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Profit and Loss statement: Retail + Fast Food Sales, COGS, Expenses & Net Profit."""
    try:
        start_d, end_d = parse_report_dates(start_date, end_date)
        logger.info(f"Admin '{current_user.username}' calculating Profit & Loss ({start_d} to {end_d})")

        total_sales_raw = db.query(func.sum(models.Sale.total)).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).scalar() or 0

        total_returned = _period_return_amount(db, start_d, end_d)

        ff_sales_raw = db.query(func.sum(models.FFOrder.total)).filter(
            models.FFOrder.status.in_(FF_STATUS_ACTIVE),
            models.FFOrder.created_at >= start_d + " 00:00:00",
            models.FFOrder.created_at <= end_d + " 23:59:59"
        ).scalar() or 0

        total_sales = (total_sales_raw - total_returned) + float(ff_sales_raw)

        retail_cost_sold = db.query(
            func.sum(models.SaleItem.qty * models.SaleItem.buy_price)
        ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59",
        ).scalar() or 0
        retail_purchase_cost = float(retail_cost_sold) - _period_return_cost(db, start_d, end_d)

        ff_items = db.query(models.FFOrderItem).join(models.FFOrder).filter(
            models.FFOrder.status.in_(FF_STATUS_ACTIVE),
            models.FFOrder.created_at >= start_d + " 00:00:00",
            models.FFOrder.created_at <= end_d + " 23:59:59"
        ).all()

        ff_cogs = 0.0
        for item in ff_items:
            if item.item_id:
                recipes = db.query(models.FFRecipe).filter_by(item_id=item.item_id).all()
                for r in recipes:
                    ff_cogs += (r.product.buy_price if r.product else 0.0) * r.qty * item.qty

        total_purchase_cost = float(retail_purchase_cost) + float(ff_cogs)

        total_expenses_raw = db.query(func.sum(models.Expense.amount)).filter(
            models.Expense.date >= start_d + " 00:00:00",
            models.Expense.date <= end_d + " 23:59:59"
        ).scalar() or 0

        total_cash_expenses = db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type.in_(CASH_OUT_EXPENSE_TYPES),
            (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
            models.CashTransaction.created_at >= start_d + " 00:00:00",
            models.CashTransaction.created_at <= end_d + " 23:59:59"
        ).scalar() or 0

        total_expenses = float(total_expenses_raw) + float(total_cash_expenses) - salary_reversal_total(
            db, start_d + " 00:00:00", end_d + " 23:59:59"
        )
        gross_profit = total_sales - total_purchase_cost
        net_profit = gross_profit - total_expenses

        return {
            "period": f"{start_d} to {end_d}",
            "total_sales": round(total_sales, 2),
            "total_purchase_cost": round(total_purchase_cost, 2),
            "gross_profit": round(gross_profit, 2),
            "total_expenses": round(total_expenses, 2),
            "net_profit": round(net_profit, 2),
        }
    except Exception as e:
        logger.error(f"Error calculating Profit & Loss: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Profit and loss statement failed: {str(e)}")


@router.get("/stock", response_model=StockReportResponse)
@limiter.limit("1200/minute")
def stock_report(
    request: Request,
    limit: int = Query(50, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    query: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    stock_filter: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Stock inventory valuation report with search, category filtering, and status breakdown."""
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock report (query={query}, category={category}, filter={stock_filter})")
        base_filter = [models.Product.is_active == True]
        if query:
            exact_ids = [r[0] for r in db.query(models.Product.id).filter(
                models.Product.is_active == True,
                or_(
                    models.Product.barcode == query,
                    models.Product.code == query
                )
            ).all()]
            
            if exact_ids:
                base_filter.append(models.Product.id.in_(exact_ids))
            else:
                base_filter.append(models.Product.name.ilike(f"%{query}%"))
        
        if category:
            base_filter.append(models.Product.category.has(name=category))

        if stock_filter == "in":
            base_filter.append(models.Product.stock > 0)
        elif stock_filter == "low":
            base_filter.append(models.Product.stock <= models.Product.min_stock)
            base_filter.append(models.Product.stock > 0)
        elif stock_filter == "zero":
            base_filter.append(models.Product.stock <= 0)

        aggs = db.query(
            func.count(models.Product.id).label("total_count"),
            func.sum(models.Product.stock * models.Product.buy_price).label("cost_value"),
            func.sum(models.Product.stock * models.Product.sell_price).label("sale_value")
        ).filter(*base_filter).first()

        products = db.query(models.Product).options(
            joinedload(models.Product.category),
            joinedload(models.Product.unit)
        ).filter(*base_filter)\
         .order_by(models.Product.name.asc())\
         .offset(offset).limit(limit).all()

        all_cats = db.query(models.Category.name).filter(models.Category.name != None).distinct().all()
        categories = sorted([c[0] for c in all_cats if c[0]])

        return {
            "count": aggs.total_count or 0,
            "total_products": aggs.total_count or 0,
            "total_stock_value": round(float(aggs.cost_value or 0), 2),
            "total_sale_value": round(float(aggs.sale_value or 0), 2),
            "categories": categories,
            "items": [
                {
                    "id": p.id,
                    "name": p.name,
                    "category": p.category.name if p.category else "Uncategorized",
                    "barcode": p.barcode,
                    "code": p.code,
                    "stock": p.stock,
                    "unit": p.unit.name if p.unit else "pcs",
                    "buy_price": p.buy_price,
                    "sell_price": p.sell_price,
                    "stock_value": round(p.stock * (p.buy_price or 0.0), 2),
                    "sale_value": round(p.stock * (p.sell_price or 0.0), 2),
                    "low": p.stock <= (p.min_stock or 0.0)
                }
                for p in products
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching stock report: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Stock report failed: {str(e)}")


@router.get("/product-sales", response_model=List[ProductSalesItem])
@limiter.limit("1200/minute")
def product_sales_report(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Product-level sales performance report with units sold, revenue, costs, and profits."""
    try:
        start_d, end_d = parse_report_dates(start_date, end_date)
        logger.info(f"Admin '{current_user.username}' fetching product sales report ({start_d} to {end_d})")

        period_sales = db.query(models.Sale).options(joinedload(models.Sale.items)).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_d + " 00:00:00",
            models.Sale.created_at <= end_d + " 23:59:59"
        ).all()

        returned_items = db.query(
            models.SaleReturnItem.product_id,
            models.SaleReturnItem.product_name,
            func.sum(models.SaleReturnItem.qty).label("total_qty"),
            func.sum(models.SaleReturnItem.allocated_amount).label("total_amount"),
            func.sum(models.SaleReturnItem.cost_amount).label("total_cost"),
        ).join(models.SaleReturn).filter(
            models.SaleReturn.posted_at >= start_d + " 00:00:00",
            models.SaleReturn.posted_at <= end_d + " 23:59:59",
        ).group_by(models.SaleReturnItem.product_id, models.SaleReturnItem.product_name).all()
        product_map = {}
        for sale in period_sales:
            allocations = allocate_sale_item_amounts(sale)
            for item in sale.items:
                row = product_map.setdefault(item.product_id, {
                    "product_id": item.product_id, "product_name": item.product_name,
                    "total_qty": 0.0, "total_amount": 0.0, "total_cost": 0.0,
                })
                row["total_qty"] += float(item.qty or 0)
                row["total_amount"] += float(allocations.get(item.id, 0))
                row["total_cost"] += float(item.qty or 0) * float(item.buy_price or 0)
        for item in returned_items:
            row = product_map.setdefault(item.product_id, {
                "product_id": item.product_id, "product_name": item.product_name,
                "total_qty": 0.0, "total_amount": 0.0, "total_cost": 0.0,
            })
            row["total_qty"] -= float(item.total_qty or 0)
            row["total_amount"] -= float(item.total_amount or 0)
            row["total_cost"] -= float(item.total_cost or 0)

        return [
            {
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "total_qty": round(item["total_qty"], 2),
                "total_amount": round(item["total_amount"], 2),
                "total_cost": round(item["total_cost"], 2),
                "total_profit": round(item["total_amount"] - item["total_cost"], 2),
            }
            for item in product_map.values()
        ]
    except Exception as e:
        logger.error(f"Error fetching product sales report: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Product sales report failed: {str(e)}")


@router.get("/product-ledger/{product_id}", response_model=ProductLedgerResponse)
@limiter.limit("1200/minute")
def product_ledger(
    request: Request,
    product_id: int,
    limit: int = Query(50, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Detailed inventory movement ledger for a single product."""
    try:
        logger.info(f"Admin '{current_user.username}' fetching product ledger for ID {product_id}")
        product = db.query(models.Product).filter_by(id=product_id).first()
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Product with ID {product_id} not found")

        total_count = db.query(models.StockMovement).filter_by(product_id=product_id).count()
        movements = db.query(models.StockMovement).filter_by(product_id=product_id)\
                      .order_by(models.StockMovement.id.desc())\
                      .offset(offset).limit(limit).all()
        
        ledger = []
        total_in = 0.0
        total_out = 0.0
        
        for m in movements:
            party = "System"
            ref = m.note or "-"
            
            if m.movement_type == "sale" and "Sale " in (m.note or ""):
                inv_no = m.note.replace("Sale ", "").strip()
                sale = db.query(models.Sale).filter_by(invoice_no=inv_no).first()
                if sale:
                    party = sale.customer.name if sale.customer else "Walk-in"
                    ref = inv_no
            elif m.movement_type == "purchase" and "Purchase " in (m.note or ""):
                bill_no = m.note.replace("Purchase ", "").strip()
                pur = db.query(models.Purchase).filter_by(bill_no=bill_no).first()
                if pur:
                    party = pur.supplier.name if pur.supplier else "Unknown"
                    ref = bill_no
            elif m.movement_type == "return" and "Return " in (m.note or ""):
                inv_no = m.note.replace("Full Return ", "").replace("Partial Return ", "").strip()
                sale = db.query(models.Sale).filter_by(invoice_no=inv_no).first()
                if sale:
                    party = sale.customer.name if sale.customer else "Walk-in"
                    ref = inv_no

            if m.qty_change > 0:
                total_in += m.qty_change
            else:
                total_out += abs(m.qty_change)

            ledger.append({
                "date": m.created_at.isoformat() if hasattr(m.created_at, "isoformat") else str(m.created_at),
                "type": m.movement_type.capitalize(),
                "party": party,
                "ref": ref,
                "in": round(m.qty_change, 2) if m.qty_change > 0 else 0,
                "out": round(abs(m.qty_change), 2) if m.qty_change < 0 else 0,
                "balance": round(m.qty_after, 2)
            })

        return {
            "product_name": product.name,
            "product_code": product.code,
            "current_stock": product.stock,
            "total_purchased": round(total_in, 2),
            "total_sold": round(total_out, 2),
            "count": total_count,
            "items": ledger
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching product ledger for ID {product_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Product ledger failed: {str(e)}")


@router.get("/daily-performance", response_model=DailyPerformanceResponse)
@limiter.limit("1200/minute")
def daily_performance_report(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Daily operational performance metrics combining sales, purchases, cash flows, and COGS."""
    try:
        if not start_date:
            start_date = (date.today() - timedelta(days=30)).isoformat()
        if not end_date:
            end_date = date.today().isoformat()

        logger.info(f"Admin '{current_user.username}' fetching daily performance ({start_date} to {end_date})")

        performance = {}
        sale_day = func.strftime("%Y-%m-%d", models.Sale.created_at)
        sale_rows = db.query(
            sale_day.label("day"),
            func.sum(models.Sale.total + models.Sale.discount).label("gross_sales"),
            func.sum(models.Sale.discount).label("total_discount"),
            func.sum(models.Sale.total).label("sales"),
            func.sum(case((models.Sale.payment_method.in_(["cash", "mixed", "credit"]), models.Sale.paid_amount), else_=0)).label("cash_collected"),
        ).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_date + " 00:00:00",
            models.Sale.created_at <= end_date + " 23:59:59",
        ).group_by(sale_day).all()
        cogs_rows = db.query(
            sale_day.label("day"),
            func.sum(models.SaleItem.qty * models.SaleItem.buy_price).label("cogs"),
        ).join(models.Sale).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_date + " 00:00:00",
            models.Sale.created_at <= end_date + " 23:59:59",
        ).group_by(sale_day).all()
        cogs_map = {str(row.day): float(row.cogs or 0) for row in cogs_rows}
        for row in sale_rows:
            performance[str(row.day)] = {
                "gross_sales": float(row.gross_sales or 0),
                "total_discount": float(row.total_discount or 0),
                "sales": float(row.sales or 0),
                "cash_collected": float(row.cash_collected or 0),
                "purchases": 0.0,
                "expenses": 0.0,
                "cogs": cogs_map.get(str(row.day), 0.0),
                "received": 0.0,
                "paid": 0.0
            }

        return_day = func.strftime("%Y-%m-%d", models.SaleReturn.posted_at)
        return_rows = db.query(
            return_day.label("day"),
            func.sum(models.SaleReturn.refund_amount).label("refunds"),
        ).filter(
            models.SaleReturn.posted_at >= start_date + " 00:00:00",
            models.SaleReturn.posted_at <= end_date + " 23:59:59",
        ).group_by(return_day).all()
        return_cost_rows = db.query(
            return_day.label("day"),
            func.sum(models.SaleReturnItem.cost_amount).label("cost"),
        ).join(models.SaleReturn).filter(
            models.SaleReturn.posted_at >= start_date + " 00:00:00",
            models.SaleReturn.posted_at <= end_date + " 23:59:59",
        ).group_by(return_day).all()
        return_cost_map = {str(row.day): float(row.cost or 0) for row in return_cost_rows}
        cash_refund_rows = db.query(
            return_day.label("day"),
            func.sum(models.CashTransaction.amount).label("amount"),
        ).join(
            models.CashTransaction,
            models.CashTransaction.id == models.SaleReturn.cash_transaction_id,
        ).filter(
            models.SaleReturn.posted_at >= start_date + " 00:00:00",
            models.SaleReturn.posted_at <= end_date + " 23:59:59",
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type == "refund",
            models.CashTransaction.account == "cash_in_hand",
        ).group_by(return_day).all()
        cash_refund_map = {str(row.day): float(row.amount or 0) for row in cash_refund_rows}
        for row in return_rows:
            day = str(row.day)
            values = performance.setdefault(day, {"gross_sales": 0.0, "total_discount": 0.0, "sales": 0.0, "cash_collected": 0.0, "purchases": 0.0, "expenses": 0.0, "cogs": 0.0, "received": 0.0, "paid": 0.0})
            values["gross_sales"] -= float(row.refunds or 0)
            values["sales"] -= float(row.refunds or 0)
            values["cash_collected"] -= cash_refund_map.get(day, 0.0)
            values["cogs"] -= return_cost_map.get(day, 0.0)

        ff_daily_orders = db.query(
            func.strftime("%Y-%m-%d", models.FFOrder.created_at).label("day"),
            func.sum(models.FFOrder.subtotal).label("gross_sales"),
            func.sum(models.FFOrder.discount).label("total_discount"),
            func.sum(models.FFOrder.total).label("sales"),
            func.sum(case((models.FFOrder.payment_method == 'cash', models.FFOrder.total), else_=0)).label("cash_collected")
        ).filter(
            models.FFOrder.status.in_(FF_STATUS_ACTIVE),
            models.FFOrder.created_at >= start_date + " 00:00:00",
            models.FFOrder.created_at <= end_date + " 23:59:59"
        ).group_by(func.strftime("%Y-%m-%d", models.FFOrder.created_at)).all()

        for ff in ff_daily_orders:
            day = str(ff.day)
            if day not in performance:
                performance[day] = {"gross_sales": 0.0, "total_discount": 0.0, "sales": 0.0, "cash_collected": 0.0, "purchases": 0.0, "expenses": 0.0, "cogs": 0.0, "received": 0.0, "paid": 0.0}
            performance[day]["gross_sales"] += float(ff.gross_sales or 0)
            performance[day]["total_discount"] += float(ff.total_discount or 0)
            performance[day]["sales"] += float(ff.sales or 0)
            performance[day]["cash_collected"] += float(ff.cash_collected or 0)

            ff_items_day = db.query(models.FFOrderItem).join(models.FFOrder).filter(
                func.strftime("%Y-%m-%d", models.FFOrder.created_at) == day,
                models.FFOrder.status.in_(FF_STATUS_ACTIVE)
            ).all()
            ff_day_cogs = 0.0
            for item in ff_items_day:
                if item.item_id:
                    recipes = db.query(models.FFRecipe).filter_by(item_id=item.item_id).all()
                    for r in recipes:
                        ff_day_cogs += (r.product.buy_price if r.product else 0.0) * r.qty * item.qty
            performance[day]["cogs"] += float(ff_day_cogs)

        purchases_query = db.query(
            func.strftime("%Y-%m-%d", models.Purchase.created_at).label("day"),
            func.sum(models.Purchase.total).label("pur_total"),
            func.sum(models.Purchase.paid_amount).label("pur_paid")
        ).filter(models.Purchase.created_at >= start_date + " 00:00:00",
                 models.Purchase.created_at <= end_date + " 23:59:59")\
         .group_by(func.strftime("%Y-%m-%d", models.Purchase.created_at)).all()

        expenses_query = db.query(
            func.strftime("%Y-%m-%d", models.Expense.date).label("day"),
            func.sum(models.Expense.amount).label("exp_total")
        ).filter(models.Expense.date >= start_date + " 00:00:00",
                 models.Expense.date <= end_date + " 23:59:59")\
         .group_by(func.strftime("%Y-%m-%d", models.Expense.date)).all()

        cash_expenses_query = db.query(
            func.strftime("%Y-%m-%d", models.CashTransaction.created_at).label("day"),
            func.sum(models.CashTransaction.amount).label("exp_total")
        ).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type.in_(CASH_OUT_EXPENSE_TYPES),
            (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
            models.CashTransaction.created_at >= start_date + " 00:00:00",
            models.CashTransaction.created_at <= end_date + " 23:59:59"
        ).group_by(func.strftime("%Y-%m-%d", models.CashTransaction.created_at)).all()

        payments_query = db.query(
            func.strftime("%Y-%m-%d", models.Payment.created_at).label("day"),
            func.sum(models.Payment.amount).filter(models.Payment.payment_type == "received").label("received"),
            func.sum(models.Payment.amount).filter(models.Payment.payment_type == "paid").label("paid")
        ).filter(models.Payment.created_at >= start_date + " 00:00:00",
                 models.Payment.created_at <= end_date + " 23:59:59",
                 models.Payment.method == "cash")\
         .group_by(func.strftime("%Y-%m-%d", models.Payment.created_at)).all()

        for p in purchases_query:
            day = str(p.day)
            if day not in performance:
                performance[day] = {"gross_sales": 0.0, "total_discount": 0.0, "sales": 0.0, "cash_collected": 0.0, "purchases": 0.0, "expenses": 0.0, "cogs": 0.0, "received": 0.0, "paid": 0.0}
            performance[day]["purchases"] = float(p.pur_total or 0)
            performance[day]["paid"] += float(p.pur_paid or 0)

        for e in expenses_query:
            day = str(e.day)
            if day not in performance:
                performance[day] = {"gross_sales": 0.0, "total_discount": 0.0, "sales": 0.0, "cash_collected": 0.0, "purchases": 0.0, "expenses": 0.0, "cogs": 0.0, "received": 0.0, "paid": 0.0}
            performance[day]["expenses"] += float(e.exp_total or 0)

        for e in cash_expenses_query:
            day = str(e.day)
            if day not in performance:
                performance[day] = {"gross_sales": 0.0, "total_discount": 0.0, "sales": 0.0, "cash_collected": 0.0, "purchases": 0.0, "expenses": 0.0, "cogs": 0.0, "received": 0.0, "paid": 0.0}
            performance[day]["expenses"] += float(e.exp_total or 0)

        for pay in payments_query:
            day = str(pay.day)
            if day not in performance:
                performance[day] = {"gross_sales": 0.0, "total_discount": 0.0, "sales": 0.0, "cash_collected": 0.0, "purchases": 0.0, "expenses": 0.0, "cogs": 0.0, "received": 0.0, "paid": 0.0}
            performance[day]["received"] = float(pay.received or 0)
            performance[day]["paid"] += float(pay.paid or 0)

        results = []
        for day, data_item in performance.items():
            sales = data_item["sales"]
            purchases = data_item["purchases"]
            expenses = data_item["expenses"]
            cogs = data_item["cogs"]
            received = data_item["received"]
            paid = data_item["paid"]
            cash_collected = data_item.get("cash_collected", 0.0)
            
            net_cash = cash_collected + received - paid - expenses
            profit = (sales - cogs) - expenses
            
            results.append({
                "date": day,
                "gross_sales": round(float(data_item.get("gross_sales", 0)), 2),
                "discount": round(float(data_item.get("total_discount", 0)), 2),
                "sales": round(sales, 2),
                "cash_collected": round(cash_collected, 2),
                "purchases": round(purchases, 2),
                "expenses": round(expenses, 2),
                "cogs": round(cogs, 2),
                "received": round(received, 2),
                "paid": round(paid, 2),
                "net_cash": round(net_cash, 2),
                "profit": round(profit, 2)
            })

        results.sort(key=lambda x: x["date"], reverse=True)
        
        global_totals = {
            "gross_sales": round(sum(r["gross_sales"] for r in results), 2),
            "discount": round(sum(r["discount"] for r in results), 2),
            "sales": round(sum(r["sales"] for r in results), 2),
            "cash_collected": round(sum(r["cash_collected"] for r in results), 2),
            "purchases": round(sum(r["purchases"] for r in results), 2),
            "expenses": round(sum(r["expenses"] for r in results), 2),
            "received": round(sum(r["received"] for r in results), 2),
            "paid": round(sum(r["paid"] for r in results), 2),
            "net_cash": round(sum(r["net_cash"] for r in results), 2),
            "profit": round(sum(r["profit"] for r in results), 2)
        }

        total_count = len(results)
        paginated = results[offset : offset + limit]
        
        return {
            "count": total_count,
            "items": paginated,
            "totals": global_totals
        }
    except Exception as e:
        logger.error(f"Error compiling daily performance report: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Daily performance report failed: {str(e)}")


@router.get("/audit-details", response_model=AuditDetailsResponse)
@limiter.limit("1200/minute")
def audit_details(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """End-of-Day audit reconciliation and transaction details."""
    try:
        if not start_date:
            start_date = date.today().isoformat()
        if not end_date:
            end_date = date.today().isoformat()

        logger.info(f"Admin '{current_user.username}' fetching audit details ({start_date} to {end_date})")

        customer_payments = db.query(models.Payment).filter(
            models.Payment.party_type == "customer",
            models.Payment.payment_type == "received",
            models.Payment.created_at >= start_date + " 00:00:00",
            models.Payment.created_at <= end_date + " 23:59:59"
        ).options(joinedload(models.Payment.customer)).all()

        expenses_list = db.query(models.Expense).filter(
            models.Expense.date >= start_date + " 00:00:00",
            models.Expense.date <= end_date + " 23:59:59"
        ).all()

        cash_expenses_list = db.query(models.CashTransaction).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type.in_(CASH_OUT_EXPENSE_TYPES),
            (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
            models.CashTransaction.created_at >= start_date + " 00:00:00",
            models.CashTransaction.created_at <= end_date + " 23:59:59"
        ).all()

        purchases = db.query(models.Purchase).filter(
            models.Purchase.created_at >= start_date + " 00:00:00",
            models.Purchase.created_at <= end_date + " 23:59:59",
            models.Purchase.paid_amount > 0
        ).options(joinedload(models.Purchase.supplier)).all()
        
        manual_supplier_payments = db.query(models.Payment).filter(
            models.Payment.party_type == "supplier",
            models.Payment.payment_type == "paid",
            models.Payment.created_at >= start_date + " 00:00:00",
            models.Payment.created_at <= end_date + " 23:59:59"
        ).options(joinedload(models.Payment.supplier)).all()

        credit_sales = db.query(models.Sale).options(
            joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction)
        ).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.payment_method != "employee_credit",
            models.Sale.total > models.Sale.paid_amount,
            models.Sale.created_at >= start_date + " 00:00:00",
            models.Sale.created_at <= end_date + " 23:59:59"
        ).options(joinedload(models.Sale.customer)).all()

        sales_today = db.query(models.Sale).filter(
            models.Sale.created_at >= start_date + " 00:00:00",
            models.Sale.created_at <= end_date + " 23:59:59",
            models.Sale.status.in_(SALE_STATUS_ACTIVE)
        ).all()

        period_returns = _period_return_amount(db, start_date, end_date)
        gross_sales = sum(float(s.total or 0) for s in sales_today) - period_returns
        sales_cash_total = sum(
            float(s.paid_amount or 0) for s in sales_today if s.payment_method in ["cash", "mixed", "credit"]
        )
        cash_refunds = db.query(func.sum(models.CashTransaction.amount)).join(
            models.SaleReturn,
            models.SaleReturn.cash_transaction_id == models.CashTransaction.id,
        ).filter(
            models.SaleReturn.posted_at >= start_date + " 00:00:00",
            models.SaleReturn.posted_at <= end_date + " 23:59:59",
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type == "refund",
            models.CashTransaction.account == "cash_in_hand",
        ).scalar() or 0
        sales_cash_total -= float(cash_refunds)

        total_customer_received = db.query(func.sum(models.Payment.amount)).filter(
            models.Payment.created_at >= start_date + " 00:00:00",
            models.Payment.created_at <= end_date + " 23:59:59",
            models.Payment.party_type == "customer",
            models.Payment.payment_type == "received"
        ).scalar() or 0.0
        cash_customer_received = db.query(func.sum(models.Payment.amount)).filter(
            models.Payment.created_at >= start_date + " 00:00:00",
            models.Payment.created_at <= end_date + " 23:59:59",
            models.Payment.party_type == "customer",
            models.Payment.payment_type == "received",
            func.lower(models.Payment.method) == "cash",
        ).scalar() or 0.0

        manual_sup_total = db.query(func.sum(models.Payment.amount)).filter(
            models.Payment.created_at >= start_date + " 00:00:00",
            models.Payment.created_at <= end_date + " 23:59:59",
            models.Payment.party_type == "supplier",
            models.Payment.payment_type == "paid"
        ).scalar() or 0.0
        cash_manual_supplier_paid = db.query(func.sum(models.Payment.amount)).filter(
            models.Payment.created_at >= start_date + " 00:00:00",
            models.Payment.created_at <= end_date + " 23:59:59",
            models.Payment.party_type == "supplier",
            models.Payment.payment_type == "paid",
            func.lower(models.Payment.method) == "cash",
        ).scalar() or 0.0

        purchase_paid_total = db.query(func.sum(models.Purchase.paid_amount)).filter(
            models.Purchase.created_at >= start_date + " 00:00:00",
            models.Purchase.created_at <= end_date + " 23:59:59"
        ).scalar() or 0.0
        cash_purchase_paid = db.query(func.sum(models.Purchase.paid_amount)).filter(
            models.Purchase.created_at >= start_date + " 00:00:00",
            models.Purchase.created_at <= end_date + " 23:59:59",
            models.Purchase.payment_source == "cash_in_hand",
        ).scalar() or 0.0

        total_supplier_paid = manual_sup_total + purchase_paid_total
        reversal_all = salary_reversal_total(db, start_date, end_date)
        reversal_cash = salary_reversal_total(db, start_date, end_date, cash_only=True)
        total_expenses_val = (
            sum(float(e.amount or 0) for e in expenses_list)
            + sum(float(e.amount or 0) for e in cash_expenses_list)
            - reversal_all
        )
        cash_expenses_val = (
            sum(float(e.amount or 0) for e in expenses_list if (e.payment_source or "cash_in_hand") == "cash_in_hand")
            + sum(float(e.amount or 0) for e in cash_expenses_list if (e.account or "cash_in_hand") == "cash_in_hand")
            - reversal_cash
        )

        expected_cash = (
            sales_cash_total
            + float(cash_customer_received)
            - float(cash_manual_supplier_paid)
            - float(cash_purchase_paid)
            - float(cash_expenses_val)
        )

        total_cogs_sold = db.query(
            func.sum(models.SaleItem.qty * models.SaleItem.buy_price)
        ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
            models.Sale.status.in_(SALE_STATUS_ACTIVE),
            models.Sale.created_at >= start_date + " 00:00:00",
            models.Sale.created_at <= end_date + " 23:59:59"
        ).scalar() or 0.0
        total_cogs = float(total_cogs_sold) - _period_return_cost(db, start_date, end_date)

        net_profit = (gross_sales - total_cogs) - total_expenses_val

        return {
            "summary": {
                "gross_sales": round(float(gross_sales), 2),
                "total_expenses": round(float(total_expenses_val), 2),
                "net_profit": round(float(net_profit), 2),
                "sales_cash_collected": round(float(sales_cash_total), 2),
                "customer_payments_total": round(float(total_customer_received), 2),
                "supplier_payments_total": round(float(total_supplier_paid), 2),
                "expected_cash": round(float(expected_cash), 2),
                "period": f"{start_date} to {end_date}"
            },
            "customer_payments": [
                {"customer": p.customer.name if p.customer else "Walk-in", "amount": p.amount, "date": p.created_at, "note": p.note}
                for p in customer_payments
            ],
            "expenses": [
                {"category": e.category, "amount": e.amount, "note": e.note, "date": e.date}
                for e in expenses_list
            ] + [
                {"category": e.category or e.cash_out_type.capitalize(), "amount": e.amount, "note": e.notes, "date": e.created_at}
                for e in cash_expenses_list
            ],
            "supplier_payments": [
                {"supplier": p.supplier.name if p.supplier else "Unknown", "amount": p.paid_amount, "date": p.created_at, "ref": f"Bill: {p.bill_no}"}
                for p in purchases
            ] + [
                {"supplier": p.supplier.name if p.supplier else "Unknown", "amount": p.amount, "date": p.created_at, "ref": p.note or "Manual Payment"}
                for p in manual_supplier_payments
            ],
            "credit_sales": [
                {
                    "invoice_no": s.invoice_no,
                    "customer": s.customer.name if s.customer else "Walk-in",
                    "total": round(max(0.0, float(s.total or 0) - _sale_return_amount(s)), 2),
                    "paid": round(max(0.0, float(s.paid_amount or 0) - _sale_paid_return_amount(s)), 2),
                    "due": round(max(
                        0.0,
                        float(s.total or 0) - _sale_return_amount(s)
                        - max(0.0, float(s.paid_amount or 0) - _sale_paid_return_amount(s)),
                    ), 2),
                    "date": s.created_at,
                }
                for s in credit_sales
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching audit details: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Audit details failed: {str(e)}")
