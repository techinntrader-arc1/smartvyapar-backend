"""
Parties router - Customers + Suppliers CRUD + Ledger & Transaction Reports
"""

import logging
import io
import csv
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
import models

def to_dec(val) -> Decimal:
    if val is None:
        return Decimal('0.0')
    return Decimal(str(val))

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

logger = logging.getLogger("smartvyapar.parties")
logger.setLevel(logging.INFO)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150, description="Customer full name")
    phone: Optional[str] = Field("", max_length=50, description="Contact phone number")
    address: Optional[str] = Field("", max_length=300, description="Physical address")
    credit_balance: float = Field(0.0, description="Initial credit or due balance")
    whatsapp_enabled: Optional[bool] = Field(True, description="WhatsApp notification toggle")
    auto_send_invoice: Optional[bool] = Field(False, description="Auto send invoice via WhatsApp")
    auto_send_ledger: Optional[bool] = Field(False, description="Auto send ledger statement via WhatsApp")


class CustomerResponse(BaseModel):
    id: int
    name: str
    phone: Optional[str] = ""
    address: Optional[str] = ""
    credit_balance: float = 0.0
    whatsapp_enabled: bool = True
    auto_send_invoice: bool = False
    auto_send_ledger: bool = False
    is_active: bool = True
    created_at: datetime

    class Config:
        orm_mode = True


class CustomerListResponse(BaseModel):
    total: int
    items: List[CustomerResponse]
    limit: int
    offset: int


class LedgerItemDetail(BaseModel):
    name: str
    qty: float
    price: float
    total: float


class CustomerLedgerItem(BaseModel):
    date: datetime
    type: str
    ref: str
    debit: float
    credit: float
    due: float
    items: Optional[List[LedgerItemDetail]] = None
    items_summary: Optional[str] = None


class CustomerLedgerResponse(BaseModel):
    customer: str
    balance: float
    opening_balance: Optional[float] = 0.0
    ledger: List[CustomerLedgerItem]


class SupplierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150, description="Supplier full name or business name")
    phone: Optional[str] = Field("", max_length=50, description="Contact phone number")
    address: Optional[str] = Field("", max_length=300, description="Physical address")
    due_balance: float = Field(0.0, description="Initial payable balance")


class SupplierResponse(BaseModel):
    id: int
    name: str
    phone: Optional[str] = ""
    address: Optional[str] = ""
    due_balance: float = 0.0
    is_active: bool = True
    created_at: datetime

    class Config:
        orm_mode = True


class SupplierListResponse(BaseModel):
    total: int
    items: List[SupplierResponse]
    limit: int
    offset: int


class SupplierLedgerItem(BaseModel):
    date: datetime
    type: str
    ref: str
    debit: float
    credit: float
    due: float
    items: Optional[List[LedgerItemDetail]] = None


class SupplierLedgerResponse(BaseModel):
    supplier: str
    balance: float
    opening_balance: Optional[float] = 0.0
    ledger: List[SupplierLedgerItem]


class PartyDeleteResponse(BaseModel):
    success: bool
    message: str


class CustomerTransactionItem(BaseModel):
    id: int
    type: str
    date: datetime
    reference: str
    amount: float
    paid: float
    due: float


class CustomerTransactionsResponse(BaseModel):
    customer_id: int
    customer_name: str
    transactions: List[CustomerTransactionItem]


class SupplierTransactionItem(BaseModel):
    id: int
    type: str
    date: datetime
    reference: str
    amount: float
    paid: float
    due: float


class SupplierTransactionsResponse(BaseModel):
    supplier_id: int
    supplier_name: str
    transactions: List[SupplierTransactionItem]


class PartiesHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Helper Functions ──────────────────────────────────────────────────────────

def _trigger_auto_backup():
    try:
        from database import get_db_path, get_backup_dir
        from services import backup_service
        backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
    except Exception as e:
        logger.warning(f"Auto-backup trigger warning: {e}")


# ── Module Health & Export Endpoints ──────────────────────────────────────────

