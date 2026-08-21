"""
Expenses router - Add, list, delete, summarize, and export expenses.
Auto-creates CashTransaction on cash expenses for unified cash book.
"""

import logging
import io
import csv
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, distinct
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user, require_admin
import models

# Safe import for rate limiting (slowapi)
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address)
except ImportError:
    class DummyLimiter:
        def limit(self, limit_value: str):
            def decorator(func):
                return func
            return decorator
    limiter = DummyLimiter()

logger = logging.getLogger("smartvyapar.expenses")
logger.setLevel(logging.INFO)

router = APIRouter()

DEFAULT_CATEGORIES = [
    "Rent", "Electricity", "Salary", "Transport", "Misc",
    "Maintenance", "Marketing", "Utilities", "Office Supplies", "Tea/Refreshments",
]


# ── Schemas ──────────────────────────────────────────────────────────────

class ExpenseCreate(BaseModel):
    category: str = Field(..., min_length=1, max_length=100, description="Expense category")
    amount: float = Field(..., gt=0, description="Expense amount in PKR")
    note: Optional[str] = Field("", max_length=500, description="Optional note or description")
    date: Optional[str] = Field(None, description="ISO Date string YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    payment_source: Optional[str] = Field("cash_in_hand", description="Payment source: cash_in_hand | bank | other")


class ExpenseItem(BaseModel):
    id: int
    category: str
    amount: float
    note: Optional[str] = None
    payment_source: Optional[str] = "cash_in_hand"
    date: datetime

    class Config:
        orm_mode = True


class ExpenseListResponse(BaseModel):
    total: int
    items: List[ExpenseItem]
    limit: int
    offset: int


class ExpenseCreateResponse(BaseModel):
    id: int
    category: str
    amount: float
    note: Optional[str] = None
    payment_source: str
    date: datetime


class ExpenseCategorySummary(BaseModel):
    category: str
    total: float
    count: int
    percentage: float


class ExpenseSummaryResponse(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    grand_total: float
    entry_count: int
    category_count: int
    categories: List[ExpenseCategorySummary]


class ExpenseDeleteResponse(BaseModel):
    success: bool
    message: str


class ExpenseDetailResponse(BaseModel):
    id: int
    category: str
    amount: float
    note: Optional[str] = None
    payment_source: str
    date: datetime
    has_cash_tx: bool = False


class ExpenseTrendItem(BaseModel):
    month: str
    total_amount: float
    count: int


class ExpenseTrendResponse(BaseModel):
    trend: List[ExpenseTrendItem]


class ExpensesHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Helper Functions ──────────────────────────────────────────────────────────

def _record_cash_tx(db: Session, expense: models.Expense, user: str):
    """Auto-create a cash_out transaction when expense is paid from cash_in_hand."""
    if (expense.payment_source or "cash_in_hand") != "cash_in_hand":
        return

    if isinstance(expense.date, datetime):
        if expense.date.hour == 0 and expense.date.minute == 0 and expense.date.second == 0:
            now = datetime.now()
            tx_dt = expense.date.replace(hour=now.hour, minute=now.minute, second=now.second)
        else:
            tx_dt = expense.date
    else:
        tx_dt = datetime.now()

    tx = models.CashTransaction(
        tx_type="cash_out",
        cash_out_type="expense" if (expense.category or "").lower() != "salary" else "salary",
        amount=expense.amount,
        account="cash_in_hand",
        paid_to="",
        category=expense.category,
        reference_type="expense",
        reference_id=expense.id,
        reference_no=f"EXP-{expense.id}",
        notes=expense.note or "",
        created_by=user,
        created_at=tx_dt,
    )
    db.add(tx)


def _expense_date_query(db: Session, start_date: Optional[str], end_date: Optional[str]):
    query = db.query(models.Expense)
    if start_date:
        query = query.filter(func.date(models.Expense.date) >= start_date)
    if end_date:
        query = query.filter(func.date(models.Expense.date) <= end_date)
    return query


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=ExpensesHealthCheckResponse)
@limiter.limit("30/minute")
def expenses_health(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Health check endpoint for expenses router."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "expenses",
        "endpoints": [
            "/summary",
            "/categories",
            "/export",
            "/trend",
            "/health",
            "/",
            "/{id}"
        ]
    }


@router.get("/summary", response_model=ExpenseSummaryResponse)
@limiter.limit("30/minute")
def expense_summary(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Category-wise totals and percentage breakdown for expense review."""
    try:
        logger.info(f"Admin '{current_user.username}' requesting expense summary (start={start_date}, end={end_date})")
        rows = (
            _expense_date_query(db, start_date, end_date)
            .with_entities(
                models.Expense.category,
                func.sum(models.Expense.amount).label("total"),
                func.count(models.Expense.id).label("count"),
            )
            .group_by(models.Expense.category)
            .order_by(func.sum(models.Expense.amount).desc())
            .all()
        )

        grand_total = sum(float(r.total or 0) for r in rows)
        entry_count = sum(int(r.count or 0) for r in rows)

        categories = []
        for r in rows:
            amt = float(r.total or 0)
            categories.append({
                "category": r.category or "Uncategorized",
                "total": round(amt, 2),
                "count": int(r.count or 0),
                "percentage": round((amt / grand_total * 100) if grand_total else 0, 1),
            })

        return {
            "start_date": start_date,
            "end_date": end_date,
            "grand_total": round(grand_total, 2),
            "entry_count": entry_count,
            "category_count": len(categories),
            "categories": categories,
        }
    except Exception as e:
        logger.error(f"Error generating expense summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate expense summary: {str(e)}"
        )


@router.get("/categories", response_model=List[str])
@limiter.limit("30/minute")
def expense_categories(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Get list of default and custom expense categories."""
    try:
        logger.info(f"Admin '{current_user.username}' fetching expense categories")
        cats = db.query(distinct(models.Expense.category)).all()
        existing = [c[0] for c in cats if c[0]]
        merged = sorted(set(DEFAULT_CATEGORIES + existing), key=str.lower)
        return merged
    except Exception as e:
        logger.error(f"Error fetching expense categories: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch expense categories: {str(e)}"
        )


@router.get("/export")
@limiter.limit("10/minute")
def export_expenses(
    request: Request,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Export expenses report in CSV or Excel format."""
    try:
        logger.info(f"Admin '{current_user.username}' exporting expenses as {format}")
        query = _expense_date_query(db, start_date, end_date)
        if category:
            query = query.filter(models.Expense.category == category)
        
        expenses = query.order_by(models.Expense.date.desc()).all()
        if not expenses:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No expenses found for export"
            )

        data = [
            {
                "ID": e.id,
                "Category": e.category,
                "Amount": e.amount,
                "Payment Source": e.payment_source or "cash_in_hand",
                "Date": e.date.strftime("%Y-%m-%d %H:%M:%S") if e.date else "",
                "Note": e.note or ""
            }
            for e in expenses
        ]

        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

        if format == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
            output.seek(0)
            return StreamingResponse(
                io.BytesIO(output.getvalue().encode('utf-8')),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=expenses_{timestamp_str}.csv"
                }
            )
        else:
            try:
                import pandas as pd
                df = pd.DataFrame(data)
                excel_output = io.BytesIO()
                with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, sheet_name='Expenses', index=False)
                excel_output.seek(0)
                return StreamingResponse(
                    excel_output,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": f"attachment; filename=expenses_{timestamp_str}.xlsx"
                    }
                )
            except ImportError:
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
                writer.writeheader()
                writer.writerows(data)
                output.seek(0)
                return StreamingResponse(
                    io.BytesIO(output.getvalue().encode('utf-8')),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f"attachment; filename=expenses_{timestamp_str}.csv"
                    }
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting expenses: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export expenses: {str(e)}"
        )


@router.get("/trend", response_model=ExpenseTrendResponse)
@limiter.limit("30/minute")
def expense_trend(
    request: Request,
    months_count: int = Query(12, ge=1, le=36, description="Number of months for trend"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Monthly expense trend analysis."""
    try:
        logger.info(f"Admin '{current_user.username}' requesting expense trend for last {months_count} months")
        
        today = date.today()
        start_month_date = (today.replace(day=1) - timedelta(days=30 * months_count)).replace(day=1)
        start_str = start_month_date.isoformat()

        expenses = db.query(
            models.Expense.date,
            models.Expense.amount
        ).filter(
            func.date(models.Expense.date) >= start_str
        ).all()

        trend_map: Dict[str, Dict[str, float]] = {}
        for e in expenses:
            if not e.date:
                continue
            month_key = e.date.strftime("%Y-%m")
            if month_key not in trend_map:
                trend_map[month_key] = {"total_amount": 0.0, "count": 0}
            trend_map[month_key]["total_amount"] += float(e.amount or 0)
            trend_map[month_key]["count"] += 1

        trend_list = [
            {
                "month": m,
                "total_amount": round(data["total_amount"], 2),
                "count": data["count"]
            }
            for m, data in sorted(trend_map.items())
        ]

        return {"trend": trend_list}
    except Exception as e:
        logger.error(f"Error calculating expense trend: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate expense trend: {str(e)}"
        )


@router.get("/", response_model=List[ExpenseItem])
@limiter.limit("30/minute")
def list_expenses(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    category: Optional[str] = Query(None, description="Filter by category"),
    search: Optional[str] = Query(None, description="Search in note or category"),
    limit: int = Query(500, ge=1, le=100000, description="Items per page"),
    offset: int = Query(0, ge=0, description="Items offset"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """List expenses with pagination, date range, category filter, and search."""
    try:
        logger.info(f"Admin '{current_user.username}' listing expenses (start={start_date}, end={end_date}, cat={category}, search={search})")
        query = _expense_date_query(db, start_date, end_date)

        if category:
            query = query.filter(models.Expense.category == category)

        if search:
            query = query.filter(
                or_(
                    models.Expense.note.ilike(f"%{search}%"),
                    models.Expense.category.ilike(f"%{search}%")
                )
            )

        expenses = query.order_by(models.Expense.date.desc()).offset(offset).limit(limit).all()
        return expenses
    except Exception as e:
        logger.error(f"Error listing expenses: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list expenses: {str(e)}"
        )


@router.get("/{id}", response_model=ExpenseDetailResponse)
@limiter.limit("30/minute")
def get_expense_by_id(
    request: Request,
    id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Get single expense details by ID."""
    try:
        logger.info(f"Admin '{current_user.username}' fetching expense ID {id}")
        expense = db.query(models.Expense).filter_by(id=id).first()
        if not expense:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Expense with ID {id} not found"
            )

        linked_tx = db.query(models.CashTransaction).filter_by(
            reference_type="expense", reference_id=id
        ).first()

        return {
            "id": expense.id,
            "category": expense.category,
            "amount": expense.amount,
            "note": expense.note,
            "payment_source": expense.payment_source or "cash_in_hand",
            "date": expense.date,
            "has_cash_tx": linked_tx is not None
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching expense ID {id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch expense details: {str(e)}"
        )


@router.post("/", response_model=ExpenseCreateResponse)
@limiter.limit("10/minute")
def create_expense(
    request: Request,
    data: ExpenseCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Create a new expense entry with auto-linked cash book transaction."""
    if data.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount must be greater than zero")
    if data.amount > 1000000000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount cannot exceed 1 Billion")

    category = (data.category or "").strip()
    if not category:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Category is required")

    user_str = current_user.username

    try:
        logger.info(f"Admin '{user_str}' creating expense: category='{category}', amount={data.amount}, source={data.payment_source}")

        if data.date:
            parsed = datetime.fromisoformat(data.date)
            now = datetime.now()
            expense_dt = parsed.replace(hour=now.hour, minute=now.minute, second=now.second)
        else:
            expense_dt = datetime.now()

        e = models.Expense(
            category=category,
            amount=data.amount,
            note=data.note or "",
            payment_source=data.payment_source or "cash_in_hand",
            date=expense_dt,
        )
        db.add(e)
        db.flush()

        _record_cash_tx(db, e, user_str)

        db.commit()
        db.refresh(e)

        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
        except Exception as backup_err:
            logger.warning(f"Auto-backup warning after expense creation: {backup_err}")

        return e
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create expense: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create expense: {str(e)}"
        )


@router.delete("/{eid}", response_model=ExpenseDeleteResponse)
@limiter.limit("10/minute")
def delete_expense(
    request: Request,
    eid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Delete an expense entry and its linked cash transaction."""
    try:
        logger.info(f"Admin '{current_user.username}' deleting expense ID {eid}")
        e = db.query(models.Expense).filter_by(id=eid).first()
        if not e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Expense with ID {eid} not found"
            )

        linked_tx = db.query(models.CashTransaction).filter_by(
            reference_type="expense", reference_id=eid
        ).first()
        if linked_tx:
            db.delete(linked_tx)

        db.delete(e)
        db.commit()

        return {"success": True, "message": f"Expense ID {eid} deleted successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete expense ID {eid}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete expense: {str(e)}"
        )
