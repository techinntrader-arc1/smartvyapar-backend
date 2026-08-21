"""
Pydantic schemas for Payment Analysis endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional


class PaymentMethodSummary(BaseModel):
    method: str
    total_amount: float
    transaction_count: int
    pct_of_total: float
    avg_transaction: float


class PaymentTrendPoint(BaseModel):
    label: str
    cash: float
    card: float
    credit: float
    mixed: float
    total: float


class PaymentReconciliation(BaseModel):
    date: str
    expected_cash: float
    expected_card: float
    total_expected: float
    paid_amount: float
    outstanding_amount: float
    outstanding_orders: int


class OutstandingPayment(BaseModel):
    sale_id: int
    invoice_no: str
    customer: str
    total: float
    paid_amount: float
    balance_due: float
    days_outstanding: int
    payment_method: str


class PaymentAnalyticsResponse(BaseModel):
    method_summary: List[PaymentMethodSummary]
    trend: List[PaymentTrendPoint]
    reconciliation: PaymentReconciliation
    outstanding_payments: List[OutstandingPayment]
    total_outstanding: float
    cash_collection_rate: float
