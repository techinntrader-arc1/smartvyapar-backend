"""
Customer Analytics Service — growth trend, top customers, frequency distribution.
Includes safe None checks for start_date and end_date.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import date, timedelta
from typing import List, Dict, Optional
import models
from services.sale_return_service import POSTED_SALE_STATUSES


def get_customer_segment_summary(db: Session, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict:
    total_customers = db.query(func.count(models.Customer.id)).scalar() or 0
    month_start = date.today().replace(day=1).isoformat()
    new_this_month = db.query(func.count(models.Customer.id)).filter(
        func.date(models.Customer.created_at) >= month_start
    ).scalar() or 0

    repeat_query = db.query(
        models.Sale.customer_id,
        func.count(models.Sale.id).label("cnt"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.customer_id.isnot(None),
    )
    if start_date:
        repeat_query = repeat_query.filter(func.date(models.Sale.created_at) >= start_date)
    if end_date:
        repeat_query = repeat_query.filter(func.date(models.Sale.created_at) <= end_date)

    repeat_q = repeat_query.group_by(models.Sale.customer_id).having(func.count(models.Sale.id) > 1).all()
    repeat_count = len(repeat_q)

    total_buyers_query = db.query(
        models.Sale.customer_id.distinct()
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.customer_id.isnot(None),
    )
    if start_date:
        total_buyers_query = total_buyers_query.filter(func.date(models.Sale.created_at) >= start_date)
    if end_date:
        total_buyers_query = total_buyers_query.filter(func.date(models.Sale.created_at) <= end_date)

    total_buyers = total_buyers_query.count()
    repeat_rate = round(repeat_count / total_buyers * 100, 1) if total_buyers else 0
    total_recv = db.query(func.sum(models.Customer.credit_balance)).scalar() or 0
    avg_ltv = db.query(func.avg(models.Customer.credit_balance)).scalar() or 0

    return {
        "total_customers": total_customers,
        "new_this_month": new_this_month,
        "repeat_customers": repeat_count,
        "repeat_rate_pct": repeat_rate,
        "avg_lifetime_value": round(float(avg_ltv or 0), 2),
        "total_receivables": round(float(total_recv or 0), 2),
    }


def get_customer_growth_trend(db: Session) -> List[Dict]:
    results = db.query(
        extract("year", models.Customer.created_at).label("year"),
        extract("month", models.Customer.created_at).label("month"),
        func.count(models.Customer.id).label("new_customers"),
    ).group_by("year", "month").order_by("year", "month").all()

    months_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    cumulative = 0
    series = []
    for r in results:
        new = int(r.new_customers or 0)
        cumulative += new
        if r.month and r.year:
            m_idx = int(r.month) - 1
            if 0 <= m_idx < 12:
                series.append({
                    "label": f"{months_names[m_idx]} {int(r.year)}",
                    "new_customers": new,
                    "cumulative": cumulative,
                })
    return series


def get_top_customers(db: Session, start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 50) -> List[Dict]:
    query = db.query(
        models.Customer.id,
        models.Customer.name,
        models.Customer.phone,
        models.Customer.credit_balance,
        func.count(models.Sale.id).label("total_orders"),
        func.sum(models.Sale.total).label("total_spend"),
        func.max(func.date(models.Sale.created_at)).label("last_purchase"),
    ).join(models.Sale, models.Sale.customer_id == models.Customer.id
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES)
    )

    if start_date:
        query = query.filter(func.date(models.Sale.created_at) >= start_date)
    if end_date:
        query = query.filter(func.date(models.Sale.created_at) <= end_date)

    results = query.group_by(
        models.Customer.id, models.Customer.name, models.Customer.phone, models.Customer.credit_balance
    ).all()

    return_query = db.query(
        models.Sale.customer_id,
        func.sum(models.SaleReturn.refund_amount).label("refund_amount"),
    ).join(models.SaleReturn, models.SaleReturn.sale_id == models.Sale.id).filter(
        models.Sale.customer_id.isnot(None),
    )
    if start_date:
        return_query = return_query.filter(func.date(models.SaleReturn.posted_at) >= start_date)
    if end_date:
        return_query = return_query.filter(func.date(models.SaleReturn.posted_at) <= end_date)
    returned_by_customer = {
        row.customer_id: float(row.refund_amount or 0)
        for row in return_query.group_by(models.Sale.customer_id).all()
    }

    customer_rows = {
        row.id: {
            "customer_id": row.id,
            "name": row.name,
            "phone": row.phone or "",
            "total_orders": int(row.total_orders or 0),
            "total_spend": float(row.total_spend or 0) - returned_by_customer.get(row.id, 0.0),
            "credit_balance": float(row.credit_balance or 0),
            "last_purchase_date": str(row.last_purchase) if row.last_purchase else None,
        }
        for row in results
    }
    return_only_ids = set(returned_by_customer) - set(customer_rows)
    if return_only_ids:
        for customer in db.query(models.Customer).filter(models.Customer.id.in_(return_only_ids)).all():
            customer_rows[customer.id] = {
                "customer_id": customer.id,
                "name": customer.name,
                "phone": customer.phone or "",
                "total_orders": 0,
                "total_spend": -returned_by_customer[customer.id],
                "credit_balance": float(customer.credit_balance or 0),
                "last_purchase_date": None,
            }

    # If top customers with linked sales is empty, return all registered customers
    if not customer_rows:
        cust_query = db.query(models.Customer).limit(limit).all()
        return [
            {
                "customer_id": c.id,
                "name": c.name,
                "phone": c.phone or "",
                "total_orders": 0,
                "total_spend": 0.0,
                "avg_order_value": 0.0,
                "credit_balance": round(float(c.credit_balance or 0), 2),
                "last_purchase_date": None,
            }
            for c in cust_query
        ]

    ranked = sorted(customer_rows.values(), key=lambda row: row["total_spend"], reverse=True)[:limit]
    for row in ranked:
        row["total_spend"] = round(row["total_spend"], 2)
        row["avg_order_value"] = round(
            row["total_spend"] / row["total_orders"], 2
        ) if row["total_orders"] else 0.0
        row["credit_balance"] = round(row["credit_balance"], 2)
    return ranked


def get_customer_frequency_distribution(db: Session, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Dict]:
    query = db.query(
        models.Sale.customer_id,
        func.count(models.Sale.id).label("order_count"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.customer_id.isnot(None),
    )

    if start_date:
        query = query.filter(func.date(models.Sale.created_at) >= start_date)
    if end_date:
        query = query.filter(func.date(models.Sale.created_at) <= end_date)

    order_counts = query.group_by(models.Sale.customer_id).all()

    buckets = {"1 order": 0, "2-5 orders": 0, "6-10 orders": 0, "10+ orders": 0}
    for r in order_counts:
        cnt = int(r.order_count or 0)
        if cnt == 1:
            buckets["1 order"] += 1
        elif cnt <= 5:
            buckets["2-5 orders"] += 1
        elif cnt <= 10:
            buckets["6-10 orders"] += 1
        else:
            buckets["10+ orders"] += 1

    total = sum(buckets.values())
    return [
        {"bucket": k, "count": v, "pct": round(v / total * 100, 1) if total else 0}
        for k, v in buckets.items()
    ]
