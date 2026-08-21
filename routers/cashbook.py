"""
SmartVyapar - Cash Book Router
Handles all Cash In / Cash Out / Day Closing / External Funds operations.
Single source of truth for cash ledger — no double-counting.
"""

import logging
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user, require_admin
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

logger = logging.getLogger("smartvyapar.cashbook")
logger.setLevel(logging.INFO)

router = APIRouter()


# ── Helper Functions ──────────────────────────────────────────────────────────

def _current_user(token_data) -> str:
    """Extract username string from token data or User object."""
    if hasattr(token_data, 'username'):
        return token_data.username
    if isinstance(token_data, dict):
        return token_data.get('username', 'system')
    return str(token_data) if token_data else 'system'


def _write_audit(db: Session, action: str, entity_type: str, entity_id: int,
                  old_val=None, new_val=None, user: str = 'system'):
    """Write an immutable audit log entry."""
    try:
        entry = models.AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=json.dumps(old_val) if old_val else None,
            new_value=json.dumps(new_val) if new_val else None,
            user=user,
        )
        db.add(entry)
    except Exception as e:
        logger.error(f"[Audit] Failed to write audit log: {e}", exc_info=True)


def _get_opening_cash(db: Session, for_date: str) -> float:
    """
    Get opening cash for a given date.
    Finds the most recent day_closing before for_date.
    Then adds all cash_in and subtracts all cash_out between that day and for_date.
    """
    try:
        last_closing = db.query(models.DayClosing)\
            .filter(models.DayClosing.date < for_date)\
            .order_by(models.DayClosing.date.desc())\
            .first()

        if last_closing:
            if last_closing.actual_counted_cash is not None:
                base_cash = float(last_closing.actual_counted_cash)
            else:
                base_cash = float(last_closing.expected_closing_cash or 0)
            start_time = last_closing.date + " 23:59:59"
        else:
            setting = db.query(models.Setting).filter_by(key="opening_cash_manual").first()
            base_cash = float(setting.value) if setting else 0.0
            start_time = "2000-01-01 00:00:00"

        end_time = for_date + " 00:00:00"

        total_in = db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_in",
            models.CashTransaction.created_at > start_time,
            models.CashTransaction.created_at < end_time,
            models.CashTransaction.account == "cash_in_hand"
        ).scalar() or 0.0

        total_out = db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.created_at > start_time,
            models.CashTransaction.created_at < end_time,
            models.CashTransaction.account == "cash_in_hand"
        ).scalar() or 0.0

        return base_cash + float(total_in) - float(total_out)
    except Exception as e:
        logger.error(f"[CashBook] Opening cash calculation error for {for_date}: {e}", exc_info=True)
    return 0.0


def _get_cash_summary(db: Session, start_date: str, end_date: str = None) -> dict:
    """Calculate cash in/out totals for a specific date range."""
    if not end_date:
        end_date = start_date
    start = start_date + " 00:00:00"
    end = end_date + " 23:59:59"

    total_in = db.query(func.sum(models.CashTransaction.amount)).filter(
        models.CashTransaction.tx_type == "cash_in",
        models.CashTransaction.created_at >= start,
        models.CashTransaction.created_at <= end,
        models.CashTransaction.account == "cash_in_hand"
    ).scalar() or 0.0

    total_out = db.query(func.sum(models.CashTransaction.amount)).filter(
        models.CashTransaction.tx_type == "cash_out",
        models.CashTransaction.created_at >= start,
        models.CashTransaction.created_at <= end,
        models.CashTransaction.account == "cash_in_hand"
    ).scalar() or 0.0

    # Breakdown by type (cash in)
    def _sum_in(type_val):
        return float(db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_in",
            models.CashTransaction.cash_in_type == type_val,
            models.CashTransaction.created_at >= start,
            models.CashTransaction.created_at <= end,
            models.CashTransaction.account == "cash_in_hand"
        ).scalar() or 0)

    def _sum_out(type_val):
        return float(db.query(func.sum(models.CashTransaction.amount)).filter(
            models.CashTransaction.tx_type == "cash_out",
            models.CashTransaction.cash_out_type == type_val,
            models.CashTransaction.created_at >= start,
            models.CashTransaction.created_at <= end,
            models.CashTransaction.account == "cash_in_hand"
        ).scalar() or 0)

    return {
        "total_in": round(float(total_in), 2),
        "total_out": round(float(total_out), 2),
        "cash_sales": _sum_in("cash_sale"),
        "customer_payments": _sum_in("customer_payment"),
        "owner_injections": _sum_in("owner_injection"),
        "borrowed_cash": _sum_in("borrowed") + _sum_in("friend_loan"),
        "bank_withdrawals": _sum_in("bank_withdrawal"),
        "other_income": _sum_in("other_income"),
        "manual_in": _sum_in("manual_in"),
        "purchase_payments": _sum_out("purchase_payment"),
        "supplier_payments": _sum_out("supplier_payment"),
        "expenses": _sum_out("expense") + _sum_out("salary"),
        "loan_returns": _sum_out("loan_return") + _sum_out("fund_return"),
        "owner_withdrawals": _sum_out("owner_withdrawal"),
        "refunds": _sum_out("refund"),
        "bank_deposits": _sum_out("bank_deposit"),
        "manual_out": _sum_out("manual_out"),
    }


