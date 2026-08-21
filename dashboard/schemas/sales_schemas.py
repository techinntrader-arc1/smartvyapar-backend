"""
Pydantic schemas for Sales Analytics endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional
from dashboard.schemas.common_schemas import TrendPoint, TrendSeries


class SalesTrendRequest(BaseModel):
    period: str = "daily"  # daily | weekly | monthly | yearly
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cashier: Optional[str] = None
    category_id: Optional[int] = None
    payment_method: Optional[str] = None


class SalesTrendResponse(BaseModel):
    period: str
    series: List[TrendPoint]
    total_revenue: float
    total_orders: int
    avg_order_value: float


class HourlyHeatmapCell(BaseModel):
    weekday: int       # 0=Mon … 6=Sun
    hour: int          # 0-23
    order_count: int
    revenue: float


class HourlyHeatmapResponse(BaseModel):
    cells: List[HourlyHeatmapCell]
    peak_hour: int
    peak_weekday: int
    peak_revenue: float


class BasketTrendPoint(BaseModel):
    label: str
    avg_basket: float
    order_count: int


class BasketTrendResponse(BaseModel):
    series: List[BasketTrendPoint]
    overall_avg_basket: float
    trend_direction: str  # "up" | "down" | "flat"


class RecentSaleItem(BaseModel):
    id: int
    invoice_no: str
    customer: str
    cashier: str
    total: float
    paid_amount: float
    payment_method: str
    status: str
    item_count: int
    date: str


class SalesTableResponse(BaseModel):
    items: List[RecentSaleItem]
    total: int
    filtered_total: float


class SalesDrilldown(BaseModel):
    sale_id: int
    invoice_no: str
    customer: str
    items: List[dict]
    totals: dict
    timeline: dict
