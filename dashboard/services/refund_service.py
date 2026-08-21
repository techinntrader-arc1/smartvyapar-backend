"""Refund analytics backed by immutable posted return documents."""

from typing import Dict, List
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from services.sale_return_service import POSTED_SALE_STATUSES


def _bounds(start_date: str, end_date: str):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"
    return st_str, en_str


def get_refund_summary(db: Session, start_date: str, end_date: str) -> Dict:
    st_str, en_str = _bounds(start_date, end_date)
    stats = db.query(
        func.count(models.SaleReturn.id).label("count"),
        func.sum(models.SaleReturn.refund_amount).label("amount"),
    ).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).first()

    gross = db.query(func.sum(models.Sale.total)).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).scalar() or 0

    refund_amount = float(stats.amount or 0) if stats else 0.0
    return {
        "refund_count_period": int(stats.count or 0) if stats else 0,
        "total_refund_amount": round(refund_amount, 2),
        "refund_rate": round(refund_amount / float(gross) * 100, 2) if gross else 0.0,
    }


def get_refund_trend(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    day = func.strftime("%Y-%m-%d", models.SaleReturn.posted_at)
    rows = db.query(
        day.label("dt"),
        func.count(models.SaleReturn.id).label("refund_count"),
        func.sum(models.SaleReturn.refund_amount).label("refund_val"),
    ).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).group_by(day).order_by(day).all()

    return [
        {"date": str(row.dt), "amount": round(float(row.refund_val or 0), 2), "count": int(row.refund_count or 0)}
        for row in rows
    ]


def get_refunds_by_product(db: Session, start_date: str, end_date: str, limit: int = 20) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    rows = db.query(
        models.SaleReturnItem.product_id,
        models.SaleReturnItem.product_name,
        func.sum(models.SaleReturnItem.qty).label("returned_qty"),
        func.count(func.distinct(models.SaleReturnItem.sale_return_id)).label("return_count"),
        func.sum(models.SaleReturnItem.allocated_amount).label("refund_amt"),
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).group_by(
        models.SaleReturnItem.product_id,
        models.SaleReturnItem.product_name,
    ).order_by(func.sum(models.SaleReturnItem.qty).desc()).limit(limit).all()

    product_ids = [row.product_id for row in rows if row.product_id is not None]
    sold_rows = db.query(
        models.SaleItem.product_id,
        func.sum(models.SaleItem.qty).label("sold_qty"),
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.SaleItem.product_id.in_(product_ids or [-1]),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).group_by(models.SaleItem.product_id).all()
    sold_by_product = {row.product_id: float(row.sold_qty or 0) for row in sold_rows}

    return [
        {
            "product_id": row.product_id,
            "product_name": row.product_name,
            "category": "",
            "return_count": int(row.return_count or 0),
            "returned_qty": round(float(row.returned_qty or 0), 2),
            "refund_amount": round(float(row.refund_amt or 0), 2),
            "return_rate_pct": round(
                float(row.returned_qty or 0) / sold_by_product.get(row.product_id, 1.0) * 100,
                1,
            ) if sold_by_product.get(row.product_id, 0) else 0.0,
        }
        for row in rows
    ]


def get_refunds_by_category(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    rows = db.query(
        models.Category.id.label("category_id"),
        models.Category.name.label("cat_name"),
        func.count(func.distinct(models.SaleReturnItem.sale_return_id)).label("return_count"),
        func.sum(models.SaleReturnItem.allocated_amount).label("refund_amt"),
    ).join(models.Product, models.Product.category_id == models.Category.id
    ).join(models.SaleReturnItem, models.SaleReturnItem.product_id == models.Product.id
    ).join(models.SaleReturn, models.SaleReturn.id == models.SaleReturnItem.sale_return_id).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
    ).group_by(models.Category.id, models.Category.name).all()

    sales_by_cat_rows = db.query(
        models.Product.category_id.label("category_id"),
        func.sum(models.SaleItem.total).label("sales_amount")
    ).join(models.Sale, models.Sale.id == models.SaleItem.sale_id
    ).join(models.Product, models.Product.id == models.SaleItem.product_id
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
    ).group_by(models.Product.category_id).all()

    sales_by_category = {r.category_id: float(r.sales_amount or 0) for r in sales_by_cat_rows}

    return [
        {
            "category": row.cat_name,
            "return_count": int(row.return_count or 0),
            "amount": round(float(row.refund_amt or 0), 2),
            "sales_amount": round(sales_by_category.get(row.category_id, 0.0), 2),
            "return_rate_pct": round(
                float(row.refund_amt or 0) / sales_by_category[row.category_id] * 100,
                1,
            ) if sales_by_category.get(row.category_id, 0) else 0.0,
        }
        for row in rows
    ]


def get_recent_returns(db: Session, limit: int = 30) -> List[Dict]:
    returns = db.query(models.SaleReturn).order_by(models.SaleReturn.posted_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "sale_id": r.sale_id,
            "invoice_no": r.sale.invoice_no if r.sale else "",
            "posted_at": r.posted_at.isoformat() if r.posted_at else "",
            "refund_amount": float(r.refund_amount or 0),
            "reason": r.notes or "",
        }
        for r in returns
    ]

