"""
Sales router - Create, list, return invoices, cart logic, stock deduction, and unified cash book integration.
"""

import logging
import io
import csv
import json
import hashlib
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import func, extract, update
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db, SessionLocal
from auth import get_current_user, require_admin
import models
from services import backup_service
from services.employee_service import (
    EmployeeDomainError,
    money as employee_money,
    post_goods_on_credit,
    post_goods_return,
)
from services.sale_return_service import (
    POSTED_SALE_STATUSES,
    SaleReturnDomainError,
    find_retry as find_return_retry,
    make_return_key,
    money as return_money,
    paid_refund_amount,
    post_sale_return,
)

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

logger = logging.getLogger("smartvyapar.sales")
logger.setLevel(logging.INFO)

router = APIRouter()
sale_lock = threading.Lock()


# ── Helper Functions ──────────────────────────────────────────────────────────

def to_dec(val) -> Decimal:
    if val is None:
        return Decimal('0.0')
    return Decimal(str(val))


def _require_sale_permission(current_user: models.User, allowed, detail: str):
    """Enforce the same page permissions exposed by the desktop application."""
    if (current_user.role or "").lower() == "admin":
        return current_user
    permissions = {
        item.strip().lower()
        for item in (current_user.permissions or "").split(",")
        if item.strip()
    }
    if permissions.intersection(allowed):
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_billing_access(current_user: models.User = Depends(get_current_user)):
    return _require_sale_permission(
        current_user, {"billing"}, "POS Billing permission required"
    )


def require_sales_return_access(current_user: models.User = Depends(get_current_user)):
    return _require_sale_permission(
        current_user, {"sales-list"}, "Sales permission required to post returns"
    )


def _next_invoice_no(db: Session) -> str:
    setting = db.query(models.Setting).filter_by(key="invoice_prefix").first()
    prefix = setting.value if setting else "INV"
    
    max_id = db.query(func.max(models.Sale.id)).scalar() or 0
    next_num = max_id + 1
    
    while True:
        inv_no = f"{prefix}-{next_num:05d}"
        exists = db.query(models.Sale).filter_by(invoice_no=inv_no).first()
        if not exists:
            return inv_no
        next_num += 1


def _trigger_auto_backup():
    try:
        from database import get_db_path, get_backup_dir
        backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
    except Exception as e:
        logger.warning(f"Auto-backup warning: {e}")


def _trigger_firebase_sync(sale_status: str):
    if sale_status in POSTED_SALE_STATUSES:
        try:
            from services.firebase_sync import firebase_sync
            if firebase_sync.is_enabled():
                _fb_db = SessionLocal()
                firebase_sync.trigger_dashboard_sync(_fb_db)
        except Exception as e:
            logger.warning(f"Firebase sync trigger warning: {e}")


def _sale_party_name(sale: models.Sale) -> str:
    if getattr(sale, "employee_id", None) and getattr(sale, "employee", None):
        return f"Employee: {sale.employee.full_name}"
    return sale.customer.name if sale.customer else "Walk-in"


def _cash_refunded_for_sale(db: Session, sale_id: int) -> Decimal:
    """Return the exact cashbook refund already paid for immutable returns."""
    amount = db.query(func.coalesce(func.sum(models.CashTransaction.amount), 0)).join(
        models.SaleReturn,
        models.SaleReturn.cash_transaction_id == models.CashTransaction.id,
    ).filter(
        models.SaleReturn.sale_id == sale_id,
        models.CashTransaction.tx_type == "cash_out",
        models.CashTransaction.cash_out_type == "refund",
        models.CashTransaction.account == "cash_in_hand",
    ).scalar()
    return return_money(amount or 0)


def _settle_posted_return(
    db: Session,
    *,
    sale: models.Sale,
    posted_return: models.SaleReturn,
    refund_amount: Decimal,
    created_by: str,
    description: str,
) -> tuple[Decimal, Decimal]:
    """Split a return between refunded tender and reversed receivable."""
    method = str(sale.payment_method or "cash").strip().lower()
    economic_amount = return_money(refund_amount)
    cash_refund = Decimal("0.00")
    receivable_reduction = Decimal("0.00")

    if method in {"cash", "mixed", "credit"} and economic_amount > 0:
        remaining_cash = max(
            Decimal("0.00"),
            return_money(sale.paid_amount) - _cash_refunded_for_sale(db, sale.id),
        )
        cash_refund = min(economic_amount, remaining_cash)

    if method in {"credit", "mixed"}:
        receivable_reduction = max(Decimal("0.00"), economic_amount - cash_refund)

    if receivable_reduction > 0:
        if not sale.customer_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This partially paid sale must be linked to a customer before its unpaid value can be returned",
            )
        customer = db.query(models.Customer).filter_by(id=sale.customer_id).first()
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The customer linked to this partially paid sale no longer exists",
            )
        customer.credit_balance = float(max(
            Decimal("0.00"),
            return_money(customer.credit_balance) - receivable_reduction,
        ))

    if cash_refund > 0:
        cust_name = sale.customer.name if sale.customer else "Walk-in"
        cash_tx = models.CashTransaction(
            tx_type="cash_out",
            cash_out_type="refund",
            amount=float(cash_refund),
            account="cash_in_hand",
            paid_to=cust_name,
            reference_type="sale_return",
            reference_id=posted_return.id,
            reference_no=f"{sale.invoice_no}-RTN-{posted_return.id}",
            notes=(
                f"{description}; invoice return {economic_amount:.2f}, "
                f"cash refund {cash_refund:.2f}, receivable reversal {receivable_reduction:.2f}"
            ),
            created_by=created_by,
            created_at=datetime.now(),
        )
        db.add(cash_tx)
        db.flush()
        posted_return.cash_transaction_id = cash_tx.id

    return cash_refund, receivable_reduction


def _employee_price_mode(db: Session) -> str:
    setting = db.query(models.Setting).filter_by(key="employee_price_mode").first()
    return "employee" if setting and str(setting.value).strip().lower() == "employee" else "retail"


