from datetime import date, datetime, time
from typing import Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

import models
from services.sale_return_service import POSTED_SALE_STATUSES, paid_refund_amount


def _return_total(sale, through=None) -> float:
    return sum(
        float(posted.refund_amount or 0)
        for posted in sale.returns
        if through is None or posted.posted_at <= through
    )


def _paid_return_total(sale, through=None) -> float:
    return sum(
        float(paid_refund_amount(posted))
        for posted in sale.returns
        if through is None or posted.posted_at <= through
    )


def get_payment_method_summary(db: Session, start_date: str, end_date: str) -> List[Dict]:
    sale_rows = db.query(
        models.Sale.payment_method.label("method"),
        func.sum(models.Sale.paid_amount).label("amount"),
        func.count(models.Sale.id).label("count"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.payment_method != "employee_credit",
        func.date(models.Sale.created_at) >= start_date,
        func.date(models.Sale.created_at) <= end_date,
    ).group_by(models.Sale.payment_method).all()
    posted_returns = db.query(models.SaleReturn).options(
        joinedload(models.SaleReturn.cash_transaction)
    ).filter(
        func.date(models.SaleReturn.posted_at) >= start_date,
        func.date(models.SaleReturn.posted_at) <= end_date,
    ).all()
    sold = {row.method or "cash": row for row in sale_rows}
    returned = {}
    for posted in posted_returns:
        method = posted.payment_method or "cash"
        bucket = returned.setdefault(method, {"amount": 0.0, "count": 0})
        bucket["amount"] += float(paid_refund_amount(posted))
        bucket["count"] += 1
    rows = []
    for method in sorted(set(sold) | set(returned)):
        sale_row = sold.get(method)
        return_row = returned.get(method)
        sale_amount = float(sale_row.amount or 0) if sale_row else 0.0
        refund_amount = float(return_row["amount"]) if return_row else 0.0
        rows.append({
            "method": method,
            "total_amount": round(sale_amount - refund_amount, 2),
            "transaction_count": (
                (int(sale_row.count or 0) if sale_row else 0)
                + (int(return_row["count"]) if return_row else 0)
            ),
        })
    grand = sum(row["total_amount"] for row in rows)
    for row in rows:
        row["pct_of_total"] = round(row["total_amount"] / grand * 100, 1) if grand else 0
        row["avg_transaction"] = round(row["total_amount"] / row["transaction_count"], 2) if row["transaction_count"] else 0
    return rows


def get_payment_trend(db: Session, start_date: str, end_date: str) -> List[Dict]:
    sale_day = func.date(models.Sale.created_at)
    sale_rows = db.query(
        sale_day.label("day"),
        models.Sale.payment_method.label("method"),
        func.sum(models.Sale.paid_amount).label("amount"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.payment_method != "employee_credit",
        sale_day >= start_date,
        sale_day <= end_date,
    ).group_by(sale_day, models.Sale.payment_method).all()
    return_rows = db.query(models.SaleReturn).options(
        joinedload(models.SaleReturn.cash_transaction)
    ).filter(
        func.date(models.SaleReturn.posted_at) >= start_date,
        func.date(models.SaleReturn.posted_at) <= end_date,
    ).all()

    data = {}
    for row in sale_rows:
        day = data.setdefault(str(row.day), {"cash": 0.0, "card": 0.0, "credit": 0.0, "mixed": 0.0})
        if row.method in day:
            day[row.method] += float(row.amount or 0)
    for row in return_rows:
        day = data.setdefault(str(row.posted_at.date()), {"cash": 0.0, "card": 0.0, "credit": 0.0, "mixed": 0.0})
        if row.payment_method in day:
            day[row.payment_method] -= float(paid_refund_amount(row))
    return [
        {
            "label": day,
            **{method: round(values[method], 2) for method in ("cash", "card", "credit", "mixed")},
            "total": round(sum(values.values()), 2),
        }
        for day, values in sorted(data.items())
    ]


def get_payment_reconciliation(db: Session, date_str: str) -> Dict:
    sales = db.query(models.Sale).options(
        joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction)
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.payment_method != "employee_credit",
        func.date(models.Sale.created_at) == date_str,
    ).all()
    refund_rows = db.query(models.SaleReturn).options(
        joinedload(models.SaleReturn.cash_transaction)
    ).filter(
        func.date(models.SaleReturn.posted_at) == date_str,
    ).all()
    refunds = {}
    for row in refund_rows:
        refunds[row.payment_method] = refunds.get(row.payment_method, 0.0) + float(paid_refund_amount(row))

    cash_methods = {"cash", "mixed", "credit"}
    expected_cash = sum(float(s.paid_amount or 0) for s in sales if s.payment_method in cash_methods) - sum(refunds.get(method, 0.0) for method in cash_methods)
    expected_card = sum(float(s.paid_amount or 0) for s in sales if s.payment_method == "card") - refunds.get("card", 0.0)
    paid_total = sum(float(s.paid_amount or 0) for s in sales) - sum(refunds.values())

    end_of_day = datetime.combine(date.fromisoformat(date_str), time.max)
    outstanding = 0.0
    outstanding_orders = 0
    for sale in sales:
        returned = _return_total(sale, end_of_day)
        net_total = max(0.0, float(sale.total or 0) - returned)
        net_paid = max(0.0, float(sale.paid_amount or 0) - _paid_return_total(sale, end_of_day))
        balance = net_total - net_paid
        if balance > 0:
            outstanding += balance
            outstanding_orders += 1

    return {
        "date": date_str,
        "expected_cash": round(expected_cash, 2),
        "expected_card": round(expected_card, 2),
        "total_expected": round(paid_total, 2),
        "paid_amount": round(paid_total, 2),
        "outstanding_amount": round(outstanding, 2),
        "outstanding_orders": outstanding_orders,
    }


def get_outstanding_payments(db: Session, limit: int = 50) -> List[Dict]:
    sales = db.query(models.Sale).options(
        joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction),
        joinedload(models.Sale.customer),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.payment_method != "employee_credit",
        models.Sale.total > models.Sale.paid_amount,
    ).order_by(models.Sale.created_at.asc()).all()

    today = date.today()
    result = []
    for sale in sales:
        returned = _return_total(sale)
        net_total = max(0.0, float(sale.total or 0) - returned)
        net_paid = max(0.0, float(sale.paid_amount or 0) - _paid_return_total(sale))
        balance = net_total - net_paid
        if balance > 0:
            days_out = (today - sale.created_at.date()).days if sale.created_at else 0
            result.append({
                "sale_id": sale.id,
                "invoice_no": sale.invoice_no,
                "customer": sale.customer.name if sale.customer else "Walk-in",
                "total": round(net_total, 2),
                "paid_amount": round(net_paid, 2),
                "balance_due": round(balance, 2),
                "days_outstanding": days_out,
                "payment_method": sale.payment_method,
            })
            if len(result) >= limit:
                break
    return result
