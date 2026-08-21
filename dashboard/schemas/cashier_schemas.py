"""
Pydantic schemas for Cashier Performance Analytics.
Provides data models for cashier summaries, trends, hourly breakdowns, shift reports, filter requests, and exports.
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class CashierRequest(BaseModel):
    """Payload schema for creating or updating a cashier"""
    username: str = Field(..., max_length=50, description="Login username")
    full_name: str = Field(..., max_length=100, description="Cashier's full name")
    is_active: bool = Field(True, description="Cashier active status")
    password: Optional[str] = Field(None, min_length=4, description="Optional password for cashier")


class CashierSalesSummary(BaseModel):
    """Individual cashier sales summary"""
    id: int = Field(0, description="Cashier ID")
    username: str = Field(..., max_length=50, description="Login username")
    full_name: str = Field("", max_length=100, description="Cashier's full name")
    is_active: bool = Field(True, description="Cashier active status")
    total_orders: int = Field(0, ge=0, description="Total number of orders")
    gross_revenue: float = Field(0.0, ge=0, description="Gross revenue before discounts")
    net_revenue: float = Field(0.0, description="Net revenue after posted returns")
    avg_basket: float = Field(0.0, ge=0, description="Average order value")
    total_discount_given: float = Field(0.0, ge=0, description="Total discounts given")
    refund_count: int = Field(0, ge=0, description="Number of refunds processed")
    refund_amount: float = Field(0.0, ge=0, description="Total refund amount")
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow, description="Cashier creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")


class CashierDailyPoint(BaseModel):
    """Daily trend data point"""
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date in YYYY-MM-DD")
    cashier: str = Field(..., description="Cashier name")
    order_count: int = Field(0, ge=0, description="Number of orders")
    revenue: float = Field(0.0, description="Net revenue; may be negative on a return-only day")
    avg_order_value: Optional[float] = Field(None, description="Average order value")


class CashierHourlyBreakdown(BaseModel):
    """Hourly performance breakdown"""
    cashier: str = Field(..., description="Cashier name")
    hour: int = Field(..., ge=0, le=23, description="Hour of day (0-23)")
    order_count: int = Field(0, ge=0, description="Orders in this hour")
    revenue: float = Field(0.0, description="Net revenue in this hour")
    avg_basket: Optional[float] = Field(None, description="Average basket size")


class CashierShiftSummary(BaseModel):
    """Cashier shift summary"""
    cashier: str = Field(..., description="Cashier name")
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Shift date")
    opening_time: Optional[str] = Field(None, description="Shift start time")
    closing_time: Optional[str] = Field(None, description="Shift end time")
    orders: int = Field(0, ge=0, description="Total orders in shift")
    gross: float = Field(0.0, ge=0, description="Gross revenue")
    net: float = Field(0.0, description="Net revenue after posted returns")
    cash_collected: float = Field(0.0, description="Net cash activity")
    card_collected: float = Field(0.0, description="Net card activity")
    credit_given: float = Field(0.0, ge=0, description="Credit given")
    discounts: float = Field(0.0, ge=0, description="Discounts applied")
    refunds: float = Field(0.0, ge=0, description="Refunds processed")


class CashierPerformanceResponse(BaseModel):
    """Complete cashier performance response"""
    summaries: List[CashierSalesSummary] = Field(..., description="All cashier summaries")
    daily_trend: List[CashierDailyPoint] = Field(..., description="Daily trend data")
    hourly_breakdown: List[CashierHourlyBreakdown] = Field(default_factory=list, description="Hourly breakdown")
    shift_summaries: List[CashierShiftSummary] = Field(..., description="Shift summaries")
    top_cashier: str = Field(..., description="Top performing cashier")
    period: str = Field(..., description="Analysis period")
    total_revenue: float = Field(0.0, description="Total revenue all cashiers")
    total_orders: int = Field(0, description="Total orders all cashiers")


class CashierFilterRequest(BaseModel):
    """Filter parameters for cashier queries"""
    start_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Start date")
    end_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="End date")
    cashier_ids: Optional[List[int]] = Field(None, description="Filter by cashier IDs")
    min_revenue: Optional[float] = Field(None, ge=0, description="Minimum revenue")
    min_orders: Optional[int] = Field(None, ge=0, description="Minimum orders")
    sort_by: Optional[str] = Field("revenue", description="Sort field")
    sort_order: Optional[str] = Field("desc", description="Sort order (asc/desc)")


class CashierListResponse(BaseModel):
    """Paginated cashier list response"""
    cashiers: List[CashierSalesSummary] = Field(..., description="List of cashiers")
    total: int = Field(..., description="Total number of cashiers")
    page: int = Field(1, ge=1, description="Current page")
    page_size: int = Field(20, ge=1, le=100, description="Items per page")
    total_pages: int = Field(..., description="Total pages")


class CashierComparisonItem(BaseModel):
    """Cashier performance comparison item"""
    cashier_name: str = Field(..., description="Cashier name")
    revenue: float = Field(..., description="Total revenue")
    orders: int = Field(..., description="Total orders")
    avg_basket: float = Field(..., description="Average basket size")
    market_share: float = Field(..., ge=0, le=100, description="Market share percentage")
    ranking: int = Field(..., ge=1, description="Rank among cashiers")


class CashierMetricsResponse(BaseModel):
    """Aggregated cashier metrics"""
    total_cashiers: int = Field(..., description="Total active cashiers")
    total_revenue: float = Field(..., description="Total revenue all cashiers")
    total_orders: int = Field(..., description="Total orders all cashiers")
    average_basket: float = Field(..., description="Average basket size")
    top_cashier: str = Field(..., description="Top performing cashier")
    bottom_cashier: str = Field(..., description="Lowest performing cashier")
    revenue_std_dev: float = Field(..., description="Revenue standard deviation")


class CashierRankingResponse(BaseModel):
    """Cashier ranking response"""
    rankings: List[CashierComparisonItem] = Field(..., description="Ranked list of cashiers")
    total_cashiers: int = Field(..., description="Total cashiers ranked")
    period: str = Field(..., description="Analysis period")
    average_revenue: float = Field(..., description="Average revenue across all")


class CashierExportResponse(BaseModel):
    """Cashier export response"""
    data: List[CashierSalesSummary] = Field(..., description="Export data")
    filename: str = Field(..., description="Export filename")
    format: str = Field("csv", description="Export format")
    generated_at: datetime = Field(default_factory=datetime.utcnow, description="Generation timestamp")


__all__ = [
    "CashierRequest",
    "CashierSalesSummary",
    "CashierDailyPoint",
    "CashierHourlyBreakdown",
    "CashierShiftSummary",
    "CashierPerformanceResponse",
    "CashierFilterRequest",
    "CashierListResponse",
    "CashierComparisonItem",
    "CashierMetricsResponse",
    "CashierRankingResponse",
    "CashierExportResponse",
]
