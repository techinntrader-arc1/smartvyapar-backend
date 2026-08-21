"""
Inventory router - Stock tracking, adjustment, and movements.
Enterprise-grade inventory management with full audit trail.
"""

import logging
import io
import csv
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from pydantic import BaseModel, Field
from database import get_db, get_db_path, get_backup_dir
from auth import require_admin
from services import backup_service
from services.sale_return_service import POSTED_SALE_STATUSES
import models

# Safe import for rate limiting (slowapi)
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address)
except ImportError:
    class DummyLimiter:
        def limit(self, limit_value: str):
            def decorator(func):
                return func
            return decorator
    limiter = DummyLimiter()

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────

class StockAdjust(BaseModel):
    """Stock adjustment request model"""
    product_id: int = Field(..., description="Product ID to adjust")
    qty_change: float = Field(..., description="Quantity change (+ for add, - for remove)")
    note: Optional[str] = Field("", max_length=500, description="Adjustment reason")


class StockItem(BaseModel):
    """Stock item response"""
    id: int
    name: str
    barcode: Optional[str]
    category: str
    unit: str
    stock: float
    min_stock: float
    low: bool
    value: Optional[float] = None


class LowStockItem(BaseModel):
    """Low stock item response"""
    id: int
    name: str
    stock: float
    min_stock: float
    days_until_out: Optional[int] = None


class StockMovementItem(BaseModel):
    """Stock movement response"""
    id: int
    product: str
    type: str
    qty_change: float
    qty_after: float
    note: Optional[str]
    date: datetime
    created_by: Optional[int] = None


class StockAdjustResponse(BaseModel):
    """Stock adjustment response"""
    product: str
    new_stock: float
    movement_id: int


class StockValuationCategory(BaseModel):
    """Category breakdown in valuation"""
    category: str
    value: float
    quantity: float
    percentage: float


class StockValuationResponse(BaseModel):
    """Stock valuation response"""
    total_value: float
    total_quantity: float
    categories: List[StockValuationCategory]


class StockTurnoverResponse(BaseModel):
    """Stock turnover response"""
    category: str
    turnover_ratio: float
    avg_days_in_stock: int


class PaginatedResponse(BaseModel):
    """Paginated response wrapper"""
    items: List[Any]
    total: int
    page: int
    page_size: int
    total_pages: int


class ProductStockDetailResponse(BaseModel):
    """Detailed single product stock response"""
    id: int
    name: str
    barcode: Optional[str]
    category: str
    unit: str
    stock: float
    min_stock: float
    max_stock: Optional[float] = None
    purchase_price: float
    selling_price: float
    stock_value: float
    low: bool
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StockAlertItem(BaseModel):
    """Stock alert item model"""
    id: int
    name: str
    stock: float
    min_stock: Optional[float] = None


class StockAlertSummary(BaseModel):
    """Summary of stock alerts"""
    total_alerts: int
    out_of_stock_count: int
    low_stock_count: int


class StockAlertsResponse(BaseModel):
    """Combined stock alerts response"""
    out_of_stock: List[StockAlertItem]
    low_stock: List[StockAlertItem]
    summary: StockAlertSummary


class HealthCheckResponse(BaseModel):
    """Health check response"""
    status: str
    timestamp: str
    endpoints: List[str]


# ── Endpoints ──────────────────────────────────────────────────────────

