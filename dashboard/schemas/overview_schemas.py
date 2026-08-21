"""
Pydantic schemas for Executive Overview endpoint responses.
"""

from pydantic import BaseModel
from typing import List, Optional
from dashboard.schemas.common_schemas import TrendPoint, KpiDelta


class TopProductItem(BaseModel):
    product_id: int
    product_name: str
    total_qty: float
    total_revenue: float
    rank: int


class TopCategoryItem(BaseModel):
    category_id: Optional[int]
    category_name: str
    total_revenue: float
    order_count: int
    contribution_pct: float


class PaymentSplit(BaseModel):
    method: str
    amount: float
    count: int
    pct: float


class ActiveCashier(BaseModel):
    username: str
    full_name: str
    sales_today: int
    revenue_today: float


class ShiftSummary(BaseModel):
    active_cashiers: List[ActiveCashier]
    total_sales_today: int
    revenue_today: float
    avg_basket_today: float


class StockSnapshot(BaseModel):
    low_stock_count: int
    out_of_stock_count: int
    total_products: int
    low_stock_items: List[dict]


class ExecutiveOverview(BaseModel):
    gross_sales_today: float
    net_sales_today: float
    gross_sales_month: float
    net_sales_month: float
    total_orders_today: int
    total_orders_month: int
    avg_order_value_today: float
    avg_order_value_month: float
    total_discount_today: float
    total_discount_month: float
    tax_collected_today: float
    tax_collected_month: float
    refund_count_month: int
    refund_amount_month: float
    return_count_month: int
    total_receivable: float
    total_payable: float
    payment_split_today: List[PaymentSplit]
    top_products_today: List[TopProductItem]
    top_categories_month: List[TopCategoryItem]
    shift_summary: ShiftSummary
    stock_snapshot: StockSnapshot
    chart_7day: List[TrendPoint]
    month_vs_prev_month: Optional[KpiDelta]