@router.get("/health", response_model=PartiesHealthCheckResponse)
@router.get("/parties/health", response_model=PartiesHealthCheckResponse)
@limiter.limit("1200/minute")
def parties_health(request: Request):
    """Health check endpoint for parties module."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "parties",
        "endpoints": [
            "/customers",
            "/customers/{cid}",
            "/customers/{cid}/ledger",
            "/customers/{cid}/transactions",
            "/suppliers",
            "/suppliers/{sid}",
            "/suppliers/{sid}/ledger",
            "/suppliers/{sid}/transactions",
            "/export",
            "/health"
        ]
    }


@router.get("/export")
@router.get("/parties/export")
@limiter.limit("10/minute")
def export_parties(
    request: Request,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    party_type: str = Query("all", pattern="^(all|customers|suppliers)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Export customers and suppliers to CSV or Excel format."""
    try:
        logger.info(f"User '{current_user.username}' exporting parties (type={party_type}, format={format})")
        data = []

        if party_type in ("all", "customers"):
            customers = db.query(models.Customer).order_by(models.Customer.name).all()
            for c in customers:
                data.append({
                    "Type": "Customer",
                    "ID": c.id,
                    "Name": c.name,
                    "Phone": c.phone or "",
                    "Address": c.address or "",
                    "Balance": c.credit_balance,
                    "Status": "Active" if c.is_active else "Inactive",
                    "Created At": c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else ""
                })

        if party_type in ("all", "suppliers"):
            suppliers = db.query(models.Supplier).order_by(models.Supplier.name).all()
            for s in suppliers:
                data.append({
                    "Type": "Supplier",
                    "ID": s.id,
                    "Name": s.name,
                    "Phone": s.phone or "",
                    "Address": s.address or "",
                    "Balance": s.due_balance,
                    "Status": "Active" if s.is_active else "Inactive",
                    "Created At": s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else ""
                })

        if not data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No party records found for export")

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
                    "Content-Disposition": f"attachment; filename=parties_{timestamp_str}.csv"
                }
            )
        else:
            try:
                import pandas as pd
                df = pd.DataFrame(data)
                excel_output = io.BytesIO()
                with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, sheet_name='Parties', index=False)
                excel_output.seek(0)
                return StreamingResponse(
                    excel_output,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": f"attachment; filename=parties_{timestamp_str}.xlsx"
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
                        "Content-Disposition": f"attachment; filename=parties_{timestamp_str}.csv"
                    }
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting parties: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to export parties: {str(e)}")


# ════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ════════════════════════════════════════════════════════════════════

