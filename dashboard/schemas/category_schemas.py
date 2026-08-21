"""
Pydantic schemas for Category Performance Analytics.
"""

from pydantic import BaseModel
from typing import List, Optional


class CategoryContribution(BaseModel):
    category_id: Optional[int]
    category_name: str
    total_revenue: float
    order_count: int
    product_count: int
    contribution_pct: float
    avg_item_value: float


class CategoryTrendPoint(BaseModel):
    label: str
    revenue: float
    order_count: int


class CategoryTrendSeries(BaseModel):
    category_name: str
    color: str
    data: List[CategoryTrendPoint]
    total: float


class CategoryAnalyticsResponse(BaseModel):
    contributions: List[CategoryContribution]
    trend_series: List[CategoryTrendSeries]
    top_category: str
    total_categories_active: int
    period: str
