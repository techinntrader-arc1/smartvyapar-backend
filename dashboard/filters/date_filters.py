"""
Date range parsing and validation helpers for dashboard filters.
Converts query params into validated date strings for SQLAlchemy filters.
"""

from datetime import date, timedelta
from typing import Tuple, Optional


PERIOD_PRESETS = {
    "today": 0,
    "yesterday": 1,
    "last7": 7,
    "last30": 30,
    "last90": 90,
    "this_month": None,
    "last_month": None,
    "this_year": None,
}


def resolve_date_range(
    start_date: Optional[str],
    end_date: Optional[str],
    preset: Optional[str] = None
) -> Tuple[str, str]:
    """
    Returns (start_date_str, end_date_str) for use in SQL filters.
    Preset overrides explicit start/end if provided.
    """
    today = date.today()

    if preset:
        if preset == "today":
            return today.isoformat(), today.isoformat()
        if preset == "yesterday":
            d = today - timedelta(days=1)
            return d.isoformat(), d.isoformat()
        if preset == "last7":
            return (today - timedelta(days=6)).isoformat(), today.isoformat()
        if preset == "last30":
            return (today - timedelta(days=29)).isoformat(), today.isoformat()
        if preset == "last90":
            return (today - timedelta(days=89)).isoformat(), today.isoformat()
        if preset == "this_month":
            return today.replace(day=1).isoformat(), today.isoformat()
        if preset == "last_month":
            first_this = today.replace(day=1)
            last_prev = first_this - timedelta(days=1)
            first_prev = last_prev.replace(day=1)
            return first_prev.isoformat(), last_prev.isoformat()
        if preset == "this_year":
            return today.replace(month=1, day=1).isoformat(), today.isoformat()

    # Defaults if no preset and no explicit dates
    if not start_date:
        start_date = today.replace(day=1).isoformat()
    if not end_date:
        end_date = today.isoformat()

    try:
        parsed_start = date.fromisoformat(start_date)
        parsed_end = date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dates must use the YYYY-MM-DD format") from exc

    if parsed_start > parsed_end:
        raise ValueError("Start date cannot be after end date")

    return parsed_start.isoformat(), parsed_end.isoformat()


def prev_period_for(start_date: str, end_date: str) -> Tuple[str, str]:
    """Calculate the equivalent previous period for comparison."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    delta = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=delta - 1)
    return prev_start.isoformat(), prev_end.isoformat()


def month_start_end(year: int, month: int) -> Tuple[str, str]:
    """Returns (first, last) day strings for given year/month."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    first = date(year, month, 1).isoformat()
    last = date(year, month, last_day).isoformat()
    return first, last