def _check_cash_balance(db: Session, amount: float, for_date: str) -> tuple:
    """Returns (has_enough, current_balance). Reads allow_negative_cash setting."""
    opening = _get_opening_cash(db, for_date)
    summary = _get_cash_summary(db, for_date)
    balance = opening + summary["total_in"] - summary["total_out"]

    allow_neg = False
    setting = db.query(models.Setting).filter_by(key="allow_negative_cash").first()
    if setting and setting.value == "true":
        allow_neg = True

    if not allow_neg and (balance - amount) < 0:
        return False, round(balance, 2)
    return True, round(balance, 2)


# ── Pydantic Request Models ───────────────────────────────────────────────────

class CashInCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Cash in amount")
    cash_in_type: str = Field(..., description="Type of cash in")
    received_from: Optional[str] = ""
    account: str = "cash_in_hand"
    reference_no: Optional[str] = ""
    notes: Optional[str] = ""
    date: Optional[str] = None
    party_name: Optional[str] = None


class CashOutCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Cash out amount")
    cash_out_type: str = Field(..., description="Type of cash out")
    paid_to: Optional[str] = ""
    account: str = "cash_in_hand"
    category: Optional[str] = ""
    reference_no: Optional[str] = ""
    notes: Optional[str] = ""
    date: Optional[str] = None
    force: bool = False


class DayClosingCreate(BaseModel):
    date: str = Field(..., description="Date YYYY-MM-DD")
    actual_counted_cash: float = Field(..., ge=0, description="Counted cash amount")
    notes: Optional[str] = ""


class OpeningCashSet(BaseModel):
    amount: float = Field(..., ge=0, description="Opening cash amount")
    notes: Optional[str] = ""


# ── Pydantic Response Models ──────────────────────────────────────────────────

class CashBalanceResponse(BaseModel):
    date: str
    opening_cash: float
    total_cash_in: float
    total_cash_out: float
    current_balance: float


class DayClosingDetail(BaseModel):
    id: int
    actual_counted_cash: Optional[float] = None
    difference: Optional[float] = None
    status: str
    notes: Optional[str] = None
    closed_by: Optional[str] = None
    closed_at: Optional[datetime] = None


class CashSummaryResponse(BaseModel):
    date: str
    opening_cash: float
    total_in: float
    total_out: float
    cash_sales: float
    customer_payments: float
    owner_injections: float
    borrowed_cash: float
    bank_withdrawals: float
    other_income: float
    manual_in: float
    purchase_payments: float
    supplier_payments: float
    expenses: float
    loan_returns: float
    owner_withdrawals: float
    refunds: float
    bank_deposits: float
    manual_out: float
    expected_closing_cash: float
    is_closed: bool
    closing: Optional[DayClosingDetail] = None


class TransactionItem(BaseModel):
    id: int
    date: str
    time: str
    tx_type: str
    cash_in_type: Optional[str] = None
    cash_out_type: Optional[str] = None
    amount: float
    account: str
    received_from: Optional[str] = None
    paid_to: Optional[str] = None
    category: Optional[str] = None
    reference_type: Optional[str] = None
    reference_no: Optional[str] = None
    notes: Optional[str] = None
    created_by: Optional[str] = None


