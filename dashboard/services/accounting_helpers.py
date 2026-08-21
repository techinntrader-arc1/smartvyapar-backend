"""Shared accounting helpers for append-only cash movement reversals."""

from sqlalchemy import func
from sqlalchemy.orm import Session, aliased

import models


def salary_reversal_total(db: Session, start_at, end_at, *, cash_only: bool = False) -> float:
    """Return cash-in reversals whose source ledger row was SALARY_PAYMENT."""
    reversal_entry = aliased(models.EmployeeLedgerEntry)
    original_entry = aliased(models.EmployeeLedgerEntry)
    query = db.query(func.sum(models.CashTransaction.amount)).join(
        reversal_entry,
        reversal_entry.cash_transaction_id == models.CashTransaction.id,
    ).join(
        original_entry,
        original_entry.id == reversal_entry.source_id,
    ).filter(
        models.CashTransaction.tx_type == "cash_in",
        models.CashTransaction.cash_in_type == "employee_reversal",
        reversal_entry.transaction_type == "REVERSAL",
        original_entry.transaction_type == "SALARY_PAYMENT",
        models.CashTransaction.created_at >= start_at,
        models.CashTransaction.created_at <= end_at,
    )
    if cash_only:
        query = query.filter(models.CashTransaction.account == "cash_in_hand")
    return float(query.scalar() or 0)

