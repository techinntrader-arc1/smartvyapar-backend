"""Immutable, decimal-safe sale-return posting and reporting helpers."""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


CENT = Decimal("0.01")
QTY_STEP = Decimal("0.0001")
POSTED_SALE_STATUSES = ("completed", "partially_returned", "returned")


class SaleReturnDomainError(ValueError):
    pass


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def quantity(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(QTY_STEP, rounding=ROUND_HALF_UP)


def _allocate_authoritative_total(sale, total: Decimal, weights) -> dict:
    """Allocate an invoice-level total to its persisted lines with no drift."""
    items = sorted(sale.items, key=lambda item: item.id or 0)
    if not items:
        return {}
    normalized_weights = {item.id: max(Decimal("0"), Decimal(str(weights(item)))) for item in items}
    weight_total = sum(normalized_weights.values(), Decimal("0"))
    remaining = money(total)
    allocations = {}
    for index, item in enumerate(items):
        if index == len(items) - 1:
            allocated = remaining
        elif weight_total > 0:
            allocated = money(money(total) * normalized_weights[item.id] / weight_total)
            allocated = min(max(Decimal("0.00"), allocated), remaining)
            remaining -= allocated
        else:
            allocated = Decimal("0.00")
        allocations[item.id] = allocated
    return allocations


def allocate_sale_item_amounts(sale) -> dict:
    """Exact per-line shares of Sale.total, including invoice-level discount."""
    return _allocate_authoritative_total(sale, money(sale.total), lambda item: money(item.total))


def allocate_sale_item_taxes(sale) -> dict:
    """Exact per-line shares of authoritative Sale.tax_amount."""
    def tax_weight(item):
        tax_pct = Decimal(str(item.tax_pct or 0))
        return (
            money(item.total) * tax_pct / (Decimal("100") + tax_pct)
            if tax_pct > 0
            else Decimal("0")
        )

    return _allocate_authoritative_total(sale, money(sale.tax_amount), tax_weight)


def make_return_key(sale_id: int, return_type: str, token: Optional[str] = None) -> str:
    if return_type == "full":
        return f"sale-return:sale:{sale_id}:full"
    cleaned = (token or "").strip()
    if not cleaned:
        raise SaleReturnDomainError("Partial returns require an Idempotency-Key")
    return f"sale-return:sale:{sale_id}:partial:{cleaned}"[:160]


def _canonical_lines(lines: Iterable[Tuple[models.SaleItem, Decimal]]):
    return sorted((int(item.id), str(quantity(qty))) for item, qty in lines)


def find_retry(
    db: Session,
    *,
    idempotency_key: str,
    sale_id: int,
    return_type: str,
    lines: Optional[Sequence[Tuple[models.SaleItem, Decimal]]] = None,
) -> Optional[models.SaleReturn]:
    posted = db.query(models.SaleReturn).filter_by(idempotency_key=idempotency_key).first()
    if not posted:
        return None
    if posted.sale_id != sale_id or posted.return_type != return_type:
        raise SaleReturnDomainError("Return retry token belongs to a different sale")
    if lines is not None:
        expected = _canonical_lines(lines)
        actual = sorted((int(line.sale_item_id), str(quantity(line.qty))) for line in posted.items)
        if actual != expected:
            raise SaleReturnDomainError("Return retry token belongs to different items")
    return posted


def total_returned_for_sale(db: Session, sale_id: int, through: Optional[datetime] = None) -> Decimal:
    query = db.query(func.sum(models.SaleReturn.refund_amount)).filter(models.SaleReturn.sale_id == sale_id)
    if through is not None:
        query = query.filter(models.SaleReturn.posted_at <= through)
    return money(query.scalar() or 0)


def paid_refund_amount(posted: models.SaleReturn) -> Decimal:
    """Return the exact reversal of collected tender for one return.

    Card returns reverse the immutable economic amount. Cash, mixed, and
    partially-paid credit returns
    use their linked cashbook row, which is capped by the source sale's actual
    paid amount. Credit and employee-credit returns reverse receivables/ledger
    principal and therefore do not reduce collected tender.
    """
    method = str(posted.payment_method or "").strip().lower()
    if method == "card":
        return money(posted.refund_amount)
    if method in {"cash", "mixed", "credit"}:
        cash_tx = getattr(posted, "cash_transaction", None)
        if (
            cash_tx is not None
            and cash_tx.tx_type == "cash_out"
            and cash_tx.cash_out_type == "refund"
            and cash_tx.account == "cash_in_hand"
        ):
            return money(cash_tx.amount)
    return Decimal("0.00")


def post_sale_return(
    db: Session,
    *,
    sale: models.Sale,
    lines: Sequence[Tuple[models.SaleItem, Decimal]],
    idempotency_key: str,
    return_type: str,
    created_by: str,
    final_return: bool = False,
    refund_amount_override=None,
    notes: Optional[str] = None,
    posted_at: Optional[datetime] = None,
) -> models.SaleReturn:
    """Create one immutable return header and its exact allocated lines.

    The caller owns the surrounding transaction.  Nothing is committed here,
    allowing stock, customer/employee balances, cashbook and return documents
    to succeed or roll back together.
    """
    if return_type not in {"full", "partial"}:
        raise SaleReturnDomainError("Return type must be full or partial")
    if not lines:
        raise SaleReturnDomainError("No returnable items remain on this sale")

    retry = find_retry(
        db,
        idempotency_key=idempotency_key,
        sale_id=sale.id,
        return_type=return_type,
        lines=lines,
    )
    if retry:
        return retry

    sale_amounts = allocate_sale_item_amounts(sale)
    sale_taxes = allocate_sale_item_taxes(sale)
    raw_lines = []
    for item, requested_qty in lines:
        qty = quantity(requested_qty)
        sold_qty = quantity(item.qty)
        if qty <= 0 or sold_qty <= 0:
            raise SaleReturnDomainError("Return quantities must be positive")
        raw_amount = (sale_amounts.get(item.id, Decimal("0")) / sold_qty) * qty
        raw_tax = (sale_taxes.get(item.id, Decimal("0")) / sold_qty) * qty
        raw_lines.append((item, qty, raw_amount, raw_tax))

    already_returned = total_returned_for_sale(db, sale.id)
    remaining_invoice = max(Decimal("0.00"), money(sale.total) - already_returned)
    if refund_amount_override is not None:
        refund_amount = money(refund_amount_override)
    elif final_return:
        refund_amount = remaining_invoice
    else:
        refund_amount = money(sum((row[2] for row in raw_lines), Decimal("0.00")))
    refund_amount = min(refund_amount, remaining_invoice)
    if refund_amount < 0:
        raise SaleReturnDomainError("These items have no remaining refundable value")
    if str(sale.payment_method or "").lower() == "employee_credit" and refund_amount <= 0:
        raise SaleReturnDomainError("Employee-credit returns must reverse a positive ledger principal")

    posted = models.SaleReturn(
        sale_id=sale.id,
        idempotency_key=idempotency_key,
        return_type=return_type,
        refund_amount=refund_amount,
        payment_method=str(sale.payment_method or "cash").lower(),
        created_by=created_by,
        notes=notes,
        posted_at=posted_at or datetime.now(),
    )
    db.add(posted)
    db.flush()

    already_returned_tax = money(db.query(func.sum(models.SaleReturnItem.allocated_tax_amount)).join(
        models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id
    ).filter(models.SaleReturn.sale_id == sale.id).scalar() or 0)
    remaining_tax = max(Decimal("0.00"), money(sale.tax_amount) - already_returned_tax)
    return_tax = remaining_tax if final_return else min(
        money(sum((row[3] for row in raw_lines), Decimal("0.00"))),
        remaining_tax,
    )

    allocated = Decimal("0.00")
    allocated_tax = Decimal("0.00")
    for index, (item, qty, raw_amount, raw_tax) in enumerate(raw_lines):
        remaining_allocation = max(Decimal("0.00"), refund_amount - allocated)
        remaining_tax_allocation = max(Decimal("0.00"), return_tax - allocated_tax)
        if index == len(raw_lines) - 1:
            amount = remaining_allocation
            tax_amount = remaining_tax_allocation
        else:
            amount = min(money(raw_amount), remaining_allocation)
            tax_amount = min(money(raw_tax), remaining_tax_allocation)
            allocated += amount
            allocated_tax += tax_amount
        line = models.SaleReturnItem(
            sale_return_id=posted.id,
            sale_item_id=item.id,
            product_id=item.product_id,
            product_name=item.product_name or "Unknown item",
            qty=qty,
            allocated_amount=amount,
            allocated_tax_amount=tax_amount,
            cost_amount=money(qty * Decimal(str(item.buy_price or 0))),
        )
        db.add(line)

    db.flush()
    return posted
