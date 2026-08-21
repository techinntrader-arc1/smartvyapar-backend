from datetime import date, timedelta
from typing import Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from dashboard.filters.date_filters import prev_period_for
from dashboard.utils.aggregation_helpers import compute_delta
from services.sale_return_service import POSTED_SALE_STATUSES


def _bounds(start_date: str, end_date: str):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"
    return st_str, en_str


def get_revenue_breakdown(
    db: Session,
    start_date: str,
    end_date: str,
    cashier: Optional[str] = None,
) -> Dict:
    """Recognize sales on sale date and exact refunds on return posting date."""
    st_str, en_str = _bounds(start_date, end_date)
    sale_day = func.strftime("%Y-%m-%d", models.Sale.created_at)

    sales_query = db.query(
        sale_day.label("day"),
        func.sum(models.Sale.total).label("total"),
        func.sum(models.Sale.discount).label("discount"),
        func.sum(models.Sale.tax_amount).label("tax"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    )
    if cashier:
        sales_query = sales_query.filter(models.Sale.cashier == cashier)
    sales_rows = sales_query.group_by("day").all()

    return_day = func.strftime("%Y-%m-%d", models.SaleReturn.posted_at)
    return_query = db.query(
        return_day.label("day"),
        func.sum(models.SaleReturn.refund_amount).label("refunds"),
    ).join(models.Sale, models.Sale.id == models.SaleReturn.sale_id).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    )
    if cashier:
        return_query = return_query.filter(models.Sale.cashier == cashier)
    return_rows = return_query.group_by("day").all()

    return_tax_query = db.query(
        return_day.label("day"),
        func.sum(models.SaleReturnItem.allocated_tax_amount).label("tax"),
    ).join(models.SaleReturn).join(models.Sale, models.Sale.id == models.SaleReturn.sale_id).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    )
    if cashier:
        return_tax_query = return_tax_query.filter(models.Sale.cashier == cashier)
    return_tax_rows = return_tax_query.group_by("day").all()

    data = {}
    for row in sales_rows:
        total = float(row.total or 0)
        discount = float(row.discount or 0)
        data[str(row.day)] = {
            "gross": total + discount,
            "discount": discount,
            "tax": float(row.tax or 0),
            "refunds": 0.0,
        }
    for row in return_rows:
        data.setdefault(str(row.day), {"gross": 0.0, "discount": 0.0, "tax": 0.0, "refunds": 0.0})["refunds"] += float(row.refunds or 0)
    for row in return_tax_rows:
        data.setdefault(str(row.day), {"gross": 0.0, "discount": 0.0, "tax": 0.0, "refunds": 0.0})["tax"] -= float(row.tax or 0)

    series = []
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while current <= end:
        label = current.isoformat()
        values = data.get(label, {"gross": 0.0, "discount": 0.0, "tax": 0.0, "refunds": 0.0})
        net = values["gross"] - values["discount"] - values["refunds"]
        series.append({
            "label": label,
            "gross": round(values["gross"], 2),
            "discount": round(values["discount"], 2),
            "tax": round(values["tax"], 2),
            "net": round(net, 2),
            "refunds": round(values["refunds"], 2),
        })
        current += timedelta(days=1)
    totals = {key: round(sum(point[key] for point in series), 2) for key in ("gross", "discount", "tax", "net", "refunds")}
    return {"series": series, "totals": totals, "period": f"{start_date} to {end_date}"}


def get_discount_analysis(db: Session, start_date: str, end_date: str) -> Dict:
    st_str, en_str = _bounds(start_date, end_date)
    base = [
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ]
    total_discount = db.query(func.sum(models.Sale.discount)).filter(*base).scalar() or 0
    discount_count = db.query(func.count(models.Sale.id)).filter(*base, models.Sale.discount > 0).scalar() or 0
    sales_total = db.query(func.sum(models.Sale.total)).filter(*base).scalar() or 0
    returned_total = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).scalar() or 0

    net_sales = float(sales_total) - float(returned_total)
    avg_disc = round(float(total_discount) / discount_count, 2) if discount_count else 0
    disc_pct = round(float(total_discount) / net_sales * 100, 1) if net_sales else 0

    top_days_q = db.query(
        func.strftime("%Y-%m-%d", models.Sale.created_at).label("dt"),
        func.sum(models.Sale.discount).label("total_disc"),
    ).filter(*base, models.Sale.discount > 0).group_by("dt").order_by(func.sum(models.Sale.discount).desc()).limit(5).all()

    cashier_disc_q = db.query(
        models.Sale.cashier,
        func.sum(models.Sale.discount).label("total_disc"),
        func.count(models.Sale.id).label("cnt"),
    ).filter(*base).group_by(models.Sale.cashier).order_by(func.sum(models.Sale.discount).desc()).all()

    return {
        "total_discount": round(float(total_discount), 2),
        "discount_count": discount_count,
        "avg_discount_per_order": avg_disc,
        "discount_as_pct_of_gross": disc_pct,
        "top_discount_days": [{"date": str(row.dt), "discount": round(float(row.total_disc or 0), 2)} for row in top_days_q],
        "discount_by_cashier": [
            {"cashier": row.cashier, "total_discount": round(float(row.total_disc or 0), 2), "orders": int(row.cnt or 0)}
            for row in cashier_disc_q
        ],
    }


