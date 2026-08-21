"""
Executive Overview Service — Turbocharged for instant dashboard performance.
Uses batch aggregation and index-friendly date filtering to reduce query count from 40+ to <5.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_
from datetime import date, datetime, timedelta
from typing import List, Dict
import models
from services.sale_return_service import POSTED_SALE_STATUSES
from dashboard.services.accounting_helpers import salary_reversal_total

def _get_date_bounds(start_iso: str, end_iso: str):
    """Convert ISO strings to datetime objects for index-friendly filtering."""
    st = datetime.fromisoformat(start_iso).replace(hour=0, minute=0, second=0, microsecond=0)
    en = datetime.fromisoformat(end_iso).replace(hour=23, minute=59, second=59, microsecond=999)
    return st, en

def get_executive_kpis(db: Session, start_date_iso: str, end_date_iso: str) -> Dict:
    """Compute all top-level KPIs using optimized aggregation."""
    st, en = _get_date_bounds(start_date_iso, end_date_iso)
    
    # 1. Posted sales are recognized on sale date; returns are recognized on
    # their immutable posting date (which may be days or months later).
    sales_stats = db.query(
        func.count(models.Sale.id).label("orders"),
        func.sum(models.Sale.total).label("gross_raw"),
        func.sum(models.Sale.discount).label("discount"),
        func.sum(models.Sale.tax_amount).label("tax_raw"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st,
        models.Sale.created_at <= en
    ).first()

    return_stats = db.query(
        func.count(models.SaleReturn.id).label("refund_count"),
        func.sum(models.SaleReturn.refund_amount).label("returned_val"),
    ).filter(
        models.SaleReturn.posted_at >= st,
        models.SaleReturn.posted_at <= en,
    ).first()
    return_line_stats = db.query(
        func.sum(models.SaleReturnItem.allocated_tax_amount).label("returned_tax"),
        func.sum(models.SaleReturnItem.cost_amount).label("returned_cost"),
    ).join(models.SaleReturn).filter(
        models.SaleReturn.posted_at >= st,
        models.SaleReturn.posted_at <= en,
    ).first()

    gross_raw = float(sales_stats.gross_raw or 0)
    tax_raw = float(sales_stats.tax_raw or 0)
    returned_val = float(return_stats.returned_val or 0) if return_stats else 0
    returned_tax = float(return_line_stats.returned_tax or 0) if return_line_stats else 0

    actual_total_after_discount = gross_raw - returned_val
    discount = float(sales_stats.discount or 0)
    gross_before_discount = actual_total_after_discount + discount
    
    orders = sales_stats.orders or 0
    tax = tax_raw - returned_tax
    refund_count = return_stats.refund_count or 0
    
    net = actual_total_after_discount
    avg_order = round(actual_total_after_discount / orders, 2) if orders else 0.0

    # 2. BATCH EXPENSES (Unified: expenses table + unlinked cashbook expense/salary transactions)
    expenses_table = db.query(func.sum(models.Expense.amount)).filter(
        models.Expense.date >= st,
        models.Expense.date <= en
    ).scalar() or 0.0

    expenses_cash = db.query(func.sum(models.CashTransaction.amount)).filter(
        models.CashTransaction.tx_type == "cash_out",
        models.CashTransaction.cash_out_type.in_(["expense", "salary"]),
        (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
        models.CashTransaction.created_at >= st,
        models.CashTransaction.created_at <= en
    ).scalar() or 0.0

    expenses = float(expenses_table) + float(expenses_cash) - salary_reversal_total(db, st, en)

    # 3. BATCH COGS (Quantity Sold * buy_price from sale_items for historical accuracy)
    cogs_sold = db.query(
        func.sum(models.SaleItem.qty * models.SaleItem.buy_price)
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st,
        models.Sale.created_at <= en
    ).scalar() or 0.0
    cogs = float(cogs_sold) - float(return_line_stats.returned_cost or 0)

    # Profit calculations
    gross_profit = round(net - float(cogs), 2)
    net_profit = round(gross_profit - float(expenses), 2)

    return {
        "gross_sales": gross_before_discount,
        "net_sales": net,
        "cogs_estimated": round(float(cogs), 2),
        "gross_profit": gross_profit,
        "total_orders": orders,
        "avg_order_value": avg_order,
        "total_discount": discount,
        "tax_collected": tax,
        "total_expenses": round(float(expenses), 2),
        "net_profit": net_profit,
        "refund_count": refund_count,
        "refund_amount": round(returned_val, 2),
    }

def get_kpis_for_range(db: Session, start_iso: str, end_iso: str) -> Dict:
    """Computes KPIs for any arbitrary date range."""
    return get_executive_kpis(db, start_iso, end_iso)

def get_today_kpis(db: Session) -> Dict:
    today = date.today().isoformat()
    return get_executive_kpis(db, today, today)

def get_month_kpis(db: Session) -> Dict:
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    return get_executive_kpis(db, month_start, today.isoformat())

def get_payment_split(db: Session, start_iso: str, end_iso: str) -> List[Dict]:
    st, en = _get_date_bounds(start_iso, end_iso)
    results_sales = db.query(
        models.Sale.payment_method,
        func.sum(models.Sale.total).label("amount_raw"),
        func.count(models.Sale.id).label("count"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st,
        models.Sale.created_at <= en,
    ).group_by(models.Sale.payment_method).all()

    results_returned = db.query(
        models.SaleReturn.payment_method,
        func.sum(models.SaleReturn.refund_amount).label("returned_val")
    ).filter(
        models.SaleReturn.posted_at >= st,
        models.SaleReturn.posted_at <= en,
    ).group_by(models.SaleReturn.payment_method).all()

    sales_map = {r.payment_method: r for r in results_sales}
    ret_map = {r.payment_method: float(r.returned_val or 0) for r in results_returned}
    results = [
        {
            "method": method,
            "amount": float(sales_map[method].amount_raw or 0) - ret_map.get(method, 0.0) if method in sales_map else -ret_map.get(method, 0.0),
            "count": int(sales_map[method].count or 0) if method in sales_map else 0,
        }
        for method in sorted(set(sales_map) | set(ret_map))
    ]
    total = sum(row["amount"] for row in results)
    return [
        {
            "method": row["method"],
            "amount": round(row["amount"], 2),
            "count": row["count"],
            "pct": round(row["amount"] / total * 100, 1) if total else 0,
        }
        for row in results
    ]

def get_shift_summary(db: Session, date_str: str) -> Dict:
    st, en = _get_date_bounds(date_str, date_str)
    results_sales = db.query(
        models.Sale.cashier,
        func.count(models.Sale.id).label("sales_count"),
        func.sum(models.Sale.total).label("revenue_raw"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st,
        models.Sale.created_at <= en,
        models.Sale.cashier.isnot(None),
    ).group_by(models.Sale.cashier).all()

    results_returned = db.query(
        models.Sale.cashier,
        func.sum(models.SaleReturn.refund_amount).label("returned_val")
    ).join(models.SaleReturn, models.SaleReturn.sale_id == models.Sale.id).filter(
        models.SaleReturn.posted_at >= st,
        models.SaleReturn.posted_at <= en,
        models.Sale.cashier.isnot(None),
    ).group_by(models.Sale.cashier).all()

    sales_map = {r.cashier: r for r in results_sales}
    ret_map = {r.cashier: float(r.returned_val or 0) for r in results_returned}
    cashiers = [
        {
            "username": cashier,
            "full_name": cashier,
            "sales_today": int(sales_map[cashier].sales_count or 0) if cashier in sales_map else 0,
            "revenue_today": round((float(sales_map[cashier].revenue_raw or 0) if cashier in sales_map else 0.0) - ret_map.get(cashier, 0.0), 2),
        }
        for cashier in sorted(set(sales_map) | set(ret_map))
    ]

    total_orders = sum(c["sales_today"] for c in cashiers)
    total_rev = sum(c["revenue_today"] for c in cashiers)
    avg_basket = round(total_rev / total_orders, 2) if total_orders else 0

    return {
        "active_cashiers": cashiers,
        "total_sales_today": total_orders,
        "revenue_today": round(total_rev, 2),
        "avg_basket_today": avg_basket,
    }

def get_stock_snapshot(db: Session) -> Dict:
    """Batch stock health counts into one query."""
    stats = db.query(
        func.count(models.Product.id).label("total"),
        func.sum(case((and_(models.Product.stock <= models.Product.min_stock, models.Product.stock > 0), 1), else_=0)).label("low"),
        func.sum(case((models.Product.stock <= 0, 1), else_=0)).label("out")
    ).filter(models.Product.is_active == True).first()

    low_items = db.query(models.Product).filter(
        models.Product.is_active == True,
        models.Product.stock <= models.Product.min_stock,
    ).order_by(models.Product.stock.asc()).limit(5).all()

    return {
        "low_stock_count": int(stats.low or 0),
        "out_of_stock_count": int(stats.out or 0),
        "total_products": int(stats.total or 0),
        "low_stock_items": [
            {"id": p.id, "name": p.name, "stock": p.stock, "min_stock": p.min_stock}
            for p in low_items
        ],
    }

def get_7day_chart(db: Session) -> List[Dict]:
    """Revenue series using a single GROUP BY query instead of a loop."""
    today_dt = datetime.now()
    start_dt = (today_dt - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # SQLite friendly date grouping
    date_label = func.strftime('%Y-%m-%d', models.Sale.created_at)
    
    results_sales = db.query(
        date_label.label("day"),
        func.sum(models.Sale.total).label("revenue_raw"),
        func.count(models.Sale.id).label("count")
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= start_dt
    ).group_by(date_label).all()

    return_date_label = func.strftime('%Y-%m-%d', models.SaleReturn.posted_at)
    results_returned = db.query(
        return_date_label.label("day"),
        func.sum(models.SaleReturn.refund_amount).label("returned_val")
    ).filter(
        models.SaleReturn.posted_at >= start_dt
    ).group_by(return_date_label).all()

    ret_map = {r.day: r.returned_val for r in results_returned}
    
    sales_map = {r.day: r for r in results_sales}
    data_map = {
        day: {
            "revenue": (float(sales_map[day].revenue_raw or 0) if day in sales_map else 0.0) - float(ret_map.get(day, 0) or 0),
            "count": int(sales_map[day].count or 0) if day in sales_map else 0,
        }
        for day in set(sales_map) | set(ret_map)
    }
    
    # Fill in zeros for missing days
    series = []
    for i in range(7):
        d = (start_dt + timedelta(days=i)).date().isoformat()
        day_data = data_map.get(d, {"revenue": 0.0, "count": 0})
        series.append({
            "label": d,
            "value": day_data["revenue"],
            "count": day_data["count"]
        })
    
    return series

def get_receivables_payables(db: Session) -> Dict:
    recv = db.query(func.sum(models.Customer.credit_balance)).scalar() or 0
    pay = db.query(func.sum(models.Supplier.due_balance)).scalar() or 0
    return {
        "total_receivable": round(float(recv), 2),
        "total_payable": round(float(pay), 2),
    }
