"""
Aggregation helpers for building time-series trend data.
Used across sales, revenue, orders analytics services.
"""

from datetime import date, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, extract, case
import models
from services.sale_return_service import POSTED_SALE_STATUSES, allocate_sale_item_amounts


PALETTE = [
    "#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6",
    "#8b5cf6", "#06b6d4", "#f97316", "#84cc16", "#ec4899"
]


def build_daily_series(
    db: Session,
    start_date: str,
    end_date: str,
    value_fn,
    count_fn=None
) -> List[Dict]:
    """
    Build a daily series between start and end dates.
    value_fn(date_str) -> float
    count_fn(date_str) -> int (optional)
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    series = []
    current = start
    while current <= end:
        d_str = current.isoformat()
        value = value_fn(d_str)
        point = {"label": d_str, "value": round(float(value or 0), 2), "count": 0}
        if count_fn:
            point["count"] = count_fn(d_str) or 0
        series.append(point)
        current += timedelta(days=1)
    return series


def build_weekly_series(
    db: Session,
    start_date: str,
    end_date: str,
    value_query_fn
) -> List[Dict]:
    """Build weekly aggregated series."""
    results = db.execute(value_query_fn(start_date, end_date)).fetchall()
    week_map = {}
    for row in results:
        key = f"W{row.week}-{row.year}" if hasattr(row, "week") else str(row[0])
        week_map[key] = {"label": key, "value": round(float(row.total or 0), 2), "count": int(row.cnt or 0)}
    return list(week_map.values())


def build_monthly_series(
    db: Session,
    start_date: str,
    end_date: str
) -> List[Dict]:
    """Build monthly sales aggregation."""
    results_sales = db.query(
        extract("year", models.Sale.created_at).label("year"),
        extract("month", models.Sale.created_at).label("month"),
        func.sum(models.Sale.total).label("total"),
        func.count(models.Sale.id).label("cnt"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= start_date,
        models.Sale.created_at <= end_date + " 23:59:59"
    ).group_by("year", "month").order_by("year", "month").all()

    results_returned = db.query(
        extract("year", models.SaleReturn.posted_at).label("year"),
        extract("month", models.SaleReturn.posted_at).label("month"),
        func.sum(models.SaleReturn.refund_amount).label("returned_val")
    ).filter(
        models.SaleReturn.posted_at >= start_date,
        models.SaleReturn.posted_at <= end_date + " 23:59:59"
    ).group_by("year", "month").order_by("year", "month").all()

    ret_map = {(int(r.year), int(r.month)): r.returned_val for r in results_returned}

    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    sales_map = {(int(r.year), int(r.month)): r for r in results_sales}
    return [
        {
            "label": f"{months[key[1] - 1]} {key[0]}",
            "value": round((float(sales_map[key].total or 0) if key in sales_map else 0.0) - float(ret_map.get(key, 0) or 0), 2),
            "count": int(sales_map[key].cnt or 0) if key in sales_map else 0,
        }
        for key in sorted(set(sales_map) | set(ret_map))
    ]


def build_hourly_matrix(db: Session, start_date: str, end_date: str) -> List[Dict]:
    """Build hour×weekday heatmap cells from completed sales."""
    results_sales = db.query(
        extract("dow", models.Sale.created_at).label("weekday"),
        extract("hour", models.Sale.created_at).label("hour"),
        func.count(models.Sale.id).label("order_count"),
        func.sum(models.Sale.total).label("revenue"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= start_date,
        models.Sale.created_at <= end_date + " 23:59:59"
    ).group_by("weekday", "hour").all()

    results_returned = db.query(
        extract("dow", models.SaleReturn.posted_at).label("weekday"),
        extract("hour", models.SaleReturn.posted_at).label("hour"),
        func.sum(models.SaleReturn.refund_amount).label("returned_val")
    ).filter(
        models.SaleReturn.posted_at >= start_date,
        models.SaleReturn.posted_at <= end_date + " 23:59:59"
    ).group_by("weekday", "hour").all()

    ret_map = {(int(r.weekday or 0), int(r.hour or 0)): r.returned_val for r in results_returned}

    sales_map = {(int(r.weekday or 0), int(r.hour or 0)): r for r in results_sales}
    return [
        {
            "weekday": key[0],
            "hour": key[1],
            "order_count": int(sales_map[key].order_count or 0) if key in sales_map else 0,
            "revenue": round((float(sales_map[key].revenue or 0) if key in sales_map else 0.0) - float(ret_map.get(key, 0) or 0), 2),
        }
        for key in sorted(set(sales_map) | set(ret_map))
    ]


def compute_delta(current: float, previous: float) -> Dict:
    """Compute percentage delta between two period values."""
    if previous == 0:
        delta_pct = 100.0 if current > 0 else 0.0
    else:
        delta_pct = round((current - previous) / previous * 100, 1)
    direction = "up" if delta_pct > 0 else ("down" if delta_pct < 0 else "flat")
    return {
        "current": round(current, 2),
        "previous": round(previous, 2),
        "delta_pct": delta_pct,
        "direction": direction,
    }


def top_n_products(
    db: Session, start_date: str, end_date: str, n: int = 10
) -> List[Dict]:
    """Return top N products by revenue in period using turbocharged SQL aggregation."""
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"

    sales_rows = db.query(
        models.SaleItem.product_id,
        models.SaleItem.product_name,
        func.sum(models.SaleItem.qty).label("total_qty"),
        func.sum(models.SaleItem.total).label("total_revenue"),
        func.count(models.SaleItem.id).label("order_count")
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str
    ).group_by(models.SaleItem.product_id, models.SaleItem.product_name).all()

    returned_rows = db.query(
        models.SaleReturnItem.product_id,
        models.SaleReturnItem.product_name,
        func.sum(models.SaleReturnItem.qty).label("qty"),
        func.sum(models.SaleReturnItem.allocated_amount).label("amount")
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str
    ).group_by(models.SaleReturnItem.product_id, models.SaleReturnItem.product_name).all()

    return_map = {r.product_id: r for r in returned_rows}
    prod_map = {}

    for row in sales_rows:
        pid = row.product_id
        ret = return_map.get(pid)
        ret_qty = float(ret.qty or 0) if ret else 0.0
        ret_amt = float(ret.amount or 0) if ret else 0.0
        prod_map[pid] = {
            "product_id": pid,
            "product_name": row.product_name,
            "total_qty": float(row.total_qty or 0) - ret_qty,
            "total_revenue": float(row.total_revenue or 0) - ret_amt,
            "order_count": int(row.order_count or 0),
        }

    sold_ids = set(prod_map)
    for ret in returned_rows:
        pid = ret.product_id
        if pid not in sold_ids:
            prod_map[pid] = {
                "product_id": pid,
                "product_name": ret.product_name,
                "total_qty": -float(ret.qty or 0),
                "total_revenue": -float(ret.amount or 0),
                "order_count": 0,
            }

    rows = sorted(prod_map.values(), key=lambda r: r["total_revenue"], reverse=True)[:n]
    return [
        {
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "total_qty": round(row["total_qty"], 2),
            "total_revenue": round(row["total_revenue"], 2),
            "order_count": row["order_count"],
            "rank": i + 1,
        }
        for i, row in enumerate(rows)
    ]


def category_revenue_breakdown(
    db: Session, start_date: str, end_date: str
) -> List[Dict]:
    """Revenue grouped by product category using turbocharged SQL aggregation."""
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"

    sales_rows = db.query(
        models.Product.category_id.label("category_id"),
        models.Category.name.label("category_name"),
        func.sum(models.SaleItem.total).label("total_revenue"),
        func.count(models.SaleItem.id).label("order_count")
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id
    ).join(models.Product, models.Product.id == models.SaleItem.product_id
    ).join(models.Category, models.Category.id == models.Product.category_id
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str
    ).group_by(models.Product.category_id, models.Category.name).all()

    returned_rows = db.query(
        models.Category.id.label("category_id"),
        models.Category.name.label("category_name"),
        func.sum(models.SaleReturnItem.allocated_amount).label("amount")
    ).join(models.Product, models.Product.category_id == models.Category.id
    ).join(models.SaleReturnItem, models.SaleReturnItem.product_id == models.Product.id
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id
    ).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str
    ).group_by(models.Category.id, models.Category.name).all()

    ret_map = {r.category_id: float(r.amount or 0) for r in returned_rows}
    cat_map = {}

    for row in sales_rows:
        cid = row.category_id
        ret_amt = ret_map.get(cid, 0.0)
        cat_map[cid] = {
            "category_id": cid,
            "category_name": row.category_name,
            "total_revenue": float(row.total_revenue or 0) - ret_amt,
            "order_count": int(row.order_count or 0),
        }

    sold_cids = set(cat_map)
    for row in returned_rows:
        cid = row.category_id
        if cid not in sold_cids:
            cat_map[cid] = {
                "category_id": cid,
                "category_name": row.category_name,
                "total_revenue": -float(row.amount or 0),
                "order_count": 0,
            }

    rows = sorted(cat_map.values(), key=lambda r: r["total_revenue"], reverse=True)
    total = sum(r["total_revenue"] for r in rows)
    return [
        {
            "category_id": r["category_id"],
            "category_name": r["category_name"],
            "total_revenue": round(r["total_revenue"], 2),
            "order_count": r["order_count"],
            "contribution_pct": round(r["total_revenue"] / total * 100, 1) if total else 0,
        }
        for r in rows
    ]

