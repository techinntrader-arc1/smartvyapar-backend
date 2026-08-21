"""
Product Performance Service — top sellers, slow movers, revenue ranking,
movement analytics, and margin analysis.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, case
from datetime import date, timedelta
from typing import List, Dict, Optional
import models
from dashboard.utils.aggregation_helpers import top_n_products
from services.sale_return_service import POSTED_SALE_STATUSES


def _bounds(start_date: str, end_date: str):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"
    return st_str, en_str


def get_top_products(
    db: Session,
    start_date: str,
    end_date: str,
    order_by: str = "revenue",
    limit: int = 20,
    category_id: Optional[int] = None,
) -> List[Dict]:
    """Top products by revenue or quantity sold."""
    results = top_n_products(db, start_date, end_date, n=100000 if category_id else limit)

    product_ids = [r["product_id"] for r in results]
    products = {p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(product_ids)).all()}
    if category_id:
        results = [row for row in results if row["product_id"] in products and products[row["product_id"]].category_id == category_id]
    results.sort(key=lambda row: row["total_revenue"] if order_by == "revenue" else row["total_qty"], reverse=True)
    results = results[:limit]

    return [
        {
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "category": products[row["product_id"]].category.name if row["product_id"] in products and products[row["product_id"]].category else "",
            "unit": products[row["product_id"]].unit.name if row["product_id"] in products and products[row["product_id"]].unit else "pcs",
            "total_qty": round(float(row["total_qty"] or 0), 2),
            "total_revenue": round(float(row["total_revenue"] or 0), 2),
            "order_count": int(row["order_count"] or 0),
            "rank": i + 1,
        }
        for i, row in enumerate(results)
    ]


def get_slow_movers(
    db: Session,
    period_days: int = 30,
    min_threshold_qty: float = 5.0,
    limit: int = 20,
) -> List[Dict]:
    """Products with very low sales velocity over the period."""
    cutoff = (date.today() - timedelta(days=period_days)).isoformat() + " 00:00:00"

    sold_rows = db.query(
        models.SaleItem.product_id,
        func.sum(models.SaleItem.qty).label("sold_qty"),
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= cutoff,
    ).group_by(models.SaleItem.product_id).all()

    returned_rows = db.query(
        models.SaleReturnItem.product_id,
        func.sum(models.SaleReturnItem.qty).label("returned_qty"),
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id).filter(
        models.SaleReturn.posted_at >= cutoff,
    ).group_by(models.SaleReturnItem.product_id).all()

    net_qty = {row.product_id: float(row.sold_qty or 0) for row in sold_rows}
    for row in returned_rows:
        net_qty[row.product_id] = net_qty.get(row.product_id, 0.0) - float(row.returned_qty or 0)

    products = db.query(models.Product).filter(
        models.Product.is_active == True,
        models.Product.stock > 0,
    ).all()
    products = sorted(
        (product for product in products if net_qty.get(product.id, 0.0) < min_threshold_qty),
        key=lambda product: float(product.stock or 0),
        reverse=True,
    )[:limit]

    return [
        {
            "product_id": p.id,
            "product_name": p.name,
            "category": p.category.name if p.category else "",
            "unit": p.unit.name if p.unit else "pcs",
            "stock": p.stock,
            "total_qty_period": round(net_qty.get(p.id, 0.0), 2),
            "last_sale_days_ago": None,
        }
        for p in products
    ]


def get_product_revenue_ranking(
    db: Session,
    start_date: str,
    end_date: str,
    limit: int = 30,
) -> List[Dict]:
    """Products ranked by revenue with margin calculations."""
    results = top_n_products(db, start_date, end_date, n=limit)

    product_ids = [row["product_id"] for row in results]
    products = {p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(product_ids)).all()}

    output = []
    for row in results:
        p = products.get(row["product_id"])
        buy = p.buy_price if p else 0
        sell = p.sell_price if p else 0
        margin = round((sell - buy) / sell * 100, 1) if sell else 0
        qty = float(row["total_qty"] or 0)
        revenue = float(row["total_revenue"] or 0)
        estimated_profit = round((sell - buy) * qty, 2) if p else 0

        output.append({
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "category": p.category.name if p and p.category else "",
            "sell_price": sell,
            "buy_price": buy,
            "margin_pct": margin,
            "total_sold_qty": round(qty, 2),
            "total_revenue": round(revenue, 2),
            "total_profit": estimated_profit,
        })
    return output


def get_product_movement_summary(
    db: Session,
    start_date: str,
    end_date: str,
    product_id: Optional[int] = None,
    limit: int = 30,
) -> List[Dict]:
    """Stock movement summary (in, out, returns, adjustments) per product."""
    st_str, en_str = _bounds(start_date, end_date)
    query = db.query(models.StockMovement)
    if product_id:
        query = query.filter(models.StockMovement.product_id == product_id)
    query = query.filter(
        models.StockMovement.created_at >= st_str,
        models.StockMovement.created_at <= en_str,
    )
    movements = query.all()

    product_map: Dict[int, Dict] = {}
    for m in movements:
        pid = m.product_id
        if pid not in product_map:
            product_map[pid] = {
                "product_id": pid,
                "product_name": m.product.name if m.product else "",
                "sale_qty": 0.0, "purchase_qty": 0.0,
                "return_qty": 0.0, "adjustment_qty": 0.0,
            }
        if m.movement_type == "sale":
            product_map[pid]["sale_qty"] += abs(m.qty_change)
        elif m.movement_type == "purchase":
            product_map[pid]["purchase_qty"] += m.qty_change
        elif m.movement_type == "return":
            product_map[pid]["return_qty"] += m.qty_change
        elif m.movement_type == "adjustment":
            product_map[pid]["adjustment_qty"] += m.qty_change

    result = []
    for pid, data in list(product_map.items())[:limit]:
        net = data["purchase_qty"] + data["return_qty"] + data["adjustment_qty"] - data["sale_qty"]
        data["net_movement"] = round(net, 2)
        data["sale_qty"] = round(data["sale_qty"], 2)
        data["purchase_qty"] = round(data["purchase_qty"], 2)
        data["return_qty"] = round(data["return_qty"], 2)
        data["adjustment_qty"] = round(data["adjustment_qty"], 2)
        data["opening_stock"] = 0.0
        data["closing_stock"] = 0.0
        result.append(data)

    return result
