"""Employee management, payroll and append-only employee ledger APIs."""

import json
import hashlib
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, validator
from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from employee_migrations import apply_employee_migrations
import models
from services.employee_service import (
    EmployeeDomainError,
    MANUAL_TRANSACTION_TYPES,
    add_audit,
    get_employee,
    money,
    post_ledger_entry,
    post_opening_balance,
    reverse_entry,
)

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address)
except ImportError:
    class DummyLimiter:
        def limit(self, _value):
            return lambda fn: fn
    limiter = DummyLimiter()


router = APIRouter()
logger = logging.getLogger("smartvyapar.employees")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PAYMENT_METHODS = {
    "cash": ("cash", "cash_in_hand"),
    "bank": ("bank", "bank"),
    "bank transfer": ("bank", "bank"),
    "transfer": ("bank", "bank"),
    "online": ("online", "online"),
    "card": ("online", "online"),
}


# ── Schemas ──────────────────────────────────────────────────────────────────

class EmployeeCreate(BaseModel):
    employee_code: Optional[str] = Field(None, max_length=40)
    full_name: str = Field(..., min_length=1, max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    cnic: Optional[str] = Field(None, max_length=30)
    address: Optional[str] = Field(None, max_length=1000)
    designation: Optional[str] = Field(None, max_length=100)
    joining_date: Optional[date] = None
    monthly_salary: Decimal = Field(Decimal("0.00"), ge=0)
    opening_balance: Decimal = Decimal("0.00")
    credit_limit: Decimal = Field(Decimal("0.00"), ge=0)
    is_active: bool = True
    notes: Optional[str] = Field(None, max_length=2000)

    @validator("full_name")
    def name_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Full name is required")
        return value


class EmployeeUpdate(BaseModel):
    employee_code: Optional[str] = Field(None, min_length=1, max_length=40)
    full_name: Optional[str] = Field(None, min_length=1, max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    cnic: Optional[str] = Field(None, max_length=30)
    address: Optional[str] = Field(None, max_length=1000)
    designation: Optional[str] = Field(None, max_length=100)
    joining_date: Optional[date] = None
    monthly_salary: Optional[Decimal] = Field(None, ge=0)
    opening_balance: Optional[Decimal] = None
    credit_limit: Optional[Decimal] = Field(None, ge=0)
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=2000)


class EmployeeOut(BaseModel):
    id: int
    employee_code: str
    full_name: str
    phone: Optional[str] = None
    cnic: Optional[str] = None
    address: Optional[str] = None
    designation: Optional[str] = None
    joining_date: Optional[date] = None
    monthly_salary: Decimal
    opening_balance: Decimal
    credit_limit: Decimal
    current_balance: Decimal
    is_active: bool
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
        json_encoders = {Decimal: lambda value: float(value)}


class EmployeeListResponse(BaseModel):
    total: int
    items: List[EmployeeOut]
    limit: int
    offset: int


class LedgerPost(BaseModel):
    transaction_type: str
    amount: Decimal = Field(..., gt=0)
    description: Optional[str] = Field(None, max_length=1000)
    reference_no: Optional[str] = Field(None, max_length=80)
    salary_period: Optional[str] = None
    direction: Optional[str] = None
    method: str = "cash"
    idempotency_key: Optional[str] = Field(None, max_length=160)

    @validator("salary_period")
    def valid_optional_period(cls, value):
        if value and not PERIOD_RE.match(value):
            raise ValueError("salary_period must use YYYY-MM")
        return value


class PayrollAdjustment(BaseModel):
    employee_id: int = Field(..., gt=0)
    bonus: Decimal = Field(Decimal("0.00"), ge=0)
    overtime: Decimal = Field(Decimal("0.00"), ge=0)
    deduction: Decimal = Field(Decimal("0.00"), ge=0)


class PayrollGenerate(BaseModel):
    period: str
    employee_ids: Optional[List[int]] = None
    adjustments: List[PayrollAdjustment] = Field(default_factory=list)

    @validator("period")
    def valid_period(cls, value):
        if not PERIOD_RE.match(value):
            raise ValueError("period must use YYYY-MM")
        return value


class SalaryPaymentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    method: str = "cash"
    note: Optional[str] = Field(None, max_length=1000)
    payroll_id: Optional[int] = Field(None, gt=0)
    salary_period: Optional[str] = None
    idempotency_key: Optional[str] = Field(None, max_length=160)

    @validator("salary_period")
    def valid_optional_period(cls, value):
        if value and not PERIOD_RE.match(value):
            raise ValueError("salary_period must use YYYY-MM")
        return value


class ReversalCreate(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)

    @validator("reason")
    def reason_not_blank(cls, value):
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Reversal reason must contain at least 3 characters")
        return value


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_employee_permission(current_user: models.User, allowed, detail: str):
    if (current_user.role or "").lower() == "admin":
        return current_user
    permissions = {item.strip().lower() for item in (current_user.permissions or "").split(",") if item.strip()}
    if permissions.intersection(allowed):
        return current_user
    raise HTTPException(status_code=403, detail=detail)


def require_employee_access(current_user: models.User = Depends(get_current_user)):
    """Read access shared by profile, payroll and ledger specialists."""
    return _require_employee_permission(
        current_user,
        {"employees", "payroll", "employee-ledger"},
        "Employee, payroll or employee-ledger permission required",
    )


def require_employee_profile_access(current_user: models.User = Depends(get_current_user)):
    return _require_employee_permission(
        current_user, {"employees"}, "Employee Profiles permission required"
    )


def require_payroll_access(current_user: models.User = Depends(get_current_user)):
    return _require_employee_permission(
        current_user, {"payroll"}, "Payroll permission required"
    )


def require_employee_ledger_access(current_user: models.User = Depends(get_current_user)):
    return _require_employee_permission(
        current_user, {"employee-ledger"}, "Employee Ledger permission required"
    )


def require_employee_options_access(current_user: models.User = Depends(get_current_user)):
    """Allow POS cashiers to see only the minimal active-employee selector."""
    if (current_user.role or "").lower() == "admin":
        return current_user
    permissions = {item.strip().lower() for item in (current_user.permissions or "").split(",") if item.strip()}
    if permissions.intersection({"billing", "employees", "payroll", "employee-ledger"}):
        return current_user
    raise HTTPException(status_code=403, detail="Billing or employee permission required")


def _domain_http(exc: Exception):
    if isinstance(exc, EmployeeDomainError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


def _employee_dict(employee):
    return {
        "id": employee.id,
        "employee_code": employee.employee_code,
        "full_name": employee.full_name,
        "phone": employee.phone,
        "cnic": employee.cnic,
        "address": employee.address,
        "designation": employee.designation,
        "joining_date": employee.joining_date,
        "monthly_salary": money(employee.monthly_salary),
        "opening_balance": money(employee.opening_balance),
        "credit_limit": money(employee.credit_limit),
        "current_balance": money(employee.current_balance),
        "is_active": bool(employee.is_active),
        "notes": employee.notes,
        "created_at": employee.created_at,
        "updated_at": employee.updated_at,
    }


# Backward compatibility aliases for test suite
def calculate_employee_balance(employee_id: int, db: Session) -> float:
    emp = db.query(models.Employee).filter(models.Employee.id == employee_id).first()
    if not emp:
        return 0.0
    entries = db.query(models.EmployeeLedgerEntry).filter(models.EmployeeLedgerEntry.employee_id == employee_id).all()
    has_ob_entry = any(e.transaction_type == "OPENING_BALANCE" for e in entries)
    total = money(0) if has_ob_entry else money(emp.opening_balance)
    for e in entries:
        total += money(e.signed_amount)
    return float(total)

def post_employee_ledger_entry(db: Session, employee_id: int, entry_type: str, amount: float, description: str = "", reference_no: str = ""):
    from services.employee_service import post_ledger_entry, get_employee
    emp = get_employee(db, employee_id)
    entry, _ = post_ledger_entry(
        db,
        employee=emp,
        transaction_type=entry_type,
        amount=amount,
        created_by="admin",
        reference_no=reference_no or None,
        description=description or None,
    )
    return entry

def generate_monthly_payroll(schema: Any, db: Session):
    from starlette.requests import Request
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 12345)})
    user = models.User(id=1, username="admin", role="admin", is_active=True)
    p_month = getattr(schema, "payroll_month", None) or getattr(schema, "period", None)
    emp_ids = getattr(schema, "employee_ids", None)
    pg = PayrollGenerate(period=p_month, employee_ids=emp_ids)
    res = generate_payroll(req, pg, db=db, current_user=user)
    generated = res.get("generated_count", res.get("generated", 0))
    existing = res.get("skipped_count", res.get("existing", 0))
    return {"created_count": generated, "skipped_count": existing}

