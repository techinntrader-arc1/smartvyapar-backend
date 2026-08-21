"""
SMART POS Dashboard — Main Router
Aggregates all dashboard sub-routers under /dashboard prefix.
"""

from datetime import datetime, timezone
from fastapi import APIRouter

from dashboard.routes import (
    overview,
    sales,
    revenue,
    orders,
    products,
    categories,
    inventory,
    customers,
    cashiers,
    payments,
    refunds,
    operations,
    alerts,
    reports,
    reconciliation,
)

router = APIRouter()

# ── Register Routes ──────────────────────────────────────────────

router.include_router(overview.router, prefix="/overview", tags=["Dashboard - Overview"])
router.include_router(sales.router, prefix="/sales", tags=["Dashboard - Sales"])
router.include_router(revenue.router, prefix="/revenue", tags=["Dashboard - Revenue"])
router.include_router(orders.router, prefix="/orders", tags=["Dashboard - Orders"])
router.include_router(products.router, prefix="/products", tags=["Dashboard - Products"])
router.include_router(categories.router, prefix="/categories", tags=["Dashboard - Categories"])
router.include_router(inventory.router, prefix="/inventory", tags=["Dashboard - Inventory"])
router.include_router(customers.router, prefix="/customers", tags=["Dashboard - Customers"])
router.include_router(cashiers.router, prefix="/cashiers", tags=["Dashboard - Cashiers"])
router.include_router(payments.router, prefix="/payments", tags=["Dashboard - Payments"])
router.include_router(refunds.router, prefix="/refunds", tags=["Dashboard - Refunds"])
router.include_router(operations.router, prefix="/operations", tags=["Dashboard - Operations"])
router.include_router(alerts.router, prefix="/alerts", tags=["Dashboard - Alerts"])
router.include_router(reports.router, prefix="/reports", tags=["Dashboard - Reports"])
router.include_router(reconciliation.router, prefix="/reconciliation", tags=["Dashboard - EOD"])


# ── Health Check ──────────────────────────────────────────────────

@router.get("/health")
def dashboard_health():
    """Dashboard module health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.3.5",
        "registered_modules": [
            "overview",
            "sales",
            "revenue",
            "orders",
            "products",
            "categories",
            "inventory",
            "customers",
            "cashiers",
            "payments",
            "refunds",
            "operations",
            "alerts",
            "reports",
            "reconciliation",
        ]
    }


__all__ = ["router"]
