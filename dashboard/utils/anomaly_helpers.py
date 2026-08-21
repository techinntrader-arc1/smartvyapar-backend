"""
Statistical anomaly detection helpers for dashboard alerts.
Detects unusual spikes/drops in sales, refunds, and discount metrics.
"""

from typing import List, Dict, Optional
from statistics import mean, stdev


def detect_spikes(
    series: List[Dict],
    value_key: str = "value",
    threshold_stddev: float = 2.0,
    min_points: int = 5
) -> List[Dict]:
    """
    Detect anomalous data points using z-score method.
    Returns points where |z-score| > threshold_stddev.

    Args:
        series: list of {label, value, ...} dicts
        value_key: key to check for anomalies
        threshold_stddev: how many standard deviations = anomaly
        min_points: minimum data points needed for detection

    Returns:
        List of anomaly dicts with severity metadata
    """
    if len(series) < min_points:
        return []

    values = [float(p.get(value_key, 0)) for p in series]
    avg = mean(values)
    if avg == 0:
        return []

    try:
        sd = stdev(values)
    except Exception:
        return []

    if sd == 0:
        return []

    anomalies = []
    for point in series:
        val = float(point.get(value_key, 0))
        z = (val - avg) / sd
        if abs(z) >= threshold_stddev:
            deviation_pct = round((val - avg) / avg * 100, 1)
            severity = "critical" if abs(z) >= threshold_stddev * 1.5 else "warning"
            anomalies.append({
                "label": point.get("label", ""),
                "metric": value_key,
                "value": round(val, 2),
                "avg_value": round(avg, 2),
                "z_score": round(z, 2),
                "deviation_pct": deviation_pct,
                "severity": severity,
                "direction": "spike" if val > avg else "drop",
            })

    return anomalies


def detect_sales_drop(daily_series: List[Dict], drop_threshold_pct: float = 30.0) -> Optional[Dict]:
    """
    Returns an alert if the latest day's sales dropped more than
    drop_threshold_pct below the rolling 7-day average.
    """
    if len(daily_series) < 2:
        return None

    recent = daily_series[-7:] if len(daily_series) >= 7 else daily_series[:-1]
    rolling_avg = mean(float(p.get("value", 0)) for p in recent)

    latest = float(daily_series[-1].get("value", 0))
    if rolling_avg == 0:
        return None

    drop_pct = (rolling_avg - latest) / rolling_avg * 100
    if drop_pct >= drop_threshold_pct:
        return {
            "type": "sales_drop",
            "severity": "critical" if drop_pct >= 50 else "warning",
            "message": f"Sales dropped {drop_pct:.0f}% below 7-day average",
            "current": round(latest, 2),
            "average": round(rolling_avg, 2),
            "drop_pct": round(drop_pct, 1),
        }
    return None


def detect_refund_spike(
    refund_series: List[Dict],
    sales_series: List[Dict],
    spike_threshold_pct: float = 10.0
) -> Optional[Dict]:
    """
    Returns an alert if refund rate (refund/sales) exceeds threshold on latest period.
    """
    if not refund_series or not sales_series:
        return None

    total_refunds = sum(float(p.get("refund_amount", 0)) for p in refund_series)
    total_sales = sum(float(p.get("value", 0)) for p in sales_series)

    if total_sales == 0:
        return None

    refund_rate = total_refunds / total_sales * 100
    if refund_rate >= spike_threshold_pct:
        return {
            "type": "refund_spike",
            "severity": "critical" if refund_rate >= 20 else "warning",
            "message": f"Refund rate is {refund_rate:.1f}% of gross sales",
            "refund_amount": round(total_refunds, 2),
            "refund_rate_pct": round(refund_rate, 1),
        }
    return None


def detect_discount_spike(
    discount_series: List[Dict],
    threshold_stddev: float = 2.0
) -> List[Dict]:
    """Detect days with unusually high discounts."""
    return detect_spikes(discount_series, value_key="discount", threshold_stddev=threshold_stddev)


def build_alert_feed(
    low_stock_count: int,
    out_of_stock_count: int,
    sales_drop_alert: Optional[Dict],
    refund_alert: Optional[Dict],
    discount_anomalies: List[Dict],
    out_of_stock_items: List[Dict] = None,
    low_stock_items: List[Dict] = None,
) -> List[Dict]:
    """Build unified alert feed from individual alert sources."""
    from datetime import datetime
    now = datetime.now().isoformat()
    alerts = []

    if out_of_stock_count > 0:
        items = out_of_stock_items or []
        item_summary = ""
        if items:
            item_summary = "Out of Stock Item Details:\n" + "\n".join(
                [f"• {p.get('product_name')} (Category: {p.get('category') or 'General'}, Stock: {p.get('current_stock', 0)} {p.get('unit', 'pcs')})" for p in items[:15]]
            )
        alerts.append({
            "type": "out_of_stock",
            "severity": "critical",
            "message": f"{out_of_stock_count} product(s) are out of stock",
            "count": out_of_stock_count,
            "items": items,
            "detail": item_summary or f"{out_of_stock_count} product(s) currently have 0 units in stock.",
            "timestamp": now
        })

    if low_stock_count > 0:
        items = low_stock_items or []
        item_summary = ""
        if items:
            item_summary = "Low Stock Item Details:\n" + "\n".join(
                [f"• {p.get('product_name')} (Category: {p.get('category') or 'General'}, Current Stock: {p.get('current_stock', 0)} {p.get('unit', 'pcs')}, Min: {p.get('min_stock', 0)} {p.get('unit', 'pcs')})" for p in items[:15]]
            )
        alerts.append({
            "type": "low_stock",
            "severity": "warning",
            "message": f"{low_stock_count} product(s) are below minimum stock",
            "count": low_stock_count,
            "items": items,
            "detail": item_summary or f"{low_stock_count} product(s) are below their defined safety threshold.",
            "timestamp": now
        })

    if sales_drop_alert:
        sales_drop_alert["timestamp"] = now
        current_val = sales_drop_alert.get("current", 0)
        avg_val = sales_drop_alert.get("average", 0)
        drop_pct = sales_drop_alert.get("drop_pct", 0)
        sales_drop_alert["detail"] = f"Sales Drop Analysis:\n• Current Period Sales: Rs. {current_val:,.0f}\n• Rolling 7-Day Average: Rs. {avg_val:,.0f}/day\n• Percentage Drop: {drop_pct:.0f}% below normal performance"
        alerts.append(sales_drop_alert)

    if refund_alert:
        refund_alert["timestamp"] = now
        refund_val = refund_alert.get("refund_amount", 0)
        refund_rate = refund_alert.get("refund_rate_pct", 0)
        refund_alert["detail"] = f"Refund Spike Analysis:\n• Total Refunds Issued: Rs. {refund_val:,.0f}\n• Refund Ratio: {refund_rate:.1f}% of gross revenue"
        alerts.append(refund_alert)

    for anomaly in discount_anomalies[:3]:
        alerts.append({
            "type": "discount_anomaly",
            "severity": anomaly.get("severity", "warning"),
            "message": f"Unusual discount on {anomaly.get('label')}: Rs. {anomaly.get('value'):,.0f}",
            "timestamp": now,
            **anomaly,
        })

    return alerts