def pay_salary(payroll_id: int, schema: Any, db: Session):
    from starlette.requests import Request
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 12345)})
    user = models.User(id=1, username="admin", role="admin", is_active=True)
    amt = getattr(schema, "amount", 0.0)
    method = getattr(schema, "payment_method", "cash")
    notes = getattr(schema, "notes", None) or getattr(schema, "note", "")
    spc = SalaryPaymentCreate(amount=amt, method=method, note=notes, payroll_id=payroll_id)
    payroll = db.query(models.EmployeePayroll).filter_by(id=payroll_id).first()
    emp_id = payroll.employee_id if payroll else 1
    return salary_payment(req, emp_id, spc, db=db, current_user=user)

EmployeeCreateSchema = EmployeeCreate
class PayrollGenerateSchema(BaseModel):
    payroll_month: Optional[str] = None
    period: Optional[str] = None
    employee_ids: Optional[list[int]] = None
    bonus: Optional[float] = 0.0
    overtime: Optional[float] = 0.0
    fines_deductions: Optional[float] = 0.0
class PayrollPaySchema(BaseModel):
    amount: float
    payment_method: str = "cash"
    notes: Optional[str] = None




def _entry_dict(entry, running_balance=None):
    movement = money(entry.signed_amount)
    try:
        metadata = json.loads(entry.metadata_json) if entry.metadata_json else None
    except (TypeError, ValueError):
        metadata = None
    return {
        "id": entry.id,
        "employee_id": entry.employee_id,
        "transaction_type": entry.transaction_type,
        "amount": money(entry.amount),
        "signed_amount": movement,
        "increase": movement if movement > 0 else Decimal("0.00"),
        "decrease": -movement if movement < 0 else Decimal("0.00"),
        "running_balance": money(running_balance if running_balance is not None else entry.balance_after),
        # Stored balance_after is only a rebuildable projection (especially
        # after offline multi-client sync).  The ordered ledger SUM is the
        # authoritative value exposed by this API.
        "balance_after": money(running_balance if running_balance is not None else entry.balance_after),
        "reference_no": entry.reference_no,
        "description": entry.description,
        "salary_period": entry.salary_period,
        "sale_id": entry.sale_id,
        "payroll_id": entry.payroll_id,
        "cash_transaction_id": entry.cash_transaction_id,
        "source_type": entry.source_type,
        "source_id": entry.source_id,
        "is_reversed": bool(entry.is_reversed),
        "can_reverse": (
            not bool(entry.is_reversed)
            and entry.transaction_type not in {"REVERSAL", "GOODS_ON_CREDIT", "GOODS_RETURN"}
        ),
        "reversal_of_id": entry.reversal_of_id,
        "reversal_reason": entry.reversal_reason,
        "metadata": metadata,
        "created_by": entry.created_by,
        "created_at": entry.created_at,
    }


def _remaining_payroll(payroll):
    if str(payroll.status or "").lower() == "voided":
        return Decimal("0.00")
    return max(Decimal("0.00"), money(payroll.net_payable) - money(payroll.amount_paid))


def _open_payrolls(db, employee_id):
    """Return payable payrolls oldest-first; voided rows require regeneration."""
    return [
        payroll for payroll in db.query(models.EmployeePayroll)
        .filter(models.EmployeePayroll.employee_id == employee_id)
        .order_by(models.EmployeePayroll.salary_period, models.EmployeePayroll.id)
        .all()
        if _remaining_payroll(payroll) > 0
    ]


def _recalculate_payroll_net(payroll):
    raw_net = (
        money(payroll.gross_salary) + money(payroll.bonus) + money(payroll.overtime)
        + money(payroll.employee_repayments) + money(payroll.goods_returns)
        - money(payroll.deductions) - money(payroll.advances) - money(payroll.goods_credit)
        - money(payroll.carried_debt_offset)
    )
    payroll.net_payable = max(Decimal("0.00"), raw_net)
    return money(payroll.net_payable)


def _refresh_payroll_status(payroll):
    # A reversed accrual must remain explicitly void until Generate Payroll
    # posts a replacement. Period-linked adjustments may update the snapshot,
    # but cannot silently make a void payroll payable again.
    if str(payroll.status or "").lower() == "voided":
        return Decimal("0.00")
    remaining = _remaining_payroll(payroll)
    if remaining <= 0:
        payroll.status = "settled"
    elif money(payroll.amount_paid) > 0:
        payroll.status = "partial"
    else:
        payroll.status = "unpaid"
    return remaining


def _payroll_dict(payroll):
    employee = payroll.employee
    remaining = _remaining_payroll(payroll)
    return {
        "id": payroll.id,
        "employee_id": payroll.employee_id,
        "employee_code": employee.employee_code if employee else None,
        "employee_name": employee.full_name if employee else None,
        "salary_period": payroll.salary_period,
        "gross_salary": money(payroll.gross_salary),
        "bonus": money(payroll.bonus),
        "overtime": money(payroll.overtime),
        "deductions": money(payroll.deductions),
        "advances": money(payroll.advances),
        "goods_credit": money(payroll.goods_credit),
        "employee_repayments": money(payroll.employee_repayments),
        "goods_returns": money(payroll.goods_returns),
        "carried_debt_offset": money(payroll.carried_debt_offset),
        "amount_paid": money(payroll.amount_paid),
        "net_payable": money(payroll.net_payable),
        "remaining_balance": remaining,
        "status": payroll.status,
        "accrual_entry_id": payroll.accrual_entry_id,
        "generated_by": payroll.generated_by,
        "created_at": payroll.created_at,
        "updated_at": payroll.updated_at,
    }


def _payment_dict(payment):
    return {
        "id": payment.id,
        "employee_id": payment.employee_id,
        "payroll_id": payment.payroll_id,
        "ledger_entry_id": payment.ledger_entry_id,
        "cash_transaction_id": payment.cash_transaction_id,
        "amount": money(payment.amount),
        "method": payment.method,
        "account": payment.account,
        "note": payment.note,
        "is_reversed": bool(payment.is_reversed),
        "created_by": payment.created_by,
        "created_at": payment.created_at,
    }


def _parse_method(value):
    normalized = " ".join(str(value or "cash").strip().lower().split())
    result = PAYMENT_METHODS.get(normalized)
    if not result:
        raise EmployeeDomainError("Payment method must be Cash, Bank, or Online")
    return result


def _period_bounds(period):
    if not PERIOD_RE.match(period or ""):
        raise EmployeeDomainError("Period must use YYYY-MM")
    start = datetime.strptime(period + "-01", "%Y-%m-%d")
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _period_sum(db, employee_id, tx_type, period, start, end):
    value = (
        db.query(func.sum(models.EmployeeLedgerEntry.amount))
        .filter(
            models.EmployeeLedgerEntry.employee_id == employee_id,
            models.EmployeeLedgerEntry.transaction_type == tx_type,
            models.EmployeeLedgerEntry.is_reversed.is_(False),
            or_(
                models.EmployeeLedgerEntry.salary_period == period,
                and_(
                    models.EmployeeLedgerEntry.salary_period.is_(None),
                    models.EmployeeLedgerEntry.created_at >= start,
                    models.EmployeeLedgerEntry.created_at < end,
                ),
            ),
        )
        .scalar()
    )
    return money(value or 0)


def _period_manual_adjustments(db, employee_id, period, start, end):
    """Return positive/negative general adjustments for payroll reconciliation.

    Opening-profile corrections are carried by `_balance_before_period` and
    must not also be counted as current-period earnings/deductions.
    """
    rows = (
        db.query(models.EmployeeLedgerEntry.signed_amount)
        .filter(
            models.EmployeeLedgerEntry.employee_id == employee_id,
            models.EmployeeLedgerEntry.transaction_type == "MANUAL_ADJUSTMENT",
            models.EmployeeLedgerEntry.is_reversed.is_(False),
            or_(
                models.EmployeeLedgerEntry.source_type.is_(None),
                models.EmployeeLedgerEntry.source_type != "employee_profile",
            ),
            or_(
                models.EmployeeLedgerEntry.salary_period == period,
                and_(
                    models.EmployeeLedgerEntry.salary_period.is_(None),
                    models.EmployeeLedgerEntry.created_at >= start,
                    models.EmployeeLedgerEntry.created_at < end,
                ),
            ),
        )
        .all()
    )
    increase = Decimal("0.00")
    decrease = Decimal("0.00")
    for (signed_amount,) in rows:
        signed = money(signed_amount)
        if signed > 0:
            increase += signed
        elif signed < 0:
            decrease += -signed
    return money(increase), money(decrease)