class CashTransactionsResponse(BaseModel):
    count: int
    items: List[TransactionItem]


class CashInResponse(BaseModel):
    success: bool
    id: int
    amount: float
    type: str


class CashOutResponse(BaseModel):
    success: bool
    id: int
    amount: float
    type: str


class CashTxDeleteResponse(BaseModel):
    success: bool
    message: str


class OpeningCashResponse(BaseModel):
    date: str
    opening_cash: float


class OpeningCashSetResponse(BaseModel):
    success: bool
    opening_cash: float


class DayClosingResponse(BaseModel):
    id: Optional[int] = None
    date: str
    status: str
    opening_cash: float
    total_cash_in: float
    total_cash_out: float
    expected_closing_cash: float
    actual_counted_cash: Optional[float] = None
    difference: Optional[float] = None
    notes: Optional[str] = None
    closed_by: Optional[str] = None
    closed_at: Optional[datetime] = None


class DayClosingCreateResponse(BaseModel):
    success: bool
    date: str
    opening_cash: float
    total_cash_in: float
    total_cash_out: float
    expected_closing_cash: float
    actual_counted_cash: float
    difference: float
    short_or_excess: str


class ExternalFundItem(BaseModel):
    id: int
    fund_type: str
    direction: str
    party_name: str
    amount: float
    notes: Optional[str] = None
    created_by: Optional[str] = None
    date: str
    time: str


class ExternalFundsResponse(BaseModel):
    total_received: float
    total_returned: float
    outstanding_balance: float
    items: List[ExternalFundItem]


