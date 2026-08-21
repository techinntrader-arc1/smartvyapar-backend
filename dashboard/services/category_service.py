"""
Category Analytics Service — contribution breakdown, trend comparison,
category revenue tables.
"""

from typing import List, Dict, Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from dashboard.utils.aggregation_helpers import PALETTE, category_revenue_breakdown
from services.sale_return_service import POSTED_SALE_STATUSES


def _bounds(start_date: str, end_date: str):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"
    return st_str, en_str


def get_category_contributions(
    db: Session,
    start_date: str,
    end_date: str,
) -> List[Dict]:
    """Revenue contribution percentage for each category."""
    st_str, en_str = _bounds(start_date, end_date)
    revenue_rows = category_revenue_breakdown(db, start_date, end_date)
    product_counts = dict(db.query(
        models.Category.id.label("category_id"),
        func.count(models.Product.id.distinct()).label("product_count"),
    ).join(models.Product, models.Product.category_id == models.Category.id).group_by(
        models.Category.id
    ).all())

    average_rows = db.query(
        models.Product.category_id,
        func.avg(models.SaleItem.price).label("avg_item_value"),
    ).join(models.SaleItem, models.SaleItem.product_id == models.Product.id).join(
        models.Sale, models.SaleItem.sale_id == models.Sale.id
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).group_by(models.Product.category_id).all()
    average_values = {row.category_id: float(row.avg_item_value or 0) for row in average_rows}

    return [
        {
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "total_revenue": row["total_revenue"],
            "order_count": row["order_count"],
            "product_count": int(product_counts.get(row["category_id"], 0)),
            "contribution_pct": row["contribution_pct"],
            "avg_item_value": round(average_values.get(row["category_id"], 0.0), 2),
        }
        for row in revenue_rows
    ]


def get_category_trend_series(
    db: Session,
    start_date: str,
    end_date: str,
    top_n: int = 5,
) -> List[Dict]:
    """Multi-line trend series for top N categories using fast SQL aggregation."""
    st_str, en_str = _bounds(start_date, end_date)

    top_cats = category_revenue_breakdown(db, start_date, end_date)[:top_n]
    top_ids = {row["category_id"] for row in top_cats}
    if not top_ids:
        return []

    sales_rows = db.query(
        models.Product.category_id.label("category_id"),
        func.strftime("%Y-%m", models.Sale.created_at).label("month_key"),
        func.sum(models.SaleItem.total).label("revenue"),
        func.count(models.SaleItem.id).label("count")
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id
    ).join(models.Product, models.Product.id == models.SaleItem.product_id
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
        models.Product.category_id.in_(top_ids)
    ).group_by(models.Product.category_id, "month_key").all()

    returned_rows = db.query(
        models.Product.category_id.label("category_id"),
        func.strftime("%Y-%m", models.SaleReturn.posted_at).label("month_key"),
        func.sum(models.SaleReturnItem.allocated_amount).label("amount")
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id
    ).join(models.Product, models.Product.id == models.SaleReturnItem.product_id
    ).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
        models.Product.category_id.in_(top_ids)
    ).group_by(models.Product.category_id, "month_key").all()

    monthly_map = {cid: {} for cid in top_ids}
    all_months = set()

    for r in sales_rows:
        cid = r.category_id
        m = r.month_key
        all_months.add(m)
        monthly_map[cid][m] = {"revenue": float(r.revenue or 0), "count": int(r.count or 0)}

    for r in returned_rows:
        cid = r.category_id
        m = r.month_key
        all_months.add(m)
        entry = monthly_map[cid].setdefault(m, {"revenue": 0.0, "count": 0})
        entry["revenue"] -= float(r.amount or 0)

    series = []
    months_sorted = sorted(all_months)
    for i, cat in enumerate(top_cats):
        cid = cat["category_id"]
        points = [
            {
                "label": m,
                "revenue": round(monthly_map[cid].get(m, {}).get("revenue", 0.0), 2),
                "count": monthly_map[cid].get(m, {}).get("count", 0),
            }
            for m in months_sorted
        ]
        series.append({
            "category_id": cid,
            "category_name": cat["category_name"],
            "color": PALETTE[i % len(PALETTE)],
            "data": points,
        })

    return series