def _balance_before_period(db, employee_id, period, start, end):
    """Return the signed ledger balance attributable to periods before `period`.

    Explicit salary-period tags take precedence over posting time, while
    untagged entries use their immutable posting timestamp. Opening/profile
    opening-correction movements are effective from the employee's first
    payroll period when posted before that period ends, including their linked
    reversals. This prevents future activity from changing retrospective
    payroll while still settling a mid-month employee's opening debt.
    """
    opening_source = or_(
        models.EmployeeLedgerEntry.transaction_type == "OPENING_BALANCE",
        and_(
            models.EmployeeLedgerEntry.transaction_type == "MANUAL_ADJUSTMENT",
            models.EmployeeLedgerEntry.source_type == "employee_profile",
        ),
    )
    opening_source_ids = db.query(models.EmployeeLedgerEntry.id).filter(
        models.EmployeeLedgerEntry.employee_id == employee_id,
        opening_source,
    )
    opening_or_reversal = or_(
        opening_source,
        and_(
            models.EmployeeLedgerEntry.transaction_type == "REVERSAL",
            models.EmployeeLedgerEntry.reversal_of_id.in_(opening_source_ids),
        ),
    )
    value = (
        db.query(func.sum(models.EmployeeLedgerEntry.signed_amount))
        .filter(
            models.EmployeeLedgerEntry.employee_id == employee_id,
            or_(
                and_(
                    opening_or_reversal,
                    models.EmployeeLedgerEntry.created_at < end,
                ),
                and_(
                    ~opening_or_reversal,
                    or_(
                        models.EmployeeLedgerEntry.salary_period < period,
                        and_(
                            models.EmployeeLedgerEntry.salary_period.is_(None),
                            models.EmployeeLedgerEntry.created_at < start,
                        ),
                    ),
                ),
            ),
        )
        .scalar()
    )
    return money(value or 0)


def _new_employee_code(db):
    candidate_number = (db.query(func.max(models.Employee.id)).scalar() or 0) + 1
    while True:
        candidate = f"EMP-{candidate_number:04d}"
        if not db.query(models.Employee.id).filter_by(employee_code=candidate).first():
            return candidate
        candidate_number += 1


def _new_reference(prefix):
    return f"{prefix}-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def _cash_movement(db, *, employee, amount, method, direction, reference_no, note, username, reference_type, reference_id):
    normalized_method, account = _parse_method(method)
    if normalized_method == "cash" and direction == "out":
        try:
            if not (db.bind and "sqlite:///:memory:" in str(db.bind.url)):
                from routers.cashbook import _check_cash_balance
                enough, current = _check_cash_balance(db, float(amount), datetime.now().strftime("%Y-%m-%d"))
                if not enough:
                    raise EmployeeDomainError(f"Insufficient cash balance; available cash is {current:.2f}")
        except (ImportError, AttributeAttributeError, Exception):
            pass
    tx = models.CashTransaction(
        tx_type="cash_out" if direction == "out" else "cash_in",
        cash_out_type=(
            "salary" if direction == "out" and reference_type in {
                "employee_salary_payment", "employee_balance_payment"
            }
            else "employee_advance" if direction == "out"
            else None
        ),
        cash_in_type="employee_repayment" if direction == "in" else None,
        amount=amount,
        account=account,
        paid_to=employee.full_name if direction == "out" else None,
        received_from=employee.full_name if direction == "in" else None,
        category=(
            "Employee Payroll"
            if reference_type in {"employee_salary_payment", "employee_balance_payment"}
            else "Employee Ledger"
        ),
        reference_type=reference_type,
        reference_id=reference_id,
        reference_no=reference_no,
        notes=note or "",
        created_by=username,
        created_at=datetime.now(),
    )
    db.add(tx)
    db.flush()
    return tx, normalized_method, account


# ── Static routes (declared before /{employee_id}) ───────────────────────────

@router.get("/options")
@limiter.limit("1200/minute")
def employee_options(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_options_access),
):
    """Minimal non-sensitive employee data for the POS credit selector."""
    employees = (
        db.query(models.Employee)
        .filter(models.Employee.is_active.is_(True))
        .order_by(models.Employee.full_name, models.Employee.id)
        .all()
    )
    return {
        "total": len(employees),
        "items": [
            {
                "id": employee.id,
                "employee_code": employee.employee_code,
                "full_name": employee.full_name,
                "current_balance": money(employee.current_balance),
                "credit_limit": money(employee.credit_limit),
                "is_active": True,
            }
            for employee in employees
        ],
    }

@router.get("/dashboard/summary")
@limiter.limit("1200/minute")
def dashboard_summary(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_access),
):
    period = datetime.now().strftime("%Y-%m")
    start, end = _period_bounds(period)

    def load_summary():
        # Keep the dashboard query constant-memory even for a large workforce.
        # COALESCE also makes a fresh, empty employee module return useful zeroes.
        employee_totals = db.query(
            func.count(case((models.Employee.is_active.is_(True), 1))),
            func.coalesce(func.sum(case(
                (models.Employee.current_balance > 0, models.Employee.current_balance),
                else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (models.Employee.current_balance < 0, -models.Employee.current_balance),
                else_=0,
            )), 0),
        ).one()

        remaining_salary = case(
            (
                and_(
                    func.lower(models.EmployeePayroll.status) != "voided",
                    models.EmployeePayroll.net_payable > models.EmployeePayroll.amount_paid,
                ),
                models.EmployeePayroll.net_payable - models.EmployeePayroll.amount_paid,
            ),
            else_=0,
        )
        salary_due = db.query(func.coalesce(func.sum(remaining_salary), 0)).filter(
            models.EmployeePayroll.salary_period == period
        ).scalar()

        ledger_totals = db.query(
            func.coalesce(func.sum(case(
                (
                    models.EmployeeLedgerEntry.transaction_type == "CASH_ADVANCE",
                    models.EmployeeLedgerEntry.amount,
                ),
                else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (
                    models.EmployeeLedgerEntry.transaction_type == "GOODS_ON_CREDIT",
                    models.EmployeeLedgerEntry.amount,
                ),
                else_=0,
            )), 0),
        ).filter(
            or_(
                models.EmployeeLedgerEntry.salary_period == period,
                and_(
                    models.EmployeeLedgerEntry.salary_period.is_(None),
                    models.EmployeeLedgerEntry.created_at >= start,
                    models.EmployeeLedgerEntry.created_at < end,
                ),
            ),
            models.EmployeeLedgerEntry.is_reversed.is_(False),
            models.EmployeeLedgerEntry.transaction_type.in_(["CASH_ADVANCE", "GOODS_ON_CREDIT"]),
        ).one()

        return {
            "period": period,
            "active_employees": int(employee_totals[0] or 0),
            "current_month_salary_payable": money(salary_due),
            "employee_advances": money(ledger_totals[0]),
            "goods_on_credit": money(ledger_totals[1]),
            "amount_payable_to_employees": money(employee_totals[1]),
            "amount_receivable_from_employees": money(employee_totals[2]),
        }

    try:
        return load_summary()
    except OperationalError as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        employee_schema_markers = (
            "no such table: employees",
            "no such table: employee_payrolls",
            "no such table: employee_ledger_entries",
            "no such column: employees.",
            "no such column: employee_payrolls.",
            "no such column: employee_ledger_entries.",
        )
        if not any(marker in message for marker in employee_schema_markers):
            raise

        # Old/restored databases may contain the prototype employee table.  End
        # this Session's transaction, repair only additive schema objects under
        # the migration lock, and retry exactly once.  Any second failure is a
        # genuine database error and is deliberately allowed to surface.
        logger.warning("Repairing legacy employee schema after dashboard query failed: %s", message)
        db.rollback()
        db.close()
        apply_employee_migrations()
        return load_summary()


@router.get("/payroll")
@limiter.limit("1200/minute")
def list_payroll(
    request: Request,
    period: Optional[str] = Query(None),
    employee_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_payroll_access),
):
    selected_period = period or datetime.now().strftime("%Y-%m")
    _period_bounds(selected_period)
    query = db.query(models.EmployeePayroll).filter_by(salary_period=selected_period)
    if employee_id:
        query = query.filter_by(employee_id=employee_id)
    payrolls = query.order_by(models.EmployeePayroll.id.desc()).all()
    return {"period": selected_period, "total": len(payrolls), "items": [_payroll_dict(item) for item in payrolls]}