def _invoice_refund_factor(sale: models.Sale) -> Decimal:
    """Allocate invoice-level discount proportionally across returned lines."""
    line_total = sum((to_dec(item.total) for item in sale.items), Decimal("0.00"))
    if line_total <= 0:
        return Decimal("0.00")
    return max(Decimal("0.00"), to_dec(sale.total) / line_total)


def _fingerprint_decimal(value) -> str:
    normalized = to_dec(value).normalize()
    return "0" if normalized == 0 else str(normalized)


def _sale_request_fingerprint(data, payment_method, sale_status, paid_dec, tendered_dec) -> str:
    payload = {
        "customer_id": data.customer_id,
        "employee_id": data.employee_id if payment_method == "employee_credit" else None,
        "payment_method": payment_method,
        "status": sale_status,
        "discount": _fingerprint_decimal(data.discount),
        "paid_amount": _fingerprint_decimal(paid_dec),
        "amount_tendered": _fingerprint_decimal(tendered_dec),
        "notes": (data.notes or "").strip(),
        "items": sorted([
            {
                "product_id": item.product_id,
                "qty": _fingerprint_decimal(item.qty),
                "price": _fingerprint_decimal(item.price),
                "discount": _fingerprint_decimal(item.discount),
                "tax_pct": _fingerprint_decimal(item.tax_pct),
            }
            for item in data.items
        ], key=lambda item: item["product_id"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sale_record_fingerprint(sale: models.Sale) -> str:
    payload = {
        "customer_id": sale.customer_id,
        "employee_id": getattr(sale, "employee_id", None),
        "payment_method": str(sale.payment_method or "cash").lower(),
        "status": str(sale.status or "").lower(),
        "discount": _fingerprint_decimal(sale.discount),
        "paid_amount": _fingerprint_decimal(sale.paid_amount),
        "amount_tendered": _fingerprint_decimal(sale.amount_tendered),
        "notes": (sale.notes or "").strip(),
        "items": sorted([
            {
                "product_id": item.product_id,
                "qty": _fingerprint_decimal(item.qty),
                "price": _fingerprint_decimal(item.price),
                "discount": _fingerprint_decimal(item.discount),
                "tax_pct": _fingerprint_decimal(item.tax_pct),
            }
            for item in sale.items
        ], key=lambda item: item["product_id"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _format_sale(s: models.Sale) -> dict:
    return {
        "id": s.id,
        "invoice_no": s.invoice_no,
        "customer_id": s.customer_id,
        "customer": _sale_party_name(s),
        "employee_id": getattr(s, "employee_id", None),
        "employee": s.employee.full_name if getattr(s, "employee", None) else None,
        "subtotal": s.subtotal,
        "discount": s.discount,
        "tax_amount": s.tax_amount,
        "total": s.total,
        "paid_amount": s.paid_amount,
        "amount_tendered": s.amount_tendered,
        "change_returned": s.change_returned,
        "payment_method": s.payment_method,
        "status": s.status,
        "cashier": s.cashier,
        "notes": s.notes,
        "date": s.created_at,
        "items": [
            {
                "product_id": i.product_id,
                "product_name": i.product_name,
                "qty": i.qty,
                "price": i.price,
                "discount": i.discount,
                "tax_pct": i.tax_pct,
                "total": i.total,
                "returned_qty": i.returned_qty,
                "buy_price": i.product.buy_price if i.product else 0
            }
            for i in s.items
        ]
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class SaleItemIn(BaseModel):
    product_id: int = Field(..., gt=0, description="Product ID")
    qty: float = Field(..., gt=0, description="Quantity")
    price: float = Field(..., ge=0, description="Unit price")
    product_name: Optional[str] = Field(None, description="Custom product snapshot name")
    discount: float = Field(0.0, ge=0, description="Line item discount")
    tax_pct: float = Field(0.0, ge=0, description="Line item tax percentage")


class SaleCreate(BaseModel):
    customer_id: Optional[int] = Field(None, description="Customer ID")
    employee_id: Optional[int] = Field(None, gt=0, description="Employee ID for employee-credit sales")
    items: List[SaleItemIn] = Field(..., min_items=1, description="Cart items")
    discount: float = Field(0.0, ge=0, description="Overall sale discount")
    payment_method: str = Field("cash", description="Payment method: cash | card | credit | mixed | employee_credit")
    paid_amount: float = Field(0.0, ge=0, description="Settled paid amount")
    amount_tendered: Optional[float] = Field(0.0, ge=0, description="Customer tendered cash")
    change_returned: Optional[float] = Field(0.0, ge=0, description="Returned change")
    notes: Optional[str] = Field("", max_length=500, description="Sale notes")
    status: str = Field("completed", description="Sale status: completed | held")
    idempotency_key: Optional[str] = Field(None, max_length=160, description="Stable checkout retry token")


class ReturnItem(BaseModel):
    product_id: int = Field(..., gt=0)
    qty: float = Field(..., gt=0)


class SaleNextInvoiceResponse(BaseModel):
    next_invoice_no: str


class SaleListItem(BaseModel):
    id: int
    invoice_no: str
    customer: str
    employee_id: Optional[int] = None
    employee: Optional[str] = None
    subtotal: float
    total: float
    paid_amount: float
    payment_method: str
    status: str
    date: datetime
    profit: float


class SaleListResponse(BaseModel):
    items: List[SaleListItem]
    total: int


class SaleItemDetail(BaseModel):
    product_id: int
    product_name: str
    qty: float
    price: float
    discount: float
    tax_pct: float
    total: float
    returned_qty: float
    buy_price: float


class SaleDetailResponse(BaseModel):
    id: int
    invoice_no: str
    customer_id: Optional[int] = None
    customer: str
    employee_id: Optional[int] = None
    employee: Optional[str] = None
    subtotal: float
    discount: float
    tax_amount: float
    total: float
    paid_amount: float
    amount_tendered: Optional[float] = 0.0
    change_returned: Optional[float] = 0.0
    payment_method: str
    status: str
    cashier: Optional[str] = None
    notes: Optional[str] = ""
    date: datetime
    items: List[SaleItemDetail]


class SaleBarcodeSearchItem(BaseModel):
    sale_id: int
    invoice_no: str
    customer: str
    date: str
    qty_sold: float
    returned_qty: float
    total: float
    price: float


class SaleBarcodeSearchResponse(BaseModel):
    product_id: int
    product_name: str
    product_barcode: Optional[str] = None
    sales: List[SaleBarcodeSearchItem]


class SaleDeleteResponse(BaseModel):
    success: bool
    message: Optional[str] = None


class FullReturnResponse(BaseModel):
    success: bool
    message: Optional[str] = None


class PartialReturnResponse(BaseModel):
    success: bool
    refund_total: float
    status: str


class RetroactiveRefundResponse(BaseModel):
    success: bool
    added_count: int
    details: List[Dict[str, Any]]


class TodaySummaryResponse(BaseModel):
    count: int
    total: float
    paid: float
    due: float


class SalesHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Health Check & Utility Endpoints ──────────────────────────────────────────

@router.get("/health", response_model=SalesHealthCheckResponse)
@router.get("/sales/health", response_model=SalesHealthCheckResponse)
@limiter.limit("1200/minute")
def sales_health(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    """Health check endpoint for sales module."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "sales",
        "endpoints": [
            "/next-number",
            "/today/summary",
            "/export",
            "/by-invoice/{inv_no}",
            "/by-product-barcode/{barcode}",
            "/health",
            "/",
            "/{sale_id}",
            "/{sale_id}/return",
            "/{sale_id}/return-items",
            "/retroactive-refunds"
        ]
    }


@router.get("/next-number", response_model=SaleNextInvoiceResponse)
@limiter.limit("1200/minute")
def get_next_invoice_number(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Generate next unique invoice number."""
    try:
        return {"next_invoice_no": _next_invoice_no(db)}
    except Exception as e:
        logger.error(f"Error fetching next invoice number: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate invoice number: {str(e)}"
        )


@router.get("/today/summary", response_model=TodaySummaryResponse)
@limiter.limit("1200/minute")
def today_summary(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get aggregated summary metrics for sales completed today."""
    try:
        today = date.today().isoformat()
        sales = db.query(models.Sale).options(
            joinedload(models.Sale.returns)
        ).filter(
            models.Sale.status.in_(["completed", "partially_returned", "returned"]),
            func.date(models.Sale.created_at) == today
        ).all()
        returns_today = db.query(models.SaleReturn).options(
            joinedload(models.SaleReturn.cash_transaction)
        ).filter(
            func.date(models.SaleReturn.posted_at) == today
        ).all()
        returned_today = sum(float(posted.refund_amount or 0) for posted in returns_today)
        paid_returned_today = sum(float(paid_refund_amount(posted)) for posted in returns_today)
        total = round(sum(float(s.total or 0) for s in sales) - returned_today, 2)
        paid = round(sum(float(s.paid_amount or 0) for s in sales if s.payment_method != "employee_credit") - paid_returned_today, 2)
        due = 0.0
        for sale in sales:
            if sale.payment_method == "employee_credit":
                continue
            returned_for_sale = sum(float(posted.refund_amount or 0) for posted in sale.returns)
            net_total = max(0.0, float(sale.total or 0) - returned_for_sale)
            refunded_tender = sum(float(paid_refund_amount(posted)) for posted in sale.returns)
            net_paid = max(0.0, float(sale.paid_amount or 0) - refunded_tender)
            due += max(0.0, net_total - net_paid)
        return {
            "count": len(sales),
            "total": total,
            "paid": paid,
            "due": round(due, 2)
        }
    except Exception as e:
        logger.error(f"Error calculating today sales summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch today's sales summary: {str(e)}"
        )


@router.get("/export")
@router.get("/sales/export")
@limiter.limit("10/minute")
def export_sales(
    request: Request,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Export sales transactions to CSV or Excel format."""
    try:
        logger.info(f"User '{current_user.username}' exporting sales (format={format}, start={start_date}, end={end_date})")
        query = db.query(models.Sale).options(
            joinedload(models.Sale.customer),
            joinedload(models.Sale.employee),
        )
        if start_date:
            query = query.filter(models.Sale.created_at >= start_date + " 00:00:00")
        if end_date:
            query = query.filter(models.Sale.created_at <= end_date + " 23:59:59")

        sales = query.order_by(models.Sale.created_at.desc()).all()

        return_query = db.query(models.SaleReturn).join(models.Sale).options(
            joinedload(models.SaleReturn.sale).joinedload(models.Sale.customer),
            joinedload(models.SaleReturn.sale).joinedload(models.Sale.employee),
            joinedload(models.SaleReturn.cash_transaction),
        )
        if start_date:
            return_query = return_query.filter(models.SaleReturn.posted_at >= start_date + " 00:00:00")
        if end_date:
            return_query = return_query.filter(models.SaleReturn.posted_at <= end_date + " 23:59:59")
        posted_returns = return_query.order_by(models.SaleReturn.posted_at.desc()).all()

        data = []
        for s in sales:
            data.append({
                "Transaction Type": "SALE",
                "Return Ref": "",
                "Sale ID": s.id,
                "Invoice No": s.invoice_no,
                "Customer / Employee": _sale_party_name(s),
                "Subtotal": s.subtotal,
                "Discount": s.discount,
                "Tax": s.tax_amount,
                "Total": s.total,
                "Paid": s.paid_amount,
                "Refund Amount": 0,
                "Payment Method": s.payment_method.upper(),
                "Status": s.status.capitalize(),
                "Cashier": s.cashier or "",
                "Date": s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else ""
            })

        for posted in posted_returns:
            sale = posted.sale
            refund = float(posted.refund_amount or 0)
            payment_method = str(posted.payment_method or sale.payment_method or "").lower()
            returned_tax = sum(float(line.allocated_tax_amount or 0) for line in posted.items)
            data.append({
                "Transaction Type": "RETURN",
                "Return Ref": f"RTN-{posted.id}",
                "Sale ID": sale.id,
                "Invoice No": sale.invoice_no,
                "Customer / Employee": _sale_party_name(sale),
                "Subtotal": round(-(refund - returned_tax), 2),
                "Discount": 0,
                "Tax": round(-returned_tax, 2),
                "Total": -refund,
                "Paid": -float(paid_refund_amount(posted)),
                "Refund Amount": refund,
                "Payment Method": payment_method.upper(),
                "Status": "Return",
                "Cashier": posted.created_by or sale.cashier or "",
                "Date": posted.posted_at.strftime("%Y-%m-%d %H:%M:%S") if posted.posted_at else "",
            })

        if not data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No sales or return records found for export"
            )
        data.sort(key=lambda row: row["Date"], reverse=True)

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
                    "Content-Disposition": f"attachment; filename=sales_{timestamp_str}.csv"
                }
            )
        else:
            try:
                import pandas as pd
                df = pd.DataFrame(data)
                excel_output = io.BytesIO()
                with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, sheet_name='Sales', index=False)
                excel_output.seek(0)
                return StreamingResponse(
                    excel_output,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": f"attachment; filename=sales_{timestamp_str}.xlsx"
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
                        "Content-Disposition": f"attachment; filename=sales_{timestamp_str}.csv"
                    }
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting sales: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export sales: {str(e)}"
        )


@router.get("/by-invoice/{inv_no:path}", response_model=SaleDetailResponse)
@limiter.limit("1200/minute")
def get_sale_by_invoice(
    request: Request,
    inv_no: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Lookup single sale by invoice number."""
    try:
        normalized = inv_no.replace("=", "-")
        s = db.query(models.Sale).filter(
            (func.lower(models.Sale.invoice_no) == func.lower(inv_no)) |
            (func.lower(models.Sale.invoice_no) == func.lower(normalized))
        ).first()

        if not s:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Invoice number '{inv_no}' not found"
            )
        return _format_sale(s)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching invoice {inv_no}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch invoice: {str(e)}"
        )


@router.get("/by-product-barcode/{barcode:path}", response_model=SaleBarcodeSearchResponse)
@limiter.limit("1200/minute")
def get_sales_by_product_barcode(
    request: Request,
    barcode: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Find all sales containing items matching a product barcode or product code."""
    try:
        normalized = barcode.replace("=", "-").strip()
        product = db.query(models.Product).filter(
            (func.lower(models.Product.barcode) == func.lower(normalized)) |
            (func.lower(models.Product.code) == func.lower(normalized))
        ).first()

        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with barcode/code '{barcode}' not found"
            )

        items = db.query(models.SaleItem).options(
            joinedload(models.SaleItem.sale).joinedload(models.Sale.customer),
            joinedload(models.SaleItem.sale).joinedload(models.Sale.employee),
        ).join(models.Sale).filter(
            models.SaleItem.product_id == product.id,
            models.Sale.status.in_(["completed", "partially_returned"])
        ).order_by(models.Sale.created_at.desc()).limit(50).all()

        matching_sales = []
        for item in items:
            sale = item.sale
            if sale:
                matching_sales.append({
                    "sale_id": sale.id,
                    "invoice_no": sale.invoice_no,
                    "customer": _sale_party_name(sale),
                    "date": sale.created_at.isoformat() if isinstance(sale.created_at, datetime) else str(sale.created_at),
                    "qty_sold": item.qty,
                    "returned_qty": item.returned_qty,
                    "total": sale.total,
                    "price": item.price
                })

        return {
            "product_id": product.id,
            "product_name": product.name,
            "product_barcode": product.barcode,
            "sales": matching_sales
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching sales for product barcode '{barcode}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search sales by product barcode: {str(e)}"
        )


# ── Core Sales Endpoints ──────────────────────────────────────────────────────

@router.get("/", response_model=SaleListResponse)
@limiter.limit("1200/minute")
def list_sales(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    customer_id: Optional[int] = Query(None, description="Filter by customer ID"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: completed | held | returned"),
    limit: int = Query(50, ge=1, le=100000, description="Limit results"),
    offset: int = Query(0, ge=0, description="Offset results"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List sales transactions with date, customer, and status filters."""
    try:
        logger.info(f"User '{current_user.username}' listing sales (customer={customer_id}, status={status_filter}, limit={limit}, offset={offset})")
        query = db.query(models.Sale).options(
            joinedload(models.Sale.customer),
            joinedload(models.Sale.employee),
            selectinload(models.Sale.items).joinedload(models.SaleItem.product)
        )
        if start_date:
            query = query.filter(models.Sale.created_at >= start_date + " 00:00:00")
        if end_date:
            query = query.filter(models.Sale.created_at <= end_date + " 23:59:59")
        if customer_id:
            query = query.filter(models.Sale.customer_id == customer_id)
        if status_filter:
            query = query.filter(models.Sale.status == status_filter)

        total_count = query.count()
        sales = query.order_by(models.Sale.created_at.desc()).offset(offset).limit(limit).all()

        items = [
            {
                "id": s.id,
                "invoice_no": s.invoice_no,
                "customer": _sale_party_name(s),
                "employee_id": getattr(s, "employee_id", None),
                "employee": s.employee.full_name if getattr(s, "employee", None) else None,
                "subtotal": s.subtotal,
                "total": s.total,
                "paid_amount": s.paid_amount,
                "payment_method": s.payment_method,
                "status": s.status,
                "date": s.created_at,
                "profit": round(s.total - sum((float(i.buy_price) if (i.buy_price is not None and float(i.buy_price) > 0) else (float(i.product.buy_price) if i.product else 0.0)) * float(i.qty or 0) for i in s.items), 2)
            }
            for s in sales
        ]
        return {"items": items, "total": total_count}
    except Exception as e:
        logger.error(f"Error listing sales: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sales: {str(e)}"
        )


@router.get("/{sale_id}", response_model=SaleDetailResponse)
@limiter.limit("1200/minute")
def get_sale(
    request: Request,
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get single sale details by sale ID."""
    try:
        s = db.query(models.Sale).filter_by(id=sale_id).first()
        if not s:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sale transaction ID {sale_id} not found"
            )
        return _format_sale(s)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sale ID {sale_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch sale details: {str(e)}"
        )


@router.post("/", response_model=SaleDetailResponse)
def create_sale(
    request: Request,
    data: SaleCreate, 
    current_user: models.User = Depends(require_billing_access)
):
    """Process POS cart checkout: validates amounts, generates invoice, deducts stock, updates customer balance, and creates cashbook entry."""
    payment_method = str(data.payment_method or "cash").strip().lower()
    sale_status = str(data.status or "").strip().lower()
    idempotency_key = (data.idempotency_key or request.headers.get("Idempotency-Key") or "").strip()[:160] or None
    if payment_method not in {"cash", "card", "credit", "mixed", "employee_credit"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported payment method")
    if sale_status not in {"completed", "held"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sale status must be completed or held")
    product_ids = [item.product_id for item in data.items]
    if len(product_ids) != len(set(product_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate products must be merged into one cart line")

    employee_price_mode = "retail"
    if payment_method == "employee_credit":
        if not data.employee_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select an active employee for Employee Credit")
        if data.customer_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee Credit cannot also be posted to a customer account")
        if to_dec(data.paid_amount) != 0 or to_dec(data.amount_tendered) != 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee Credit must post the full invoice to the employee ledger")

    elif data.employee_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="employee_id is only valid with Employee Credit")

    if data.amount_tendered is not None and data.amount_tendered < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount tendered cannot be negative")

    if data.paid_amount < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Paid amount cannot be negative")

    subtotal_dec = Decimal('0.0')
    tax_total_dec = Decimal('0.0')
    for item in data.items:
        qty_dec = to_dec(item.qty)
        price_dec = to_dec(item.price)
        disc_dec = to_dec(item.discount)
        tax_pct_dec = to_dec(item.tax_pct)

        if qty_dec < 0 or price_dec < 0 or disc_dec < 0 or tax_pct_dec < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Negative values not allowed in item fields")

        gross_line_dec = price_dec * qty_dec
        if disc_dec > gross_line_dec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line discount cannot exceed line value for product ID {item.product_id}",
            )

        item_subtotal = gross_line_dec - disc_dec
        item_tax = item_subtotal * (tax_pct_dec / Decimal('100.0'))
        subtotal_dec += item_subtotal
        tax_total_dec += item_tax

    discount_dec = to_dec(data.discount)
    if discount_dec < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Discount cannot be negative")

    invoice_value_before_discount = subtotal_dec + tax_total_dec
    if discount_dec > invoice_value_before_discount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice discount cannot exceed subtotal plus tax",
        )

    total_dec = (subtotal_dec + tax_total_dec - discount_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if data.amount_tendered is not None and data.amount_tendered > 0:
        tendered_dec = to_dec(data.amount_tendered).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        if payment_method in {"credit", "employee_credit"}:
            tendered_dec = to_dec(data.paid_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            if data.paid_amount > 0:
                tendered_dec = to_dec(data.paid_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                tendered_dec = total_dec

    if payment_method not in {"credit", "employee_credit"}:
        tendered_dec = max(tendered_dec, to_dec(data.paid_amount))

    change_dec = max(Decimal('0.0'), tendered_dec - total_dec)

    if payment_method == "employee_credit":
        paid_dec = Decimal("0.00")
        tendered_dec = Decimal("0.00")
        change_dec = Decimal("0.00")
    elif payment_method == "credit":
        paid_dec = min(to_dec(data.paid_amount), total_dec)
    else:
        paid_dec = min(tendered_dec, total_dec)

    paid_dec = min(paid_dec, total_dec)
    if payment_method not in {"credit", "employee_credit"}:
        paid_dec = min(paid_dec, tendered_dec)

    if payment_method in {"credit", "mixed"} and total_dec > paid_dec and not data.customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A sale with an unpaid customer balance must be linked to a customer",
        )

    request_fingerprint = _sale_request_fingerprint(
        data,
        payment_method,
        sale_status,
        paid_dec,
        tendered_dec,
    )

    subtotal = float(subtotal_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    tax_total = float(tax_total_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    total = float(total_dec)
    paid = float(paid_dec)
    amount_tendered = float(tendered_dec)
    change_returned = float(change_dec)

    if payment_method == "employee_credit" and sale_status == "completed" and total_dec <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee Credit invoice total must be greater than zero")

    logger.info(f"User '{current_user.username}' processing checkout (total={total}, paid={paid}, method={payment_method})")

    with sale_lock:
        for attempt in range(20):
            db = SessionLocal()
            try:
                if idempotency_key:
                    existing_sale = db.query(models.Sale).filter_by(idempotency_key=idempotency_key).first()
                    if existing_sale:
                        if _sale_record_fingerprint(existing_sale) != request_fingerprint:
                            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Checkout retry token belongs to a different sale")
                        return _format_sale(existing_sale)

                # Employee pricing and eligibility are validated in the same
                # database transaction that posts stock and ledger movements.
                if payment_method == "employee_credit":
                    employee = db.query(models.Employee).filter_by(id=data.employee_id, is_active=True).first()
                    if not employee:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee not found or inactive")
                    employee_price_mode = _employee_price_mode(db)
                    for item in data.items:
                        product = db.query(models.Product).filter_by(id=item.product_id, is_active=True).first()
                        if not product:
                            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product ID {item.product_id} not found")
                        configured = getattr(product, "employee_price", None)
                        expected_price = (
                            to_dec(configured)
                            if employee_price_mode == "employee" and configured is not None and to_dec(configured) > 0
                            else to_dec(product.sell_price)
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        submitted_price = to_dec(item.price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        if submitted_price != expected_price:
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=f"Price changed for {product.name}; refresh the cart (expected {expected_price:.2f})",
                            )

                inv_no = _next_invoice_no(db)
                
                neg_setting = db.query(models.Setting).filter_by(key="negative_stock").first()
                negative_stock_enabled = neg_setting and neg_setting.value == "true"

                sale = models.Sale(
                    invoice_no=inv_no,
                    idempotency_key=idempotency_key,
                    customer_id=data.customer_id,
                    employee_id=data.employee_id if payment_method == "employee_credit" else None,
                    subtotal=subtotal,
                    discount=data.discount,
                    tax_amount=tax_total,
                    total=total,
                    paid_amount=paid,
                    amount_tendered=amount_tendered,
                    change_returned=change_returned,
                    payment_method=payment_method,
                    status=sale_status,
                    cashier=current_user.username,
                    notes=data.notes or "",
                    created_at=datetime.now(),
                )
                db.add(sale)
                db.flush() 

                for item in data.items:
                    product = db.query(models.Product).filter_by(id=item.product_id).first()
                    if not product:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product ID {item.product_id} not found")

                    item_qty_dec = to_dec(item.qty)
                    item_price_dec = to_dec(item.price)
                    item_disc_dec = to_dec(item.discount)
                    item_tax_pct_dec = to_dec(item.tax_pct)
                    
                    i_subtotal_dec = (item_price_dec * item_qty_dec) - item_disc_dec
                    i_tax_dec = i_subtotal_dec * (item_tax_pct_dec / Decimal('100.0'))
                    item_total = float((i_subtotal_dec + i_tax_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
                    
                    si = models.SaleItem(
                        sale_id=sale.id,
                        product_id=item.product_id,
                        product_name=item.product_name or product.name,
                        qty=item.qty,
                        price=item.price,
                        discount=item.discount,
                        tax_pct=item.tax_pct,
                        total=item_total,
                        buy_price=product.buy_price
                    )
                    db.add(si)

                    if sale_status == "completed" and not product.is_service:
                        if negative_stock_enabled:
                            stmt = update(models.Product).where(
                                models.Product.id == item.product_id
                            ).values(stock=models.Product.stock - item.qty)
                        else:
                            stmt = update(models.Product).where(
                                models.Product.id == item.product_id,
                                models.Product.stock >= item.qty
                            ).values(stock=models.Product.stock - item.qty)
                        
                        res = db.execute(stmt)
                        if res.rowcount == 0:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Insufficient stock for {product.name} (Available: {product.stock})"
                            )

                        db.flush()
                        db.refresh(product)
                        
                        sm = models.StockMovement(
                            product_id=product.id,
                            movement_type="sale",
                            qty_change=-item.qty,
                            qty_after=product.stock,
                            note=f"Sale {inv_no}"
                        )
                        db.add(sm)

                # Flush every line before creating the employee-credit snapshot.
                # The helper deliberately does not commit, so sale, stock and
                # ledger either all succeed or all roll back together.
                db.flush()
                if payment_method == "employee_credit" and sale_status == "completed":
                    post_goods_on_credit(
                        db,
                        employee_id=data.employee_id,
                        sale=sale,
                        amount=total_dec,
                        created_by=current_user.username,
                        price_mode=employee_price_mode,
                        idempotency_key=f"employee-credit:sale:{sale.id}",
                        description=f"Goods on employee credit - {inv_no}",
                        metadata={
                            "invoice_no": inv_no,
                            "invoice_discount": str(discount_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
                            "price_mode": employee_price_mode,
                        },
                    )

                if data.customer_id and payment_method in {"credit", "mixed"}:
                    customer = db.query(models.Customer).filter_by(id=data.customer_id).first()
                    if not customer and payment_method in {"credit", "mixed"} and total_dec > paid_dec:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected customer was not found")
                    if customer:
                        credit_diff = max(Decimal("0.00"), total_dec - paid_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        new_balance = (to_dec(customer.credit_balance) + credit_diff).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        customer.credit_balance = float(new_balance)

                if paid > 0 and payment_method in ("cash", "mixed", "credit"):
                    cust_name = ""
                    if data.customer_id:
                        cust = db.query(models.Customer).filter_by(id=data.customer_id).first()
                        if cust:
                            cust_name = cust.name
                    cash_tx = models.CashTransaction(
                        tx_type="cash_in",
                        cash_in_type="cash_sale",
                        amount=paid,
                        account="cash_in_hand",
                        received_from=cust_name or "Walk-in",
                        reference_type="sale",
                        reference_id=sale.id,
                        reference_no=inv_no,
                        notes=data.notes or "",
                        created_by=current_user.username,
                        created_at=datetime.now(),
                    )
                    db.add(cash_tx)

                db.commit()
                result = _format_sale(sale)

                _trigger_auto_backup()
                _trigger_firebase_sync(sale_status)

                return result

            except HTTPException:
                db.rollback()
                raise
            except EmployeeDomainError as e:
                db.rollback()
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            except Exception as e:
                db.rollback()
                error_str = str(e).lower()
                if ("unique" in error_str or "integrityerror" in error_str) and attempt < 19:
                    import time
                    time.sleep(0.01)
                    continue
                logger.error(f"Failed checkout attempt {attempt+1}: {e}", exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Database error creating sale: {str(e)}"
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete sale checkout after multiple retries."
        )


@router.delete("/{sale_id}", response_model=SaleDeleteResponse)
@limiter.limit("10/minute")
def delete_sale(
    request: Request,
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_billing_access)
):
    """Delete a held sale bill (only held bills can be deleted)."""
    try:
        logger.info(f"User '{current_user.username}' deleting held sale ID {sale_id}")
        sale = db.query(models.Sale).filter_by(id=sale_id).first()
        if not sale:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sale ID {sale_id} not found")
        if sale.status != "held":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only held bills can be deleted. Completed sales must be returned."
            )
        
        cash_tx = db.query(models.CashTransaction).filter_by(reference_type="sale", reference_id=sale_id).first()
        if cash_tx:
            db.delete(cash_tx)
            
        db.delete(sale)
        db.commit()

        _trigger_auto_backup()

        return {"success": True, "message": f"Held sale ID {sale_id} deleted successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting sale ID {sale_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete sale: {str(e)}"
        )


@router.post("/{sale_id}/return", response_model=FullReturnResponse)
@limiter.limit("10/minute")
def return_full_sale(
    request: Request,
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_sales_return_access)
):
    """Process a full return for a completed sale, restoring inventory and recording refund."""
    try:
        logger.info(f"User '{current_user.username}' processing full return for sale ID {sale_id}")
        sale = db.query(models.Sale).filter_by(id=sale_id).first()
        if not sale:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sale ID {sale_id} not found for return processing")

        # Full-return callers have historically sent no retry header, so the
        # source sale itself is the stable idempotency boundary.
        return_key = make_return_key(sale.id, "full")
        prior_return = find_return_retry(
            db,
            idempotency_key=return_key,
            sale_id=sale.id,
            return_type="full",
        )
        if prior_return:
            return {"success": True, "message": f"Full return already processed for sale {sale.invoice_no}"}
        if sale.status == "returned":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sale is already fully returned")
        if sale.status not in {"completed", "partially_returned"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only posted sales can be returned")

        return_lines = []
        employee_item_returns = []
        for item in sale.items:
            item_qty_dec = to_dec(item.qty)
            item_returned_qty_dec = to_dec(item.returned_qty)
            remaining_dec = item_qty_dec - item_returned_qty_dec
            if remaining_dec > 0:
                return_lines.append((item, remaining_dec))
                employee_item_returns.append({"product_id": item.product_id, "qty": float(remaining_dec)})

        refund_override = None
        employee_return_key = f"employee-return:sale:{sale.id}:full"
        if str(sale.payment_method or "").lower() == "employee_credit":
            credit = db.query(models.EmployeeGoodsCredit).filter_by(sale_id=sale.id).first()
            if not credit:
                raise EmployeeDomainError("Employee credit record is missing for this sale")
            refund_override = (employee_money(credit.total) - employee_money(credit.returned_amount)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        posted_return = post_sale_return(
            db,
            sale=sale,
            lines=return_lines,
            idempotency_key=return_key,
            return_type="full",
            created_by=current_user.username,
            final_return=True,
            refund_amount_override=refund_override,
            notes=f"Full return of {sale.invoice_no}",
        )
        refund_amount_dec = return_money(posted_return.refund_amount)

        # Every linked side effect is posted in this same database transaction.
        for item, remaining_dec in return_lines:
            product = db.query(models.Product).filter_by(id=item.product_id).first()
            if product and not product.is_service:
                product.stock = float(to_dec(product.stock) + remaining_dec)
                db.flush()
                db.refresh(product)
                db.add(models.StockMovement(
                    product_id=product.id,
                    movement_type="return",
                    qty_change=float(remaining_dec),
                    qty_after=product.stock,
                    note=f"Full Return {sale.invoice_no} / RTN-{posted_return.id}",
                ))
            item.returned_qty = item.qty

        if str(sale.payment_method or "").lower() == "employee_credit":
            post_goods_return(
                db,
                sale_id=sale.id,
                amount=refund_amount_dec,
                created_by=current_user.username,
                reference_no=f"{sale.invoice_no}-RETURN-FULL",
                idempotency_key=employee_return_key,
                item_returns=employee_item_returns,
                description=f"Full goods return against {sale.invoice_no}",
                metadata={"return_type": "full", "invoice_no": sale.invoice_no, "sale_return_id": posted_return.id},
            )

        refund_amount = float(refund_amount_dec)
        
        if sale.payment_method != "employee_credit":
            _settle_posted_return(
                db,
                sale=sale,
                posted_return=posted_return,
                refund_amount=refund_amount_dec,
                created_by=current_user.username,
                description=f"Refund for full return of sale {sale.invoice_no}",
            )

        sale.status = "returned"
        db.commit()

        _trigger_auto_backup()
        _trigger_firebase_sync(sale.status)

        return {"success": True, "message": f"Full return processed for sale {sale.invoice_no}"}
    except HTTPException:
        db.rollback()
        raise
    except EmployeeDomainError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except SaleReturnDomainError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        db.rollback()
        logger.error(f"Error processing full return for sale ID {sale_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing full return: {str(e)}"
        )


@router.post("/{sale_id}/return-items", response_model=PartialReturnResponse)
@limiter.limit("10/minute")
def return_partial_items(
    request: Request,
    sale_id: int,
    items_to_return: List[ReturnItem], 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_sales_return_access)
):
    """Process partial return for specific items in a sale, restoring stock and logging refund."""
    try:
        logger.info(f"User '{current_user.username}' processing partial return for sale ID {sale_id}")
        sale = db.query(models.Sale).filter_by(id=sale_id).first()
        if not sale:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sale ID {sale_id} not found")
        is_employee_credit = str(sale.payment_method or "").lower() == "employee_credit"
        return_token = (request.headers.get("Idempotency-Key") or "").strip()[:100]
        canonical_return_items = sorted(
            [
                {"product_id": item.product_id, "qty": str(to_dec(item.qty).normalize())}
                for item in items_to_return
            ],
            key=lambda item: item["product_id"],
        )

        if not return_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Partial returns require an Idempotency-Key",
            )
        return_key = make_return_key(sale.id, "partial", return_token)
        employee_return_key = f"employee-return:sale:{sale.id}:partial:{return_token}"[:160]

        if len({item.product_id for item in items_to_return}) != len(items_to_return):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate products must be merged into one return line")
        resolved_lines = []
        for r_item in items_to_return:
            sale_item = next((item for item in sale.items if item.product_id == r_item.product_id), None)
            if not sale_item:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product ID {r_item.product_id} not found in this sale")
            resolved_lines.append((sale_item, to_dec(r_item.qty)))

        try:
            prior_return = find_return_retry(
                db,
                idempotency_key=return_key,
                sale_id=sale.id,
                return_type="partial",
                lines=resolved_lines,
            )
        except SaleReturnDomainError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if prior_return:
            return {
                "success": True,
                "refund_total": float(return_money(prior_return.refund_amount)),
                "status": sale.status,
            }
        if sale.status == "returned":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Full sale is already returned")
        if sale.status not in {"completed", "partially_returned"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only posted sales can be returned")

        projected_returns = {item.id: to_dec(item.returned_qty) for item in sale.items}
        employee_item_returns = []
        for si, qty_to_return_dec in resolved_lines:
            si_qty_dec = to_dec(si.qty)
            si_returned_qty_dec = to_dec(si.returned_qty)
            can_return_dec = si_qty_dec - si_returned_qty_dec
            if qty_to_return_dec > can_return_dec:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot return {float(qty_to_return_dec)} units of product ID {si.product_id}. Max available: {float(can_return_dec)}"
                )
            employee_item_returns.append({"product_id": si.product_id, "qty": float(qty_to_return_dec)})
            projected_returns[si.id] = si_returned_qty_dec + qty_to_return_dec

        all_returned = all(projected_returns[item.id] >= to_dec(item.qty) for item in sale.items)

        refund_override = None
        if is_employee_credit:
            credit = db.query(models.EmployeeGoodsCredit).filter_by(sale_id=sale.id).first()
            if not credit:
                raise EmployeeDomainError("Employee credit record is missing for this sale")
            remaining_credit = (employee_money(credit.total) - employee_money(credit.returned_amount)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if all_returned:
                refund_override = remaining_credit
            if remaining_credit <= 0:
                raise EmployeeDomainError("These returned items have no remaining employee-credit value")

        posted_return = post_sale_return(
            db,
            sale=sale,
            lines=resolved_lines,
            idempotency_key=return_key,
            return_type="partial",
            created_by=current_user.username,
            final_return=all_returned,
            refund_amount_override=refund_override,
            notes=f"Partial return of {sale.invoice_no}",
        )
        refund_total_dec = return_money(posted_return.refund_amount)

        for si, qty_to_return_dec in resolved_lines:
            product = db.query(models.Product).filter_by(id=si.product_id).first()
            if product and not product.is_service:
                product.stock = float(to_dec(product.stock) + qty_to_return_dec)
                db.flush()
                db.refresh(product)
                db.add(models.StockMovement(
                    product_id=product.id,
                    movement_type="return",
                    qty_change=float(qty_to_return_dec),
                    qty_after=product.stock,
                    note=f"Partial Return {sale.invoice_no} / RTN-{posted_return.id}",
                ))
            si.returned_qty = float(to_dec(si.returned_qty) + qty_to_return_dec)

        if is_employee_credit:
            return_ref = f"{sale.invoice_no}-RETURN-{return_token[-24:]}"[:80]
            post_goods_return(
                db,
                sale_id=sale.id,
                amount=refund_total_dec,
                created_by=current_user.username,
                reference_no=return_ref,
                idempotency_key=employee_return_key,
                item_returns=employee_item_returns,
                description=f"Partial goods return against {sale.invoice_no}",
                metadata={
                    "return_type": "partial",
                    "invoice_no": sale.invoice_no,
                    "items": canonical_return_items,
                    "sale_return_id": posted_return.id,
                },
            )

        refund_total = float(refund_total_dec)

        if not is_employee_credit:
            _settle_posted_return(
                db,
                sale=sale,
                posted_return=posted_return,
                refund_amount=refund_total_dec,
                created_by=current_user.username,
                description=f"Refund for partial return of sale {sale.invoice_no}",
            )

        sale.status = "returned" if all_returned else "partially_returned"
        
        db.commit()
        db.refresh(sale)
        
        _trigger_auto_backup()
        _trigger_firebase_sync(sale.status)

        return {"success": True, "refund_total": refund_total, "status": sale.status}
    except HTTPException:
        db.rollback()
        raise
    except EmployeeDomainError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except SaleReturnDomainError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        db.rollback()
        logger.error(f"Error processing partial return for sale ID {sale_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing partial return: {str(e)}"
        )


@router.post("/retroactive-refunds", response_model=RetroactiveRefundResponse)
@limiter.limit("10/minute")
def fix_missing_refunds(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Link or create missing cashbook rows from authoritative return documents."""
    try:
        logger.info(f"User '{current_user.username}' initiating retroactive refund sync")
        returns = db.query(models.SaleReturn).options(
            joinedload(models.SaleReturn.sale).joinedload(models.Sale.customer)
        ).filter(
            models.SaleReturn.payment_method.in_(["cash", "mixed", "credit"]),
            models.SaleReturn.refund_amount > 0,
            models.SaleReturn.cash_transaction_id.is_(None),
            models.SaleReturn.idempotency_key.like("legacy-sale-return:%"),
        ).order_by(models.SaleReturn.id).all()
        added_count = 0
        details = []
        changed = False
        for posted_return in returns:
            sale = posted_return.sale
            refund_total = float(return_money(posted_return.refund_amount))
            existing = db.query(models.CashTransaction).filter_by(
                reference_type="sale_return",
                reference_id=posted_return.id,
            ).first()
            if not existing:
                # Pre-document releases used source sale id + invoice number.
                # Link that exact legacy row rather than issuing a duplicate.
                existing_old = db.query(models.CashTransaction).filter(
                    models.CashTransaction.reference_id == sale.id,
                    models.CashTransaction.reference_no == sale.invoice_no,
                    models.CashTransaction.tx_type == "cash_out",
                    models.CashTransaction.cash_out_type == "refund",
                    models.CashTransaction.amount == refund_total,
                ).first()
                if existing_old:
                    posted_return.cash_transaction_id = existing_old.id
                    changed = True
                    continue

                remaining_cash = max(
                    Decimal("0.00"),
                    return_money(sale.paid_amount) - _cash_refunded_for_sale(db, sale.id),
                )
                cash_refund = min(return_money(refund_total), remaining_cash)
                if cash_refund <= 0:
                    continue
                cust_name = sale.customer.name if sale.customer else "Walk-in"
                cash_tx = models.CashTransaction(
                    tx_type="cash_out",
                    cash_out_type="refund",
                    amount=float(cash_refund),
                    account="cash_in_hand",
                    paid_to=cust_name,
                    reference_type="sale_return",
                    reference_id=posted_return.id,
                    reference_no=f"{sale.invoice_no}-RTN-{posted_return.id}",
                    notes=f"Retroactive refund logging for return of sale {sale.invoice_no}",
                    created_by="system_fix",
                    created_at=posted_return.posted_at,
                )
                db.add(cash_tx)
                db.flush()
                posted_return.cash_transaction_id = cash_tx.id
                changed = True
                added_count += 1
                details.append({
                    "sale_id": sale.id,
                    "invoice_no": sale.invoice_no,
                    "amount": float(cash_refund),
                    "date": posted_return.posted_at.isoformat() if hasattr(posted_return.posted_at, "isoformat") else str(posted_return.posted_at)
                })
            else:
                posted_return.cash_transaction_id = existing.id
                changed = True

        if changed:
            db.commit()
            
        return {"success": True, "added_count": added_count, "details": details}
    except Exception as e:
        db.rollback()
        logger.error(f"Error retroactively logging refunds: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retroactively logging refunds: {str(e)}"
        )
