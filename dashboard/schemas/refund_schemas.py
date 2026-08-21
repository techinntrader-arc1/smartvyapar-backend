"""
Pydantic schemas for Refund and Returns Analysis endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional
from dashboard.schemas.common_schemas import TrendPoint


class RefundTrendPoint(BaseModel):
    label: str
    refund_count: int
    refund_amount: float
    return_count: int


class ProductRefundItem(BaseModel):
    product_id: int
    product_name: str
    category: str
    return_count: int
    returned_qty: float
    refund_amount: float
    return_rate_pct: float


class CategoryRefundItem(BaseModel):
    category_name: str
    return_count: int
    refund_amount: float
    sales_amount: float
    return_rate_pct: float


class ReturnedSaleItem(BaseModel):
    sale_id: int
    invoice_no: str
    customer: str
    cashier: str
    original_total: float
    refund_amount: float
    status: str
    return_date: str
    items_returned: int


class AnomalyFlag(BaseModel):
    date: str
    metric: str
    value: float
    avg_value: float
    deviation_pct: float
    severity: str   # "warning" | "critical"


class RefundAnalyticsResponse(BaseModel):
    refund_count_period: int
    refund_amount_period: float
    refund_rate_pct: float
    trend: List[RefundTrendPoint]
    by_product: List[ProductRefundItem]
    by_category: List[CategoryRefundItem]
    recent_returns: List[ReturnedSaleItem]
    anomaly_flags: List[AnomalyFlag]