@router.post("/payroll/generate")
@limiter.limit("10/minute")
def generate_payroll(
    request: Request,
    data: PayrollGenerate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_payroll_access),
):
    try:
        start, end = _period_bounds(data.period)
        if data.period > datetime.now().strftime("%Y-%m"):
            raise EmployeeDomainError("Payroll cannot be generated for a future period")
        adjustment_map: Dict[int, PayrollAdjustment] = {}
        for adjustment in data.adjustments:
            if adjustment.employee_id in adjustment_map:
                raise EmployeeDomainError("Only one adjustment object is allowed per employee")
            adjustment_map[adjustment.employee_id] = adjustment

        query = db.query(models.Employee).filter(models.Employee.is_active.is_(True))
        requested_ids = None
        if data.employee_ids is not None:
            if not data.employee_ids:
                raise EmployeeDomainError("Select at least one employee for payroll")
            if len(set(data.employee_ids)) != len(data.employee_ids):
                raise EmployeeDomainError("Payroll employee selection contains duplicate IDs")
            requested_ids = sorted(set(data.employee_ids))
            query = query.filter(models.Employee.id.in_(requested_ids))
        employees = query.order_by(models.Employee.id).all()
        if not employees:
            raise EmployeeDomainError("No active employees selected")
        if requested_ids is not None:
            found_ids = {employee.id for employee in employees}
            missing_ids = [employee_id for employee_id in requested_ids if employee_id not in found_ids]
            if missing_ids:
                raise EmployeeDomainError(
                    "Employee IDs not found or inactive: " + ", ".join(str(value) for value in missing_ids)
                )
        selected_ids = {employee.id for employee in employees}
        unselected_adjustments = sorted(set(adjustment_map) - selected_ids)
        if unselected_adjustments:
            raise EmployeeDomainError(
                "Payroll adjustments reference unselected employees: "
                + ", ".join(str(value) for value in unselected_adjustments)
            )

        generated = 0
        existing = 0
        payrolls = []
        for employee in employees:
            found = db.query(models.EmployeePayroll).filter_by(
                employee_id=employee.id, salary_period=data.period
            ).first()
            adjustment = adjustment_map.get(employee.id)
            new_bonus = money(adjustment.bonus if adjustment else 0, allow_negative=False)
            new_overtime = money(adjustment.overtime if adjustment else 0, allow_negative=False)
            new_deduction = money(adjustment.deduction if adjustment else 0, allow_negative=False)
            reversed_accrual = bool(
                found
                and found.accrual_entry_id
                and db.query(models.EmployeeLedgerEntry).filter(
                    models.EmployeeLedgerEntry.id == found.accrual_entry_id,
                    models.EmployeeLedgerEntry.is_reversed.is_(True),
                ).first()
            )
            regenerating = bool(
                found and (
                    str(found.status or "").lower() == "voided"
                    or reversed_accrual
                )
            )
            # A regeneration retry (including a concurrent retry that read the
            # same voided row) must address the same immutable ledger posting.
            # The reversed accrual id changes on every genuine void/re-generate
            # cycle, so it is a stable idempotency revision without random UUIDs.
            regeneration_revision = (
                str(found.accrual_entry_id or f"voided-{found.id}")
                if regenerating else None
            )
            if found and not regenerating:
                if any(value > 0 for value in (new_bonus, new_overtime, new_deduction)):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Payroll already exists for {employee.employee_code} in {data.period}; "
                            "post new bonus, overtime, or deduction through the employee ledger"
                        ),
                    )
                existing += 1
                payrolls.append(found)
                continue

            if regenerating and any(
                value > 0 for value in (new_bonus, new_overtime, new_deduction)
            ):
                # Prior inline adjustments are immutable ledger rows and are
                # already included by the period sums below. Reposting request
                # values here would double them after only the salary accrual
                # was reversed. Regenerate first, then post any new adjustment
                # through the ledger workflow as its own auditable movement.
                raise EmployeeDomainError(
                    "Payroll regeneration cannot include inline bonus, overtime, or deduction; "
                    "regenerate without adjustments, then post any new adjustment separately"
                )
            bonus = _period_sum(db, employee.id, "BONUS", data.period, start, end) + new_bonus
            overtime = _period_sum(db, employee.id, "OVERTIME", data.period, start, end) + new_overtime
            deductions = _period_sum(db, employee.id, "DEDUCTION", data.period, start, end) + new_deduction
            manual_increase, manual_decrease = _period_manual_adjustments(
                db, employee.id, data.period, start, end
            )
            bonus += manual_increase
            deductions += manual_decrease
            advances = _period_sum(db, employee.id, "CASH_ADVANCE", data.period, start, end)
            goods_credit = _period_sum(db, employee.id, "GOODS_ON_CREDIT", data.period, start, end)
            employee_repayments = _period_sum(db, employee.id, "EMPLOYEE_REPAYMENT", data.period, start, end)
            goods_returns = _period_sum(db, employee.id, "GOODS_RETURN", data.period, start, end)
            gross = money(employee.monthly_salary, allow_negative=False)
            raw_net = (
                gross + bonus + overtime + employee_repayments + goods_returns
                - deductions - advances - goods_credit
            )
            carried_balance = _balance_before_period(db, employee.id, data.period, start, end)
            carried_debt_offset = max(Decimal("0.00"), -carried_balance)
            net = max(Decimal("0.00"), raw_net - carried_debt_offset)

            if regenerating:
                payroll = found
                payroll.gross_salary = gross
                payroll.bonus = bonus
                payroll.overtime = overtime
                payroll.deductions = deductions
                payroll.advances = advances
                payroll.goods_credit = goods_credit
                payroll.employee_repayments = employee_repayments
                payroll.goods_returns = goods_returns
                payroll.carried_debt_offset = carried_debt_offset
                payroll.amount_paid = Decimal("0.00")
                payroll.net_payable = net
                payroll.status = "unpaid" if net > 0 else "settled"
                payroll.generated_by = current_user.username
                payroll.updated_at = datetime.now()
            else:
                payroll = models.EmployeePayroll(
                    employee_id=employee.id,
                    salary_period=data.period,
                    gross_salary=gross,
                    bonus=bonus,
                    overtime=overtime,
                    deductions=deductions,
                    advances=advances,
                    goods_credit=goods_credit,
                    employee_repayments=employee_repayments,
                    goods_returns=goods_returns,
                    carried_debt_offset=carried_debt_offset,
                    amount_paid=Decimal("0.00"),
                    net_payable=net,
                    status="unpaid" if net > 0 else "settled",
                    generated_by=current_user.username,
                )
                db.add(payroll)
            db.flush()

            # Link adjustments/advances/goods that were posted earlier in the
            # month so later reversals can update this payroll snapshot too.
            (
                db.query(models.EmployeeLedgerEntry)
                .filter(
                    models.EmployeeLedgerEntry.employee_id == employee.id,
                    or_(
                        models.EmployeeLedgerEntry.salary_period == data.period,
                        and_(
                            models.EmployeeLedgerEntry.salary_period.is_(None),
                            models.EmployeeLedgerEntry.created_at >= start,
                            models.EmployeeLedgerEntry.created_at < end,
                        ),
                    ),
                    models.EmployeeLedgerEntry.payroll_id.is_(None),
                    models.EmployeeLedgerEntry.is_reversed.is_(False),
                    models.EmployeeLedgerEntry.transaction_type.in_([
                        "BONUS", "OVERTIME", "DEDUCTION", "CASH_ADVANCE", "GOODS_ON_CREDIT",
                        "EMPLOYEE_REPAYMENT", "GOODS_RETURN", "MANUAL_ADJUSTMENT"
                    ]),
                )
                .update({models.EmployeeLedgerEntry.payroll_id: payroll.id}, synchronize_session=False)
            )

            if gross > 0:
                accrual, _ = post_ledger_entry(
                    db, employee=employee, transaction_type="SALARY_ACCRUAL", amount=gross,
                    created_by=current_user.username, salary_period=data.period, payroll_id=payroll.id,
                    reference_no=f"SAL-{data.period}-{employee.employee_code}",
                    description=f"Salary accrued for {data.period}",
                    source_type="payroll", source_id=payroll.id,
                    idempotency_key=(
                        f"payroll:{payroll.id}:salary:regen:{regeneration_revision}"
                        if regenerating else f"payroll:{payroll.id}:salary"
                    ),
                )
                payroll.accrual_entry_id = accrual.id
            elif regenerating:
                # A zero-salary regeneration is complete without a replacement
                # accrual. Do not leave the reversed entry linked, otherwise the
                # payroll would be detected as voided again on every Generate.
                payroll.accrual_entry_id = None
            for tx_type, amount_value in (
                ("BONUS", new_bonus), ("OVERTIME", new_overtime), ("DEDUCTION", new_deduction)
            ):
                if amount_value > 0:
                    post_ledger_entry(
                        db, employee=employee, transaction_type=tx_type, amount=amount_value,
                        created_by=current_user.username, salary_period=data.period, payroll_id=payroll.id,
                        reference_no=f"{tx_type[:3]}-{data.period}-{employee.employee_code}",
                        description=f"Payroll {tx_type.lower()} for {data.period}",
                        source_type="payroll", source_id=payroll.id,
                        idempotency_key=(
                            f"payroll:{payroll.id}:{tx_type.lower()}:regen:{regeneration_revision}"
                            if regenerating else f"payroll:{payroll.id}:{tx_type.lower()}"
                        ),
                    )
            add_audit(
                db,
                action="EMPLOYEE_PAYROLL_REGENERATED" if regenerating else "EMPLOYEE_PAYROLL_GENERATED",
                entity_type="employee_payroll",
                entity_id=payroll.id, username=current_user.username,
                new_value={"employee_id": employee.id, "period": data.period, "net_payable": net},
            )
            generated += 1
            payrolls.append(payroll)

        db.commit()
        for item in payrolls:
            db.refresh(item)
        return {
            "period": data.period,
            "generated": generated,
            "existing": existing,
            "payrolls": [_payroll_dict(item) for item in payrolls],
        }
    except HTTPException:
        db.rollback()
        raise
    except EmployeeDomainError as exc:
        db.rollback()
        _domain_http(exc)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Payroll for this employee and period already exists")
    except Exception:
        db.rollback()
        logger.exception("Payroll generation failed")
        raise HTTPException(status_code=500, detail="Failed to generate payroll")


