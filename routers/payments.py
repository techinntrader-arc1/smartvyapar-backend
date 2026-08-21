"""
Payments router - Record payments received from customers or paid to suppliers.
Auto-creates CashTransaction entries for cash payments — unified cash book.
"""

import logging
import io
import csv
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
import models

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

logger = logging.getLogger("smartvyapar.payments")
logger.setLevel(logging.INFO)

router = APIRouter()

PAYMENT_METHOD_ALIASES = {
    "cash": "cash",
    "bank": "bank",
    "bank transfer": "bank",
    "transfer": "bank",
    "check": "check",
    "cheque": "check",
    "card": "online",
    "pos": "online",
    "online": "online",
    "online payment": "online",
}


def normalize_payment_method(value: str) -> str:
    normalized = " ".join(str(value or "cash").strip().lower().split())
    method = PAYMENT_METHOD_ALIASES.get(normalized)
    if not method:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment method must be Cash, Bank Transfer, Check, or Online Payment",
        )
    return method


# ── Schemas ──────────────────────────────────────────────────────────────

class PaymentCreate(BaseModel):
    party_type: str = Field(..., description="Party type: customer | supplier")
    party_id: int = Field(..., gt=0, description="Party ID")
    amount: float = Field(..., gt=0, description="Payment amount in PKR")
    payment_type: str = Field(..., description="Payment direction: received | paid")
    method: str = Field("cash", description="Payment method: cash | bank | check | card | online")
    note: Optional[str] = Field("", max_length=500, description="Optional note")


class PartyShortInfo(BaseModel):
    id: int
    name: str

    class Config:
        orm_mode = True


class PaymentResponse(BaseModel):
    id: int
    party_type: str
    party_id: int
    amount: float
    payment_type: str
    method: str
    note: Optional[str] = ""
    created_at: datetime
    customer: Optional[PartyShortInfo] = None
    supplier: Optional[PartyShortInfo] = None

    class Config:
        orm_mode = True


class PaymentCreateResponse(BaseModel):
    id: int
    party_type: str
    party_id: int
    amount: float
    payment_type: str
    method: str
    note: Optional[str] = ""
    created_at: datetime
    party_name: str
    new_balance: float
    cash_transaction_id: Optional[int] = None
    customer: Optional[PartyShortInfo] = None
    supplier: Optional[PartyShortInfo] = None

    class Config:
        orm_mode = True


class PaymentListResponse(BaseModel):
    total: int
    items: List[PaymentResponse]
    limit: int
    offset: int


class PaymentDeleteResponse(BaseModel):
    success: bool
    message: str


class PaymentSummaryItem(BaseModel):
    party_type: str
    payment_type: str
    total_amount: float
    count: int


