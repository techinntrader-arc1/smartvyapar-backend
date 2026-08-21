"""
Common SQLAlchemy query builder helpers for dashboard analytics.
Provides reusable filter application patterns.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from typing import Optional
import models
from services.sale_return_service import POSTED_SALE_STATUSES


def apply_sale_date_filter(query, start_date: str, end_date: str):
    """Apply date range filter to a Sale-based query."""
    return query.filter(
        func.date(models.Sale.created_at) >= start_date,
        func.date(models.Sale.created_at) <= end_date,
    )


def apply_cashier_filter(query, cashier: Optional[str]):
    """Apply cashier filter to a Sale-based query."""
    if cashier:
        return query.filter(models.Sale.cashier == cashier)
    return query


def apply_payment_method_filter(query, payment_method: Optional[str]):
    """Apply payment method filter to a Sale-based query."""
    if payment_method:
        return query.filter(models.Sale.payment_method == payment_method)
    return query


def apply_category_filter(query, category_id: Optional[int]):
    """Apply category filter via SaleItem→Product join."""
    if category_id:
        return query.join(models.SaleItem).join(
            models.Product,
            models.SaleItem.product_id == models.Product.id
        ).filter(models.Product.category_id == category_id)
    return query


def apply_customer_filter(query, customer_id: Optional[int]):
    """Apply customer filter to a Sale-based query."""
    if customer_id:
        return query.filter(models.Sale.customer_id == customer_id)
    return query


def completed_sales_base(db: Session):
    """Base query for all posted sales, including fully returned invoices."""
    return db.query(models.Sale).filter(models.Sale.status.in_(POSTED_SALE_STATUSES))


def get_distinct_cashiers(db: Session):
    """Return list of distinct cashier names from sales."""
    results = db.query(models.Sale.cashier).filter(
        models.Sale.cashier.isnot(None)
    ).distinct().all()
    return [r.cashier for r in results if r.cashier]


def sum_sale_column(db: Session, column, start_date: str, end_date: str,
                    status: str = "completed") -> float:
    """Sum a Sale column over a date range."""
    val = db.query(func.sum(column)).filter(
        models.Sale.status == status,
        func.date(models.Sale.created_at) >= start_date,
        func.date(models.Sale.created_at) <= end_date,
    ).scalar()
    return round(float(val or 0), 2)


def count_sales(db: Session, start_date: str, end_date: str,
                status: str = "completed") -> int:
    """Count sales over a date range."""
    return db.query(func.count(models.Sale.id)).filter(
        models.Sale.status == status,
        func.date(models.Sale.created_at) >= start_date,
        func.date(models.Sale.created_at) <= end_date,
    ).scalar() or 0
