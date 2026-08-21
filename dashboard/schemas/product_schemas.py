"""
Pydantic schemas for Product Performance Analytics.
"""

from pydantic import BaseModel
from typing import List, Optional


class TopProduct(BaseModel):
    product_id: int
    product_name: str
    category: str
    total_qty: float
    total_revenue: float
    order_count: int
    unit: str
    rank: int


class SlowMoverProduct(BaseModel):
    product_id: int
    product_name: str
    category: str
    stock: float
    last_sale_days_ago: Optional[int]
    total_qty_period: float
    unit: str


class ProductMovementItem(BaseModel):
    product_id: int
    product_name: str
    sale_qty: float
    purchase_qty: float
    return_qty: float
    adjustment_qty: float
    net_movement: float
    opening_stock: float
    closing_stock: float


class ProductRevenueItem(BaseModel):
    product_id: int
    product_name: str
    category: str
    sell_price: float
    buy_price: float
    margin_pct: float
    total_sold_qty: float
    total_revenue: float
    total_profit: float


class ProductPerformanceResponse(BaseModel):
    top_sellers: List[TopProduct]
    slow_movers: List[SlowMoverProduct]
    revenue_ranked: List[ProductRevenueItem]
    movement_summary: List[ProductMovementItem]
    total_products_active: int
    total_revenue_period: float