class PaymentSummaryResponse(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    total_received: float
    total_paid: float
    summary: List[PaymentSummaryItem]


class PaymentDetailResponse(BaseModel):
    id: int
    party_type: str
    party_id: int
    party_name: str
    amount: float
    payment_type: str
    method: str
    note: Optional[str] = ""
    created_at: datetime
    has_cash_tx: bool = False
    cash_tx_id: Optional[int] = None


class PaymentsHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Helper Functions ──────────────────────────────────────────────────────────

def to_dec(val) -> Decimal:
    if val is None:
        return Decimal('0.0')
    return Decimal(str(val))


def _auto_cash_tx(db: Session, payment: models.Payment, party_name: str, user: str) -> Optional[int]:
    """
    Auto-create a CashTransaction for cash payments.
    - Customer payment received (cash) -> cash_in
    - Supplier payment paid (cash) -> cash_out
    Bank/card payments are NOT recorded in the cash book.
    Returns the created CashTransaction ID if applicable.
    """
    method = (payment.method or "").lower()
    if method not in ("cash",):
        return None

    now_dt = datetime.now()

    if payment.party_type == "customer" and payment.payment_type == "received":
        tx = models.CashTransaction(
            tx_type="cash_in",
            cash_in_type="customer_payment",
            amount=payment.amount,
            account="cash_in_hand",
            received_from=party_name,
            reference_type="payment",
            reference_id=payment.id,
            reference_no=f"PAY-{payment.id}",
            notes=payment.note or "",
            created_by=user,
            created_at=now_dt,
        )
        db.add(tx)
        db.flush()
        return tx.id

    elif payment.party_type == "supplier" and payment.payment_type == "paid":
        tx = models.CashTransaction(
            tx_type="cash_out",
            cash_out_type="supplier_payment",
            amount=payment.amount,
            account="cash_in_hand",
            paid_to=party_name,
            reference_type="payment",
            reference_id=payment.id,
            reference_no=f"PAY-{payment.id}",
            notes=payment.note or "",
            created_by=user,
            created_at=now_dt,
        )
        db.add(tx)
        db.flush()
        return tx.id

    return None


def _trigger_auto_backup():
    try:
        from database import get_db_path, get_backup_dir
        from services import backup_service
        backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
    except Exception as e:
        logger.warning(f"Auto-backup warning: {e}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health", response_model=PaymentsHealthCheckResponse)
@limiter.limit("1200/minute")
def payments_health(request: Request):
    """Health check endpoint for payments router."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "payments",
        "endpoints": [
            "/summary",
            "/export",
            "/health",
            "/",
            "/{payment_id}",
            "/{payment_id}/details"
        ]
    }


@router.get("/summary", response_model=PaymentSummaryResponse)
@limiter.limit("1200/minute")
def payment_summary(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get payment summary grouped by party type and payment direction."""
    try:
        logger.info(f"User '{current_user.username}' fetching payment summary (start={start_date}, end={end_date})")
        query = db.query(
            models.Payment.party_type,
            models.Payment.payment_type,
            func.sum(models.Payment.amount).label("total"),
            func.count(models.Payment.id).label("count")
        )

        if start_date:
            query = query.filter(func.date(models.Payment.created_at) >= start_date)
        if end_date:
            query = query.filter(func.date(models.Payment.created_at) <= end_date)

        rows = query.group_by(models.Payment.party_type, models.Payment.payment_type).all()

        total_received = 0.0
        total_paid = 0.0
        summary_list = []

        for r in rows:
            amt = float(r.total or 0)
            cnt = int(r.count or 0)
            ptype = (r.party_type or "").lower()
            pay_dir = (r.payment_type or "").lower()

            if pay_dir == "received":
                total_received += amt
            elif pay_dir == "paid":
                total_paid += amt

            summary_list.append({
                "party_type": ptype,
                "payment_type": pay_dir,
                "total_amount": round(amt, 2),
                "count": cnt
            })

        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_received": round(total_received, 2),
            "total_paid": round(total_paid, 2),
            "summary": summary_list
        }
    except Exception as e:
        logger.error(f"Error calculating payment summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate payment summary: {str(e)}"
        )


