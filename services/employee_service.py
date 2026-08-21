"""Transactional domain service for employees, payroll and employee credit.

All public posting helpers deliberately call ``flush()`` but never ``commit()``.
That lets POS, stock-return and payroll callers include every related change in
one SQLAlchemy transaction: either sale/stock/cashbook/ledger all succeed, or
the caller rolls the whole unit of work back.
"""

import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


MONEY_PLACES = Decimal("0.01")

# Positive signed amount = company owes employee more.
# Negative signed amount = employee owes company more / company owes less.
ENTRY_SIGNS = {
    "SALARY_ACCRUAL": 1,
    "SALARY_PAYMENT": -1,
    "CASH_ADVANCE": -1,
    "GOODS_ON_CREDIT": -1,
    "BONUS": 1,
    "OVERTIME": 1,
    "DEDUCTION": -1,
    "EMPLOYEE_REPAYMENT": 1,
    "GOODS_RETURN": 1,
    "REVERSAL": 1,
    "OPENING_BALANCE": 1,
}

MANUAL_TRANSACTION_TYPES = {
    "SALARY_PAYMENT",
    "CASH_ADVANCE",
    "BONUS",
    "OVERTIME",
    "DEDUCTION",
    "EMPLOYEE_REPAYMENT",
    "MANUAL_ADJUSTMENT",
}


class EmployeeDomainError(ValueError):
    """Safe, user-displayable business validation failure."""


def money(value: Any, *, allow_negative: bool = True) -> Decimal:
    """Convert any input to a finite, two-decimal Decimal."""
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise EmployeeDomainError("Invalid monetary amount") from exc
    if not result.is_finite():
        raise EmployeeDomainError("Monetary amount must be finite")
    result = result.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
    if not allow_negative and result < 0:
        raise EmployeeDomainError("Amount cannot be negative")
    return result


