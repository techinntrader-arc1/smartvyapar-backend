"""
Pydantic schemas for Customer Analytics endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional
from dashboard.schemas.common_schemas import TrendPoint


class CustomerGrowthPoint(BaseModel):
    label: str
    new_customers: int
    cumulative: int


class TopCustomer(BaseModel):
    customer_id: int
    name: str
    phone: Optional[str]
    total_orders: int
    total_spend: float
    avg_order_value: float
    credit_balance: float
    last_purchase_date: Optional[str]


class CustomerFrequencyBucket(BaseModel):
    bucket: str    # "1 order" | "2-5 orders" | "6-10" | "10+"
    count: int
    pct: float


class CustomerSegmentSummary(BaseModel):
    total_customers: int
    new_this_month: int
    repeat_customers: int
    repeat_rate_pct: float
    avg_lifetime_value: float
    total_receivables: float


class CustomerAnalyticsResponse(BaseModel):
    segment_summary: CustomerSegmentSummary
    growth_trend: List[CustomerGrowthPoint]
    top_customers: List[TopCustomer]
    frequency_distribution: List[CustomerFrequencyBucket]
    high_value_threshold: float