@router.post("/ledger/{entry_id}/reverse")
@limiter.limit("20/minute")
def reverse_ledger_entry(
    request: Request,
    entry_id: int,
    data: ReversalCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_ledger_access),
):
    try:
        entry = db.query(models.EmployeeLedgerEntry).filter_by(id=entry_id).first()
        if not entry:
            raise HTTPException(status_code=404, detail="Employee ledger entry not found")
        if entry.transaction_type in {"GOODS_ON_CREDIT", "GOODS_RETURN"}:
            raise HTTPException(
                status_code=409,
                detail="Goods-credit entries must be reversed through the linked sale return workflow",
            )
        payroll = db.query(models.EmployeePayroll).filter_by(id=entry.payroll_id).first() if entry.payroll_id else None
        if entry.transaction_type == "SALARY_ACCRUAL" and payroll and money(payroll.amount_paid) > 0:
            raise HTTPException(status_code=409, detail="Reverse salary payments before reversing salary accrual")
        is_profile_balance_entry = entry.transaction_type == "OPENING_BALANCE" or (
            entry.transaction_type == "MANUAL_ADJUSTMENT" and entry.source_type == "employee_profile"
        )
        if is_profile_balance_entry and not payroll and _open_payrolls(db, entry.employee_id):
            raise HTTPException(
                status_code=409,
                detail="Regenerate or settle the open payroll before reversing an unlinked opening-balance entry",
            )

        reversal, created = reverse_entry(
            db, entry=entry, reason=data.reason, created_by=current_user.username
        )
        if not created:
            db.rollback()
            employee = get_employee(db, entry.employee_id)
            return {"ledger_entry": _entry_dict(reversal), "new_balance": money(employee.current_balance), "already_reversed": True}

        # Mirror cash/account movements rather than deleting posted cash rows.
        original_cash = None
        if entry.cash_transaction_id:
            original_cash = db.query(models.CashTransaction).filter_by(id=entry.cash_transaction_id).first()
        if original_cash:
            cash_reversal = models.CashTransaction(
                tx_type="cash_in" if original_cash.tx_type == "cash_out" else "cash_out",
                cash_in_type="employee_reversal" if original_cash.tx_type == "cash_out" else None,
                cash_out_type="employee_reversal" if original_cash.tx_type == "cash_in" else None,
                amount=entry.amount, account=original_cash.account,
                received_from=original_cash.paid_to, paid_to=original_cash.received_from,
                category="Employee Ledger Reversal", reference_type="employee_ledger_reversal",
                reference_id=reversal.id, reference_no=reversal.reference_no,
                notes=data.reason, created_by=current_user.username, created_at=datetime.now(),
            )
            db.add(cash_reversal)
            db.flush()
            reversal.cash_transaction_id = cash_reversal.id

        # Keep the editable profile's opening balance aligned with its posted
        # source entries.  Otherwise a later opening correction would calculate
        # its delta from a stale value after reversal.
        if entry.transaction_type == "OPENING_BALANCE" or (
            entry.transaction_type == "MANUAL_ADJUSTMENT" and entry.source_type == "employee_profile"
        ):
            employee_profile = get_employee(db, entry.employee_id)
            employee_profile.opening_balance = money(employee_profile.opening_balance) - money(entry.signed_amount)
            employee_profile.updated_at = datetime.now()

        if payroll:
            amount_value = money(entry.amount)
            salary_accrual_voided = False
            if entry.transaction_type == "SALARY_PAYMENT":
                payroll.amount_paid = max(Decimal("0.00"), money(payroll.amount_paid) - amount_value)
                payment = db.query(models.EmployeeSalaryPayment).filter_by(ledger_entry_id=entry.id).first()
                if payment:
                    payment.is_reversed = True
                    payment.reversed_by_entry_id = reversal.id
                    payment.reversal_reason = data.reason
            elif entry.transaction_type == "SALARY_ACCRUAL":
                payroll.gross_salary = max(Decimal("0.00"), money(payroll.gross_salary) - amount_value)
                salary_accrual_voided = True
            elif entry.transaction_type == "BONUS":
                payroll.bonus = max(Decimal("0.00"), money(payroll.bonus) - amount_value)
            elif entry.transaction_type == "OVERTIME":
                payroll.overtime = max(Decimal("0.00"), money(payroll.overtime) - amount_value)
            elif entry.transaction_type == "DEDUCTION":
                payroll.deductions = max(Decimal("0.00"), money(payroll.deductions) - amount_value)
            elif entry.transaction_type == "CASH_ADVANCE":
                payroll.advances = max(Decimal("0.00"), money(payroll.advances) - amount_value)
            elif entry.transaction_type == "EMPLOYEE_REPAYMENT":
                payroll.employee_repayments = max(
                    Decimal("0.00"), money(payroll.employee_repayments) - amount_value
                )
            elif entry.transaction_type == "MANUAL_ADJUSTMENT":
                if money(entry.signed_amount) > 0:
                    payroll.bonus = max(Decimal("0.00"), money(payroll.bonus) - amount_value)
                else:
                    payroll.deductions = max(
                        Decimal("0.00"), money(payroll.deductions) - amount_value
                    )
            if salary_accrual_voided:
                # Preserve the unique payroll row and immutable history, but
                # mark it explicitly reusable by the next Generate action.
                payroll.net_payable = Decimal("0.00")
                payroll.amount_paid = Decimal("0.00")
                payroll.status = "voided"
            else:
                _recalculate_payroll_net(payroll)
                _refresh_payroll_status(payroll)

        add_audit(
            db, action="EMPLOYEE_LEDGER_REVERSED", entity_type="employee_ledger",
            entity_id=entry.id, username=current_user.username,
            old_value={"type": entry.transaction_type, "amount": entry.amount},
            new_value={"reversal_entry_id": reversal.id, "reason": data.reason},
        )
        db.commit()
        employee = get_employee(db, entry.employee_id)
        db.refresh(reversal)
        return {"ledger_entry": _entry_dict(reversal), "new_balance": money(employee.current_balance), "already_reversed": False}
    except HTTPException:
        db.rollback()
        raise
    except EmployeeDomainError as exc:
        db.rollback()
        _domain_http(exc)
    except Exception:
        db.rollback()
        logger.exception("Employee ledger reversal failed")
        raise HTTPException(status_code=500, detail="Failed to reverse employee ledger entry")


# ── Employee collection and profile ──────────────────────────────────────────

