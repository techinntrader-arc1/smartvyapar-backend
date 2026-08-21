"""
Inventory insights route handlers.
Provides stock KPIs, low/out-of-stock items, valuation, restock suggestions, movement history, turnover, aging, and CSV exports.
"""

import logging
import csv
import io
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from auth import get_current_user
from dashboard.services import inventory_service
from services.sale_return_service import POSTED_SALE_STATUSES
import models

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.inventory")
logger.setLevel(logging.INFO)

router = APIRouter()


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class StockKPI(BaseModel):
    total_products: int = Field(0, description="Total active product count")
    total_stock_value: float = Field(0.0, description="Total stock valuation at purchase price")
    low_stock_count: int = Field(0, description="Count of low stock items")
    out_of_stock_count: int = Field(0, description="Count of out of stock items")
    healthy_stock_count: int = Field(0, description="Count of healthy stock items")
    avg_stock_value_per_product: float = Field(0.0, description="Average stock value per product")


class LowStockItem(BaseModel):
    id: int
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    stock_qty: float = 0.0
    min_stock_level: float = 0.0
    unit: Optional[str] = "Pcs"
    purchase_price: float = 0.0
    selling_price: float = 0.0


class OutOfStockItem(BaseModel):
    id: int
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_name: Optional[str] = None
    stock_qty: float = 0.0
    min_stock_level: float = 0.0
    unit: Optional[str] = "Pcs"


class StockValueByCategory(BaseModel):
    category_id: Optional[int] = None
    category_name: str
    product_count: int = 0
    total_products: int = 0
    total_qty: float = 0.0
    avg_stock: float = 0.0
    total_value: float = 0.0
    total_stock_value: float = 0.0


class StockMovement(BaseModel):
    id: int
    product_id: Optional[int] = 0
    product_name: str
    movement_type: str = Field("purchase", description="sale, purchase, adjustment, return")
    qty: float = 0.0
    qty_change: float = 0.0
    qty_after: float = 0.0
    reference: Optional[str] = None
    note: Optional[str] = None
    created_at: str


class RestockSuggestion(BaseModel):
    product_id: int
    product_name: str
    current_stock: float = 0.0
    min_stock_level: float = 0.0
    min_stock: float = 0.0
    suggested_restock_qty: float = 0.0
    shortage: float = 0.0
    estimated_cost: float = 0.0


class InventoryOverview(BaseModel):
    kpi: StockKPI
    low_stock: List[LowStockItem]
    out_of_stock: List[OutOfStockItem]
    value_by_category: List[StockValueByCategory]
    movements: List[StockMovement]
    suggestions: List[RestockSuggestion]


class InventoryItemListItem(BaseModel):
    id: int
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_name: Optional[str] = None
    stock_qty: float
    min_stock_level: float
    purchase_price: float
    selling_price: float
    total_value: float
    stock_status: str = Field(..., description="healthy, low_stock, out_of_stock")


class InventoryItemsListResponse(BaseModel):
    items: List[InventoryItemListItem]
    count: int


class InventoryItemDetailsResponse(BaseModel):
    product_id: int
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    category_name: Optional[str] = None
    stock_qty: float
    min_stock_level: float
    purchase_price: float
    selling_price: float
    total_stock_value: float
    stock_status: str
    total_units_sold_30d: float = 0.0
    last_movement_at: Optional[str] = None


class InventoryTurnoverItem(BaseModel):
    category_name: str
    avg_inventory_value: float
    cogs_30d: float
    turnover_ratio: float


class InventoryTurnoverResponse(BaseModel):
    categories: List[InventoryTurnoverItem]


class InventoryValuationResponse(BaseModel):
    total_valuation_cost: float
    total_valuation_retail: float
    potential_gross_profit: float
    total_active_skus: int


class InventoryAlertsResponse(BaseModel):
    low_stock_count: int
    out_of_stock_count: int
    total_alerts: int
    alerts: List[Dict[str, Any]]


class InventoryAgingItem(BaseModel):
    product_id: int
    product_name: str
    stock_qty: float
    stock_value: float
    last_purchase_date: Optional[str]
    days_in_stock: int
    aging_bracket: str = Field(..., description="0-30 days, 31-60 days, 61-90 days, 90+ days")


class InventoryAgingResponse(BaseModel):
    items: List[InventoryAgingItem]


class HealthCheckResponse(BaseModel):
    status: str
    module: str
    timestamp: str


# ── Helper Functions ──────────────────────────────────────────────────────────
def _get_stock_status(qty: float, min_qty: float) -> str:
    if qty <= 0:
        return "out_of_stock"
    elif qty <= min_qty:
        return "low_stock"
    return "healthy"