@router.get("/customers", response_model=List[CustomerResponse])
@limiter.limit("1200/minute")
def list_customers(
    request: Request,
    q: Optional[str] = Query(None, description="Search by customer name or phone"),
    include_inactive: bool = Query(False, description="Include archived/inactive customers"),
    limit: int = Query(500, ge=1, le=100000, description="Limit entries"),
    offset: int = Query(0, ge=0, description="Offset entries"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List all customers with optional search, pagination, and status filters."""
    try:
        logger.info(f"User '{current_user.username}' searching customers (q={q}, include_inactive={include_inactive})")
        query = db.query(models.Customer)
        if not include_inactive:
            query = query.filter_by(is_active=True)
        if q:
            query = query.filter(
                or_(
                    models.Customer.name.ilike(f"%{q}%"),
                    models.Customer.phone.ilike(f"%{q}%")
                )
            )

        customers = query.order_by(models.Customer.name).offset(offset).limit(limit).all()
        return customers
    except Exception as e:
        logger.error(f"Error listing customers: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list customers: {str(e)}")


@router.get("/customers/{cid}", response_model=CustomerResponse)
@limiter.limit("1200/minute")
def get_customer(
    request: Request,
    cid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get single customer details by ID."""
    try:
        logger.info(f"User '{current_user.username}' fetching customer ID {cid}")
        c = db.query(models.Customer).filter_by(id=cid).first()
        if not c:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer with ID {cid} not found")
        return c
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer ID {cid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get customer: {str(e)}")


@router.post("/customers", response_model=CustomerResponse)
@limiter.limit("10/minute")
def create_customer(
    request: Request,
    data: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Create a new customer entry."""
    try:
        logger.info(f"User '{current_user.username}' creating customer '{data.name}'")
        c = models.Customer(**data.dict())
        db.add(c)
        db.commit()
        db.refresh(c)
        
        _trigger_auto_backup()
        return c
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create customer: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to create customer: {str(e)}")


@router.put("/customers/{cid}", response_model=CustomerResponse)
@limiter.limit("10/minute")
def update_customer(
    request: Request,
    cid: int,
    data: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Update existing customer details."""
    try:
        logger.info(f"User '{current_user.username}' updating customer ID {cid}")
        c = db.query(models.Customer).filter_by(id=cid).first()
        if not c:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer with ID {cid} not found")
        
        for k, v in data.dict().items():
            setattr(c, k, v)
        
        db.commit()
        db.refresh(c)
        
        _trigger_auto_backup()
        return c
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update customer ID {cid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update customer: {str(e)}")


@router.delete("/customers/{cid}", response_model=PartyDeleteResponse)
@limiter.limit("10/minute")
def delete_customer(
    request: Request,
    cid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Soft delete or hard delete customer record based on transaction history."""
    try:
        logger.info(f"User '{current_user.username}' deleting customer ID {cid}")
        c = db.query(models.Customer).filter_by(id=cid).first()
        if not c:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer with ID {cid} not found")
        
        has_sales = db.query(models.Sale).filter_by(customer_id=cid).first() is not None
        has_payments = db.query(models.Payment).filter_by(party_type="customer", party_id=cid).first() is not None
        
        if has_sales or has_payments:
            if not c.is_active:
                return {"success": True, "message": "Customer already archived."}
            c.is_active = False
            db.commit()
            return {"success": True, "message": "Customer archived (transactions exist)."}
        
        db.delete(c)
        db.commit()

        _trigger_auto_backup()
        return {"success": True, "message": "Customer deleted successfully."}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete customer ID {cid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete customer: {str(e)}")


@router.get("/customers/{cid}/ledger", response_model=CustomerLedgerResponse)
@limiter.limit("1200/minute")
def customer_ledger(
    request: Request,
    cid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns formatted sales + payments ledger statement for a customer."""
    try:
        logger.info(f"User '{current_user.username}' generating ledger for customer ID {cid}")
        customer = db.query(models.Customer).filter_by(id=cid).first()
        if not customer:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer ID {cid} not found for ledger")
        
        sales = db.query(models.Sale).filter_by(customer_id=cid).order_by(models.Sale.created_at).all()
        payments = db.query(models.Payment).filter_by(party_type="customer", party_id=cid).order_by(models.Payment.created_at).all()
        
        ledger = []
        for s in sales:
            items_summary = ", ".join([f"{item.product_name} ({item.qty})" for item in s.items])
            ledger.append({
                "date": s.created_at,
                "type": "sale",
                "ref": s.invoice_no,
                "debit": s.total,
                "credit": s.paid_amount,
                "due": 0.0,
                "items": [{"name": i.product_name, "qty": i.qty, "price": i.price, "total": i.total} for i in s.items],
                "items_summary": items_summary
            })
            
        for p in payments:
            ledger.append({
                "date": p.created_at,
                "type": "payment",
                "ref": f"PMT-{p.id}",
                "debit": 0.0,
                "credit": p.amount,
                "due": 0.0
            })
            
        ledger.sort(key=lambda x: x["date"])

        total_debit = sum(to_dec(x["debit"]) for x in ledger)
        total_credit = sum(to_dec(x["credit"]) for x in ledger)
        opening_balance = to_dec(customer.credit_balance) - (total_debit - total_credit)
        running_bal = opening_balance
        for item in ledger:
            running_bal += to_dec(item["debit"]) - to_dec(item["credit"])
            item["due"] = float(running_bal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        return {
            "customer": customer.name,
            "balance": customer.credit_balance,
            "opening_balance": float(opening_balance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "ledger": ledger
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching ledger for customer ID {cid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to generate customer ledger: {str(e)}")


@router.get("/customers/{cid}/transactions", response_model=CustomerTransactionsResponse)
@router.get("/parties/customers/{cid}/transactions", response_model=CustomerTransactionsResponse)
@limiter.limit("1200/minute")
def get_customer_transactions(
    request: Request,
    cid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns all sales and payment transactions for a customer."""
    try:
        logger.info(f"User '{current_user.username}' fetching transactions for customer ID {cid}")
        customer = db.query(models.Customer).filter_by(id=cid).first()
        if not customer:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer ID {cid} not found")
        
        sales = db.query(models.Sale).filter_by(customer_id=cid).all()
        payments = db.query(models.Payment).filter_by(party_type="customer", party_id=cid).all()
        
        tx_list = []
        for s in sales:
            tx_list.append({
                "id": s.id,
                "type": "sale",
                "date": s.created_at,
                "reference": s.invoice_no,
                "amount": s.total,
                "paid": s.paid_amount,
                "due": s.total - s.paid_amount
            })
        for p in payments:
            tx_list.append({
                "id": p.id,
                "type": "payment",
                "date": p.created_at,
                "reference": f"PMT-{p.id}",
                "amount": p.amount,
                "paid": p.amount,
                "due": 0.0
            })
            
        tx_list.sort(key=lambda x: x["date"], reverse=True)
        return {
            "customer_id": customer.id,
            "customer_name": customer.name,
            "transactions": tx_list
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transactions for customer ID {cid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to fetch customer transactions: {str(e)}")


# ════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ════════════════════════════════════════════════════════════════════

@router.get("/suppliers", response_model=List[SupplierResponse])
@limiter.limit("1200/minute")
def list_suppliers(
    request: Request,
    q: Optional[str] = Query(None, description="Search by supplier name or phone"),
    include_inactive: bool = Query(False, description="Include archived/inactive suppliers"),
    limit: int = Query(500, ge=1, le=100000, description="Limit entries"),
    offset: int = Query(0, ge=0, description="Offset entries"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List all suppliers with optional search, pagination, and status filters."""
    try:
        logger.info(f"User '{current_user.username}' searching suppliers (q={q}, include_inactive={include_inactive})")
        query = db.query(models.Supplier)
        if not include_inactive:
            query = query.filter_by(is_active=True)
        if q:
            query = query.filter(
                or_(
                    models.Supplier.name.ilike(f"%{q}%"),
                    models.Supplier.phone.ilike(f"%{q}%")
                )
            )

        suppliers = query.order_by(models.Supplier.name).offset(offset).limit(limit).all()
        return suppliers
    except Exception as e:
        logger.error(f"Error listing suppliers: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list suppliers: {str(e)}")


@router.get("/suppliers/{sid}", response_model=SupplierResponse)
@limiter.limit("1200/minute")
def get_supplier(
    request: Request,
    sid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get single supplier details by ID."""
    try:
        logger.info(f"User '{current_user.username}' fetching supplier ID {sid}")
        s = db.query(models.Supplier).filter_by(id=sid).first()
        if not s:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Supplier ID {sid} not found")
        return s
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching supplier ID {sid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get supplier: {str(e)}")


@router.post("/suppliers", response_model=SupplierResponse)
@limiter.limit("10/minute")
def create_supplier(
    request: Request,
    data: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Create a new supplier entry."""
    try:
        logger.info(f"User '{current_user.username}' creating supplier '{data.name}'")
        s = models.Supplier(**data.dict())
        db.add(s)
        db.commit()
        db.refresh(s)
        
        _trigger_auto_backup()
        return s
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create supplier: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to create supplier: {str(e)}")


@router.put("/suppliers/{sid}", response_model=SupplierResponse)
@limiter.limit("10/minute")
def update_supplier(
    request: Request,
    sid: int,
    data: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Update existing supplier details."""
    try:
        logger.info(f"User '{current_user.username}' updating supplier ID {sid}")
        s = db.query(models.Supplier).filter_by(id=sid).first()
        if not s:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Supplier ID {sid} not found")
        
        for k, v in data.dict().items():
            setattr(s, k, v)
        
        db.commit()
        db.refresh(s)
        
        _trigger_auto_backup()
        return s
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update supplier ID {sid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update supplier: {str(e)}")


@router.delete("/suppliers/{sid}", response_model=PartyDeleteResponse)
@limiter.limit("10/minute")
def delete_supplier(
    request: Request,
    sid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Soft delete or hard delete supplier record based on transaction history."""
    try:
        logger.info(f"User '{current_user.username}' deleting supplier ID {sid}")
        s = db.query(models.Supplier).filter_by(id=sid).first()
        if not s:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Supplier ID {sid} not found")
        
        has_purchases = db.query(models.Purchase).filter_by(supplier_id=sid).first() is not None
        has_payments = db.query(models.Payment).filter_by(party_type="supplier", party_id=sid).first() is not None
        
        if has_purchases or has_payments:
            if not s.is_active:
                return {"success": True, "message": "Supplier already archived."}
            s.is_active = False
            db.commit()
            return {"success": True, "message": "Supplier archived (transactions exist)."}

        db.delete(s)
        db.commit()

        _trigger_auto_backup()
        return {"success": True, "message": "Supplier deleted successfully."}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete supplier ID {sid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete supplier: {str(e)}")


@router.get("/suppliers/{sid}/ledger", response_model=SupplierLedgerResponse)
@limiter.limit("1200/minute")
def supplier_ledger(
    request: Request,
    sid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns formatted purchases + payments ledger statement for a supplier."""
    try:
        logger.info(f"User '{current_user.username}' generating ledger for supplier ID {sid}")
        supplier = db.query(models.Supplier).filter_by(id=sid).first()
        if not supplier:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Supplier ID {sid} not found for ledger")
        
        purchases = db.query(models.Purchase).filter_by(supplier_id=sid).order_by(models.Purchase.created_at).all()
        payments = db.query(models.Payment).filter_by(party_type="supplier", party_id=sid).order_by(models.Payment.created_at).all()
        
        ledger = []
        for p in purchases:
            ledger.append({
                "date": p.created_at,
                "type": "purchase",
                "ref": p.bill_no,
                "debit": p.total,
                "credit": p.paid_amount,
                "due": 0.0,
                "items": [{"name": i.product_name, "qty": i.qty, "price": i.price, "total": i.total} for i in p.items]
            })
        for pay in payments:
            ledger.append({
                "date": pay.created_at,
                "type": "payment",
                "ref": f"PMT-{pay.id}",
                "debit": 0.0,
                "credit": pay.amount,
                "due": 0.0
            })
            
        ledger.sort(key=lambda x: x["date"])

        total_debit = sum(to_dec(x["debit"]) for x in ledger)
        total_credit = sum(to_dec(x["credit"]) for x in ledger)
        opening_balance = to_dec(supplier.due_balance) - (total_debit - total_credit)
        running_bal = opening_balance
        for item in ledger:
            running_bal += to_dec(item["debit"]) - to_dec(item["credit"])
            item["due"] = float(running_bal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        return {
            "supplier": supplier.name,
            "balance": supplier.due_balance,
            "opening_balance": float(opening_balance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "ledger": ledger
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching ledger for supplier ID {sid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to generate supplier ledger: {str(e)}")


@router.get("/suppliers/{sid}/transactions", response_model=SupplierTransactionsResponse)
@router.get("/parties/suppliers/{sid}/transactions", response_model=SupplierTransactionsResponse)
@limiter.limit("1200/minute")
def get_supplier_transactions(
    request: Request,
    sid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns all purchase and payment transactions for a supplier."""
    try:
        logger.info(f"User '{current_user.username}' fetching transactions for supplier ID {sid}")
        supplier = db.query(models.Supplier).filter_by(id=sid).first()
        if not supplier:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Supplier ID {sid} not found")
        
        purchases = db.query(models.Purchase).filter_by(supplier_id=sid).all()
        payments = db.query(models.Payment).filter_by(party_type="supplier", party_id=sid).all()
        
        tx_list = []
        for p in purchases:
            tx_list.append({
                "id": p.id,
                "type": "purchase",
                "date": p.created_at,
                "reference": p.bill_no,
                "amount": p.total,
                "paid": p.paid_amount,
                "due": p.total - p.paid_amount
            })
        for pay in payments:
            tx_list.append({
                "id": pay.id,
                "type": "payment",
                "date": pay.created_at,
                "reference": f"PMT-{pay.id}",
                "amount": pay.amount,
                "paid": pay.amount,
                "due": 0.0
            })
            
        tx_list.sort(key=lambda x: x["date"], reverse=True)
        return {
            "supplier_id": supplier.id,
            "supplier_name": supplier.name,
            "transactions": tx_list
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transactions for supplier ID {sid}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to fetch supplier transactions: {str(e)}")
