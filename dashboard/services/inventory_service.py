"""
Inventory Insights Service — stock overview, low stock alerts, out-of-stock,
restock suggestions, stock value by category, and movement log.
"""

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case, and_
from datetime import date, timedelta
from typing import List, Dict
import models
from services.sale_return_service import POSTED_SALE_STATUSES


def get_stock_kpi(db: Session) -> Dict:
    res = db.query(
        func.count(models.Product.id).label("total"),
        func.sum(models.Product.stock * models.Product.buy_price).label("total_value"),
        func.sum(case((and_(models.Product.stock > 0, models.Product.stock <= models.Product.min_stock), 1), else_=0)).label("low"),
        func.sum(case((models.Product.stock <= 0, 1), else_=0)).label("out")
    ).filter(models.Product.is_active == True).first()

    total = res.total or 0
    total_value = float(res.total_value or 0)
    low = int(res.low or 0)
    out = int(res.out or 0)
    healthy = total - low - out
    avg_value = round(total_value / total, 2) if total else 0
    
    return {
        "total_products": total,
        "total_stock_value": round(total_value, 2),
        "low_stock_count": low,
        "out_of_stock_count": out,
        "healthy_stock_count": healthy,
        "avg_stock_value_per_product": avg_value,
    }


def get_low_stock_items(db: Session, include_zero: bool = False) -> List[Dict]:
    query = db.query(models.Product).options(
        joinedload(models.Product.category),
        joinedload(models.Product.unit)
    ).filter(
        models.Product.is_active == True,
        models.Product.stock <= models.Product.min_stock,
    )
    if not include_zero:
        query = query.filter(models.Product.stock > 0)
    products = query.order_by(models.Product.stock.asc()).all()
    return [
        {
            "product_id": p.id, "product_name": p.name,
            "category": p.category.name if p.category else "",
            "unit": p.unit.name if p.unit else "pcs",
            "current_stock": p.stock, "min_stock": p.min_stock or 0.0,
            "shortage": max(0, (p.min_stock or 0.0) - p.stock),
            "last_purchase_date": None, "last_sale_date": None,
        }
        for p in products
    ]


def get_out_of_stock(db: Session) -> List[Dict]:
    products = db.query(models.Product).options(
        joinedload(models.Product.category),
        joinedload(models.Product.unit)
    ).filter(
        models.Product.is_active == True,
        models.Product.stock <= 0,
    ).order_by(models.Product.name.asc()).all()
    return [
        {
            "product_id": p.id, "product_name": p.name,
            "category": p.category.name if p.category else "",
            "unit": p.unit.name if p.unit else "pcs",
            "current_stock": 0, "min_stock": p.min_stock,
            "shortage": p.min_stock,
            "last_purchase_date": None, "last_sale_date": None,
        }
        for p in products
    ]


def get_stock_value_by_category(db: Session) -> List[Dict]:
    results = db.query(
        models.Category.name.label("cat_name"),
        func.count(models.Product.id).label("product_count"),
        func.sum(models.Product.stock * models.Product.buy_price).label("total_value"),
        func.avg(models.Product.stock).label("avg_stock"),
    ).join(models.Product, models.Product.category_id == models.Category.id
    ).filter(models.Product.is_active == True
    ).group_by(models.Category.name
    ).order_by(func.sum(models.Product.stock * models.Product.buy_price).desc()).all()
    return [
        {
            "category_name": r.cat_name,
            "total_products": int(r.product_count or 0),
            "total_stock_value": round(float(r.total_value or 0), 2),
            "avg_stock": round(float(r.avg_stock or 0), 2),
        }
        for r in results
    ]


def get_recent_stock_movements(db: Session, limit: int = 50) -> List[Dict]:
    movements = db.query(models.StockMovement).options(
        joinedload(models.StockMovement.product)
    ).order_by(
        models.StockMovement.created_at.desc()
    ).limit(limit).all()
    return [
        {
            "id": m.id,
            "product_name": m.product.name if m.product else "",
            "movement_type": m.movement_type,
            "qty_change": m.qty_change, "qty_after": m.qty_after,
            "note": m.note or "",
            "created_at": m.created_at.isoformat() if m.created_at else "",
        }
        for m in movements
    ]


def get_restock_suggestions(db: Session) -> List[Dict]:
    today = date.today()
    cutoff_30 = (today - timedelta(days=30)).isoformat() + " 00:00:00"
    
    sold_rows = db.query(
        models.SaleItem.product_id,
        func.sum(models.SaleItem.qty).label("sold_30d"),
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= cutoff_30,
    ).group_by(models.SaleItem.product_id).all()

    returned_rows = db.query(
        models.SaleReturnItem.product_id,
        func.sum(models.SaleReturnItem.qty).label("returned_30d"),
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id).filter(
        models.SaleReturn.posted_at >= cutoff_30,
    ).group_by(models.SaleReturnItem.product_id).all()
    velocity = {row.product_id: float(row.sold_30d or 0) for row in sold_rows}
    for row in returned_rows:
        velocity[row.product_id] = velocity.get(row.product_id, 0.0) - float(row.returned_30d or 0)

    products = db.query(models.Product).options(
        joinedload(models.Product.category),
        joinedload(models.Product.unit)
    ).filter(
        models.Product.is_active == True,
        models.Product.stock <= models.Product.min_stock * 1.5,
    ).order_by(models.Product.stock.asc()).limit(200).all()

    suggestions = []
    for p in products:
        sold_30 = velocity.get(p.id, 0.0)
        avg_daily = round(sold_30 / 30, 2) if sold_30 > 0 else 0
        days_until_out = round(p.stock / avg_daily, 1) if avg_daily > 0 and p.stock > 0 else None
        min_stk = p.min_stock or 0.0
        shortage = max(0, min_stk - p.stock)
        suggested = max(shortage, min_stk * 2)
        priority = "critical" if p.stock <= 0 else ("high" if p.stock <= min_stk * 0.5 else "medium")
        
        suggestions.append({
            "product_id": p.id, "product_name": p.name,
            "category": p.category.name if p.category else "",
            "unit": p.unit.name if p.unit else "pcs",
            "current_stock": p.stock, "min_stock": p.min_stock,
            "suggested_qty": round(suggested, 2), "priority": priority,
            "avg_daily_sales": avg_daily, "days_until_stockout": days_until_out,
        })

    priority_order = {"critical": 0, "high": 1, "medium": 2}
    suggestions.sort(key=lambda x: priority_order.get(x["priority"], 3))
    return suggestions


def get_inventory_overview(db: Session) -> Dict:
    """Consolidated high-performance overview fetching."""
    return {
        "kpi": get_stock_kpi(db),
        "low_stock": get_low_stock_items(db, include_zero=False)[:100],
        "out_of_stock": get_out_of_stock(db)[:100],
        "value_by_category": get_stock_value_by_category(db),
        "movements": get_recent_stock_movements(db, limit=50),
        "suggestions": get_restock_suggestions(db),
    }
