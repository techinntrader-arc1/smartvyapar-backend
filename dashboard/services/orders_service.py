"""Order analytics with returns recognized on their posting date."""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from services.sale_return_service import POSTED_SALE_STATUSES


def _bounds(start_date: str, end_date: str):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"
    return st_str, en_str


def get_order_status_breakdown(db: Session, start_date: str, end_date: str) -> Dict:
    st_str, en_str = _bounds(start_date, end_date)
    rows = db.query(models.Sale.status, func.count(models.Sale.id).label("cnt")).filter(
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).group_by(models.Sale.status).all()
    result = {"completed": 0, "held": 0, "returned": 0, "partially_returned": 0}
    for row in rows:
        if row.status in result:
            result[row.status] = int(row.cnt or 0)
    result["total"] = sum(result.values())
    return result


def _period_key(value, period: str):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if period == "daily":
        return (value.year, value.month, value.day)
    if period == "weekly":
        iso = value.isocalendar()
        return (iso.year, iso.week)
    if period == "yearly":
        return (value.year,)
    return (value.year, value.month)


def _period_label(key, period: str) -> str:
    if period == "daily":
        return f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}"
    if period == "weekly":
        return f"W{key[1]}/{key[0]}"
    if period == "yearly":
        return str(key[0])
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{months[key[1] - 1]} {key[0]}"


def get_orders_volume_trend(db: Session, start_date: str, end_date: str, period: str = "daily") -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    period = period if period in {"daily", "weekly", "monthly", "yearly"} else "monthly"
    sales = db.query(models.Sale.created_at, models.Sale.total).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).all()
    returns = db.query(models.SaleReturn.posted_at, models.SaleReturn.refund_amount).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).all()
    data = {}
    for posted_at, total in sales:
        values = data.setdefault(_period_key(posted_at, period), {"count": 0, "revenue": 0.0})
        values["count"] += 1
        values["revenue"] += float(total or 0)
    for posted_at, amount in returns:
        values = data.setdefault(_period_key(posted_at, period), {"count": 0, "revenue": 0.0})
        values["revenue"] -= float(amount or 0)

    if period == "daily":
        current = datetime.strptime(start_date[:10], "%Y-%m-%d")
        end = datetime.strptime(end_date[:10], "%Y-%m-%d")
        while current <= end:
            data.setdefault(_period_key(current, period), {"count": 0, "revenue": 0.0})
            current += timedelta(days=1)
    result = []
    for key in sorted(data):
        values = data[key]
        result.append({
            "label": _period_label(key, period),
            "count": values["count"],
            "revenue": round(values["revenue"], 2),
            "avg_value": round(values["revenue"] / values["count"], 2) if values["count"] else 0.0,
        })
    return result


def get_orders_by_cashier(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    sales = db.query(
        models.Sale.cashier,
        func.count(models.Sale.id).label("count"),
        func.sum(models.Sale.total).label("amount"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by(models.Sale.cashier).all()
    returns = db.query(
        models.Sale.cashier,
        func.sum(models.SaleReturn.refund_amount).label("amount"),
    ).join(models.SaleReturn, models.SaleReturn.sale_id == models.Sale.id).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by(models.Sale.cashier).all()
    sale_map = {row.cashier: row for row in sales}
    return_map = {row.cashier: float(row.amount or 0) for row in returns}
    result = []
    for cashier in set(sale_map) | set(return_map):
        count = int(sale_map[cashier].count or 0) if cashier in sale_map else 0
        revenue = (float(sale_map[cashier].amount or 0) if cashier in sale_map else 0.0) - return_map.get(cashier, 0.0)
        result.append({
            "cashier": cashier,
            "order_count": count,
            "total_revenue": round(revenue, 2),
            "avg_order": round(revenue / count, 2) if count else 0.0,
        })
    return sorted(result, key=lambda row: row["total_revenue"], reverse=True)


def get_orders_by_payment_method(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    sales = db.query(
        models.Sale.payment_method,
        func.count(models.Sale.id).label("count"),
        func.sum(models.Sale.total).label("amount"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).group_by(models.Sale.payment_method).all()
    returns = db.query(
        models.SaleReturn.payment_method,
        func.sum(models.SaleReturn.refund_amount).label("amount"),
    ).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).group_by(models.SaleReturn.payment_method).all()
    sale_map = {row.payment_method: row for row in sales}
    return_map = {row.payment_method: float(row.amount or 0) for row in returns}
    items = []
    for method in set(sale_map) | set(return_map):
        count = int(sale_map[method].count or 0) if method in sale_map else 0
        total = (float(sale_map[method].amount or 0) if method in sale_map else 0.0) - return_map.get(method, 0.0)
        items.append({"method": method, "count": count, "total": round(total, 2)})
    grand = sum(item["total"] for item in items)
    for item in items:
        item["pct"] = round(item["total"] / grand * 100, 1) if grand else 0.0
    return items


def get_peak_patterns(db: Session, start_date: str, end_date: str) -> Dict:
    from dashboard.utils.aggregation_helpers import build_hourly_matrix
    cells = build_hourly_matrix(db, start_date, end_date)
    if not cells:
        return {"peak_hour": 0, "busiest_weekday": 0}
    hour_map: Dict[int, float] = {}
    weekday_map: Dict[int, float] = {}
    for cell in cells:
        hour_map[cell["hour"]] = hour_map.get(cell["hour"], 0) + cell["revenue"]
        weekday_map[cell["weekday"]] = weekday_map.get(cell["weekday"], 0) + cell["revenue"]
    return {
        "peak_hour": max(hour_map, key=hour_map.get) if hour_map else 0,
        "busiest_weekday": max(weekday_map, key=weekday_map.get) if weekday_map else 0,
    }


def get_recent_orders_timeline(
    db: Session,
    start_date: str,
    end_date: str,
    limit: int = 50,
    cashier: Optional[str] = None,
) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    query = db.query(models.Sale).filter(
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    )
    if cashier:
        query = query.filter(models.Sale.cashier == cashier)
    orders = query.order_by(models.Sale.created_at.desc()).limit(limit).all()
    return [
        {
            "id": sale.id,
            "invoice_no": sale.invoice_no,
            "cashier": sale.cashier or "",
            "customer": sale.customer.name if sale.customer else "Walk-in",
            "total": sale.total,
            "status": sale.status,
            "payment_method": sale.payment_method,
            "item_count": len(sale.items),
            "created_at": sale.created_at.isoformat() if sale.created_at else "",
        }
        for sale in orders
    ]