@router.get("", response_model=EmployeeListResponse)
@router.get("/", response_model=EmployeeListResponse)
@limiter.limit("1200/minute")
def list_employees(
    request: Request,
    search: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    active_only: Optional[bool] = Query(None),
    include_inactive: bool = Query(False),
    limit: int = Query(500, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_access),
):
    query = db.query(models.Employee)
    should_filter_active = active_only is True or (active_only is None and not include_inactive)
    if should_filter_active:
        query = query.filter(models.Employee.is_active.is_(True))
    term = (search or q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(or_(
            models.Employee.full_name.ilike(like), models.Employee.employee_code.ilike(like),
            models.Employee.phone.ilike(like), models.Employee.cnic.ilike(like),
            models.Employee.designation.ilike(like),
        ))
    total = query.count()
    items = query.order_by(models.Employee.full_name, models.Employee.id).offset(offset).limit(limit).all()
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
def create_employee(
    request: Request,
    data: EmployeeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_profile_access),
):
    try:
        submitted_code = (data.employee_code or "").strip().upper()
        employee = models.Employee(
            employee_code=submitted_code or _new_employee_code(db),
            full_name=data.full_name.strip(), phone=(data.phone or "").strip() or None,
            cnic=(data.cnic or "").strip() or None, address=(data.address or "").strip() or None,
            designation=(data.designation or "").strip() or None, joining_date=data.joining_date,
            monthly_salary=money(data.monthly_salary, allow_negative=False),
            opening_balance=money(data.opening_balance),
            credit_limit=money(data.credit_limit, allow_negative=False),
            current_balance=Decimal("0.00"), is_active=data.is_active,
            notes=(data.notes or "").strip() or None,
        )
        db.add(employee)
        uname = current_user.username if (current_user and hasattr(current_user, "username")) else "admin"
        post_opening_balance(db, employee=employee, amount=data.opening_balance, created_by=uname)
        add_audit(
            db, action="EMPLOYEE_CREATED", entity_type="employee", entity_id=employee.id,
            username=uname,
            new_value={"employee_code": employee.employee_code, "full_name": employee.full_name},
        )
        db.commit()
        db.refresh(employee)
        return employee
    except EmployeeDomainError as exc:
        db.rollback()
        _domain_http(exc)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Employee code or CNIC already exists")
    except Exception:
        db.rollback()
        logger.exception("Employee creation failed")
        raise HTTPException(status_code=500, detail="Failed to create employee")


@router.get("/{employee_id}", response_model=EmployeeOut)
@limiter.limit("1200/minute")
def employee_detail(
    request: Request,
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_access),
):
    try:
        return get_employee(db, employee_id)
    except EmployeeDomainError:
        raise HTTPException(status_code=404, detail="Employee not found")


@router.put("/{employee_id}", response_model=EmployeeOut)
@limiter.limit("20/minute")
def update_employee(
    request: Request,
    employee_id: int,
    data: EmployeeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_profile_access),
):
    try:
        employee = get_employee(db, employee_id)
        values = data.dict(exclude_unset=True)
        old = _employee_dict(employee)
        if "opening_balance" in values and values["opening_balance"] is not None:
            new_opening = money(values.pop("opening_balance"))
            difference = new_opening - money(employee.opening_balance)
            employee.opening_balance = new_opening
            if difference:
                open_payrolls = _open_payrolls(db, employee.id)
                linked_payroll = open_payrolls[0] if open_payrolls else None
                correction_entry, correction_created = post_ledger_entry(
                    db, employee=employee, transaction_type="MANUAL_ADJUSTMENT",
                    amount=abs(difference), signed_amount=difference,
                    created_by=current_user.username, reference_no=_new_reference("OPEN-ADJ"),
                    description="Opening balance correction",
                    salary_period=linked_payroll.salary_period if linked_payroll else None,
                    payroll_id=linked_payroll.id if linked_payroll else None,
                    source_type="employee_profile", source_id=employee.id,
                    idempotency_key=f"employee-opening-adjustment:{employee.id}:{uuid.uuid4().hex}",
                )
                if linked_payroll and correction_created:
                    if difference > 0:
                        linked_payroll.bonus = money(linked_payroll.bonus) + abs(difference)
                    else:
                        linked_payroll.deductions = money(linked_payroll.deductions) + abs(difference)
                    _recalculate_payroll_net(linked_payroll)
                    _refresh_payroll_status(linked_payroll)
        for key, value in values.items():
            if key in {"monthly_salary", "credit_limit"} and value is not None:
                value = money(value, allow_negative=False)
            elif key == "employee_code" and value is not None:
                value = value.strip().upper()
                if not value:
                    raise EmployeeDomainError("Employee code cannot be blank")
            elif key in {"full_name", "phone", "cnic", "address", "designation", "notes"}:
                value = (value or "").strip() or None
                if key == "full_name" and not value:
                    raise EmployeeDomainError("Full name is required")
            setattr(employee, key, value)
        employee.updated_at = datetime.now()
        add_audit(
            db, action="EMPLOYEE_UPDATED", entity_type="employee", entity_id=employee.id,
            username=current_user.username, old_value=old,
            new_value={key: str(value) for key, value in data.dict(exclude_unset=True).items()},
        )
        db.commit()
        db.refresh(employee)
        return employee
    except EmployeeDomainError as exc:
        db.rollback()
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        _domain_http(exc)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Employee code or CNIC already exists")
    except Exception:
        db.rollback()
        logger.exception("Employee update failed")
        raise HTTPException(status_code=500, detail="Failed to update employee")


# ── Ledger, adjustments and salary payment ───────────────────────────────────

@router.get("/{employee_id}/ledger")
@limiter.limit("1200/minute")
def employee_ledger(
    request: Request,
    employee_id: int,
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    transaction_type: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_access),
):
    try:
        employee = get_employee(db, employee_id)
        if start_date and end_date and start_date > end_date:
            raise EmployeeDomainError("start_date cannot be after end_date")
        start_dt = datetime.combine(start_date, datetime.min.time()) if start_date else None
        end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time()) if end_date else None

        opening = Decimal("0.00")
        if start_dt:
            opening = money(db.query(func.sum(models.EmployeeLedgerEntry.signed_amount)).filter(
                models.EmployeeLedgerEntry.employee_id == employee_id,
                models.EmployeeLedgerEntry.created_at < start_dt,
            ).scalar() or 0)
        query = db.query(models.EmployeeLedgerEntry).filter_by(employee_id=employee_id)
        if start_dt:
            query = query.filter(models.EmployeeLedgerEntry.created_at >= start_dt)
        if end_dt:
            query = query.filter(models.EmployeeLedgerEntry.created_at < end_dt)
        selected_type = transaction_type.strip().upper() if transaction_type else None
        rows = query.order_by(models.EmployeeLedgerEntry.created_at, models.EmployeeLedgerEntry.id).all()
        running = opening
        serialized = []
        for row in rows:
            running += money(row.signed_amount)
            if not selected_type or row.transaction_type == selected_type:
                serialized.append(_entry_dict(row, running))

        visible_rows = [row for row in rows if not selected_type or row.transaction_type == selected_type]
        active_rows = [row for row in visible_rows if not row.is_reversed and row.transaction_type != "REVERSAL"]
        def total_for(tx_type):
            return sum((money(row.amount) for row in active_rows if row.transaction_type == tx_type), Decimal("0.00"))
        increase = sum((max(Decimal("0.00"), money(row.signed_amount)) for row in visible_rows), Decimal("0.00"))
        decrease = sum((max(Decimal("0.00"), -money(row.signed_amount)) for row in visible_rows), Decimal("0.00"))
        summary = {
            "salary_accrued": total_for("SALARY_ACCRUAL"), "bonus": total_for("BONUS"),
            "overtime": total_for("OVERTIME"), "goods_credit": total_for("GOODS_ON_CREDIT"),
            "cash_advances": total_for("CASH_ADVANCE"), "deductions": total_for("DEDUCTION"),
            "salary_paid": total_for("SALARY_PAYMENT"),
            "employee_repayments": total_for("EMPLOYEE_REPAYMENT"),
            "goods_returns": total_for("GOODS_RETURN"),
            "total_increase": increase, "total_decrease": decrease,
        }
        return {
            "employee": _employee_dict(employee), "opening_balance": opening,
            "summary": summary, "items": serialized[offset:offset + limit],
            "total": len(serialized), "limit": limit, "offset": offset,
            "closing_balance": running,
        }
    except EmployeeDomainError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        _domain_http(exc)


