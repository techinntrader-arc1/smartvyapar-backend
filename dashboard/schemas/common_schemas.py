"""
Common shared Pydantic schemas for dashboard filters and responses.
Used across all dashboard route handlers.
"""

from pydantic import BaseModel, validator
from typing import Optional, List, Any
from datetime import date


class DateRangeFilter(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cashier: Optional[str] = None
    category_id: Optional[int] = None
    payment_method: Optional[str] = None
    customer_id: Optional[int] = None

    @validator("start_date", "end_date", pre=True, always=True)
    def validate_date_format(cls, v):
        if v is None:
            return v
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD")
        return v


class TrendPoint(BaseModel):
    label: str
    value: float
    count: int = 0


class TrendSeries(BaseModel):
    name: str
    data: List[TrendPoint]
    total: float = 0.0
    color: Optional[str] = None


class KpiDelta(BaseModel):
    current: float
    previous: float
    delta_pct: float
    direction: str  # "up" | "down" | "flat"


class PaginatedResponse(BaseModel):
    items: List[Any]
    total: int
    page: int = 1
    page_size: int = 50


class ErrorResponse(BaseModel):
    detail: str
    code: str = "DASHBOARD_ERROR"


class SuccessResponse(BaseModel):
    success: bool = True
    message: str = ""