class AuditLogItem(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: Optional[int] = None
    user: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    date: str


class AuditLogResponse(BaseModel):
    count: int
    items: List[AuditLogItem]


class CashbookHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/balance", response_model=CashBalanceResponse)
@limiter.limit("30/minute")
def get_cash_balance(
    request: Request,
    query_date: Optional[str] = Query(None, description="Date YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get current cash-in-hand balance for a given date (default: today)."""
    try:
        user_str = _current_user(current_user)
        for_date = query_date or date.today().isoformat()
        logger.info(f"User '{user_str}' fetching cash balance for {for_date}")

        opening = _get_opening_cash(db, for_date)
        summary = _get_cash_summary(db, for_date)
        balance = opening + summary["total_in"] - summary["total_out"]

        return {
            "date": for_date,
            "opening_cash": round(opening, 2),
            "total_cash_in": summary["total_in"],
            "total_cash_out": summary["total_out"],
            "current_balance": round(balance, 2),
        }
    except Exception as e:
        logger.error(f"Error getting cash balance: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve cash balance: {str(e)}"
        )


@router.get("/summary", response_model=CashSummaryResponse)
@limiter.limit("30/minute")
def get_cash_summary(
    request: Request,
    query_date: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Full Cash Book summary for a date — all in/out breakdown + expected closing."""
    try:
        user_str = _current_user(current_user)
        logger.info(f"User '{user_str}' fetching cash summary for query_date={query_date}, start={start_date}, end={end_date}")

        if query_date and not start_date:
            start_date = query_date
            end_date = query_date
        elif not start_date:
            start_date = date.today().isoformat()
            end_date = start_date
        if not end_date:
            end_date = start_date

        opening = _get_opening_cash(db, start_date)
        summary = _get_cash_summary(db, start_date, end_date)
        expected_closing = opening + summary["total_in"] - summary["total_out"]

        closing = db.query(models.DayClosing).filter_by(date=end_date).first()

        result = {
            "date": end_date,
            "opening_cash": round(opening, 2),
            **summary,
            "expected_closing_cash": round(expected_closing, 2),
            "is_closed": closing is not None and closing.status == "closed",
            "closing": None
        }

        if closing:
            result["closing"] = {
                "id": closing.id,
                "actual_counted_cash": closing.actual_counted_cash,
                "difference": closing.difference,
                "status": closing.status,
                "notes": closing.notes,
                "closed_by": closing.closed_by,
                "closed_at": closing.closed_at,
            }
        return result
    except Exception as e:
        logger.error(f"Error getting cash summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve cash summary: {str(e)}"
        )


@router.get("/transactions", response_model=CashTransactionsResponse)
@limiter.limit("30/minute")
def list_transactions(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    tx_type: Optional[str] = Query(None),
    account: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List cash transactions with filters."""
    try:
        user_str = _current_user(current_user)
        logger.info(f"User '{user_str}' listing cash transactions (start={start_date}, end={end_date}, tx_type={tx_type})")

        if not start_date:
            start_date = date.today().isoformat()
        if not end_date:
            end_date = date.today().isoformat()

        q = db.query(models.CashTransaction).filter(
            models.CashTransaction.created_at >= start_date + " 00:00:00",
            models.CashTransaction.created_at <= end_date + " 23:59:59",
        )
        if tx_type and tx_type != "all":
            q = q.filter(models.CashTransaction.tx_type == tx_type)
        if account:
            q = q.filter(models.CashTransaction.account == account)

        total = q.count()
        rows = q.order_by(models.CashTransaction.created_at.desc()).offset(offset).limit(limit).all()

        return {
            "count": total,
            "items": [
                {
                    "id": r.id,
                    "date": r.created_at.strftime("%Y-%m-%d") if r.created_at else "",
                    "time": r.created_at.strftime("%H:%M") if r.created_at else "",
                    "tx_type": r.tx_type,
                    "cash_in_type": r.cash_in_type,
                    "cash_out_type": r.cash_out_type,
                    "amount": round(r.amount, 2),
                    "account": r.account,
                    "received_from": r.received_from,
                    "paid_to": r.paid_to,
                    "category": r.category,
                    "reference_type": r.reference_type,
                    "reference_no": r.reference_no,
                    "notes": r.notes,
                    "created_by": r.created_by,
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.error(f"Error listing cash transactions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list cash transactions: {str(e)}"
        )


@router.post("/cash-in", response_model=CashInResponse)
@limiter.limit("10/minute")
def create_cash_in(
    request: Request,
    data: CashInCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Record a Cash In entry (manual or external fund)."""
    if data.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be greater than zero")
    if data.amount > 1000000000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount cannot exceed 1 Billion")

    user = _current_user(current_user)
    tx_date = data.date or date.today().isoformat()
    tx_datetime = datetime.strptime(tx_date, "%Y-%m-%d").replace(
        hour=datetime.now().hour, minute=datetime.now().minute, second=datetime.now().second
    )

    try:
        logger.info(f"User '{user}' creating cash-in: amount={data.amount}, type={data.cash_in_type}")
        tx = models.CashTransaction(
            tx_type="cash_in",
            cash_in_type=data.cash_in_type,
            amount=data.amount,
            account=data.account,
            received_from=data.received_from or (data.party_name or ""),
            reference_type="manual",
            reference_no=data.reference_no or "",
            notes=data.notes or "",
            created_by=user,
            created_at=tx_datetime,
        )
        db.add(tx)
        db.flush()

        if data.cash_in_type in ("owner_injection", "borrowed", "friend_loan"):
            ef = models.ExternalFund(
                fund_type=data.cash_in_type,
                direction="in",
                party_name=data.party_name or data.received_from or "Unknown",
                amount=data.amount,
                cash_tx_id=tx.id,
                notes=data.notes or "",
                created_by=user,
                created_at=tx_datetime,
            )
            db.add(ef)

        db.commit()
        db.refresh(tx)

        _write_audit(db, "cash_in_created", "cash_transaction", tx.id,
                     new_val={"amount": data.amount, "type": data.cash_in_type}, user=user)
        db.commit()

        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
        except Exception as backup_err:
            logger.warning(f"Auto-backup warning after cash-in: {backup_err}")

        return {"success": True, "id": tx.id, "amount": tx.amount, "type": tx.cash_in_type}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Cash In failed for user '{user}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Cash In failed: {str(e)}")


@router.post("/cash-out", response_model=CashOutResponse)
@limiter.limit("10/minute")
def create_cash_out(
    request: Request,
    data: CashOutCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Record a Cash Out entry (manual)."""
    if data.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be greater than zero")
    if data.amount > 1000000000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount cannot exceed 1 Billion")

    user = _current_user(current_user)
    tx_date = data.date or date.today().isoformat()

    if not data.force and data.account == "cash_in_hand":
        has_enough, balance = _check_cash_balance(db, data.amount, tx_date)
        if not has_enough:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient cash. Available: Rs. {balance:.0f}. Enable 'Allow Negative Cash' in settings to override."
            )

    tx_datetime = datetime.strptime(tx_date, "%Y-%m-%d").replace(
        hour=datetime.now().hour, minute=datetime.now().minute, second=datetime.now().second
    )

    try:
        logger.info(f"User '{user}' creating cash-out: amount={data.amount}, type={data.cash_out_type}")
        tx = models.CashTransaction(
            tx_type="cash_out",
            cash_out_type=data.cash_out_type,
            amount=data.amount,
            account=data.account,
            paid_to=data.paid_to or "",
            category=data.category or "",
            reference_type="manual",
            reference_no=data.reference_no or "",
            notes=data.notes or "",
            created_by=user,
            created_at=tx_datetime,
        )
        db.add(tx)
        db.flush()

        if data.cash_out_type in ("loan_return", "fund_return", "owner_withdrawal"):
            ef = models.ExternalFund(
                fund_type=data.cash_out_type,
                direction="out",
                party_name=data.paid_to or "Unknown",
                amount=data.amount,
                cash_tx_id=tx.id,
                notes=data.notes or "",
                created_by=user,
                created_at=tx_datetime,
            )
            db.add(ef)

        db.commit()
        db.refresh(tx)

        _write_audit(db, "cash_out_created", "cash_transaction", tx.id,
                     new_val={"amount": data.amount, "type": data.cash_out_type}, user=user)
        db.commit()

        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
        except Exception as backup_err:
            logger.warning(f"Auto-backup warning after cash-out: {backup_err}")

        return {"success": True, "id": tx.id, "amount": tx.amount, "type": tx.cash_out_type}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Cash Out failed for user '{user}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Cash Out failed: {str(e)}")


@router.delete("/transaction/{tx_id}", response_model=CashTxDeleteResponse)
@limiter.limit("10/minute")
def delete_transaction(
    request: Request,
    tx_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Delete a manual cash transaction (admin only). Cannot delete auto-linked entries."""
    user = _current_user(current_user)
    try:
        logger.info(f"Admin '{user}' deleting transaction ID {tx_id}")
        tx = db.query(models.CashTransaction).filter_by(id=tx_id).first()
        if not tx:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

        linked_employee_record = (
            db.query(models.EmployeeLedgerEntry.id)
            .filter(models.EmployeeLedgerEntry.cash_transaction_id == tx_id)
            .first()
            or db.query(models.EmployeeSalaryPayment.id)
            .filter(models.EmployeeSalaryPayment.cash_transaction_id == tx_id)
            .first()
            or db.query(models.SaleReturn.id)
            .filter(models.SaleReturn.cash_transaction_id == tx_id)
            .first()
        )
        if str(tx.reference_type or "").strip().lower() not in {"", "manual"} or linked_employee_record:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Posted financial transactions cannot be deleted; use the related reversal/return workflow",
            )

        old_val = {
            "amount": tx.amount,
            "type": tx.tx_type,
            "subtype": tx.cash_in_type or tx.cash_out_type,
            "reference_type": tx.reference_type,
            "reference_id": tx.reference_id,
            "reference_no": tx.reference_no
        }

        db.query(models.ExternalFund).filter_by(cash_tx_id=tx_id).delete()
        db.flush()

        db.delete(tx)
        db.commit()

        _write_audit(db, "cash_tx_deleted", "cash_transaction", tx_id, old_val=old_val, user=user)
        db.commit()

        return {"success": True, "message": "Transaction deleted"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete transaction {tx_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Delete transaction failed: {str(e)}")


@router.get("/opening-cash", response_model=OpeningCashResponse)
@limiter.limit("30/minute")
def get_opening_cash(
    request: Request,
    query_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get opening cash for a date."""
    try:
        user = _current_user(current_user)
        for_date = query_date or date.today().isoformat()
        logger.info(f"User '{user}' fetching opening cash for {for_date}")
        opening = _get_opening_cash(db, for_date)
        return {"date": for_date, "opening_cash": round(opening, 2)}
    except Exception as e:
        logger.error(f"Error fetching opening cash: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get opening cash: {str(e)}")


@router.put("/opening-cash", response_model=OpeningCashSetResponse)
@limiter.limit("10/minute")
def set_opening_cash(
    request: Request,
    data: OpeningCashSet,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Manually set opening cash for first-day setup (admin only, audited)."""
    user = _current_user(current_user)
    if data.amount < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Opening cash cannot be negative")
    if data.amount > 1000000000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Opening cash cannot exceed 1 Billion")

    try:
        logger.info(f"Admin '{user}' setting opening cash to {data.amount}")
        setting = db.query(models.Setting).filter_by(key="opening_cash_manual").first()
        old_val = float(setting.value) if setting else None

        if setting:
            setting.value = str(data.amount)
        else:
            db.add(models.Setting(key="opening_cash_manual", value=str(data.amount)))

        db.commit()

        _write_audit(db, "opening_cash_changed", "opening_cash", 0,
                     old_val={"amount": old_val}, new_val={"amount": data.amount, "notes": data.notes}, user=user)
        db.commit()

        return {"success": True, "opening_cash": data.amount}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to set opening cash: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Set opening cash failed: {str(e)}")


@router.get("/day-closing/{closing_date}", response_model=DayClosingResponse)
@limiter.limit("30/minute")
def get_day_closing(
    request: Request,
    closing_date: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get day closing record for a date."""
    try:
        user = _current_user(current_user)
        logger.info(f"User '{user}' fetching day closing for {closing_date}")
        closing = db.query(models.DayClosing).filter_by(date=closing_date).first()
        if not closing:
            opening = _get_opening_cash(db, closing_date)
            summary = _get_cash_summary(db, closing_date)
            expected = opening + summary["total_in"] - summary["total_out"]
            return {
                "date": closing_date,
                "status": "open",
                "opening_cash": round(opening, 2),
                "total_cash_in": summary["total_in"],
                "total_cash_out": summary["total_out"],
                "expected_closing_cash": round(expected, 2),
                "actual_counted_cash": None,
                "difference": None,
            }
        return {
            "id": closing.id,
            "date": closing.date,
            "status": closing.status,
            "opening_cash": closing.opening_cash,
            "total_cash_in": closing.total_cash_in,
            "total_cash_out": closing.total_cash_out,
            "expected_closing_cash": closing.expected_closing_cash,
            "actual_counted_cash": closing.actual_counted_cash,
            "difference": closing.difference,
            "notes": closing.notes,
            "closed_by": closing.closed_by,
            "closed_at": closing.closed_at,
        }
    except Exception as e:
        logger.error(f"Error fetching day closing for {closing_date}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get day closing: {str(e)}")


@router.post("/day-closing", response_model=DayClosingCreateResponse)
@limiter.limit("10/minute")
def create_day_closing(
    request: Request,
    data: DayClosingCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Finalize day closing with actual counted cash."""
    user = _current_user(current_user)
    if data.actual_counted_cash < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Actual counted cash cannot be negative")
    if data.actual_counted_cash > 1000000000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Actual counted cash cannot exceed 1 Billion")

    try:
        logger.info(f"User '{user}' creating day closing for {data.date}: counted={data.actual_counted_cash}")
        existing = db.query(models.DayClosing).filter_by(date=data.date).first()
        if existing and existing.status == "closed":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Day {data.date} is already closed. Contact admin to re-open.")

        opening = _get_opening_cash(db, data.date)
        summary = _get_cash_summary(db, data.date)
        expected = opening + summary["total_in"] - summary["total_out"]
        difference = data.actual_counted_cash - expected

        if existing:
            existing.opening_cash = round(opening, 2)
            existing.total_cash_in = summary["total_in"]
            existing.total_cash_out = summary["total_out"]
            existing.expected_closing_cash = round(expected, 2)
            existing.actual_counted_cash = data.actual_counted_cash
            existing.difference = round(difference, 2)
            existing.status = "closed"
            existing.notes = data.notes or ""
            existing.closed_by = user
            existing.closed_at = datetime.now()
            closing = existing
        else:
            closing = models.DayClosing(
                date=data.date,
                opening_cash=round(opening, 2),
                total_cash_in=summary["total_in"],
                total_cash_out=summary["total_out"],
                expected_closing_cash=round(expected, 2),
                actual_counted_cash=data.actual_counted_cash,
                difference=round(difference, 2),
                status="closed",
                notes=data.notes or "",
                closed_by=user,
                closed_at=datetime.now(),
            )
            db.add(closing)

        db.commit()
        db.refresh(closing)

        _write_audit(db, "day_closed", "day_closing", closing.id,
                     new_val={"date": data.date, "expected": round(expected, 2),
                               "actual": data.actual_counted_cash, "diff": round(difference, 2)}, user=user)
        db.commit()

        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
        except Exception as backup_err:
            logger.warning(f"Auto-backup warning after day closing: {backup_err}")

        return {
            "success": True,
            "date": data.date,
            "opening_cash": round(opening, 2),
            "total_cash_in": summary["total_in"],
            "total_cash_out": summary["total_out"],
            "expected_closing_cash": round(expected, 2),
            "actual_counted_cash": data.actual_counted_cash,
            "difference": round(difference, 2),
            "short_or_excess": "excess" if difference > 0 else ("short" if difference < 0 else "exact"),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Day Closing failed for user '{user}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Day closing failed: {str(e)}")


@router.get("/external-funds", response_model=ExternalFundsResponse)
@limiter.limit("30/minute")
def list_external_funds(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    fund_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List borrowed money, owner injections, friend loans and their returns."""
    try:
        user = _current_user(current_user)
        logger.info(f"User '{user}' fetching external funds (start={start_date}, end={end_date}, type={fund_type})")
        if not start_date:
            start_date = date.today().replace(day=1).isoformat()
        if not end_date:
            end_date = date.today().isoformat()

        q = db.query(models.ExternalFund).filter(
            models.ExternalFund.created_at >= start_date + " 00:00:00",
            models.ExternalFund.created_at <= end_date + " 23:59:59",
        )
        if fund_type:
            q = q.filter(models.ExternalFund.fund_type == fund_type)

        funds = q.order_by(models.ExternalFund.created_at.desc()).all()

        total_in = sum(f.amount for f in funds if f.direction == "in")
        total_out = sum(f.amount for f in funds if f.direction == "out")

        return {
            "total_received": round(total_in, 2),
            "total_returned": round(total_out, 2),
            "outstanding_balance": round(total_in - total_out, 2),
            "items": [
                {
                    "id": f.id,
                    "fund_type": f.fund_type,
                    "direction": f.direction,
                    "party_name": f.party_name,
                    "amount": round(f.amount, 2),
                    "notes": f.notes,
                    "created_by": f.created_by,
                    "date": f.created_at.strftime("%Y-%m-%d") if f.created_at else "",
                    "time": f.created_at.strftime("%H:%M") if f.created_at else "",
                }
                for f in funds
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching external funds: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list external funds: {str(e)}")


@router.get("/audit-log", response_model=AuditLogResponse)
@limiter.limit("30/minute")
def list_audit_log(
    request: Request,
    limit: int = Query(100, ge=1, le=1000000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """View audit log for cash operations."""
    try:
        user = _current_user(current_user)
        logger.info(f"Admin '{user}' fetching audit log (limit={limit}, offset={offset})")
        total = db.query(models.AuditLog).count()
        entries = db.query(models.AuditLog).order_by(
            models.AuditLog.created_at.desc()
        ).offset(offset).limit(limit).all()
        return {
            "count": total,
            "items": [
                {
                    "id": e.id, "action": e.action, "entity_type": e.entity_type,
                    "entity_id": e.entity_id, "user": e.user,
                    "old_value": e.old_value, "new_value": e.new_value,
                    "date": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
                }
                for e in entries
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching audit log: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list audit log: {str(e)}")


@router.get("/health", response_model=CashbookHealthCheckResponse)
@limiter.limit("30/minute")
def cashbook_health(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Health check for cashbook module."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "cashbook",
        "endpoints": [
            "/balance",
            "/summary",
            "/transactions",
            "/cash-in",
            "/cash-out",
            "/transaction/{tx_id}",
            "/opening-cash",
            "/day-closing",
            "/external-funds",
            "/audit-log",
            "/health"
        ]
    }