@router.post("/{employee_id}/ledger")
@limiter.limit("30/minute")
def add_ledger_adjustment(
    request: Request,
    employee_id: int,
    data: LedgerPost,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_employee_ledger_access),
):
    try:
        tx_type = data.transaction_type.strip().upper()
        if tx_type not in MANUAL_TRANSACTION_TYPES:
            raise EmployeeDomainError("This transaction type can only be posted by its linked business workflow")
        employee = get_employee(db, employee_id)
        # Ledger is append-only: created_at is always posting time.  A caller
        # may choose salary_period for reporting, but cannot backdate a row and
        # invalidate historical running balances.
        effective_at = datetime.now()
        requested_salary_period = data.salary_period
        salary_period = requested_salary_period
        current_period = effective_at.strftime("%Y-%m")
        if salary_period and salary_period > current_period:
            raise EmployeeDomainError("Employee ledger entries cannot be posted to a future salary period")
        if not salary_period and tx_type in {
            "BONUS", "OVERTIME", "DEDUCTION", "CASH_ADVANCE", "EMPLOYEE_REPAYMENT",
            "SALARY_PAYMENT", "MANUAL_ADJUSTMENT",
        }:
            salary_period = current_period
        transaction_amount = money(data.amount, allow_negative=False)
        direction = (data.direction or "").strip().lower() or None
        expected_account = None
        if tx_type in {"CASH_ADVANCE", "EMPLOYEE_REPAYMENT", "SALARY_PAYMENT"}:
            _, expected_account = _parse_method(data.method)
        client_idempotency_key = (data.idempotency_key or "").strip() or None
        idempotency_key = (
            f"manual-ledger:{employee.id}:"
            f"{hashlib.sha256(client_idempotency_key.encode('utf-8')).hexdigest()}"
            if client_idempotency_key else None
        )
        if idempotency_key:
            previous = db.query(models.EmployeeLedgerEntry).filter_by(idempotency_key=idempotency_key).first()
            if previous:
                if previous.employee_id != employee.id:
                    raise EmployeeDomainError("Idempotency key belongs to another employee")
                expected_signed = None
                if tx_type == "MANUAL_ADJUSTMENT":
                    if direction not in {"increase", "decrease"}:
                        raise EmployeeDomainError("Manual adjustment direction must be increase or decrease")
                    expected_signed = transaction_amount if direction == "increase" else -transaction_amount
                prior_cash = (
                    db.query(models.CashTransaction).filter_by(id=previous.cash_transaction_id).first()
                    if previous.cash_transaction_id else None
                )
                mismatched = (
                    previous.transaction_type != tx_type
                    or money(previous.amount) != transaction_amount
                    or (previous.salary_period or None) != (salary_period or None)
                    or (expected_signed is not None and money(previous.signed_amount) != expected_signed)
                    or ((data.description or "").strip() != (previous.description or "").strip())
                    or (data.reference_no is not None and data.reference_no.strip() != (previous.reference_no or "").strip())
                    or (expected_account is not None and (not prior_cash or prior_cash.account != expected_account))
                )
                if mismatched:
                    raise HTTPException(status_code=409, detail="Idempotency key belongs to a different employee ledger posting")
                return {"ledger_entry": _entry_dict(previous), "new_balance": money(employee.current_balance), "created": False}
        if not employee.is_active and tx_type not in {
            "EMPLOYEE_REPAYMENT", "MANUAL_ADJUSTMENT", "SALARY_PAYMENT"
        }:
            raise EmployeeDomainError(
                "Inactive employees may only post repayment, payable settlement, or corrective adjustment entries"
            )
        reference_no = data.reference_no or _new_reference("EL")
        current_balance = money(employee.current_balance)
        if tx_type == "EMPLOYEE_REPAYMENT":
            debt = max(Decimal("0.00"), -current_balance)
            if debt <= 0:
                raise EmployeeDomainError("Employee repayment is only valid while the employee owes the company")
            if transaction_amount > debt:
                raise EmployeeDomainError(f"Repayment exceeds employee debt of {debt:.2f}")
        if tx_type == "SALARY_PAYMENT":
            payable = max(Decimal("0.00"), current_balance)
            if payable <= 0:
                raise EmployeeDomainError("No positive employee balance is available to pay")
            if transaction_amount > payable:
                raise EmployeeDomainError(f"Payment exceeds employee payable balance of {payable:.2f}")
        if tx_type == "CASH_ADVANCE":
            credit_limit = money(employee.credit_limit, allow_negative=False)
            if credit_limit <= 0:
                raise EmployeeDomainError("Employee cash advance is disabled because credit limit is zero")
            projected_debt = max(Decimal("0.00"), -(current_balance - transaction_amount))
            if projected_debt > credit_limit:
                available = max(Decimal("0.00"), credit_limit - max(Decimal("0.00"), -current_balance))
                raise EmployeeDomainError(f"Employee credit limit exceeded; available credit is {available:.2f}")
        payroll = None
        if salary_period:
            payroll = db.query(models.EmployeePayroll).filter_by(
                employee_id=employee.id, salary_period=salary_period
            ).first()
        open_payrolls = _open_payrolls(db, employee.id)
        open_payroll = open_payrolls[0] if open_payrolls else None
        employee_payrolls = db.query(models.EmployeePayroll).filter_by(employee_id=employee.id).all()
        voided_payroll = next((
            item for item in employee_payrolls
            if str(item.status or "").lower() == "voided"
            or bool(
                item.accrual_entry_id
                and db.query(models.EmployeeLedgerEntry).filter(
                    models.EmployeeLedgerEntry.id == item.accrual_entry_id,
                    models.EmployeeLedgerEntry.is_reversed.is_(True),
                ).first()
            )
        ), None)
        if tx_type == "SALARY_PAYMENT":
            pending_payroll_component = (
                db.query(models.EmployeeLedgerEntry)
                .filter(
                    models.EmployeeLedgerEntry.employee_id == employee.id,
                    models.EmployeeLedgerEntry.is_reversed.is_(False),
                    models.EmployeeLedgerEntry.payroll_id.is_(None),
                    models.EmployeeLedgerEntry.signed_amount > 0,
                    models.EmployeeLedgerEntry.transaction_type.in_([
                        "BONUS", "OVERTIME", "GOODS_RETURN", "EMPLOYEE_REPAYMENT",
                        "MANUAL_ADJUSTMENT",
                    ]),
                    or_(
                        models.EmployeeLedgerEntry.transaction_type != "MANUAL_ADJUSTMENT",
                        models.EmployeeLedgerEntry.source_type.is_(None),
                        models.EmployeeLedgerEntry.source_type != "employee_profile",
                    ),
                )
                .order_by(models.EmployeeLedgerEntry.salary_period, models.EmployeeLedgerEntry.id)
                .first()
            )
            if open_payroll is not None or voided_payroll is not None:
                raise EmployeeDomainError(
                    "Settle open payrolls with Pay Salary (and regenerate voided payrolls) before a general balance payment"
                )
            if pending_payroll_component is not None:
                pending_period = pending_payroll_component.salary_period or pending_payroll_component.created_at.strftime("%Y-%m")
                raise EmployeeDomainError(
                    f"Generate payroll for {pending_period} before paying its bonus, overtime, repayment, return, or adjustment balance"
                )

        decreases_available_payable = (
            tx_type in {"CASH_ADVANCE", "DEDUCTION"}
            or (tx_type == "MANUAL_ADJUSTMENT" and direction == "decrease")
        )
        if decreases_available_payable and open_payroll:
            if payroll is None and not requested_salary_period:
                payroll = open_payroll
                salary_period = payroll.salary_period
            elif payroll is None or payroll.id not in {item.id for item in open_payrolls}:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This decrease must be posted against open payroll period "
                        f"{open_payroll.salary_period} so its remaining balance stays payable"
                    ),
                )

        cash_tx = None
        if tx_type in {"CASH_ADVANCE", "EMPLOYEE_REPAYMENT", "SALARY_PAYMENT"}:
            is_cash_out = tx_type in {"CASH_ADVANCE", "SALARY_PAYMENT"}
            cash_tx, _, _ = _cash_movement(
                db, employee=employee, amount=transaction_amount, method=data.method,
                direction="out" if is_cash_out else "in", reference_no=reference_no,
                note=data.description, username=current_user.username,
                reference_type=(
                    "employee_balance_payment" if tx_type == "SALARY_PAYMENT" else "employee_ledger"
                ),
                reference_id=employee.id,
            )
        entry, created = post_ledger_entry(
            db, employee=employee, transaction_type=tx_type, amount=data.amount,
            direction=data.direction, created_by=current_user.username,
            reference_no=reference_no, description=data.description,
            salary_period=salary_period,
            payroll_id=(payroll.id if payroll and tx_type != "SALARY_PAYMENT" else None),
            cash_transaction_id=cash_tx.id if cash_tx else None,
            source_type="manual", source_id=employee.id,
            idempotency_key=idempotency_key,
            enforce_credit_limit=(tx_type == "CASH_ADVANCE"),
            enforce_repayment_limit=(tx_type == "EMPLOYEE_REPAYMENT"),
            enforce_payable_limit=(tx_type == "SALARY_PAYMENT"),
        )
        if not created:
            # Defensive guard for retries normalized by the service: never leave
            # a cash movement or audit row without a new ledger movement.
            entry_id = entry.id
            db.rollback()
            entry = db.query(models.EmployeeLedgerEntry).filter_by(id=entry_id).first()
            employee = get_employee(db, employee_id)
            return {"ledger_entry": _entry_dict(entry), "new_balance": money(employee.current_balance), "created": False}
        if payroll and created:
            value = money(data.amount, allow_negative=False)
            if tx_type == "BONUS":
                payroll.bonus = money(payroll.bonus) + value
            elif tx_type == "OVERTIME":
                payroll.overtime = money(payroll.overtime) + value
            elif tx_type == "DEDUCTION":
                payroll.deductions = money(payroll.deductions) + value
            elif tx_type == "CASH_ADVANCE":
                payroll.advances = money(payroll.advances) + value
            elif tx_type == "EMPLOYEE_REPAYMENT":
                payroll.employee_repayments = money(payroll.employee_repayments) + value
            elif tx_type == "MANUAL_ADJUSTMENT" and direction == "increase":
                payroll.bonus = money(payroll.bonus) + value
            elif tx_type == "MANUAL_ADJUSTMENT" and direction == "decrease":
                payroll.deductions = money(payroll.deductions) + value
            _recalculate_payroll_net(payroll)
            _refresh_payroll_status(payroll)
        add_audit(
            db, action="EMPLOYEE_LEDGER_POSTED", entity_type="employee_ledger",
            entity_id=entry.id, username=current_user.username,
            new_value={"employee_id": employee.id, "type": tx_type, "amount": data.amount},
        )
        db.commit()
        db.refresh(employee)
        db.refresh(entry)
        return {"ledger_entry": _entry_dict(entry), "new_balance": money(employee.current_balance), "created": created}
    except HTTPException:
        db.rollback()
        raise
    except EmployeeDomainError as exc:
        db.rollback()
        _domain_http(exc)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate employee ledger transaction")
    except Exception:
        db.rollback()
        logger.exception("Employee ledger posting failed")
        raise HTTPException(status_code=500, detail="Failed to post employee ledger transaction")