@router.get("/stock", response_model=PaginatedResponse)
@limiter.limit("30/minute")
def stock_report(
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    offset: int = Query(0, ge=0, description="Items to skip"),
    search: Optional[str] = Query(None, description="Search by product name or barcode"),
    category_id: Optional[int] = Query(None, description="Filter by category"),
    min_stock: Optional[float] = Query(None, ge=0, description="Minimum stock level"),
    max_stock: Optional[float] = Query(None, ge=0, description="Maximum stock level"),
    low_only: bool = Query(False, description="Show only low stock items"),
    sort_by: Optional[str] = Query("name", pattern="^(name|stock|min_stock|category)$"),
    sort_order: Optional[str] = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    All products with current stock levels.
    Supports pagination, search, filtering, and sorting.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock report (search={search}, cat={category_id}, limit={limit}, offset={offset})")
        
        query = db.query(models.Product).filter(models.Product.is_active == True)
        
        # Search by product name or barcode
        if search:
            query = query.filter(
                or_(
                    models.Product.name.ilike(f"%{search}%"),
                    models.Product.barcode.ilike(f"%{search}%")
                )
            )
        
        # Category Filter
        if category_id:
            query = query.filter(models.Product.category_id == category_id)
        
        if min_stock is not None:
            query = query.filter(models.Product.stock >= min_stock)
        
        if max_stock is not None:
            query = query.filter(models.Product.stock <= max_stock)
        
        if low_only:
            query = query.filter(models.Product.stock <= models.Product.min_stock)
        
        # Sorting
        sort_column = {
            "name": models.Product.name,
            "stock": models.Product.stock,
            "min_stock": models.Product.min_stock,
            "category": models.Product.category_id
        }.get(sort_by, models.Product.name)
        
        if sort_order == "desc":
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column.asc())
        
        total = query.count()
        products = query.offset(offset).limit(limit).all()
        
        items = [
            {
                "id": p.id,
                "name": p.name,
                "barcode": p.barcode,
                "category": p.category.name if p.category else "",
                "unit": p.unit.name if p.unit else "pcs",
                "stock": p.stock,
                "min_stock": p.min_stock or 0.0,
                "low": p.stock <= (p.min_stock or 0.0),
                "value": p.stock * (getattr(p, 'purchase_price', getattr(p, 'buy_price', 0.0)) or 0.0)
            }
            for p in products
        ]
        
        logger.info(f"Retrieved {len(items)} stock items out of {total} total")
        
        page = (offset // limit) + 1 if limit > 0 else 1
        total_pages = ((total + limit - 1) // limit) if limit > 0 else 1
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": limit,
            "total_pages": total_pages
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching stock report: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stock report: {str(e)}"
        )


@router.get("/low-stock", response_model=List[LowStockItem])
@limiter.limit("30/minute")
def get_low_stock(
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="Max low stock items"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Get all low stock items (stock <= min_stock).
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching low stock items (limit={limit})")
        
        products = db.query(models.Product).filter(
            models.Product.is_active == True,
            models.Product.stock <= models.Product.min_stock
        ).limit(limit).all()
        
        logger.info(f"Found {len(products)} low stock items")
        
        return [
            {
                "id": p.id,
                "name": p.name,
                "stock": p.stock,
                "min_stock": p.min_stock or 0.0,
                "days_until_out": None
            }
            for p in products
        ]
        
    except Exception as e:
        logger.error(f"Error fetching low stock items: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch low stock items: {str(e)}"
        )


@router.get("/stock/{product_id}", response_model=ProductStockDetailResponse)
@limiter.limit("60/minute")
def get_product_stock(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Get detailed stock information for a single product.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock details for product ID {product_id}")
        
        product = db.query(models.Product).filter(
            models.Product.id == product_id,
            models.Product.is_active == True
        ).first()
        
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with ID {product_id} not found"
            )
        
        buy_price = getattr(product, 'purchase_price', getattr(product, 'buy_price', 0.0)) or 0.0
        sell_price = getattr(product, 'selling_price', getattr(product, 'sell_price', 0.0)) or 0.0
        max_stock = getattr(product, 'max_stock', None)
        created_at = getattr(product, 'created_at', None)
        updated_at = getattr(product, 'updated_at', None)
        
        return {
            "id": product.id,
            "name": product.name,
            "barcode": product.barcode,
            "category": product.category.name if product.category else "",
            "unit": product.unit.name if product.unit else "pcs",
            "stock": product.stock,
            "min_stock": product.min_stock or 0.0,
            "max_stock": max_stock,
            "purchase_price": buy_price,
            "selling_price": sell_price,
            "stock_value": product.stock * buy_price,
            "low": product.stock <= (product.min_stock or 0.0),
            "is_active": product.is_active,
            "created_at": created_at,
            "updated_at": updated_at
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching product stock for ID {product_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch product stock: {str(e)}"
        )


@router.post("/adjust", response_model=StockAdjustResponse)
@limiter.limit("10/minute")
def adjust_stock(
    request: Request,
    data: StockAdjust,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Adjust stock for a product with atomic update and audit trail.
    """
    try:
        logger.info(f"Admin '{current_user.username}' adjusting stock for product {data.product_id}: change={data.qty_change}, note='{data.note}'")
        
        product = db.query(models.Product).filter_by(id=data.product_id).first()
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with ID {data.product_id} not found"
            )
        
        old_stock = product.stock
        new_stock = old_stock + data.qty_change
        
        # Don't allow negative stock
        if new_stock < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reduce stock below 0. Current stock: {old_stock}, Change: {data.qty_change}"
            )
        
        # Atomic update
        product.stock = new_stock
        db.flush()
        db.refresh(product)
        
        # Create stock movement audit record
        sm = models.StockMovement(
            product_id=product.id,
            movement_type="adjustment",
            qty_change=data.qty_change,
            qty_after=product.stock,
            note=data.note or f"Manual adjustment by {current_user.username}"
        )
        db.add(sm)
        db.flush()
        db.refresh(sm)
        db.commit()
        
        logger.info(f"Stock adjusted for '{product.name}' (ID {product.id}): {old_stock} -> {new_stock} by {current_user.username}")
        
        # Trigger background auto backup if available
        try:
            backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
        except Exception as backup_error:
            logger.warning(f"Background backup failed after stock adjustment: {backup_error}")
        
        return {
            "product": product.name,
            "new_stock": product.stock,
            "movement_id": sm.id
        }
        
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error adjusting stock for product {data.product_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to adjust stock: {str(e)}"
        )


@router.get("/movements", response_model=PaginatedResponse)
@limiter.limit("30/minute")
def stock_movements(
    request: Request,
    product_id: Optional[int] = Query(None, description="Filter by product ID"),
    movement_type: Optional[str] = Query(None, pattern="^(adjustment|sale|purchase|return|waste)$"),
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    offset: int = Query(0, ge=0, description="Items to skip"),
    start_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Get stock movements with pagination and filtering.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock movements (product={product_id}, type={movement_type})")
        
        query = db.query(models.StockMovement).order_by(
            models.StockMovement.created_at.desc()
        )
        
        if product_id:
            query = query.filter(models.StockMovement.product_id == product_id)
        
        if movement_type:
            query = query.filter(models.StockMovement.movement_type == movement_type)
        
        if start_date:
            query = query.filter(models.StockMovement.created_at >= start_date)
        
        if end_date:
            query = query.filter(models.StockMovement.created_at <= end_date + " 23:59:59")
        
        total = query.count()
        movements = query.offset(offset).limit(limit).all()
        
        items = [
            {
                "id": m.id,
                "product": m.product.name if m.product else "Unknown",
                "type": m.movement_type,
                "qty_change": m.qty_change,
                "qty_after": m.qty_after,
                "note": m.note,
                "date": m.created_at,
                "created_by": getattr(m, 'created_by', None)
            }
            for m in movements
        ]
        
        logger.info(f"Found {total} stock movements, returning {len(items)}")
        
        page = (offset // limit) + 1 if limit > 0 else 1
        total_pages = ((total + limit - 1) // limit) if limit > 0 else 1
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": limit,
            "total_pages": total_pages
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching stock movements: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stock movements: {str(e)}"
        )


@router.get("/movements/product/{product_id}")
@limiter.limit("60/minute")
def get_product_movements(
    request: Request,
    product_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Get stock movement history for a specific product.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching movements for product ID {product_id}")
        
        product = db.query(models.Product).filter_by(id=product_id).first()
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with ID {product_id} not found"
            )
        
        query = db.query(models.StockMovement).filter(
            models.StockMovement.product_id == product_id
        ).order_by(models.StockMovement.created_at.desc())
        
        total = query.count()
        movements = query.offset(offset).limit(limit).all()
        
        items = [
            {
                "id": m.id,
                "type": m.movement_type,
                "qty_change": m.qty_change,
                "qty_after": m.qty_after,
                "note": m.note,
                "date": m.created_at
            }
            for m in movements
        ]
        
        page = (offset // limit) + 1 if limit > 0 else 1
        total_pages = ((total + limit - 1) // limit) if limit > 0 else 1
        
        return {
            "product": {
                "id": product.id,
                "name": product.name,
                "current_stock": product.stock
            },
            "items": items,
            "total": total,
            "page": page,
            "page_size": limit,
            "total_pages": total_pages
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching movements for product {product_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch product movements: {str(e)}"
        )


@router.get("/valuation", response_model=StockValuationResponse)
@limiter.limit("30/minute")
def stock_valuation(
    request: Request,
    category_id: Optional[int] = Query(None, description="Filter valuation by category ID"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Get total inventory valuation grouped by category.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock valuation")
        
        query = db.query(models.Product).filter(models.Product.is_active == True)
        
        if category_id:
            query = query.filter(models.Product.category_id == category_id)
        
        products = query.all()
        
        total_value = sum(p.stock * (getattr(p, 'purchase_price', getattr(p, 'buy_price', 0.0)) or 0.0) for p in products)
        total_quantity = sum(p.stock for p in products)
        
        category_values: Dict[str, Dict[str, float]] = {}
        for p in products:
            cat_name = p.category.name if p.category else "Uncategorized"
            if cat_name not in category_values:
                category_values[cat_name] = {"value": 0.0, "quantity": 0.0}
            buy_price = getattr(p, 'purchase_price', getattr(p, 'buy_price', 0.0)) or 0.0
            category_values[cat_name]["value"] += p.stock * buy_price
            category_values[cat_name]["quantity"] += p.stock
        
        categories = [
            {
                "category": name,
                "value": round(data["value"], 2),
                "quantity": round(data["quantity"], 2),
                "percentage": round((data["value"] / total_value * 100), 2) if total_value > 0 else 0.0
            }
            for name, data in category_values.items()
        ]
        
        categories.sort(key=lambda x: x["value"], reverse=True)
        
        return {
            "total_value": round(total_value, 2),
            "total_quantity": round(total_quantity, 2),
            "categories": categories
        }
        
    except Exception as e:
        logger.error(f"Error fetching stock valuation: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stock valuation: {str(e)}"
        )


@router.get("/turnover", response_model=List[StockTurnoverResponse])
@limiter.limit("30/minute")
def stock_turnover(
    request: Request,
    start_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Get stock turnover ratio and average days in stock grouped by category.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock turnover ratio")
        
        categories = db.query(models.Category).all()
        turnover_data = []
        
        for cat in categories:
            prods = db.query(models.Product).filter(
                models.Product.category_id == cat.id,
                models.Product.is_active == True
            ).all()
            
            if not prods:
                continue
                
            total_stock_qty = sum(p.stock for p in prods)
            # Calculate total quantity sold for category items
            sold_query = db.query(func.sum(models.SaleItem.qty)).join(models.Sale).filter(
                models.SaleItem.product_id.in_([p.id for p in prods]),
                models.Sale.status.in_(POSTED_SALE_STATUSES),
            )
            returned_query = db.query(func.sum(models.SaleReturnItem.qty)).join(models.SaleReturn).filter(
                models.SaleReturnItem.product_id.in_([p.id for p in prods]),
            )
            if start_date:
                sold_query = sold_query.filter(func.date(models.Sale.created_at) >= start_date)
                returned_query = returned_query.filter(func.date(models.SaleReturn.posted_at) >= start_date)
            if end_date:
                sold_query = sold_query.filter(func.date(models.Sale.created_at) <= end_date)
                returned_query = returned_query.filter(func.date(models.SaleReturn.posted_at) <= end_date)
            sale_items = float(sold_query.scalar() or 0) - float(returned_query.scalar() or 0)
            
            turnover_ratio = round(sale_items / total_stock_qty, 2) if total_stock_qty > 0 else 0.0
            avg_days = int(365 / turnover_ratio) if turnover_ratio > 0 else 365
            
            turnover_data.append({
                "category": cat.name,
                "turnover_ratio": turnover_ratio,
                "avg_days_in_stock": avg_days
            })
            
        if not turnover_data:
            turnover_data.append({
                "category": "General",
                "turnover_ratio": 1.0,
                "avg_days_in_stock": 365
            })
            
        return turnover_data
        
    except Exception as e:
        logger.error(f"Error fetching stock turnover: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stock turnover: {str(e)}"
        )


@router.get("/alerts", response_model=StockAlertsResponse)
@limiter.limit("30/minute")
def stock_alerts(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Combined stock alerts: out of stock items + low stock items.
    """
    try:
        logger.info(f"Admin '{current_user.username}' fetching stock alerts")
        
        out_of_stock = db.query(models.Product).filter(
            models.Product.is_active == True,
            models.Product.stock <= 0
        ).all()
        
        low_stock = db.query(models.Product).filter(
            models.Product.is_active == True,
            models.Product.stock > 0,
            models.Product.stock <= models.Product.min_stock
        ).all()
        
        out_items = [
            {"id": p.id, "name": p.name, "stock": p.stock, "min_stock": p.min_stock}
            for p in out_of_stock
        ]
        low_items = [
            {"id": p.id, "name": p.name, "stock": p.stock, "min_stock": p.min_stock}
            for p in low_stock
        ]
        
        return {
            "out_of_stock": out_items,
            "low_stock": low_items,
            "summary": {
                "total_alerts": len(out_items) + len(low_items),
                "out_of_stock_count": len(out_items),
                "low_stock_count": len(low_items)
            }
        }
        
    except Exception as e:
        logger.error(f"Error fetching stock alerts: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch stock alerts: {str(e)}"
        )


@router.get("/export")
@limiter.limit("10/minute")
def export_stock_report(
    request: Request,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """
    Export stock report in CSV or Excel format.
    """
    try:
        logger.info(f"Admin '{current_user.username}' exporting stock report as {format}")
        
        products = db.query(models.Product).filter(models.Product.is_active == True).all()
        
        if not products:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No data found for export"
            )
        
        data = [
            {
                "ID": p.id,
                "Name": p.name,
                "Barcode": p.barcode or "",
                "Category": p.category.name if p.category else "",
                "Unit": p.unit.name if p.unit else "pcs",
                "Stock": p.stock,
                "Min Stock": p.min_stock or 0.0,
                "Purchase Price": getattr(p, 'purchase_price', getattr(p, 'buy_price', 0.0)) or 0.0,
                "Selling Price": getattr(p, 'selling_price', getattr(p, 'sell_price', 0.0)) or 0.0,
                "Stock Value": p.stock * (getattr(p, 'purchase_price', getattr(p, 'buy_price', 0.0)) or 0.0)
            }
            for p in products
        ]
        
        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        
        if format == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
            output.seek(0)
            return StreamingResponse(
                io.BytesIO(output.getvalue().encode('utf-8')),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=stock_report_{timestamp_str}.csv"
                }
            )
        else:
            try:
                import pandas as pd
                df = pd.DataFrame(data)
                excel_output = io.BytesIO()
                with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, sheet_name='Stock Report', index=False)
                excel_output.seek(0)
                return StreamingResponse(
                    excel_output,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": f"attachment; filename=stock_report_{timestamp_str}.xlsx"
                    }
                )
            except ImportError:
                # Fallback to CSV format if pandas/xlsxwriter is not installed
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
                writer.writeheader()
                writer.writerows(data)
                output.seek(0)
                return StreamingResponse(
                    io.BytesIO(output.getvalue().encode('utf-8')),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f"attachment; filename=stock_report_{timestamp_str}.csv"
                    }
                )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting stock report: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export stock report: {str(e)}"
        )


@router.get("/health", response_model=HealthCheckResponse)
def inventory_health():
    """Health check for inventory module."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoints": [
            "/stock",
            "/low-stock",
            "/stock/{product_id}",
            "/adjust",
            "/movements",
            "/movements/product/{product_id}",
            "/valuation",
            "/turnover",
            "/alerts",
            "/export"
        ]
    }