def get_tax_analysis(db: Session, start_date: str, end_date: str) -> Dict:
    st_str, en_str = _bounds(start_date, end_date)

    tax_sales = db.query(func.sum(models.Sale.tax_amount)).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).scalar() or 0

    tax_returned = db.query(func.sum(models.SaleReturnItem.allocated_tax_amount)).join(models.SaleReturn).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).scalar() or 0
    total_tax = float(tax_sales) - float(tax_returned)

    sales_total = db.query(func.sum(models.Sale.total)).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).scalar() or 0

    returned_total = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).scalar() or 0
    taxable_sales = float(sales_total) - float(returned_total)

    sales_by_category = db.query(
        models.Category.id.label("category_id"),
        models.Category.name.label("category"),
        func.sum(models.SaleItem.total).label("revenue"),
        func.sum(models.SaleItem.total * models.SaleItem.tax_pct / 100.0).label("tax"),
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id
    ).join(models.Product, models.Product.id == models.SaleItem.product_id
    ).join(models.Category, models.Category.id == models.Product.category_id
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).group_by(models.Category.id, models.Category.name).all()

    returns_by_category = db.query(
        models.Category.id.label("category_id"),
        models.Category.name.label("category"),
        func.sum(models.SaleReturnItem.allocated_amount).label("revenue"),
        func.sum(models.SaleReturnItem.allocated_tax_amount).label("tax"),
    ).join(models.Product, models.Product.category_id == models.Category.id
    ).join(models.SaleReturnItem, models.SaleReturnItem.product_id == models.Product.id
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id
    ).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).group_by(models.Category.id, models.Category.name).all()

    sale_map = {row.category_id: row for row in sales_by_category}
    return_map = {row.category_id: row for row in returns_by_category}
    by_category = []
    for category_id in set(sale_map) | set(return_map):
        sold = sale_map.get(category_id)
        returned = return_map.get(category_id)
        by_category.append({
            "category": sold.category if sold else returned.category,
            "tax_amount": round((float(sold.tax or 0) if sold else 0.0) - (float(returned.tax or 0) if returned else 0.0), 2),
            "revenue": round((float(sold.revenue or 0) if sold else 0.0) - (float(returned.revenue or 0) if returned else 0.0), 2),
        })
    return {
        "total_tax_collected": round(total_tax, 2),
        "effective_tax_rate": round(total_tax / taxable_sales * 100, 2) if taxable_sales else 0,
        "taxable_sales": round(taxable_sales, 2),
        "non_taxable_sales": 0.0,
        "tax_by_product_category": by_category,
    }


def get_period_comparison(db: Session, start_date: str, end_date: str) -> Dict:
    prev_start, prev_end = prev_period_for(start_date, end_date)

    def stats(st: str, en: str):
        st_str, en_str = _bounds(st, en)
        sale = db.query(
            func.sum(models.Sale.total).label("total"),
            func.sum(models.Sale.discount).label("discount"),
            func.sum(models.Sale.tax_amount).label("tax"),
            func.count(models.Sale.id).label("orders"),
        ).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            models.Sale.created_at >= st_str,
            models.Sale.created_at <= en_str,
        ).first()

        returned = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
            models.SaleReturn.posted_at >= st_str,
            models.SaleReturn.posted_at <= en_str,
        ).scalar() or 0

        returned_tax = db.query(func.sum(models.SaleReturnItem.allocated_tax_amount)).join(models.SaleReturn).filter(
            models.SaleReturn.posted_at >= st_str,
            models.SaleReturn.posted_at <= en_str,
        ).scalar() or 0

        total = float(sale.total or 0) if sale else 0.0
        discount = float(sale.discount or 0) if sale else 0.0
        refunds = float(returned)
        orders = int(sale.orders or 0) if sale else 0
        return total + discount, total - refunds, discount, float(sale.tax or 0) - float(returned_tax), orders, refunds

    curr_gross, curr_net, curr_disc, curr_tax, curr_orders, curr_refunds = stats(start_date, end_date)
    prev_gross, prev_net, prev_disc, prev_tax, prev_orders, prev_refunds = stats(prev_start, prev_end)
    curr_avg = round(curr_net / curr_orders, 2) if curr_orders else 0
    prev_avg = round(prev_net / prev_orders, 2) if prev_orders else 0
    return {
        "current_period": f"{start_date} to {end_date}",
        "previous_period": f"{prev_start} to {prev_end}",
        "gross_sales": compute_delta(curr_gross, prev_gross),
        "net_sales": compute_delta(curr_net, prev_net),
        "order_count": compute_delta(curr_orders, prev_orders),
        "avg_basket": compute_delta(curr_avg, prev_avg),
        "discounts": compute_delta(curr_disc, prev_disc),
        "tax": compute_delta(curr_tax, prev_tax),
        "refunds": compute_delta(curr_refunds, prev_refunds),
    }