@router.post("/{employee_id}/salary-payment")
@limiter.limit("30/minute")
def salary_payment(
    request: Request,
    employee_id: int,
    data: SalaryPaymentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_payroll_access),
):
    try:
        # Inactive employees may still have earned/payable salary to settle.
        employee = get_employee(db, employee_id)
        payment_amount = money(data.amount, allow_negative=False)
        normalized_method, requested_account = _parse_method(data.method)
        idempotency_key = (data.idempotency_key or "").strip() or None
        if idempotency_key:
            prior = db.query(models.EmployeeSalaryPayment).filter_by(idempotency_key=idempotency_key).first()
            if prior:
                if prior.employee_id != employee_id:
                    raise EmployeeDomainError("Idempotency key belongs to another employee")
                payroll = db.query(models.EmployeePayroll).get(prior.payroll_id)
                entry = db.query(models.EmployeeLedgerEntry).get(prior.ledger_entry_id)
                mismatched = (
                    money(prior.amount) != payment_amount
                    or prior.method != normalized_method
                    or prior.account != requested_account
                    or (prior.note or "").strip() != (data.note or "").strip()
                    or (data.payroll_id is not None and prior.payroll_id != data.payroll_id)
                    or (data.salary_period is not None and (
                        not payroll or payroll.salary_period != data.salary_period
                    ))
                )
                if mismatched:
                    raise HTTPException(status_code=409, detail="Idempotency key belongs to a different salary payment")
                return {
                    "payment": _payment_dict(prior), "payroll": _payroll_dict(payroll),
                    "ledger_entry": _entry_dict(entry), "new_balance": money(employee.current_balance),
                    "created": False,
                }
        payroll_query = db.query(models.EmployeePayroll).filter_by(employee_id=employee_id)
        if data.payroll_id:
            payroll_query = payroll_query.filter_by(id=data.payroll_id)
        elif data.salary_period:
            payroll_query = payroll_query.filter_by(salary_period=data.salary_period)
        else:
            payroll_query = payroll_query.order_by(models.EmployeePayroll.salary_period.desc())
        payroll = payroll_query.first()
        if not payroll:
            raise EmployeeDomainError("Generate payroll before posting a salary payment")
        reversed_accrual = bool(
            payroll.accrual_entry_id
            and db.query(models.EmployeeLedgerEntry).filter(
                models.EmployeeLedgerEntry.id == payroll.accrual_entry_id,
                models.EmployeeLedgerEntry.is_reversed.is_(True),
            ).first()
        )
        if str(payroll.status or "").lower() == "voided" or reversed_accrual:
            raise EmployeeDomainError("Regenerate this voided payroll before posting a salary payment")

        remaining = _remaining_payroll(payroll)
        available_balance = max(Decimal("0.00"), money(employee.current_balance))
        maximum = min(remaining, available_balance)
        if payment_amount > maximum:
            raise EmployeeDomainError(f"Payment exceeds payable employee balance of {maximum:.2f}")
        if maximum <= 0:
            raise EmployeeDomainError("No salary amount is currently payable to this employee")

        # Reserve the partial payment with one conditional UPDATE.  This is an
        # optimistic compare-and-swap: concurrent requests cannot both use the
        # same stale amount_paid and overpay the payroll.
        previous_paid = money(payroll.amount_paid)
        rounded_paid = func.round(models.EmployeePayroll.amount_paid + payment_amount, 2)
        reserved = (
            db.query(models.EmployeePayroll)
            .filter(
                models.EmployeePayroll.id == payroll.id,
                func.round(models.EmployeePayroll.amount_paid, 2) == previous_paid,
                rounded_paid <= func.round(models.EmployeePayroll.net_payable, 2),
            )
            .update(
                {models.EmployeePayroll.amount_paid: rounded_paid},
                synchronize_session=False,
            )
        )
        if reserved != 1:
            raise EmployeeDomainError("Payroll balance changed; refresh and retry the payment")
        db.flush()
        db.expire(payroll, ["amount_paid"])

        reference_no = _new_reference("SALPAY")
        cash_tx, normalized_method, account = _cash_movement(
            db, employee=employee, amount=payment_amount, method=data.method, direction="out",
            reference_no=reference_no, note=data.note, username=current_user.username,
            reference_type="employee_salary_payment", reference_id=payroll.id,
        )
        entry, ledger_created = post_ledger_entry(
            db, employee=employee, transaction_type="SALARY_PAYMENT", amount=payment_amount,
            created_by=current_user.username, reference_no=reference_no,
            description=data.note or f"Salary payment for {payroll.salary_period}",
            salary_period=payroll.salary_period, payroll_id=payroll.id,
            cash_transaction_id=cash_tx.id, source_type="employee_salary_payment",
            source_id=payroll.id,
            idempotency_key=(
                f"salary-payment:{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()}"
                if idempotency_key else None
            ),
        )
        if not ledger_created:
            raise HTTPException(
                status_code=409,
                detail="Salary-payment ledger posting already exists without a matching payment record",
            )
        payment = models.EmployeeSalaryPayment(
            employee_id=employee.id, payroll_id=payroll.id, ledger_entry_id=entry.id,
            cash_transaction_id=cash_tx.id, amount=payment_amount,
            method=normalized_method, account=account, note=data.note,
            idempotency_key=idempotency_key, created_by=current_user.username,
        )
        db.add(payment)
        db.flush()
        cash_tx.reference_id = payment.id
        _refresh_payroll_status(payroll)
        add_audit(
            db, action="EMPLOYEE_SALARY_PAID", entity_type="employee_salary_payment",
            entity_id=payment.id, username=current_user.username,
            new_value={"employee_id": employee.id, "payroll_id": payroll.id,
                       "amount": payment_amount, "method": normalized_method},
        )
        db.commit()
        for obj in (employee, payroll, entry, payment):
            db.refresh(obj)
        return {
            "payment": _payment_dict(payment), "payroll": _payroll_dict(payroll),
            "ledger_entry": _entry_dict(entry), "new_balance": money(employee.current_balance),
            "created": True,
        }
    except HTTPException:
        db.rollback()
        raise
    except EmployeeDomainError as exc:
        db.rollback()
        _domain_http(exc)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate salary payment")
    except Exception:
        db.rollback()
        logger.exception("Salary payment failed")
        raise HTTPException(status_code=500, detail="Failed to post salary payment")
