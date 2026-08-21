"""
Export data builders for CSV and report generation.
Provides structured data ready for CSV serialization or printable views.
"""

from typing import List, Dict, Any
import csv
import io
from datetime import datetime


def _safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution when a CSV is opened."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def build_csv_string(headers: List[str], rows: List[List[Any]]) -> str:
    """Build an Excel-friendly, formula-safe CSV string."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([_safe_csv_cell(value) for value in headers])
    for row in rows:
        writer.writerow([_safe_csv_cell(value) for value in row])
    return output.getvalue()


def sales_report_csv(sales_data: List[Dict]) -> str:
    headers = [
        "Invoice No", "Date", "Customer", "Cashier",
        "Subtotal", "Discount", "Tax", "Total", "Paid", "Balance", "Method", "Status"
    ]
    rows = []
    for s in sales_data:
        rows.append([
            s.get("invoice_no", ""),
            s.get("date", ""),
            s.get("customer", "Walk-in"),
            s.get("cashier", ""),
            s.get("subtotal", 0),
            s.get("discount", 0),
            s.get("tax_amount", 0),
            s.get("total", 0),
            s.get("paid_amount", 0),
            round(float(s.get("total", 0)) - float(s.get("paid_amount", 0)), 2),
            s.get("payment_method", ""),
            s.get("status", ""),
        ])
    return build_csv_string(headers, rows)


def product_performance_csv(products: List[Dict]) -> str:
    headers = [
        "Product", "Category", "Unit",
        "Qty Sold", "Revenue", "Orders", "Rank"
    ]
    rows = [
        [
            p.get("product_name", ""),
            p.get("category", ""),
            p.get("unit", ""),
            p.get("total_qty", 0),
            p.get("total_revenue", 0),
            p.get("order_count", 0),
            p.get("rank", ""),
        ]
        for p in products
    ]
    return build_csv_string(headers, rows)


def inventory_alert_csv(items: List[Dict]) -> str:
    headers = [
        "Product", "Category", "Unit",
        "Current Stock", "Min Stock", "Shortage", "Priority"
    ]
    rows = [
        [
            item.get("product_name", ""),
            item.get("category", ""),
            item.get("unit", ""),
            item.get("current_stock", 0),
            item.get("min_stock", 0),
            item.get("shortage", 0),
            item.get("priority", ""),
        ]
        for item in items
    ]
    return build_csv_string(headers, rows)


def cashier_report_csv(cashiers: List[Dict]) -> str:
    headers = [
        "Cashier", "Orders", "Gross Revenue", "Net Revenue",
        "Avg Basket", "Total Discount", "Refunds"
    ]
    rows = [
        [
            c.get("username", ""),
            c.get("total_orders", 0),
            c.get("gross_revenue", 0),
            c.get("net_revenue", 0),
            c.get("avg_basket", 0),
            c.get("total_discount_given", 0),
            c.get("refund_amount", 0),
        ]
        for c in cashiers
    ]
    return build_csv_string(headers, rows)


def payment_summary_csv(data: List[Dict]) -> str:
    headers = ["Method", "Transactions", "Total Amount", "Avg Transaction", "% of Total"]
    rows = [
        [
            d.get("method", ""),
            d.get("transaction_count", 0),
            d.get("total_amount", 0),
            round(float(d.get("avg_transaction", 0)), 2),
            d.get("pct_of_total", 0),
        ]
        for d in data
    ]
    return build_csv_string(headers, rows)


def eod_report_text(eod_data: Dict) -> str:
    """Build a text-based EOD report for print/export."""
    now = datetime.now().strftime("%d %b %Y %H:%M")
    lines = [
        "=" * 48,
        "        SMART POS — END OF DAY REPORT",
        f"        Generated: {now}",
        "=" * 48,
        "",
        f"  Date:          {eod_data.get('date', '')}",
        f"  Total Orders:  {eod_data.get('total_orders', 0)}",
        "",
        "  SALES SUMMARY",
        f"  Gross Sales:   Rs. {eod_data.get('gross_sales', 0):,.0f}",
        f"  Discounts:     Rs. {eod_data.get('total_discount', 0):,.0f}",
        f"  Tax:           Rs. {eod_data.get('total_tax', 0):,.0f}",
        f"  Net Sales:     Rs. {eod_data.get('net_sales', 0):,.0f}",
        f"  Refunds:       Rs. {eod_data.get('total_refunds', 0):,.0f}",
        "",
        "  PAYMENT BREAKDOWN",
        f"  Cash:          Rs. {eod_data.get('cash_total', 0):,.0f}",
        f"  Card:          Rs. {eod_data.get('card_total', 0):,.0f}",
        f"  Credit Given:  Rs. {eod_data.get('credit_total', 0):,.0f}",
        f"  Credit Payments: Rs. {eod_data.get('credit_payments_total', 0):,.0f}",
        f"  Mixed:         Rs. {eod_data.get('mixed_total', 0):,.0f}",
        "",
        f"  Expected Cash: Rs. {eod_data.get('expected_cash', 0):,.0f}",
        f"  Outstanding:   Rs. {eod_data.get('outstanding', 0):,.0f}",
        "=" * 48,
        "        Thank you - SMART POS",
        "=" * 48,
    ]
    return "\n".join(lines)
