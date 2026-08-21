"""
Pydantic schemas for Orders Analytics endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional
from dashboard.schemas.common_schemas import TrendPoint


class OrderStatusBreakdown(BaseModel):
    completed: int
    held: int
    returned: int
    partially_returned: int
    total: int


class OrdersByPeriod(BaseModel):
    label: str
    count: int
    revenue: float
    avg_value: float


class OrderTimelineItem(BaseModel):
    id: int
    invoice_no: str
    cashier: str
    customer: str
    total: float
    status: str
    payment_method: str
    item_count: int
    created_at: str


class OrdersByCashier(BaseModel):
    cashier: str
    order_count: int
    total_revenue: float
    avg_order: float


class OrdersByPaymentMethod(BaseModel):
    method: str
    count: int
    total: float
    pct: float


class OrdersAnalyticsResponse(BaseModel):
    status_breakdown: OrderStatusBreakdown
    volume_trend: List[OrdersByPeriod]
    by_cashier: List[OrdersByCashier]
    by_payment_method: List[OrdersByPaymentMethod]
    recent: List[OrderTimelineItem]
    peak_hour: int
    busiest_weekday: int
