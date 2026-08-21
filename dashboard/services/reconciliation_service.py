"""End-of-day reconciliation using immutable return posting dates and values."""

from datetime import date, datetime, time, timedelta
from typing import Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

import models
from dashboard.utils.export_helpers import eod_report_text
from dashboard.services.accounting_helpers import salary_reversal_total
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


def get_eod_summary(db: Session, date_str: str) -> Dict:
    sales = db.query(models.Sale).options(
        joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction)
    ).filter(
        func.date(models.Sale.created_at) == date_str,
        models.Sale.status.in_(POSTED_SALE_STATUSES),
    ).all()
    returns = db.query(models.SaleReturn).options(
        joinedload(models.SaleReturn.cash_transaction)
    ).filter(func.date(models.SaleReturn.posted_at) == date_str).all()
    returned_tax = db.query(func.sum(models.SaleReturnItem.allocated_tax_amount)).join(models.SaleReturn).filter(
        func.date(models.SaleReturn.posted_at) == date_str
    ).scalar() or 0

    total_orders = len(sales)
    gross_sales = sum(float(sale.total or 0) for sale in sales)
    total_discount = sum(float(sale.discount or 0) for sale in sales)
    total_tax = sum(float(sale.tax_amount or 0) for sale in sales) - float(returned_tax)
    total_refund_amount = sum(float(posted.refund_amount or 0) for posted in returns)

    # Employee-credit is settled exclusively through the employee ledger and
    # must never appear as generic customer/walk-in outstanding or cash.
    payment_sales = [sale for sale in sales if sale.payment_method != "employee_credit"]
    paid_by_method = {method: 0.0 for method in ("cash", "card", "credit", "mixed")}
    for sale in payment_sales:
        if sale.payment_method in paid_by_method:
            paid_by_method[sale.payment_method] += float(sale.paid_amount or 0)
    for posted in returns:
        if posted.payment_method in paid_by_method:
            paid_by_method[posted.payment_method] -= float(paid_refund_amount(posted))

    start_of_day = datetime.combine(date.fromisoformat(date_str), time.min)
    end_of_day = datetime.combine(date.fromisoformat(date_str), time.max)
    outstanding = 0.0
    for sale in payment_sales:
        returned = _return_total(sale, end_of_day)
        net_total = max(0.0, float(sale.total or 0) - returned)
        net_paid = max(0.0, float(sale.paid_amount or 0) - _paid_return_total(sale, end_of_day))
        outstanding += max(0.0, net_total - net_paid)

    payments = db.query(models.Payment).filter(
        func.date(models.Payment.created_at) == date_str,
        models.Payment.party_type == "customer",
        models.Payment.payment_type == "received",
    ).all()
    pay_cash = sum(float(p.amount or 0) for p in payments if p.method == "cash")
    pay_card_bank = sum(float(p.amount or 0) for p in payments if p.method in ["card", "bank"])

    supplier_payments = db.query(models.Payment).filter(
        func.date(models.Payment.created_at) == date_str,
        models.Payment.party_type == "supplier",
        models.Payment.payment_type == "paid",
    ).all()
    sup_pay_cash = sum(float(p.amount or 0) for p in supplier_payments if p.method == "cash")
    sup_pay_bank = sum(float(p.amount or 0) for p in supplier_payments if p.method in ["card", "bank"])
    purchases = db.query(models.Purchase).filter(
        func.date(models.Purchase.created_at) == date_str,
        models.Purchase.payment_source == "cash_in_hand",
    ).all()
    sup_pay_cash += sum(float(p.paid_amount or 0) for p in purchases)

    cash_total = paid_by_method["cash"] + pay_cash
    card_total = paid_by_method["card"] + pay_card_bank
    credit_total = paid_by_method["credit"]
    mixed_total = paid_by_method["mixed"]
    total_paid = sum(paid_by_method.values()) + pay_cash + pay_card_bank

    expenses_table = db.query(func.sum(models.Expense.amount)).filter(
        func.strftime('%Y-%m-%d', models.Expense.date) == date_str
    ).scalar() or 0.0
    expenses_cash = db.query(func.sum(models.CashTransaction.amount)).filter(
        models.CashTransaction.tx_type == "cash_out",
        models.CashTransaction.cash_out_type.in_(["expense", "salary"]),
        (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
        func.strftime('%Y-%m-%d', models.CashTransaction.created_at) == date_str,
    ).scalar() or 0.0
    salary_reversals = salary_reversal_total(db, start_of_day, end_of_day)
    expenses = float(expenses_table) + float(expenses_cash) - salary_reversals
    drawer_expenses_table = db.query(func.sum(models.Expense.amount)).filter(
        func.strftime('%Y-%m-%d', models.Expense.date) == date_str,
        models.Expense.payment_source == "cash_in_hand",
    ).scalar() or 0.0
    drawer_expenses_cashbook = db.query(func.sum(models.CashTransaction.amount)).filter(
        models.CashTransaction.tx_type == "cash_out",
        models.CashTransaction.cash_out_type.in_(["expense", "salary"]),
        models.CashTransaction.account == "cash_in_hand",
        (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
        func.strftime('%Y-%m-%d', models.CashTransaction.created_at) == date_str,
    ).scalar() or 0.0
    drawer_expenses = (
        float(drawer_expenses_table)
        + float(drawer_expenses_cashbook)
        - salary_reversal_total(db, start_of_day, end_of_day, cash_only=True)
    )

    return {
        "date": date_str,
        "total_orders": total_orders,
        "gross_sales": round(gross_sales, 2),
        "total_discount": round(total_discount, 2),
        "total_tax": round(total_tax, 2),
        "net_sales": round(gross_sales - total_refund_amount, 2),
        "total_refunds": round(total_refund_amount, 2),
        "refund_count": len(returns),
        "total_expenses": round(expenses, 2),
        "cash_total": round(cash_total, 2),
        "card_total": round(card_total, 2),
        "credit_total": round(credit_total, 2),
        "mixed_total": round(mixed_total, 2),
        "expected_cash": round(cash_total + mixed_total + credit_total - drawer_expenses - sup_pay_cash, 2),
        "total_paid": round(total_paid, 2),
        "outstanding": round(outstanding, 2),
        "credit_payments_total": round(pay_cash + pay_card_bank, 2),
        "supplier_payments_total": round(sup_pay_cash + sup_pay_bank, 2),
        "supplier_cash_paid": round(sup_pay_cash, 2),
    }


def get_eod_cashier_breakdown(db: Session, date_str: str) -> List[Dict]:
    sales = db.query(models.Sale).filter(
        func.date(models.Sale.created_at) == date_str,
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.cashier.isnot(None),
    ).all()
    returns = db.query(models.SaleReturn).options(
        joinedload(models.SaleReturn.sale),
        joinedload(models.SaleReturn.cash_transaction),
    ).filter(
        func.date(models.SaleReturn.posted_at) == date_str
    ).all()
    cashier_map = {}
    for sale in sales:
        data = cashier_map.setdefault(sale.cashier, {"orders": 0, "gross": 0.0, "disc": 0.0, "paid": 0.0})
        data["orders"] += 1
        data["gross"] += float(sale.total or 0)
        data["disc"] += float(sale.discount or 0)
        if sale.payment_method != "employee_credit":
            data["paid"] += float(sale.paid_amount or 0)
    for posted in returns:
        cashier = posted.sale.cashier or posted.created_by
        data = cashier_map.setdefault(cashier, {"orders": 0, "gross": 0.0, "disc": 0.0, "paid": 0.0})
        data["gross"] -= float(posted.refund_amount or 0)
        data["paid"] -= float(paid_refund_amount(posted))
    return [
        {
            "cashier": cashier,
            "orders": data["orders"],
            "gross": round(data["gross"], 2),
            "net": round(data["gross"], 2),
            "paid": round(data["paid"], 2),
        }
        for cashier, data in cashier_map.items()
    ]


def get_eod_printable_report(db: Session, date_str: str) -> str:
    return eod_report_text(get_eod_summary(db, date_str))


def get_closing_summary(db: Session, date_str: str) -> Dict:
    eod = get_eod_summary(db, date_str)
    eod["cashier_breakdown"] = get_eod_cashier_breakdown(db, date_str)
    prev_date = str(date.fromisoformat(date_str) - timedelta(days=1))
    prev_sales = db.query(func.sum(models.Sale.total)).filter(
        func.date(models.Sale.created_at) == prev_date,
        models.Sale.status.in_(POSTED_SALE_STATUSES),
    ).scalar() or 0
    prev_returns = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
        func.date(models.SaleReturn.posted_at) == prev_date
    ).scalar() or 0
    prev_net = float(prev_sales) - float(prev_returns)
    eod["prev_day_gross"] = round(prev_net, 2)
    eod["day_over_day_change"] = round(eod["net_sales"] - prev_net, 2)
    return eod