def _reference(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def _json(value: Optional[Dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _refresh_payroll_status(payroll: models.EmployeePayroll) -> None:
    if str(payroll.status or "").lower() == "voided":
        return
    remaining = money(payroll.net_payable) - money(payroll.amount_paid)
    if remaining <= 0:
        payroll.status = "settled"
    elif money(payroll.amount_paid) > 0:
        payroll.status = "partial"
    else:
        payroll.status = "unpaid"


def _recalculate_payroll_net(payroll: models.EmployeePayroll) -> None:
    raw_net = (
        money(payroll.gross_salary) + money(payroll.bonus) + money(payroll.overtime)
        + money(payroll.employee_repayments) + money(payroll.goods_returns)
        - money(payroll.deductions) - money(payroll.advances) - money(payroll.goods_credit)
        - money(payroll.carried_debt_offset)
    )
    payroll.net_payable = max(Decimal("0.00"), raw_net)


def add_audit(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: Optional[int],
    username: str,
    old_value: Optional[Dict[str, Any]] = None,
    new_value: Optional[Dict[str, Any]] = None,
) -> models.AuditLog:
    audit = models.AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=_json(old_value),
        new_value=_json(new_value),
        user=username,
    )
    db.add(audit)
    return audit


def get_employee(db: Session, employee_id: int, *, active_only: bool = False) -> models.Employee:
    query = db.query(models.Employee).filter(models.Employee.id == employee_id)
    if active_only:
        query = query.filter(models.Employee.is_active.is_(True))
    employee = query.first()
    if not employee:
        raise EmployeeDomainError("Employee not found or inactive" if active_only else "Employee not found")
    return employee


def signed_for(transaction_type: str, amount: Any, direction: Optional[str] = None) -> Decimal:
    tx_type = str(transaction_type or "").strip().upper()
    absolute = money(amount, allow_negative=False)
    if absolute <= 0:
        raise EmployeeDomainError("Amount must be greater than zero")
    if tx_type == "MANUAL_ADJUSTMENT":
        normalized_direction = str(direction or "").strip().lower()
        if normalized_direction not in {"increase", "decrease"}:
            raise EmployeeDomainError("Manual adjustment direction must be increase or decrease")
        return absolute if normalized_direction == "increase" else -absolute
    sign = ENTRY_SIGNS.get(tx_type)
    if sign is None:
        raise EmployeeDomainError(f"Unsupported employee transaction type: {tx_type}")
    return absolute * sign


def post_ledger_entry(
    db: Session,
    *,
    employee: models.Employee,
    transaction_type: str,
    amount: Any,
    created_by: str,
    signed_amount: Optional[Any] = None,
    direction: Optional[str] = None,
    reference_no: Optional[str] = None,
    description: Optional[str] = None,
    salary_period: Optional[str] = None,
    sale_id: Optional[int] = None,
    payroll_id: Optional[int] = None,
    cash_transaction_id: Optional[int] = None,
    source_type: Optional[str] = None,
    source_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    reversal_of_id: Optional[int] = None,
    reversal_reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    enforce_credit_limit: bool = False,
    enforce_repayment_limit: bool = False,
    enforce_payable_limit: bool = False,
) -> Tuple[models.EmployeeLedgerEntry, bool]:
    """Append a ledger movement and atomically update the cached balance.

    Returns ``(entry, created)``.  Repeating an idempotency key returns the
    original row without changing the employee balance again.
    """
    tx_type = str(transaction_type or "").strip().upper()
    absolute = money(amount, allow_negative=False)
    if absolute <= 0:
        raise EmployeeDomainError("Amount must be greater than zero")
    movement = (
        money(signed_amount)
        if signed_amount is not None
        else signed_for(tx_type, absolute, direction)
    )
    if movement == 0 or abs(movement) != absolute:
        raise EmployeeDomainError("Signed movement must equal the absolute amount")

    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()[:160] or None
    if idempotency_key:
        key = idempotency_key
        existing = (
            db.query(models.EmployeeLedgerEntry)
            .filter(models.EmployeeLedgerEntry.idempotency_key == key)
            .first()
        )
        if existing:
            metadata_json = _json(metadata)
            mismatched = (
                existing.employee_id != employee.id
                or existing.transaction_type != tx_type
                or money(existing.amount) != absolute
                or money(existing.signed_amount) != movement
                or existing.sale_id != sale_id
                or existing.source_type != source_type
                or existing.source_id != source_id
                or existing.reversal_of_id != reversal_of_id
                or (payroll_id is not None and existing.payroll_id != payroll_id)
                or (cash_transaction_id is not None and existing.cash_transaction_id != cash_transaction_id)
                or (salary_period is not None and existing.salary_period != salary_period)
                or (reference_no is not None and (existing.reference_no or "") != str(reference_no)[:80])
                or (description is not None and (existing.description or "").strip() != str(description).strip())
                or (metadata is not None and (existing.metadata_json or None) != metadata_json)
            )
            if mismatched:
                raise EmployeeDomainError("Idempotency key belongs to a different employee ledger posting")
            return existing, False
        idempotency_key = key

    # SQL expression update makes the balance movement atomic at the database
    # level; SQLite serializes concurrent writers and the outer transaction owns
    # the write lock until commit/rollback.  ROUND is part of both the predicate
    # and assignment because SQLite implements NUMERIC arithmetic with binary
    # floats; without cent rounding, 100 movements of 0.01 can stop at 0.99.
    balance_update = db.query(models.Employee).filter(models.Employee.id == employee.id)
    rounded_balance = func.round(models.Employee.current_balance + movement, 2)
    if enforce_credit_limit:
        balance_update = balance_update.filter(
            models.Employee.credit_limit > 0,
            rounded_balance >= -func.round(models.Employee.credit_limit, 2),
        )
    if enforce_repayment_limit:
        balance_update = balance_update.filter(
            models.Employee.current_balance < 0,
            rounded_balance <= 0,
        )
    if enforce_payable_limit:
        balance_update = balance_update.filter(
            models.Employee.current_balance > 0,
            rounded_balance >= 0,
        )
    updated = (
        balance_update.update(
            {models.Employee.current_balance: rounded_balance},
            synchronize_session=False,
        )
    )
    if updated != 1:
        if enforce_credit_limit:
            raise EmployeeDomainError("Employee credit limit exceeded or credit is disabled")
        if enforce_repayment_limit:
            raise EmployeeDomainError("Repayment exceeds the employee's current debt")
        if enforce_payable_limit:
            raise EmployeeDomainError("Payment exceeds the employee's current payable balance")
        if employee in db or (hasattr(employee, "id") and employee.id):
            employee.current_balance = money(employee.current_balance + movement)
            db.flush()
        else:
            raise EmployeeDomainError("Employee no longer exists")
    else:
        db.flush()
        db.expire(employee, ["current_balance"])
    balance_after = money(employee.current_balance)

    entry = models.EmployeeLedgerEntry(
        employee_id=employee.id,
        transaction_type=tx_type,
        amount=absolute,
        signed_amount=movement,
        balance_after=balance_after,
        reference_no=(reference_no or _reference("EMP"))[:80],
        description=(description or "").strip(),
        salary_period=salary_period,
        sale_id=sale_id,
        payroll_id=payroll_id,
        cash_transaction_id=cash_transaction_id,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
        reversal_of_id=reversal_of_id,
        reversal_reason=reversal_reason,
        metadata_json=_json(metadata),
        created_by=created_by,
        created_at=datetime.now(),
    )
    db.add(entry)
    db.flush()
    return entry, True


def post_opening_balance(
    db: Session, *, employee: models.Employee, amount: Any, created_by: str
) -> Optional[models.EmployeeLedgerEntry]:
    opening = money(amount)
    if opening == 0:
        return None
    entry, _ = post_ledger_entry(
        db,
        employee=employee,
        transaction_type="OPENING_BALANCE",
        amount=abs(opening),
        signed_amount=opening,
        created_by=created_by,
        reference_no=f"OPEN-{employee.employee_code}",
        description="Employee opening balance",
        source_type="employee",
        source_id=employee.id,
        idempotency_key=f"employee-opening:{employee.id}",
    )
    return entry


def post_goods_on_credit(
    db: Session,
    *,
    employee_id: int,
    sale: models.Sale,
    amount: Any,
    created_by: str,
    price_mode: str = "retail",
    idempotency_key: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[models.EmployeeLedgerEntry, models.EmployeeGoodsCredit, bool]:
    """Link an already-built normal Sale to employee credit without committing.

    Call this after the sale and sale items have been flushed, inside the same
    transaction that reduces stock.  A repeated sale id is a no-op.
    """
    employee = get_employee(db, employee_id, active_only=True)
    if not getattr(sale, "id", None):
        db.flush()
    credit_amount = money(amount, allow_negative=False)
    if credit_amount <= 0:
        raise EmployeeDomainError("Employee credit amount must be greater than zero")
    normalized_price_mode = "employee" if str(price_mode).lower() == "employee" else "retail"
    existing_credit = (
        db.query(models.EmployeeGoodsCredit)
        .filter(models.EmployeeGoodsCredit.sale_id == sale.id)
        .first()
    )
    if existing_credit:
        if (
            existing_credit.employee_id != employee_id
            or money(existing_credit.total) != credit_amount
            or existing_credit.price_mode != normalized_price_mode
        ):
            raise EmployeeDomainError("Sale is already linked to a different employee-credit posting")
        existing_entry = db.query(models.EmployeeLedgerEntry).get(existing_credit.ledger_entry_id)
        if (
            not existing_entry
            or existing_entry.employee_id != employee_id
            or existing_entry.transaction_type != "GOODS_ON_CREDIT"
            or existing_entry.sale_id != sale.id
            or money(existing_entry.amount) != credit_amount
        ):
            raise EmployeeDomainError("Employee-credit ledger link is missing or inconsistent")
        return existing_entry, existing_credit, False
    limit = money(employee.credit_limit, allow_negative=False)
    projected_balance = money(employee.current_balance) - credit_amount
    projected_debt = max(Decimal("0.00"), -projected_balance)
    if limit <= 0:
        raise EmployeeDomainError("Employee credit is disabled because credit limit is zero")
    if projected_debt > limit:
        available = max(Decimal("0.00"), limit - max(Decimal("0.00"), -money(employee.current_balance)))
        raise EmployeeDomainError(f"Employee credit limit exceeded; available credit is {available:.2f}")

    posting_period = datetime.now().strftime("%Y-%m")
    payroll_candidates = (
        db.query(models.EmployeePayroll)
        .filter(models.EmployeePayroll.employee_id == employee.id)
        .order_by(models.EmployeePayroll.salary_period, models.EmployeePayroll.id)
        .all()
    )
    payroll = next(
        (
            item for item in payroll_candidates
            if str(item.status or "").lower() != "voided"
            and money(item.net_payable) > money(item.amount_paid)
        ),
        None,
    )
    if payroll is None:
        payroll = next(
            (item for item in payroll_candidates if item.salary_period == posting_period),
            None,
        )
    salary_period = payroll.salary_period if payroll else posting_period
    entry, created = post_ledger_entry(
        db,
        employee=employee,
        transaction_type="GOODS_ON_CREDIT",
        amount=credit_amount,
        created_by=created_by,
        reference_no=getattr(sale, "invoice_no", None) or _reference("EC"),
        description=description or f"Goods on employee credit - {getattr(sale, 'invoice_no', sale.id)}",
        salary_period=salary_period,
        sale_id=sale.id,
        payroll_id=payroll.id if payroll else None,
        source_type="sale",
        source_id=sale.id,
        idempotency_key=idempotency_key or f"employee-credit:sale:{sale.id}",
        metadata=metadata,
        enforce_credit_limit=True,
    )
    if not created:
        credit = db.query(models.EmployeeGoodsCredit).filter_by(ledger_entry_id=entry.id).first()
        if not credit or credit.sale_id != sale.id:
            raise EmployeeDomainError(
                "Employee-credit ledger posting already exists without a matching goods-credit record"
            )
        return entry, credit, False

    credit = models.EmployeeGoodsCredit(
        employee_id=employee.id,
        sale_id=sale.id,
        invoice_no=getattr(sale, "invoice_no", None) or f"SALE-{sale.id}",
        total=credit_amount,
        returned_amount=Decimal("0.00"),
        price_mode=normalized_price_mode,
        status="active",
        ledger_entry_id=entry.id,
    )
    db.add(credit)
    db.flush()

    if payroll:
        payroll.goods_credit = money(payroll.goods_credit) + credit_amount
        _recalculate_payroll_net(payroll)
        _refresh_payroll_status(payroll)

    for item in list(getattr(sale, "items", None) or []):
        db.add(models.EmployeeGoodsCreditItem(
            goods_credit_id=credit.id,
            product_id=getattr(item, "product_id", None),
            product_name=getattr(item, "product_name", None) or "Item",
            qty=float(getattr(item, "qty", 0) or 0),
            price=money(getattr(item, "price", 0), allow_negative=False),
            cost=money(getattr(item, "buy_price", 0), allow_negative=False),
            total=money(getattr(item, "total", 0), allow_negative=False),
            returned_qty=float(getattr(item, "returned_qty", 0) or 0),
        ))
    db.flush()
    return entry, credit, True


def post_goods_return(
    db: Session,
    *,
    sale_id: int,
    amount: Any,
    created_by: str,
    reference_no: str,
    idempotency_key: Optional[str] = None,
    item_returns: Optional[Iterable[Dict[str, Any]]] = None,
    description: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[models.EmployeeLedgerEntry, models.EmployeeGoodsCredit, bool]:
    """Reverse employee debt for a stock return, without committing.

    Stock restoration remains the sales-return service's responsibility.  Call
    both helpers in the same session/transaction so they cannot diverge.
    """
    credit = (
        db.query(models.EmployeeGoodsCredit)
        .filter(models.EmployeeGoodsCredit.sale_id == sale_id)
        .first()
    )
    if not credit:
        raise EmployeeDomainError("Employee credit sale not found")
    employee = get_employee(db, credit.employee_id)
    return_amount = money(amount, allow_negative=False)
    if return_amount <= 0:
        raise EmployeeDomainError("Return amount must be greater than zero")

    key = str(
        idempotency_key or f"employee-return:sale:{sale_id}:{str(reference_no).strip()}"
    ).strip()[:160]
    existing = db.query(models.EmployeeLedgerEntry).filter_by(idempotency_key=key).first()
    if existing:
        if (
            existing.employee_id != credit.employee_id
            or existing.sale_id != sale_id
            or existing.transaction_type != "GOODS_RETURN"
            or money(existing.amount) != return_amount
            or existing.source_type != "sale_return"
        ):
            raise EmployeeDomainError("Idempotency key belongs to another employee goods return")
        return existing, credit, False

    remaining = money(credit.total) - money(credit.returned_amount)
    if return_amount > remaining:
        raise EmployeeDomainError(f"Return exceeds employee credit outstanding amount of {remaining:.2f}")

    original_entry = db.query(models.EmployeeLedgerEntry).get(credit.ledger_entry_id)
    # A return is a new economic event in the posting month.  Keep the sale
    # link for traceability, but do not silently reopen an older payroll period.
    salary_period = datetime.now().strftime("%Y-%m")
    payroll = db.query(models.EmployeePayroll).filter_by(
        employee_id=employee.id, salary_period=salary_period
    ).first()
    entry, created = post_ledger_entry(
        db,
        employee=employee,
        transaction_type="GOODS_RETURN",
        amount=return_amount,
        created_by=created_by,
        reference_no=reference_no,
        description=description or f"Goods returned against {credit.invoice_no}",
        salary_period=salary_period,
        sale_id=sale_id,
        payroll_id=payroll.id if payroll else None,
        source_type="sale_return",
        source_id=sale_id,
        idempotency_key=key,
        metadata=metadata,
    )
    if not created:
        # Defensive race/canonicalization guard.  No credit, payroll, or item
        # mutation is allowed unless this call appended the ledger movement.
        return entry, credit, False
    credit.returned_amount = money(credit.returned_amount) + return_amount
    credit.status = "returned" if money(credit.returned_amount) == money(credit.total) else "partially_returned"
    if payroll:
        payroll.goods_returns = money(payroll.goods_returns) + return_amount
        _recalculate_payroll_net(payroll)
        _refresh_payroll_status(payroll)

    by_item_id = {item.id: item for item in credit.items}
    by_product_id = {item.product_id: item for item in credit.items if item.product_id is not None}
    for returned in item_returns or []:
        item = by_item_id.get(returned.get("credit_item_id")) or by_product_id.get(returned.get("product_id"))
        qty = float(returned.get("qty") or 0)
        if not item or qty <= 0:
            raise EmployeeDomainError("Invalid returned employee-credit item")
        if float(item.returned_qty or 0) + qty > float(item.qty or 0) + 1e-9:
            raise EmployeeDomainError(f"Returned quantity exceeds sold quantity for {item.product_name}")
        item.returned_qty = float(item.returned_qty or 0) + qty
    db.flush()
    return entry, credit, created


def reverse_entry(
    db: Session,
    *,
    entry: models.EmployeeLedgerEntry,
    reason: str,
    created_by: str,
) -> Tuple[models.EmployeeLedgerEntry, bool]:
    """Append the exact opposite movement and mark the original as reversed."""
    if entry.transaction_type == "REVERSAL":
        raise EmployeeDomainError("A reversal entry cannot itself be reversed")
    if entry.is_reversed:
        existing = db.query(models.EmployeeLedgerEntry).filter_by(reversal_of_id=entry.id).first()
        if existing:
            return existing, False
        raise EmployeeDomainError("Ledger entry is already reversed")
    employee = get_employee(db, entry.employee_id)
    reversal, created = post_ledger_entry(
        db,
        employee=employee,
        transaction_type="REVERSAL",
        amount=entry.amount,
        signed_amount=-money(entry.signed_amount),
        created_by=created_by,
        reference_no=f"REV-{entry.reference_no}"[:80],
        description=f"Reversal of {entry.reference_no}: {reason.strip()}",
        salary_period=entry.salary_period,
        sale_id=entry.sale_id,
        payroll_id=entry.payroll_id,
        source_type="ledger_reversal",
        source_id=entry.id,
        idempotency_key=f"employee-ledger-reversal:{entry.id}",
        reversal_of_id=entry.id,
        reversal_reason=reason.strip(),
        metadata={"original_type": entry.transaction_type},
    )
    entry.is_reversed = True
    entry.reversal_reason = reason.strip()
    db.flush()
    return reversal, created