@router.get("/export")
@limiter.limit("10/minute")
def export_payments(
    request: Request,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    party_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Export payment transactions to CSV or Excel format."""
    try:
        logger.info(f"User '{current_user.username}' exporting payments (format={format}, party_type={party_type})")
        query = db.query(models.Payment).options(
            joinedload(models.Payment.customer),
            joinedload(models.Payment.supplier)
        )

        if party_type:
            query = query.filter(models.Payment.party_type == party_type.lower())
        if start_date:
            query = query.filter(func.date(models.Payment.created_at) >= start_date)
        if end_date:
            query = query.filter(func.date(models.Payment.created_at) <= end_date)

        payments = query.order_by(models.Payment.created_at.desc()).all()
        if not payments:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No payment records found for export"
            )

        data = []
        for p in payments:
            party_name = "Unknown"
            if p.customer:
                party_name = p.customer.name
            elif p.supplier:
                party_name = p.supplier.name

            data.append({
                "Payment ID": p.id,
                "Party Type": p.party_type.capitalize(),
                "Party Name": party_name,
                "Direction": p.payment_type.capitalize(),
                "Method": p.method.upper(),
                "Amount": p.amount,
                "Date": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "",
                "Note": p.note or ""
            })

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
                    "Content-Disposition": f"attachment; filename=payments_{timestamp_str}.csv"
                }
            )
        else:
            try:
                import pandas as pd
                df = pd.DataFrame(data)
                excel_output = io.BytesIO()
                with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, sheet_name='Payments', index=False)
                excel_output.seek(0)
                return StreamingResponse(
                    excel_output,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": f"attachment; filename=payments_{timestamp_str}.xlsx"
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
                        "Content-Disposition": f"attachment; filename=payments_{timestamp_str}.csv"
                    }
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting payments: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export payments: {str(e)}"
        )


@router.get("/", response_model=List[PaymentResponse])
@limiter.limit("1200/minute")
def list_payments(
    request: Request,
    party_type: Optional[str] = Query(None, description="Filter by party_type: customer | supplier"),
    party_id: Optional[int] = Query(None, description="Filter by party_id"),
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    limit: int = Query(500, ge=1, le=200000, description="Max entries to return (default 500, max 200000)"),
    offset: int = Query(0, ge=0, description="Entries offset"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List payment transactions with filters and pagination."""
    try:
        logger.info(f"User '{current_user.username}' listing payments (party_type={party_type}, party_id={party_id}, limit={limit}, offset={offset})")
        query = db.query(models.Payment).options(
            joinedload(models.Payment.customer),
            joinedload(models.Payment.supplier)
        )
        if party_type:
            query = query.filter(models.Payment.party_type == party_type.lower())
        if party_id:
            query = query.filter(models.Payment.party_id == party_id)
        if start_date:
            query = query.filter(func.date(models.Payment.created_at) >= start_date)
        if end_date:
            query = query.filter(func.date(models.Payment.created_at) <= end_date)

        payments = query.order_by(models.Payment.created_at.desc()).offset(offset).limit(limit).all()
        return payments
    except Exception as e:
        logger.error(f"Error listing payments: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list payments: {str(e)}"
        )


@router.get("/{payment_id}/details", response_model=PaymentDetailResponse)
@router.get("/{payment_id}", response_model=PaymentResponse)
@limiter.limit("1200/minute")
def get_payment_details(
    request: Request,
    payment_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get single payment details including party name and linked cash transaction info."""
    try:
        logger.info(f"User '{current_user.username}' fetching payment ID {payment_id}")
        payment = db.query(models.Payment).options(
            joinedload(models.Payment.customer),
            joinedload(models.Payment.supplier)
        ).filter_by(id=payment_id).first()

        if not payment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Payment with ID {payment_id} not found"
            )

        # Check if requesting specific details endpoint
        if request.url.path.endswith("/details"):
            party_name = "Unknown"
            if payment.customer:
                party_name = payment.customer.name
            elif payment.supplier:
                party_name = payment.supplier.name

            linked_tx = db.query(models.CashTransaction).filter_by(
                reference_type="payment", reference_id=payment_id
            ).first()

            return {
                "id": payment.id,
                "party_type": payment.party_type,
                "party_id": payment.party_id,
                "party_name": party_name,
                "amount": payment.amount,
                "payment_type": payment.payment_type,
                "method": payment.method,
                "note": payment.note or "",
                "created_at": payment.created_at,
                "has_cash_tx": linked_tx is not None,
                "cash_tx_id": linked_tx.id if linked_tx else None
            }

        return payment
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching payment ID {payment_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch payment details: {str(e)}"
        )


@router.post("/", response_model=PaymentCreateResponse)
@limiter.limit("10/minute")
def create_payment(
    request: Request,
    data: PaymentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Create a payment entry, update party balance, and auto-link cash transaction."""
    if data.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment amount must be greater than zero")
    if data.amount > 1000000000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment amount cannot exceed 1 Billion")

    username = current_user.username

    try:
        logger.info(f"User '{username}' creating payment: party_type={data.party_type}, party_id={data.party_id}, amount={data.amount}, method={data.method}")
        
        rounded_amt_dec = to_dec(data.amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        rounded_amount = float(rounded_amt_dec)

        payment = models.Payment(
            party_type=data.party_type.lower(),
            party_id=data.party_id,
            amount=rounded_amount,
            payment_type=data.payment_type.lower(),
            method=normalize_payment_method(data.method),
            note=data.note or "",
            created_at=datetime.now(),
        )
        db.add(payment)

        party_name = "Unknown"
        new_balance = 0.0

        if data.party_type.lower() == "customer" and data.payment_type.lower() == "received":
            customer = db.query(models.Customer).filter_by(id=data.party_id).first()
            if not customer:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer with ID {data.party_id} not found")
            party_name = customer.name
            bal_dec = to_dec(customer.credit_balance)
            new_bal_dec = max(Decimal('0.0'), bal_dec - rounded_amt_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            customer.credit_balance = float(new_bal_dec)
            new_balance = float(new_bal_dec)

        elif data.party_type.lower() == "supplier" and data.payment_type.lower() == "paid":
            supplier = db.query(models.Supplier).filter_by(id=data.party_id).first()
            if not supplier:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Supplier with ID {data.party_id} not found")
            party_name = supplier.name
            bal_dec = to_dec(supplier.due_balance)
            new_bal_dec = max(Decimal('0.0'), bal_dec - rounded_amt_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            supplier.due_balance = float(new_bal_dec)
            new_balance = float(new_bal_dec)

        db.flush()

        cash_tx_id = _auto_cash_tx(db, payment, party_name, username)

        db.commit()
        db.refresh(payment)
        
        _trigger_auto_backup()

        response_dict = {
            "id": payment.id,
            "party_type": payment.party_type,
            "party_id": payment.party_id,
            "amount": payment.amount,
            "payment_type": payment.payment_type,
            "method": payment.method,
            "note": payment.note or "",
            "created_at": payment.created_at,
            "party_name": party_name,
            "new_balance": round(new_balance, 2),
            "cash_transaction_id": cash_tx_id,
            "customer": payment.customer,
            "supplier": payment.supplier
        }

        return response_dict
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create payment: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create payment: {str(e)}"
        )


@router.delete("/{payment_id}", response_model=PaymentDeleteResponse)
@limiter.limit("10/minute")
def delete_payment(
    request: Request,
    payment_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Delete/reverse a payment, restore party balance, and clean up cash transaction."""
    try:
        logger.info(f"User '{current_user.username}' deleting payment ID {payment_id}")
        payment = db.query(models.Payment).filter_by(id=payment_id).first()
        if not payment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Payment with ID {payment_id} not found"
            )

        amt_dec = to_dec(payment.amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if payment.party_type == "customer" and payment.payment_type == "received":
            customer = db.query(models.Customer).filter_by(id=payment.party_id).first()
            if customer:
                bal_dec = to_dec(customer.credit_balance)
                new_bal = (bal_dec + amt_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                customer.credit_balance = float(new_bal)
        elif payment.party_type == "supplier" and payment.payment_type == "paid":
            supplier = db.query(models.Supplier).filter_by(id=payment.party_id).first()
            if supplier:
                bal_dec = to_dec(supplier.due_balance)
                new_bal = (bal_dec + amt_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                supplier.due_balance = float(new_bal)

        linked_tx = db.query(models.CashTransaction).filter_by(
            reference_type="payment", reference_id=payment_id
        ).first()
        if linked_tx:
            db.delete(linked_tx)

        db.delete(payment)
        db.commit()

        _trigger_auto_backup()

        return {"success": True, "message": f"Payment ID {payment_id} reversed successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete payment ID {payment_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete payment: {str(e)}"
        )