def _get_prod_qty(p) -> float:
    return float(getattr(p, 'stock', getattr(p, 'stock_qty', 0.0)) or 0.0)


def _get_prod_min(p) -> float:
    return float(getattr(p, 'min_stock', getattr(p, 'min_stock_level', 5.0)) or 5.0)


def _get_prod_cost(p) -> float:
    return float(getattr(p, 'cost_price', getattr(p, 'buy_price', 0.0)) or 0.0)


def _get_prod_price(p) -> float:
    return float(getattr(p, 'price', 0.0) or 0.0)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthCheckResponse, summary="Inventory module health check")
def inventory_health():
    """Health check endpoint for the inventory module."""
    return HealthCheckResponse(
        status="ok",
        module="inventory",
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@router.get("/kpi", response_model=StockKPI, summary="Get stock high-level KPI metrics")
def stock_kpi(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves high-level inventory KPI metrics (total products, valuation, low stock count, etc.)."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested stock KPIs")
        kpi_data = inventory_service.get_stock_kpi(db)
        return StockKPI(**kpi_data)
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get stock KPI: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving stock KPIs: {str(e)}"
        )


@router.get("/low-stock", response_model=List[LowStockItem], summary="Get low stock items")
def low_stock(
    include_zero: bool = Query(False, description="Include products with 0 stock"),
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves products running below their minimum stock threshold."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested low stock items")
        raw = inventory_service.get_low_stock_items(db, include_zero=include_zero)
        
        if category_id:
            raw = [r for r in raw if r.get("category_id") == category_id]

        items = []
        for r in raw:
            d = dict(r)
            d["purchase_price"] = float(d.get("purchase_price", d.get("cost_price", d.get("buy_price", 0.0))))
            d["selling_price"] = float(d.get("selling_price", d.get("price", 0.0)))
            d["stock_qty"] = float(d.get("stock_qty", d.get("stock", 0.0)))
            d["min_stock_level"] = float(d.get("min_stock_level", d.get("min_stock", 5.0)))
            items.append(LowStockItem(**d))
        return items
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get low stock items: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving low stock items: {str(e)}"
        )


@router.get("/out-of-stock", response_model=List[OutOfStockItem], summary="Get out of stock items")
def out_of_stock(
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves products with 0 stock quantity."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested out of stock items")
        raw = inventory_service.get_out_of_stock(db)
        
        if category_id:
            raw = [r for r in raw if r.get("category_id") == category_id]

        items = []
        for r in raw:
            d = dict(r)
            d["stock_qty"] = float(d.get("stock_qty", d.get("stock", 0.0)))
            d["min_stock_level"] = float(d.get("min_stock_level", d.get("min_stock", 5.0)))
            items.append(OutOfStockItem(**d))
        return items
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get out of stock items: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving out of stock items: {str(e)}"
        )


@router.get("/value-by-category", response_model=List[StockValueByCategory], summary="Get inventory valuation grouped by category")
def value_by_category(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves total stock value and quantity aggregated by product category."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested stock value by category")
        raw = inventory_service.get_stock_value_by_category(db)
        items = []
        for r in raw:
            d = dict(r)
            p_cnt = int(d.get("product_count", d.get("total_products", 0)))
            val = float(d.get("total_value", d.get("total_stock_value", 0.0)))
            d["product_count"] = p_cnt
            d["total_products"] = p_cnt
            d["total_value"] = val
            d["total_stock_value"] = val
            items.append(StockValueByCategory(**d))
        return items
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get stock value by category: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving stock value by category: {str(e)}"
        )


