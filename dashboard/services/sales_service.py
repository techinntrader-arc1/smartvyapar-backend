"""Sales analytics with immutable return-document accounting."""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

import models
from dashboard.utils.aggregation_helpers import build_hourly_matrix
from services.sale_return_service import POSTED_SALE_STATUSES, paid_refund_amount


def _key(value, period):
    if period == "daily":
        return (value.year, value.month, value.day)
    if period == "weekly":
        iso = value.isocalendar()
        return (iso.year, iso.week)
    if period == "yearly":
        return (value.year,)
    return (value.year, value.month)


def _label(key, period):
    if period == "daily":
        return f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}"
    if period == "weekly":
        return f"W{key[1]}/{key[0]}"
    if period == "yearly":
        return str(key[0])
    return f"{key[0]:04d}-{key[1]:02d}"


def _trend_data(db, start_date, end_date, period, cashier=None, payment_method=None):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"

    sales_q = db.query(models.Sale.created_at, models.Sale.total).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    )
    returns_q = db.query(models.SaleReturn.posted_at, models.SaleReturn.refund_amount).join(models.Sale).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    )
    if cashier:
        sales_q = sales_q.filter(models.Sale.cashier == cashier)
        returns_q = returns_q.filter(models.Sale.cashier == cashier)
    if payment_method:
        sales_q = sales_q.filter(models.Sale.payment_method == payment_method)
        returns_q = returns_q.filter(models.Sale.payment_method == payment_method)
    data = {}
    for posted_at, amount in sales_q.all():
        values = data.setdefault(_key(posted_at, period), {"value": 0.0, "count": 0})
        values["value"] += float(amount or 0)
        values["count"] += 1
    for posted_at, amount in returns_q.all():
        values = data.setdefault(_key(posted_at, period), {"value": 0.0, "count": 0})
        values["value"] -= float(amount or 0)
    if period == "daily":
        current = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        while current <= end:
            data.setdefault(_key(datetime.combine(current, datetime.min.time()), period), {"value": 0.0, "count": 0})
            current += timedelta(days=1)
    return data


def get_sales_trend(
    db: Session,
    start_date: str,
    end_date: str,
    period: str = "daily",
    cashier: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> Dict:
    if period not in {"daily", "weekly", "monthly", "yearly"}:
        return {"period": period, "series": [], "total_revenue": 0.0, "total_orders": 0, "avg_order_value": 0.0}
    data = _trend_data(db, start_date, end_date, period, cashier, payment_method)
    series = [
        {"label": _label(key, period), "value": round(values["value"], 2), "count": values["count"]}
        for key, values in sorted(data.items())
    ]
    total_revenue = sum(point["value"] for point in series)
    total_orders = sum(point["count"] for point in series)
    return {
        "period": period,
        "series": series,
        "total_revenue": round(total_revenue, 2),
        "total_orders": total_orders,
        "avg_order_value": round(total_revenue / total_orders, 2) if total_orders else 0,
    }


def get_hourly_heatmap(db: Session, start_date: str, end_date: str) -> Dict:
    cells = build_hourly_matrix(db, start_date, end_date)
    peak = max(cells, key=lambda cell: cell["revenue"]) if cells else {}
    return {
        "cells": cells,
        "peak_hour": peak.get("hour", 0),
        "peak_weekday": peak.get("weekday", 0),
        "peak_revenue": round(float(peak.get("revenue", 0)), 2),
    }


def get_basket_trend(db: Session, start_date: str, end_date: str, period: str = "daily") -> Dict:
    if period != "daily":
        series = []
        data = _trend_data(db, start_date, end_date, "daily")
    else:
        data = _trend_data(db, start_date, end_date, "daily")
        series = [
            {
                "label": _label(key, "daily"),
                "avg_basket": round(values["value"] / values["count"], 2) if values["count"] else 0.0,
                "order_count": values["count"],
            }
            for key, values in sorted(data.items())
        ]
    total_value = sum(values["value"] for values in data.values())
    total_count = sum(values["count"] for values in data.values())
    if len(series) >= 4:
        half = len(series) // 2
        first = sum(point["avg_basket"] for point in series[:half]) / half
        second = sum(point["avg_basket"] for point in series[half:]) / (len(series) - half)
        direction = "up" if second > first * 1.02 else ("down" if second < first * 0.98 else "flat")
    else:
        direction = "flat"
    return {
        "series": series,
        "overall_avg_basket": round(total_value / total_count, 2) if total_count else 0.0,
        "trend_direction": direction,
    }


def get_recent_sales(
    db: Session,
    start_date: str,
    end_date: str,
    cashier: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict:
    query = db.query(models.Sale).options(
        joinedload(models.Sale.items),
        joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction),
    ).filter(
        models.Sale.created_at >= start_date,
        models.Sale.created_at <= end_date + " 23:59:59",
    )
    if cashier:
        query = query.filter(models.Sale.cashier == cashier)
    if payment_method:
        query = query.filter(models.Sale.payment_method == payment_method)
    if status:
        query = query.filter(models.Sale.status == status)
    total = query.count()
    sales = query.order_by(models.Sale.created_at.desc()).offset(offset).limit(limit).all()
    items = []
    filtered_total = 0.0
    for sale in sales:
        returned = sum(float(posted.refund_amount or 0) for posted in sale.returns)
        net_total = max(0.0, float(sale.total or 0) - returned)
        filtered_total += net_total
        refunded_tender = sum(float(paid_refund_amount(posted)) for posted in sale.returns)
        net_paid = max(0.0, float(sale.paid_amount or 0) - refunded_tender)
        items.append({
            "id": sale.id,
            "invoice_no": sale.invoice_no,
            "customer": sale.customer.name if sale.customer else "Walk-in",
            "cashier": sale.cashier or "",
            "total": round(net_total, 2),
            "paid_amount": round(net_paid, 2),
            "payment_method": sale.payment_method,
            "status": sale.status,
            "item_count": len(sale.items),
            "date": sale.created_at.isoformat() if sale.created_at else "",
        })
    return {"items": items, "total": total, "filtered_total": round(filtered_total, 2)}
