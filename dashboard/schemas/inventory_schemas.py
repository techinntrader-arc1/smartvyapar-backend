"""
Pydantic schemas for Inventory Insights endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional


class StockOverviewKpi(BaseModel):
    total_products: int
    total_stock_value: float
    low_stock_count: int
    out_of_stock_count: int
    healthy_stock_count: int
    avg_stock_value_per_product: float


class LowStockItem(BaseModel):
    product_id: int
    product_name: str
    category: str
    unit: str
    current_stock: float
    min_stock: float
    shortage: float
    last_purchase_date: Optional[str]
    last_sale_date: Optional[str]


class StockValueByCategory(BaseModel):
    category_name: str
    total_products: int
    total_stock_value: float
    avg_stock: float


class RecentStockMovement(BaseModel):
    id: int
    product_name: str
    movement_type: str
    qty_change: float
    qty_after: float
    note: str
    created_at: str


class RestockSuggestion(BaseModel):
    product_id: int
    product_name: str
    category: str
    unit: str
    current_stock: float
    min_stock: float
    suggested_qty: float
    priority: str   # critical | high | medium
    avg_daily_sales: float
    days_until_stockout: Optional[float]


class InventoryInsightsResponse(BaseModel):
    kpi: StockOverviewKpi
    low_stock_items: List[LowStockItem]
    out_of_stock_items: List[LowStockItem]
    value_by_category: List[StockValueByCategory]
    recent_movements: List[RecentStockMovement]
    restock_suggestions: List[RestockSuggestion]