@router.get("/movements", response_model=List[StockMovement], summary="Get recent stock movements")
def stock_movements(
    limit: int = Query(50, ge=1, le=500, description="Limit max returned records"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    product_name: Optional[str] = Query(None, description="Search by product name"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves recent inventory movement transactions (sales, purchases, adjustments)."""
    try:
        lim = limit if isinstance(limit, int) else 50
        off = offset if isinstance(offset, int) else 0

        logger.info(f"[Inventory] User '{current_user.username}' requested stock movements (limit={lim}, offset={off})")
        raw = inventory_service.get_recent_stock_movements(db, limit=lim + off)
        
        if product_name and isinstance(product_name, str):
            p_lower = product_name.lower().strip()
            raw = [m for m in raw if p_lower in str(m.get("product_name", "")).lower()]

        paginated = raw[off : off + lim]

        items = []
        for m in paginated:
            d = dict(m)
            # Ensure valid movement_type enum
            m_type = str(d.get("movement_type", "purchase")).lower()
            if m_type not in ["sale", "purchase", "adjustment", "return"]:
                m_type = "purchase"
            d["movement_type"] = m_type
            
            d["product_id"] = int(d.get("product_id", 0))
            d["qty_change"] = float(d.get("qty_change", d.get("qty", 0.0)))
            d["qty"] = float(d.get("qty", d.get("qty_change", 0.0)))
            d["qty_after"] = float(d.get("qty_after", 0.0))
            items.append(StockMovement(**d))

        return items
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get stock movements: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving stock movements: {str(e)}"
        )


@router.get("/restock-suggestions", response_model=List[RestockSuggestion], summary="Get restock suggestions")
def restock_suggestions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates suggested reorder quantities and estimated costs for low/out-of-stock items."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested restock suggestions")
        raw = inventory_service.get_restock_suggestions(db)
        items = []
        for r in raw:
            d = dict(r)
            min_q = float(d.get("min_stock_level", d.get("min_stock", 0.0)))
            s_qty = float(d.get("suggested_restock_qty", d.get("shortage", 0.0)))
            d["min_stock_level"] = min_q
            d["min_stock"] = min_q
            d["suggested_restock_qty"] = s_qty
            d["shortage"] = s_qty
            items.append(RestockSuggestion(**d))
        return items
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get restock suggestions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving restock suggestions: {str(e)}"
        )


@router.get("/overview", summary="Get comprehensive inventory overview payload")
def inventory_overview(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves full aggregated inventory overview payload used by desktop UI."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested inventory overview")
        overview = inventory_service.get_inventory_overview(db)
        return overview
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get inventory overview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving inventory overview: {str(e)}"
        )


@router.get("/items", response_model=InventoryItemsListResponse, summary="Get paginated list of inventory items")
def list_inventory_items(
    query: Optional[str] = Query(None, description="Search by product name, SKU, or barcode"),
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    stock_status: Optional[str] = Query(None, description="Filter: healthy, low_stock, out_of_stock"),
    limit: int = Query(50, ge=1, le=1000, description="Pagination limit"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns paginated inventory list with stock statuses and valuations."""
    try:
        logger.info(f"[Inventory] User '{current_user.username}' requested inventory items list")
        q = db.query(models.Product).filter(models.Product.is_active == 1)

        if query and isinstance(query, str):
            q_str = f"%{query.strip()}%"
            q = q.filter(
                (models.Product.name.ilike(q_str)) |
                (models.Product.barcode.ilike(q_str))
            )
        if category_id and isinstance(category_id, int):
            q = q.filter(models.Product.category_id == category_id)

        lim = limit if isinstance(limit, int) else 50
        off = offset if isinstance(offset, int) else 0

        total_count = q.count()
        prods = q.order_by(models.Product.name.asc()).offset(off).limit(lim).all()

        items = []
        for p in prods:
            qty = _get_prod_qty(p)
            min_q = _get_prod_min(p)
            p_price = _get_prod_cost(p)
            s_price = _get_prod_price(p)
            val = qty * p_price
            st = _get_stock_status(qty, min_q)

            if stock_status and isinstance(stock_status, str) and st != stock_status.lower():
                continue

            c_name = p.category.name if p.category else "Uncategorized"

            items.append(InventoryItemListItem(
                id=p.id,
                name=p.name,
                sku=getattr(p, 'sku', None),
                barcode=p.barcode,
                category_name=c_name,
                stock_qty=qty,
                min_stock_level=min_q,
                purchase_price=p_price,
                selling_price=s_price,
                total_value=round(val, 2),
                stock_status=st
            ))

        return InventoryItemsListResponse(items=items, count=total_count)
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to list inventory items: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing inventory items: {str(e)}"
        )


@router.get("/details/{product_id}", response_model=InventoryItemDetailsResponse, summary="Get inventory details for a single product")
def get_inventory_item_details(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves inventory details and 30-day movement metrics for an individual product."""
    try:
        p = db.query(models.Product).filter(models.Product.id == product_id).first()
        if not p:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Product ID {product_id} not found")

        qty = _get_prod_qty(p)
        min_q = _get_prod_min(p)
        p_price = _get_prod_cost(p)
        s_price = _get_prod_price(p)
        val = qty * p_price
        st = _get_stock_status(qty, min_q)

        # 30 day sales calculation
        thirty_days_ago = date.today() - timedelta(days=30)
        sales_30d = db.query(func.sum(models.SaleItem.qty)).join(models.Sale).filter(
            models.SaleItem.product_id == product_id,
            models.Sale.status.in_(POSTED_SALE_STATUSES),
            func.date(models.Sale.created_at) >= thirty_days_ago.isoformat()
        ).scalar() or 0.0
        returned_30d = db.query(func.sum(models.SaleReturnItem.qty)).join(models.SaleReturn).filter(
            models.SaleReturnItem.product_id == product_id,
            func.date(models.SaleReturn.posted_at) >= thirty_days_ago.isoformat(),
        ).scalar() or 0.0
        sales_30d = float(sales_30d) - float(returned_30d)

        # Last movement timestamp
        last_m = db.query(models.StockMovement.created_at).filter(
            models.StockMovement.product_id == product_id
        ).order_by(models.StockMovement.created_at.desc()).first()

        last_at = last_m[0].isoformat() if last_m and hasattr(last_m[0], 'isoformat') else (str(last_m[0]) if last_m else None)

        c_name = p.category.name if p.category else "Uncategorized"

        return InventoryItemDetailsResponse(
            product_id=p.id,
            name=p.name,
            sku=getattr(p, 'sku', None),
            barcode=p.barcode,
            category_name=c_name,
            stock_qty=qty,
            min_stock_level=min_q,
            purchase_price=p_price,
            selling_price=s_price,
            total_stock_value=round(val, 2),
            stock_status=st,
            total_units_sold_30d=float(sales_30d),
            last_movement_at=last_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to get product inventory details for ID {product_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching product inventory details: {str(e)}"
        )


@router.get("/valuation", response_model=InventoryValuationResponse, summary="Get total inventory valuation breakdown")
def inventory_valuation(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Calculates overall inventory valuation at cost price vs retail price."""
    try:
        prods = db.query(models.Product).filter(models.Product.is_active == 1).all()

        tot_cost = sum(_get_prod_qty(p) * _get_prod_cost(p) for p in prods)
        tot_retail = sum(_get_prod_qty(p) * _get_prod_price(p) for p in prods)
        potential_profit = tot_retail - tot_cost

        return InventoryValuationResponse(
            total_valuation_cost=round(tot_cost, 2),
            total_valuation_retail=round(tot_retail, 2),
            potential_gross_profit=round(potential_profit, 2),
            total_active_skus=len(prods)
        )
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to calculate inventory valuation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating inventory valuation: {str(e)}"
        )


@router.get("/alerts", response_model=InventoryAlertsResponse, summary="Get combined inventory alerts")
def inventory_alerts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Combines low stock and out of stock alerts into a single stream."""
    try:
        low = inventory_service.get_low_stock_items(db, include_zero=False)
        out = inventory_service.get_out_of_stock(db)

        combined = []
        for o in out:
            combined.append({
                "type": "out_of_stock",
                "severity": "critical",
                "product_id": o.get("id"),
                "product_name": o.get("name"),
                "message": f"Product '{o.get('name')}' is OUT OF STOCK!",
                "stock_qty": float(o.get("stock_qty", o.get("stock", 0.0)))
            })

        for l in low:
            qty = float(l.get("stock_qty", l.get("stock", 0.0)))
            combined.append({
                "type": "low_stock",
                "severity": "warning",
                "product_id": l.get("id"),
                "product_name": l.get("name"),
                "message": f"Product '{l.get('name')}' is running low ({qty} remaining).",
                "stock_qty": qty
            })

        return InventoryAlertsResponse(
            low_stock_count=len(low),
            out_of_stock_count=len(out),
            total_alerts=len(combined),
            alerts=combined
        )
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to generate inventory alerts: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating inventory alerts: {str(e)}"
        )


@router.get("/export", summary="Export inventory report as CSV")
def export_inventory_report(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Generates and downloads a CSV export of active inventory stock & valuation."""
    try:
        prods = db.query(models.Product).filter(models.Product.is_active == 1).order_by(models.Product.name.asc()).all()

        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV Header
        writer.writerow(["Product ID", "Product Name", "Barcode", "Category", "Stock Qty", "Min Stock Level", "Purchase Price (PKR)", "Selling Price (PKR)", "Total Stock Value (PKR)", "Status"])
        
        for p in prods:
            qty = _get_prod_qty(p)
            min_q = _get_prod_min(p)
            p_price = _get_prod_cost(p)
            s_price = _get_prod_price(p)
            val = qty * p_price
            st = _get_stock_status(qty, min_q)
            c_name = p.category.name if p.category else "Uncategorized"

            writer.writerow([
                p.id,
                p.name,
                p.barcode or "",
                c_name,
                f"{qty:.2f}",
                f"{min_q:.2f}",
                f"{p_price:.2f}",
                f"{s_price:.2f}",
                f"{val:.2f}",
                st
            ])

        output.seek(0)
        today_str = date.today().isoformat()
        filename = f"inventory_valuation_{today_str}.csv"
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"[Inventory Error] Failed to export CSV: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error exporting inventory report CSV: {str(e)}"
        )
