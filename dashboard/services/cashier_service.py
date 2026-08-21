"""
Cashier Performance Service — sales by cashier, shift summaries,
avg basket, discount patterns, refund tracking per cashier.
"""

from typing import List, Dict, Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from services.sale_return_service import POSTED_SALE_STATUSES


def _bounds(start_date: str, end_date: str):
    st_str = start_date if (" " in start_date or "T" in start_date) else f"{start_date} 00:00:00"
    en_str = end_date if (" " in end_date or "T" in end_date) else f"{end_date} 23:59:59"
    return st_str, en_str


def get_cashier_summaries(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    results = db.query(
        models.Sale.cashier,
        func.count(models.Sale.id).label("total_orders"),
        func.sum(models.Sale.total).label("gross_revenue"),
        func.sum(models.Sale.discount).label("total_discount"),
        func.avg(models.Sale.total).label("avg_basket"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by(models.Sale.cashier).order_by(func.sum(models.Sale.total).desc()).all()

    return_rows = db.query(
        models.Sale.cashier,
        func.count(models.SaleReturn.id).label("count"),
        func.sum(models.SaleReturn.refund_amount).label("amount"),
    ).join(models.SaleReturn).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by(models.Sale.cashier).all()

    sale_map = {row.cashier: row for row in results}
    return_map = {row.cashier: row for row in return_rows}
    summaries = []
    for cashier in set(sale_map) | set(return_map):
        sale_row = sale_map.get(cashier)
        return_row = return_map.get(cashier)
        gross = float(sale_row.gross_revenue or 0) if sale_row else 0.0
        disc = float(sale_row.total_discount or 0) if sale_row else 0.0
        refund_amt = float(return_row.amount or 0) if return_row else 0.0
        summaries.append({
            "username": cashier,
            "full_name": cashier,
            "total_orders": int(sale_row.total_orders or 0) if sale_row else 0,
            "gross_revenue": round(gross, 2),
            "net_revenue": round(gross - refund_amt, 2),
            "avg_basket": round(float(sale_row.avg_basket or 0), 2) if sale_row else 0.0,
            "total_discount_given": round(disc, 2),
            "refund_count": int(return_row.count or 0) if return_row else 0,
            "refund_amount": round(refund_amt, 2),
        })
    return summaries


def get_cashier_daily_trend(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    results = db.query(
        func.strftime("%Y-%m-%d", models.Sale.created_at).label("dt"),
        models.Sale.cashier,
        func.count(models.Sale.id).label("order_count"),
        func.sum(models.Sale.total).label("revenue"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by("dt", models.Sale.cashier).order_by("dt").all()

    returned = db.query(
        func.strftime("%Y-%m-%d", models.SaleReturn.posted_at).label("dt"),
        models.Sale.cashier,
        func.sum(models.SaleReturn.refund_amount).label("amount"),
    ).join(models.Sale).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by("dt", models.Sale.cashier).all()

    sale_map = {(str(row.dt), row.cashier): row for row in results}
    return_map = {(str(row.dt), row.cashier): float(row.amount or 0) for row in returned}
    return [
        {
            "date": key[0],
            "cashier": key[1],
            "order_count": int(sale_map[key].order_count or 0) if key in sale_map else 0,
            "revenue": round((float(sale_map[key].revenue or 0) if key in sale_map else 0.0) - return_map.get(key, 0.0), 2),
        }
        for key in sorted(set(sale_map) | set(return_map))
    ]


def get_cashier_shift_summaries(db: Session, start_date: str, end_date: str) -> List[Dict]:
    st_str, en_str = _bounds(start_date, end_date)
    results = db.query(
        func.strftime("%Y-%m-%d", models.Sale.created_at).label("sale_date"),
        models.Sale.cashier,
        func.count(models.Sale.id).label("orders"),
        func.sum(models.Sale.total).label("gross"),
        func.sum(models.Sale.discount).label("discounts"),
        func.sum(models.Sale.paid_amount).label("paid"),
    ).filter(
        models.Sale.status.in_(POSTED_SALE_STATUSES),
        models.Sale.created_at >= st_str,
        models.Sale.created_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by("sale_date", models.Sale.cashier).order_by("sale_date").all()

    return_rows = db.query(
        func.strftime("%Y-%m-%d", models.SaleReturn.posted_at).label("day"),
        models.Sale.cashier,
        models.SaleReturn.payment_method,
        func.sum(models.SaleReturn.refund_amount).label("amount"),
    ).join(models.Sale).filter(
        models.SaleReturn.posted_at >= st_str,
        models.SaleReturn.posted_at <= en_str,
        models.Sale.cashier.isnot(None),
    ).group_by("day", models.Sale.cashier, models.SaleReturn.payment_method).all()

    return_map = {}
    for row in return_rows:
        values = return_map.setdefault((str(row.day), row.cashier), {"total": 0.0, "cash": 0.0, "card": 0.0, "mixed": 0.0})
        amount = float(row.amount or 0)
        values["total"] += amount
        if row.payment_method in values:
            values[row.payment_method] += amount

    sale_map = {(str(row.sale_date), row.cashier): row for row in results}
    shifts = []
    for shift_key in sorted(set(sale_map) | set(return_map)):
        r = sale_map.get(shift_key)
        shift_date, cashier = shift_key
        gross = float(r.gross or 0) if r else 0.0
        disc = float(r.discounts or 0) if r else 0.0

        shift_st = f"{shift_date} 00:00:00"
        shift_en = f"{shift_date} 23:59:59"

        pm_q = db.query(
            models.Sale.payment_method,
            func.sum(models.Sale.paid_amount).label("amt"),
        ).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            models.Sale.created_at >= shift_st,
            models.Sale.created_at <= shift_en,
            models.Sale.cashier == cashier,
        ).group_by(models.Sale.payment_method).all()
        pm_map = {row.payment_method: float(row.amt or 0) for row in pm_q}
        refunds = return_map.get(shift_key, {"total": 0.0, "cash": 0.0, "card": 0.0, "mixed": 0.0})
        shifts.append({
            "cashier": cashier,
            "date": shift_date,
            "opening_time": None,
            "closing_time": None,
            "orders": int(r.orders or 0) if r else 0,
            "gross": round(gross, 2),
            "net": round(gross - refunds["total"], 2),
            "cash_collected": round(pm_map.get("cash", 0) - refunds["cash"], 2),
            "card_collected": round(pm_map.get("card", 0) - refunds["card"], 2),
            "credit_given": round(pm_map.get("credit", 0), 2),
            "discounts": round(disc, 2),
            "refunds": round(refunds["total"], 2),
        })
    return shifts
