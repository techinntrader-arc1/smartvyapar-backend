"""
Pydantic schemas for Revenue Analytics endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional
from dashboard.schemas.common_schemas import TrendPoint, KpiDelta


class RevenueBreakdownPoint(BaseModel):
    label: str
    gross: float
    discount: float
    tax: float
    net: float
    refunds: float


class RevenueBreakdownResponse(BaseModel):
    series: List[RevenueBreakdownPoint]
    totals: dict
    period: str


class DiscountAnalysis(BaseModel):
    total_discount: float
    discount_count: int
    avg_discount_per_order: float
    discount_as_pct_of_gross: float
    top_discount_days: List[dict]
    discount_by_cashier: List[dict]


class TaxAnalysis(BaseModel):
    total_tax_collected: float
    tax_by_product_category: List[dict]
    taxable_sales: float
    non_taxable_sales: float
    effective_tax_rate: float


class PeriodComparison(BaseModel):
    current_period: str
    previous_period: str
    gross_sales: KpiDelta
    net_sales: KpiDelta
    order_count: KpiDelta
    avg_basket: KpiDelta
    discounts: KpiDelta
    refunds: KpiDelta
    tax: KpiDelta


class RefundImpact(BaseModel):
    total_refunds: float
    refund_count: int
    refund_rate_pct: float
    refund_as_pct_of_gross: float
    refund_trend: List[TrendPoint]
