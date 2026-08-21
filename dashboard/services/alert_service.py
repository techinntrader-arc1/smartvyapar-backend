"""
Alert Service — builds the unified alert feed combining inventory, sales,
refund, and discount anomaly detection.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta
from typing import List, Dict
import models
from services.sale_return_service import POSTED_SALE_STATUSES
from dashboard.utils.anomaly_helpers import (
    detect_spikes, detect_sales_drop, detect_refund_spike, build_alert_feed
)
from dashboard.services import inventory_service
from dashboard.utils.aggregation_helpers import build_daily_series


def get_alert_feed(db: Session) -> List[Dict]:
    today = date.today()
    start_30 = (today - timedelta(days=29)).isoformat()
    end = today.isoformat()

    # Stock counts & items
    kpi = inventory_service.get_stock_kpi(db)
    low_stock_count = kpi["low_stock_count"]
    out_of_stock_count = kpi["out_of_stock_count"]
    out_of_stock_items = inventory_service.get_out_of_stock(db)
    low_stock_items = inventory_service.get_low_stock_items(db)

    # Sales trend for drop detection
    def val_fn(d: str) -> float:
        sold = db.query(func.sum(models.Sale.total)).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            func.date(models.Sale.created_at) == d,
        ).scalar() or 0
        returned = db.query(func.sum(models.SaleReturn.refund_amount)).filter(
            func.date(models.SaleReturn.posted_at) == d,
        ).scalar() or 0
        return float(sold) - float(returned)

    sales_series = build_daily_series(db, start_30, end, val_fn)
    sales_drop = detect_sales_drop(sales_series)

    # Refund spike detection
    refund_series = []
    refund_alert = None

    # Discount anomalies
    def disc_fn(d: str) -> float:
        return db.query(func.sum(models.Sale.discount)).filter(
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            func.date(models.Sale.created_at) == d,
        ).scalar() or 0

    disc_series_raw = build_daily_series(db, start_30, end, disc_fn)
    # Rename 'value' to 'discount' for anomaly helper
    disc_series = [{"label": p["label"], "discount": p["value"]} for p in disc_series_raw]
    discount_anomalies = detect_spikes(disc_series, value_key="discount")

    # Enrich discount anomalies with exact invoice numbers, cashiers, and statistical reasoning
    for anomaly in discount_anomalies:
        anom_date = anomaly.get("label")
        avg_val = anomaly.get("avg_value", 0)
        actual_val = anomaly.get("value", 0)
        dev_pct = anomaly.get("deviation_pct", 0)

        if anom_date:
            sales_with_discount = db.query(models.Sale).filter(
                models.Sale.status.in_(POSTED_SALE_STATUSES),
                func.date(models.Sale.created_at) == anom_date,
                models.Sale.discount > 0
            ).all()

            stat_summary = f"Statistical Spike: Rs. {actual_val:,.0f} discount on {anom_date} is +{dev_pct:,.0f}% higher than your 30-day average (Rs. {avg_val:,.0f}/day)."

            if sales_with_discount:
                inv_list = [
                    f"Invoice #{s.invoice_no}: Rs. {s.discount:,.0f} discount (Cashier: {s.cashier or 'System'}, Total: Rs. {s.total:,.0f})"
                    for s in sales_with_discount
                ]
                anomaly["detail"] = f"{stat_summary}\n\nDiscount breakdown on {anom_date}:\n• " + "\n• ".join(inv_list)
            else:
                anomaly["detail"] = f"{stat_summary}\n\nTotal discount of Rs. {actual_val:,.0f} detected across transactions on {anom_date}."

    return build_alert_feed(low_stock_count, out_of_stock_count, sales_drop, refund_alert, discount_anomalies, out_of_stock_items=out_of_stock_items, low_stock_items=low_stock_items)


def get_stale_data_indicators(db: Session) -> List[Dict]:
    """Check for stale data situations like no sales today, last backup age, etc."""
    today = date.today().isoformat()
    indicators = []

    today_sales = db.query(func.count(models.Sale.id)).filter(
        func.date(models.Sale.created_at) == today
    ).scalar() or 0

    if today_sales == 0:
        from datetime import datetime
        indicators.append({
            "type": "no_sales_today",
            "severity": "warning",
            "message": "No sales recorded today yet",
            "timestamp": datetime.now().isoformat()
        })

    return indicators
