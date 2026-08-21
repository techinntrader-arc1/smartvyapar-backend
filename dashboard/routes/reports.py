"""Reports and export data route handlers."""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date
from database import get_db
from auth import get_current_user
import models
from dashboard.filters.date_filters import resolve_date_range
from dashboard.services import (
    sales_service, cashier_service, payment_service, inventory_service
)
from dashboard.utils.export_helpers import (
    sales_report_csv, product_performance_csv,
    inventory_alert_csv, cashier_report_csv, payment_summary_csv
)
from dashboard.utils.aggregation_helpers import top_n_products

router = APIRouter()


@router.get("/sales-csv")
def export_sales_csv(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    data = sales_service.get_recent_sales(db, start, end, limit=10000)
    csv_str = sales_report_csv(data["items"])
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sales_{start}_{end}.csv"}
    )


@router.get("/products-csv")
def export_products_csv(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    data = top_n_products(db, start, end, n=200)
    csv_str = product_performance_csv(data)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=product_performance.csv"}
    )


@router.get("/inventory-csv")
def export_inventory_csv(
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    suggestions = inventory_service.get_restock_suggestions(db)
    csv_str = inventory_alert_csv(suggestions)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory_alerts.csv"}
    )


@router.get("/cashier-csv")
def export_cashier_csv(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    data = cashier_service.get_cashier_summaries(db, start, end)
    csv_str = cashier_report_csv(data)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cashier_report.csv"}
    )


@router.get("/payment-csv")
def export_payment_csv(
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    data = payment_service.get_payment_method_summary(db, start, end)
    csv_str = payment_summary_csv(data)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payment_summary.csv"}
    )


@router.get("/summary-data")
def summary_report_data(
    report_type: str = Query("sales"),
    preset: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Returns structured JSON for the Custom Reports UI generator."""
    start, end = resolve_date_range(start_date, end_date, preset or "this_month")
    
    headers = []
    keys = []
    rows = []
    
    if report_type == "sales":
        data = sales_service.get_recent_sales(db, start, end, limit=5000)
        headers = ["Invoice", "Date", "Customer", "Total", "Status", "Payment Method"]
        keys = ["invoice_no", "date", "customer", "total", "status", "payment_method"]
        rows = data["items"]
        
    elif report_type == "products":
        from dashboard.utils.aggregation_helpers import top_n_products
        data = top_n_products(db, start, end, n=500)
        headers = ["Product", "Category", "Qty Sold", "Revenue", "Orders"]
        keys = ["product_name", "category", "total_qty", "total_revenue", "order_count"]
        rows = data
        
    elif report_type == "inventory_valuation":
        prods = db.query(models.Product).filter(models.Product.is_active == True).all()
        headers = ["Product", "Category", "Current Stock", "Purchase Price", "Inventory Value"]
        keys = ["name", "category", "stock", "buy_price", "valuation"]
        for p in prods:
            rows.append({
                "name": p.name,
                "category": p.category.name if p.category else "Uncategorized",
                "stock": p.stock,
                "buy_price": p.buy_price,
                "valuation": round(p.stock * p.buy_price, 2)
            })
            
    elif report_type == "cashier_shifts":
        data = cashier_service.get_cashier_summaries(db, start, end)
             
        headers = ["Cashier", "Total Orders", "Gross Revenue", "Avg Basket"]
        keys = ["username", "total_orders", "gross_revenue", "avg_basket"]
        rows = data
        
    elif report_type == "taxes":
        from dashboard.services.revenue_service import get_tax_analysis
        data = get_tax_analysis(db, start, end)
        headers = ["Category", "Taxable Revenue", "Tax Collected"]
        keys = ["category", "revenue", "tax_amount"]
        rows = data["tax_by_product_category"]
        
    else:
        # Generic Overview (Legacy compatibility)
        from dashboard.services.revenue_service import get_period_comparison
        from dashboard.utils.aggregation_helpers import category_revenue_breakdown, top_n_products
        return {
            "period": f"{start} to {end}",
            "comparison": get_period_comparison(db, start, end),
            "top_products": top_n_products(db, start, end, n=10),
            "category_breakdown": category_revenue_breakdown(db, start, end),
        }

    return {
        "period": f"{start} to {end}",
        "headers": headers,
        "keys": keys,
        "rows": rows
    }
