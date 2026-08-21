"""
SmartVyapar - Sync Manager
Handles SQLite triggers configuration, background synchronization thread,
and conflict-free push/pull API communication with Cloud Backend.
"""

import os
import sys
import json
import sqlite3
import threading
import urllib.request
import urllib.error
from datetime import datetime
from decimal import Decimal, InvalidOperation

# ── TABLES TO SYNC ──
SYNC_TABLES = [
    "users",
    "categories",
    "units",
    "brands",
    "customers",
    "suppliers",
    "employees",
    "products",
    "sales",
    "sale_items",
    "sale_returns",
    "sale_return_items",
    "purchases",
    "purchase_items",
    "payments",
    "expenses",
    "stock_movements",
    "settings",
    "cash_transactions",
    "employee_payrolls",
    "employee_ledger_entries",
    "employee_salary_payments",
    "employee_goods_credits",
    "employee_goods_credit_items",
    "day_closings",
    "external_funds"
]

# Posted accounting records are append-only business documents. They may be
# corrected only by their domain reversal/return workflows, never by a generic
# sync tombstone. Protect linked core parents too: deleting a sale, its lines, a
# cash entry, or a stock movement could otherwise orphan the employee audit
# chain on SQLite connections where foreign-key enforcement was historically
# disabled.
PROTECTED_EMPLOYEE_SYNC_TABLES = frozenset({
    "employees",
    "stock_movements",
    "employee_payrolls",
    "employee_ledger_entries",
    "employee_salary_payments",
    "employee_goods_credits",
    "employee_goods_credit_items",
    "sale_returns",
    "sale_return_items",
})


def sync_delete_allowed(cursor, table_name: str, record_id) -> bool:
    """Validate the narrow set of deletions that may cross sync boundaries.

    Immutable rows are never deleted. Core parent rows are deletable only for
    the application's explicitly unposted/manual workflows. A missing allowed
    row is treated as an idempotent replay.
    """
    if table_name in PROTECTED_EMPLOYEE_SYNC_TABLES:
        return False
    if table_name == "sales":
        row = cursor.execute(
            "SELECT status FROM sales WHERE id = ?", (record_id,)
        ).fetchone()
        if not row:
            return True
        if str(row[0] or "").lower() != "held":
            return False
        linked = cursor.execute("""
            SELECT
                EXISTS(SELECT 1 FROM employee_goods_credits WHERE sale_id = ?)
                OR EXISTS(SELECT 1 FROM employee_ledger_entries WHERE sale_id = ?)
                OR EXISTS(SELECT 1 FROM sale_returns WHERE sale_id = ?)
        """, (record_id, record_id, record_id)).fetchone()
        return not bool(linked and linked[0])
    if table_name == "sale_items":
        row = cursor.execute("""
            SELECT si.sale_id, COALESCE(s.status, '')
            FROM sale_items si LEFT JOIN sales s ON s.id = si.sale_id
            WHERE si.id = ?
        """, (record_id,)).fetchone()
        if not row:
            return True
        sale_id, sale_status = row[0], str(row[1] or "").lower()
        if sale_status != "held":
            return False
        linked = cursor.execute("""
            SELECT
                EXISTS(SELECT 1 FROM sale_return_items WHERE sale_item_id = ?)
                OR EXISTS(SELECT 1 FROM employee_goods_credits WHERE sale_id = ?)
        """, (record_id, sale_id)).fetchone()
        return not bool(linked and linked[0])
    if table_name == "cash_transactions":
        row = cursor.execute(
            "SELECT reference_type FROM cash_transactions WHERE id = ?", (record_id,)
        ).fetchone()
        if not row:
            return True
        if str(row[0] or "").strip().lower() not in {"", "manual"}:
            return False
        linked = cursor.execute("""
            SELECT
                EXISTS(SELECT 1 FROM employee_ledger_entries WHERE cash_transaction_id = ?)
                OR EXISTS(SELECT 1 FROM employee_salary_payments WHERE cash_transaction_id = ?)
                OR EXISTS(SELECT 1 FROM sale_returns WHERE cash_transaction_id = ?)
        """, (record_id, record_id, record_id)).fetchone()
        return not bool(linked and linked[0])
    return True

SENSITIVE_SETTING_KEYS = {
    "cloud_sync_token",
    "firebase_cloud_password",
    "google_token",
    "google_refresh_token",
    "google_client_id",
    "google_client_secret",
}


# These rows are posted accounting documents.  Their business identity is
# immutable after INSERT; only the small projection fields listed here may move
# forward as an explicit reversal/return workflow completes on another client.
IMMUTABLE_FINANCIAL_SYNC_TABLES = frozenset({
    "sales",
    "sale_items",
    "purchases",
    "purchase_items",
    "payments",
    "expenses",
    "stock_movements",
    "external_funds",
    "employee_payrolls",
    "employee_ledger_entries",
    "employee_salary_payments",
    "sale_returns",
    "sale_return_items",
    "employee_goods_credits",
    "employee_goods_credit_items",
})

NON_AUTHORITATIVE_SYNC_FIELDS = {
    "employees": frozenset({"current_balance"}),
    "employee_ledger_entries": frozenset({"balance_after"}),
}

MONOTONIC_SYNC_FIELDS = {
    "sales": frozenset({"status"}),
    "sale_items": frozenset({"returned_qty"}),
    "employee_payrolls": frozenset({
        "gross_salary", "bonus", "overtime", "deductions", "advances",
        "goods_credit", "employee_repayments", "goods_returns",
        "carried_debt_offset", "amount_paid", "net_payable", "status",
        "accrual_entry_id", "generated_by", "updated_at",
    }),
    "employee_ledger_entries": frozenset({
        "is_reversed", "reversal_reason", "payroll_id", "cash_transaction_id",
    }),
    "employee_salary_payments": frozenset({
        "is_reversed", "reversed_by_entry_id", "reversal_reason",
    }),
    "sale_returns": frozenset({"cash_transaction_id"}),
    "employee_goods_credits": frozenset({"returned_amount", "status"}),
    "employee_goods_credit_items": frozenset({"returned_qty"}),
}

REPLAY_DERIVED_FIELDS = {
    "sales": frozenset({"status"}),
    "sale_items": frozenset({"returned_qty"}),
    "employee_goods_credits": frozenset({"returned_amount", "status"}),
    "employee_goods_credit_items": frozenset({"returned_qty"}),
    "employee_payrolls": frozenset({
        "gross_salary", "bonus", "overtime", "deductions", "advances",
        "goods_credit", "employee_repayments", "goods_returns", "amount_paid",
        "net_payable", "status", "accrual_entry_id", "updated_at",
    }),
}


def _is_immutable_financial_row(table_name: str, incoming: dict, existing: dict) -> bool:
    if table_name in IMMUTABLE_FINANCIAL_SYNC_TABLES:
        return True
    if table_name == "cash_transactions":
        # Purchase replacement is a legacy workflow that intentionally reuses
        # its cash row.  All posted employee/sale/accounting cash movements are
        # immutable and must be corrected through a compensating transaction.
        reference_type = str(
            incoming.get("reference_type", existing.get("reference_type") if existing else "") or ""
        ).strip().lower()
        return reference_type not in {"", "manual", "purchase"}
    return False


class SyncConflictError(RuntimeError):
    """A fail-closed conflict between two independently posted sync rows."""

    def __init__(self, table_name: str, record_id, error: str):
        self.table_name = str(table_name)
        self.record_id = str(record_id)
        self.error = str(error)
        super().__init__(f"{self.table_name}:{self.record_id}: {self.error}")

    def detail(self):
        return {
            "error": "sync_conflict",
            "table": self.table_name,
            "record_id": self.record_id,
            "message": self.error,
        }


def _quoted(identifier: str) -> str:
    """Quote an identifier that has already come from our table schema."""
    return '"' + str(identifier).replace('"', '""') + '"'


def _table_columns(cursor, table_name: str):
    cursor.execute(f"PRAGMA table_info({_quoted(table_name)})")
    return {str(row[1]): str(row[2] or "").upper() for row in cursor.fetchall()}


def _canonical_value(value, declared_type: str = "", column_name: str = ""):
    """Normalize SQLite/JSON representations before replay comparison."""
    if value is None:
        return None
    declared_type = str(declared_type or "").upper()
    if "BOOL" in declared_type:
        return _as_bool(value)
    if "INT" in declared_type:
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)
    if any(marker in declared_type for marker in ("NUM", "DEC", "REAL", "FLOA", "DOUB")):
        try:
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                return str(value)
            return numeric.normalize()
        except (InvalidOperation, TypeError, ValueError):
            return str(value)
    if str(column_name).endswith("_json") and isinstance(value, str):
        try:
            return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            pass
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return str(value)


def _as_decimal(value, *, table_name: str, record_id, field: str) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        raise SyncConflictError(table_name, record_id, f"invalid numeric value for {field}")
    if not result.is_finite():
        raise SyncConflictError(table_name, record_id, f"invalid numeric value for {field}")
    return result


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _fetch_existing_row(cursor, table_name: str, pk: str, record_id, columns):
    names = list(columns)
    cursor.execute(
        f"SELECT {', '.join(_quoted(name) for name in names)} "
        f"FROM {_quoted(table_name)} WHERE {_quoted(pk)} = ?",
        (record_id,),
    )
    row = cursor.fetchone()
    return dict(zip(names, row)) if row else None


def _assert_same_fields(
    table_name: str,
    record_id,
    incoming: dict,
    existing: dict,
    column_types: dict,
    *,
    excluded=frozenset(),
):
    for field, incoming_value in incoming.items():
        if field in excluded:
            continue
        if _canonical_value(incoming_value, column_types.get(field), field) != _canonical_value(
            existing.get(field), column_types.get(field), field
        ):
            raise SyncConflictError(
                table_name,
                record_id,
                f"immutable field '{field}' differs from the existing row",
            )


def _validate_monotonic_projection(
    cursor,
    table_name: str,
    record_id,
    incoming: dict,
    existing: dict,
):
    """Return the projection-only UPDATE values after validating direction."""
    allowed = MONOTONIC_SYNC_FIELDS.get(table_name, frozenset())
    projection = {key: value for key, value in incoming.items() if key in allowed}

    if table_name in {
        "sales", "sale_items",
        "employee_goods_credits", "employee_goods_credit_items",
    }:
        # These absolute snapshots lose increments under concurrent clients.
        # Ignore them here; validate and rebuild from immutable return/ledger
        # rows after the whole batch is visible.
        return {}

    if table_name == "sales":
        old_status = str(existing.get("status") or "").strip().lower()
        new_status = str(projection.get("status", old_status) or "").strip().lower()
        if old_status == "held" and new_status != "held":
            raise SyncConflictError(table_name, record_id, "a held sale cannot be generically posted or returned")
        ranks = {"completed": 0, "partially_returned": 1, "returned": 2}
        if old_status != "held" and (
            old_status not in ranks or new_status not in ranks or ranks[new_status] < ranks[old_status]
        ):
            raise SyncConflictError(table_name, record_id, "sale return status cannot move backwards")

    elif table_name == "sale_items":
        old_qty = _as_decimal(
            existing.get("returned_qty"), table_name=table_name,
            record_id=record_id, field="returned_qty",
        )
        new_qty = _as_decimal(
            projection.get("returned_qty", old_qty), table_name=table_name,
            record_id=record_id, field="returned_qty",
        )
        sold_qty = _as_decimal(
            existing.get("qty"), table_name=table_name,
            record_id=record_id, field="qty",
        )
        if new_qty < old_qty or new_qty > sold_qty:
            raise SyncConflictError(
                table_name, record_id,
                "returned_qty must advance monotonically and cannot exceed sold qty",
            )

    elif table_name == "employee_payrolls":
        money_fields = {
            "gross_salary", "bonus", "overtime", "deductions", "advances",
            "goods_credit", "employee_repayments", "goods_returns",
            "carried_debt_offset", "amount_paid", "net_payable",
        }
        for field in money_fields:
            value = projection.get(field, existing.get(field))
            if _as_decimal(value, table_name=table_name, record_id=record_id, field=field) < 0:
                raise SyncConflictError(table_name, record_id, f"{field} cannot be negative")
        old_accrual = existing.get("accrual_entry_id")
        new_accrual = projection.get("accrual_entry_id", old_accrual)
        old_generator = existing.get("generated_by")
        new_generator = projection.get("generated_by", old_generator)
        if new_generator != old_generator and _canonical_value(new_accrual, "INTEGER") == _canonical_value(old_accrual, "INTEGER"):
            raise SyncConflictError(table_name, record_id, "generated_by can change only during payroll regeneration")
        old_carried = _as_decimal(
            existing.get("carried_debt_offset"), table_name=table_name,
            record_id=record_id, field="carried_debt_offset",
        )
        new_carried = _as_decimal(
            projection.get("carried_debt_offset", old_carried), table_name=table_name,
            record_id=record_id, field="carried_debt_offset",
        )
        if new_carried != old_carried:
            old_status = str(existing.get("status") or "").lower()
            if old_status != "voided" or _canonical_value(new_accrual, "INTEGER") == _canonical_value(old_accrual, "INTEGER"):
                raise SyncConflictError(
                    table_name, record_id,
                    "carried debt can change only during explicit payroll regeneration",
                )

    elif table_name == "employee_ledger_entries":
        old_reversed = _as_bool(existing.get("is_reversed"))
        new_reversed = _as_bool(projection.get("is_reversed", old_reversed))
        if old_reversed and not new_reversed:
            raise SyncConflictError(table_name, record_id, "a reversed ledger entry cannot be reopened")
        old_reason = existing.get("reversal_reason")
        new_reason = projection.get("reversal_reason", old_reason)
        if old_reason not in (None, "") and new_reason != old_reason:
            raise SyncConflictError(table_name, record_id, "reversal_reason cannot be replaced")
        if new_reversed and new_reason in (None, ""):
            raise SyncConflictError(table_name, record_id, "reversal requires a reason")
        if not old_reversed and new_reversed:
            reversal = cursor.execute("""
                SELECT employee_id, signed_amount
                FROM employee_ledger_entries
                WHERE reversal_of_id = ? AND transaction_type = 'REVERSAL'
            """, (record_id,)).fetchone()
            if not reversal:
                raise SyncConflictError(table_name, record_id, "reversal projection has no linked REVERSAL entry")
            if (
                _canonical_value(reversal[0], "INTEGER")
                != _canonical_value(existing.get("employee_id"), "INTEGER")
                or _as_decimal(
                    reversal[1], table_name=table_name, record_id=record_id, field="reversal signed_amount"
                ) != -_as_decimal(
                    existing.get("signed_amount"), table_name=table_name, record_id=record_id, field="signed_amount"
                )
            ):
                raise SyncConflictError(table_name, record_id, "linked REVERSAL entry does not offset this ledger row")
        old_payroll_id = existing.get("payroll_id")
        new_payroll_id = projection.get("payroll_id", old_payroll_id)
        if old_payroll_id is not None and _canonical_value(new_payroll_id, "INTEGER") != _canonical_value(old_payroll_id, "INTEGER"):
            raise SyncConflictError(table_name, record_id, "ledger payroll link cannot be removed or replaced")
        if old_payroll_id is None and new_payroll_id is not None:
            payroll = cursor.execute(
                "SELECT employee_id, salary_period FROM employee_payrolls WHERE id = ?",
                (new_payroll_id,),
            ).fetchone()
            if not payroll:
                raise SyncConflictError(table_name, record_id, "ledger payroll link target is missing")
            if _canonical_value(payroll[0], "INTEGER") != _canonical_value(existing.get("employee_id"), "INTEGER"):
                raise SyncConflictError(table_name, record_id, "ledger payroll link belongs to another employee")
            entry_period = existing.get("salary_period")
            if entry_period and str(entry_period) != str(payroll[1]):
                raise SyncConflictError(table_name, record_id, "ledger payroll link belongs to another salary period")
        old_cash_id = existing.get("cash_transaction_id")
        new_cash_id = projection.get("cash_transaction_id", old_cash_id)
        if old_cash_id is not None and _canonical_value(new_cash_id, "INTEGER") != _canonical_value(old_cash_id, "INTEGER"):
            raise SyncConflictError(table_name, record_id, "ledger cash link cannot be removed or replaced")
        if old_cash_id is None and new_cash_id is not None:
            cash_row = cursor.execute(
                "SELECT reference_type, reference_id, amount FROM cash_transactions WHERE id = ?",
                (new_cash_id,),
            ).fetchone()
            if not cash_row:
                raise SyncConflictError(table_name, record_id, "ledger cash link target is missing")
            if _as_decimal(
                cash_row[2], table_name=table_name, record_id=record_id, field="cash amount"
            ) != _as_decimal(
                existing.get("amount"), table_name=table_name, record_id=record_id, field="ledger amount"
            ):
                raise SyncConflictError(table_name, record_id, "ledger cash link amount is inconsistent")

    elif table_name == "employee_salary_payments":
        old_reversed = _as_bool(existing.get("is_reversed"))
        new_reversed = _as_bool(projection.get("is_reversed", old_reversed))
        if old_reversed and not new_reversed:
            raise SyncConflictError(table_name, record_id, "a reversed salary payment cannot be reopened")
        old_link = existing.get("reversed_by_entry_id")
        new_link = projection.get("reversed_by_entry_id", old_link)
        if old_link is not None and _canonical_value(new_link, "INTEGER") != _canonical_value(old_link, "INTEGER"):
            raise SyncConflictError(table_name, record_id, "reversed_by_entry_id cannot be replaced")
        old_reason = existing.get("reversal_reason")
        new_reason = projection.get("reversal_reason", old_reason)
        if old_reason not in (None, "") and new_reason != old_reason:
            raise SyncConflictError(table_name, record_id, "reversal_reason cannot be replaced")
        if new_reversed and (new_link is None or new_reason in (None, "")):
            raise SyncConflictError(table_name, record_id, "salary payment reversal requires its ledger link and reason")
        if not old_reversed and new_reversed:
            reversal = cursor.execute("""
                SELECT employee_id, reversal_of_id, transaction_type
                FROM employee_ledger_entries WHERE id = ?
            """, (new_link,)).fetchone()
            if not reversal or str(reversal[2] or "").upper() != "REVERSAL":
                raise SyncConflictError(table_name, record_id, "salary payment reversal ledger row is missing")
            if (
                _canonical_value(reversal[0], "INTEGER")
                != _canonical_value(existing.get("employee_id"), "INTEGER")
                or _canonical_value(reversal[1], "INTEGER")
                != _canonical_value(existing.get("ledger_entry_id"), "INTEGER")
            ):
                raise SyncConflictError(table_name, record_id, "salary payment reversal link is inconsistent")

    elif table_name == "sale_returns":
        old_cash_id = existing.get("cash_transaction_id")
        new_cash_id = projection.get("cash_transaction_id", old_cash_id)
        if old_cash_id is not None and _canonical_value(new_cash_id, "INTEGER") != _canonical_value(old_cash_id, "INTEGER"):
            raise SyncConflictError(table_name, record_id, "sale-return cash link cannot be removed or replaced")
        if old_cash_id is None and new_cash_id is not None:
            cash_row = cursor.execute("""
                SELECT tx_type, cash_out_type, amount, reference_type, reference_id
                FROM cash_transactions WHERE id = ?
            """, (new_cash_id,)).fetchone()
            if not cash_row:
                raise SyncConflictError(table_name, record_id, "sale-return cash link target is missing")
            if (
                str(cash_row[0] or "").lower() != "cash_out"
                or str(cash_row[1] or "").lower() != "refund"
                or str(cash_row[3] or "").lower() != "sale_return"
                or _canonical_value(cash_row[4], "INTEGER") != _canonical_value(record_id, "INTEGER")
                or _as_decimal(
                    cash_row[2], table_name=table_name, record_id=record_id, field="cash refund amount"
                ) > _as_decimal(
                    existing.get("refund_amount"), table_name=table_name,
                    record_id=record_id, field="refund_amount",
                )
            ):
                raise SyncConflictError(table_name, record_id, "sale-return cash link is inconsistent")

    elif table_name == "employee_goods_credits":
        old_amount = _as_decimal(existing.get("returned_amount"), table_name=table_name, record_id=record_id, field="returned_amount")
        new_amount = _as_decimal(projection.get("returned_amount", old_amount), table_name=table_name, record_id=record_id, field="returned_amount")
        total = _as_decimal(existing.get("total"), table_name=table_name, record_id=record_id, field="total")
        if new_amount < old_amount or new_amount > total:
            raise SyncConflictError(table_name, record_id, "returned_amount must advance monotonically and cannot exceed total")
        ranks = {"active": 0, "partially_returned": 1, "returned": 2}
        old_status = str(existing.get("status") or "active").lower()
        new_status = str(projection.get("status", old_status) or "").lower()
        if old_status not in ranks or new_status not in ranks or ranks[new_status] < ranks[old_status]:
            raise SyncConflictError(table_name, record_id, "goods-credit status cannot move backwards")
        expected = "active" if new_amount == 0 else ("returned" if new_amount == total else "partially_returned")
        if new_status != expected:
            raise SyncConflictError(table_name, record_id, "goods-credit status does not match returned_amount")
    elif table_name == "employee_goods_credit_items":
        old_qty = _as_decimal(existing.get("returned_qty"), table_name=table_name, record_id=record_id, field="returned_qty")
        new_qty = _as_decimal(projection.get("returned_qty", old_qty), table_name=table_name, record_id=record_id, field="returned_qty")
        total_qty = _as_decimal(existing.get("qty"), table_name=table_name, record_id=record_id, field="qty")
        if new_qty < old_qty or new_qty > total_qty:
            raise SyncConflictError(table_name, record_id, "returned_qty must advance monotonically and cannot exceed qty")

    if table_name == "employee_payrolls":
        # Monetary/status fields are rebuilt after the complete sync batch from
        # linked immutable ledger/payment rows. Only regeneration metadata and
        # its one-way accrual link cross the trust boundary directly.
        return {
            key: value for key, value in projection.items()
            if key in {
                "carried_debt_offset", "accrual_entry_id",
                "generated_by", "updated_at",
            }
        }
    return projection


def _employee_for_sync_row(cursor, table_name: str, record_id, data: dict, existing: dict):
    if table_name == "employees":
        return record_id
    if table_name in {
        "employee_ledger_entries", "employee_payrolls",
        "employee_salary_payments", "employee_goods_credits",
    }:
        return data.get("employee_id", existing.get("employee_id") if existing else None)
    if table_name == "employee_goods_credit_items":
        credit_id = data.get("goods_credit_id", existing.get("goods_credit_id") if existing else None)
        if credit_id is not None:
            row = cursor.execute(
                "SELECT employee_id FROM employee_goods_credits WHERE id = ?", (credit_id,)
            ).fetchone()
            return row[0] if row else None
    if table_name == "sale_returns":
        sale_id = data.get("sale_id", existing.get("sale_id") if existing else None)
        if sale_id is not None:
            row = cursor.execute("SELECT employee_id FROM sales WHERE id = ?", (sale_id,)).fetchone()
            return row[0] if row else None
    if table_name == "sale_return_items":
        return_id = data.get("sale_return_id", existing.get("sale_return_id") if existing else None)
        if return_id is not None:
            row = cursor.execute("""
                SELECT s.employee_id
                FROM sale_returns sr JOIN sales s ON s.id = sr.sale_id
                WHERE sr.id = ?
            """, (return_id,)).fetchone()
            return row[0] if row else None
    return None


def _mark_sync_validation_targets(
    cursor,
    table_name: str,
    record_id,
    data: dict,
    existing: dict,
    validation_state: dict,
):
    if validation_state is None:
        return
    payroll_ids = validation_state.setdefault("payroll_ids", set())
    sale_item_ids = validation_state.setdefault("sale_item_ids", set())
    sale_ids = validation_state.setdefault("sale_ids", set())
    goods_credit_ids = validation_state.setdefault("goods_credit_ids", set())
    new_ledger_entries = validation_state.setdefault("new_ledger_entries", [])
    starting_balances = validation_state.setdefault("starting_balances", {})

    if table_name == "employee_payrolls":
        payroll_ids.add(int(record_id))
    elif table_name in {"employee_ledger_entries", "employee_salary_payments"}:
        payroll_id = data.get("payroll_id", existing.get("payroll_id") if existing else None)
        if payroll_id is not None:
            payroll_ids.add(int(payroll_id))

    if table_name == "sale_items":
        sale_item_ids.add(int(record_id))
        sale_id = data.get("sale_id", existing.get("sale_id") if existing else None)
        if sale_id is not None:
            sale_ids.add(int(sale_id))
    elif table_name in {"sales", "sale_returns"}:
        sale_id = record_id if table_name == "sales" else data.get(
            "sale_id", existing.get("sale_id") if existing else None
        )
        if sale_id is not None:
            sale_ids.add(int(sale_id))
    elif table_name == "sale_return_items":
        sale_item_id = data.get("sale_item_id", existing.get("sale_item_id") if existing else None)
        if sale_item_id is not None:
            sale_item_ids.add(int(sale_item_id))
            row = cursor.execute(
                "SELECT sale_id FROM sale_items WHERE id = ?", (sale_item_id,)
            ).fetchone()
            if row:
                sale_ids.add(int(row[0]))
    elif table_name == "cash_transactions" and str(
        data.get("reference_type", existing.get("reference_type") if existing else "") or ""
    ).lower() == "sale_return":
        return_id = data.get("reference_id", existing.get("reference_id") if existing else None)
        if return_id is not None:
            row = cursor.execute(
                "SELECT sale_id FROM sale_returns WHERE id = ?", (return_id,)
            ).fetchone()
            if row:
                sale_ids.add(int(row[0]))

    if table_name == "employee_goods_credits":
        goods_credit_ids.add(int(record_id))
    elif table_name == "employee_goods_credit_items":
        credit_id = data.get("goods_credit_id", existing.get("goods_credit_id") if existing else None)
        if credit_id is not None:
            goods_credit_ids.add(int(credit_id))
    elif table_name == "employee_ledger_entries" and str(
        data.get("transaction_type", existing.get("transaction_type") if existing else "") or ""
    ).upper() in {"GOODS_ON_CREDIT", "GOODS_RETURN"}:
        sale_id = data.get("sale_id", existing.get("sale_id") if existing else None)
        if sale_id is not None:
            row = cursor.execute(
                "SELECT id FROM employee_goods_credits WHERE sale_id = ?", (sale_id,)
            ).fetchone()
            if row:
                goods_credit_ids.add(int(row[0]))

    if table_name == "employee_ledger_entries" and existing is None:
        employee_id = data.get("employee_id")
        if employee_id is not None:
            employee_id = int(employee_id)
            if employee_id not in starting_balances:
                row = cursor.execute(
                    "SELECT current_balance FROM employees WHERE id = ?", (employee_id,)
                ).fetchone()
                starting_balances[employee_id] = row[0] if row else 0
            new_ledger_entries.append(int(record_id))


def apply_sync_change(
    cursor,
    change: dict,
    touched_employee_ids: set,
    validation_state: dict = None,
):
    """Apply one validated row without committing the surrounding batch."""
    if not isinstance(change, dict):
        raise ValueError("Each sync item must be an object")
    try:
        table_name = str(change["table_name"])
        record_id = change["record_id"]
        action = str(change["action"]).upper()
        data = change.get("data")
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed synchronization item") from exc

    if table_name not in SYNC_TABLES:
        raise ValueError("Table is not allowed for synchronization")
    if action not in {"DELETE", "INSERT", "UPDATE"}:
        raise ValueError("Unsupported synchronization action")
    if data is not None and not isinstance(data, dict):
        raise ValueError("Sync item data must be an object")

    pk = "key" if table_name == "settings" else "id"
    columns = _table_columns(cursor, table_name)
    if not columns or pk not in columns:
        raise SyncConflictError(table_name, record_id, "destination schema is missing the synchronized table or primary key")

    existing = _fetch_existing_row(cursor, table_name, pk, record_id, columns)
    if action == "DELETE":
        if not sync_delete_allowed(cursor, table_name, record_id):
            return False
        cursor.execute(
            f"DELETE FROM {_quoted(table_name)} WHERE {_quoted(pk)} = ?", (record_id,)
        )
        return True

    if not data:
        raise SyncConflictError(table_name, record_id, f"{action} requires row data")

    filtered = {key: value for key, value in data.items() if key in columns}
    ignored = NON_AUTHORITATIVE_SYNC_FIELDS.get(table_name, frozenset())
    filtered = {key: value for key, value in filtered.items() if key not in ignored}
    incoming_pk = filtered.get(pk, record_id)
    if _canonical_value(incoming_pk, columns.get(pk), pk) != _canonical_value(record_id, columns.get(pk), pk):
        raise SyncConflictError(table_name, record_id, f"payload {pk} does not match record_id")
    filtered[pk] = record_id

    if action == "INSERT" and existing:
        _assert_same_fields(
            table_name, record_id, filtered, existing, columns,
            excluded=frozenset({pk}) | REPLAY_DERIVED_FIELDS.get(table_name, frozenset()),
        )
        employee_id = _employee_for_sync_row(cursor, table_name, record_id, filtered, existing)
        if employee_id is not None:
            touched_employee_ids.add(int(employee_id))
        _mark_sync_validation_targets(
            cursor, table_name, record_id, filtered, existing, validation_state
        )
        return False

    if action == "UPDATE" and not existing:
        raise SyncConflictError(table_name, record_id, "UPDATE target does not exist")

    if existing and _is_immutable_financial_row(table_name, filtered, existing):
        allowed = MONOTONIC_SYNC_FIELDS.get(table_name, frozenset())
        _assert_same_fields(
            table_name, record_id, filtered, existing, columns,
            excluded=frozenset({pk}) | allowed,
        )
        update_data = _validate_monotonic_projection(cursor, table_name, record_id, filtered, existing)
    else:
        update_data = {key: value for key, value in filtered.items() if key != pk}

    if existing:
        if update_data:
            set_clause = ", ".join(f"{_quoted(key)} = ?" for key in update_data)
            cursor.execute(
                f"UPDATE {_quoted(table_name)} SET {set_clause} WHERE {_quoted(pk)} = ?",
                [update_data[key] for key in update_data] + [record_id],
            )
    else:
        # Derived balances never cross the trust boundary.  Seed their required
        # storage columns, then rebuild them from immutable ledger movements.
        if table_name == "employees" and "current_balance" in columns:
            filtered["current_balance"] = 0
        if table_name == "employee_ledger_entries" and "balance_after" in columns:
            filtered["balance_after"] = 0
        names = list(filtered)
        cursor.execute(
            f"INSERT INTO {_quoted(table_name)} "
            f"({', '.join(_quoted(name) for name in names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [filtered[name] for name in names],
        )

    employee_id = _employee_for_sync_row(cursor, table_name, record_id, filtered, existing)
    if employee_id is not None:
        touched_employee_ids.add(int(employee_id))
    _mark_sync_validation_targets(
        cursor, table_name, record_id, filtered, existing, validation_state
    )
    return True


def validate_sync_financial_invariants(cursor, validation_state: dict):
    """Validate cross-row invariants after every row in the batch is visible."""
    validation_state = validation_state or {}
    quantity_places = Decimal("0.0001")
    money_places = Decimal("0.01")

    for sale_item_id in sorted(validation_state.get("sale_item_ids", set())):
        item = cursor.execute(
            "SELECT sale_id, qty FROM sale_items WHERE id = ?", (sale_item_id,)
        ).fetchone()
        if not item:
            continue
        sold_qty = _as_decimal(
            item[1], table_name="sale_items", record_id=sale_item_id, field="qty"
        ).quantize(quantity_places)
        returned_qty = _as_decimal(
            cursor.execute(
                "SELECT COALESCE(SUM(qty), 0) FROM sale_return_items WHERE sale_item_id = ?",
                (sale_item_id,),
            ).fetchone()[0],
            table_name="sale_items", record_id=sale_item_id, field="cumulative returned qty",
        ).quantize(quantity_places)
        if returned_qty > sold_qty:
            raise SyncConflictError(
                "sale_items", sale_item_id,
                f"cumulative returned qty {returned_qty} exceeds sold qty {sold_qty}",
            )
        cursor.execute(
            "UPDATE sale_items SET returned_qty = ? WHERE id = ?",
            (str(returned_qty), sale_item_id),
        )

    for sale_id in sorted(validation_state.get("sale_ids", set())):
        sale = cursor.execute(
            "SELECT paid_amount, status FROM sales WHERE id = ?", (sale_id,)
        ).fetchone()
        if not sale:
            continue
        if str(sale[1] or "").lower() != "held":
            quantities = cursor.execute("""
                SELECT qty, returned_qty FROM sale_items WHERE sale_id = ?
            """, (sale_id,)).fetchall()
            any_returned = any(
                _as_decimal(returned, table_name="sales", record_id=sale_id, field="returned_qty") > 0
                for _qty, returned in quantities
            )
            all_returned = bool(quantities) and all(
                _as_decimal(returned, table_name="sales", record_id=sale_id, field="returned_qty")
                >= _as_decimal(qty, table_name="sales", record_id=sale_id, field="qty")
                for qty, returned in quantities
            )
            derived_status = "returned" if all_returned else ("partially_returned" if any_returned else "completed")
            cursor.execute("UPDATE sales SET status = ? WHERE id = ?", (derived_status, sale_id))
        cash_refunded = Decimal("0.00")
        linked_refunds = cursor.execute("""
            SELECT sr.id, sr.refund_amount, ct.id, ct.tx_type, ct.cash_out_type,
                   ct.amount, ct.reference_type, ct.reference_id
            FROM sale_returns sr
            JOIN cash_transactions ct ON ct.id = sr.cash_transaction_id
            WHERE sr.sale_id = ? AND sr.cash_transaction_id IS NOT NULL
        """, (sale_id,)).fetchall()
        for return_id, refund_amount, _cash_id, tx_type, out_type, amount, ref_type, ref_id in linked_refunds:
            cash_amount = _as_decimal(
                amount, table_name="sale_returns", record_id=return_id,
                field="cash refund amount",
            ).quantize(money_places)
            if (
                str(tx_type or "").lower() != "cash_out"
                or str(out_type or "").lower() != "refund"
                or str(ref_type or "").lower() != "sale_return"
                or _canonical_value(ref_id, "INTEGER") != int(return_id)
                or cash_amount > _as_decimal(
                    refund_amount, table_name="sale_returns", record_id=return_id,
                    field="refund_amount",
                ).quantize(money_places)
            ):
                raise SyncConflictError(
                    "sale_returns", return_id,
                    "linked refund cash transaction is inconsistent",
                )
            cash_refunded += cash_amount
        paid_amount = _as_decimal(
            sale[0], table_name="sales", record_id=sale_id, field="paid_amount",
        ).quantize(money_places)
        if cash_refunded > paid_amount:
            raise SyncConflictError(
                "sales", sale_id,
                f"cumulative cash refunds {cash_refunded} exceed collected tender {paid_amount}",
            )

    for credit_id in sorted(validation_state.get("goods_credit_ids", set())):
        credit = cursor.execute("""
            SELECT employee_id, sale_id, total
            FROM employee_goods_credits WHERE id = ?
        """, (credit_id,)).fetchone()
        if not credit:
            continue
        returned_amount = _as_decimal(
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM employee_ledger_entries
                WHERE employee_id = ? AND sale_id = ? AND transaction_type = 'GOODS_RETURN'
            """, (credit[0], credit[1])).fetchone()[0],
            table_name="employee_goods_credits", record_id=credit_id,
            field="cumulative goods returns",
        ).quantize(money_places)
        total = _as_decimal(
            credit[2], table_name="employee_goods_credits",
            record_id=credit_id, field="total",
        ).quantize(money_places)
        if returned_amount > total:
            raise SyncConflictError(
                "employee_goods_credits", credit_id,
                f"cumulative goods returns {returned_amount} exceed credit total {total}",
            )
        derived_status = (
            "active" if returned_amount == 0
            else ("returned" if returned_amount == total else "partially_returned")
        )
        cursor.execute(
            "UPDATE employee_goods_credits SET returned_amount = ?, status = ? WHERE id = ?",
            (str(returned_amount), derived_status, credit_id),
        )
        credit_items = cursor.execute("""
            SELECT id, product_id, qty
            FROM employee_goods_credit_items WHERE goods_credit_id = ?
        """, (credit_id,)).fetchall()
        for credit_item_id, product_id, sold_qty in credit_items:
            if product_id is None:
                continue
            returned_qty = _as_decimal(
                cursor.execute("""
                    SELECT COALESCE(SUM(sri.qty), 0)
                    FROM sale_return_items sri
                    JOIN sale_returns sr ON sr.id = sri.sale_return_id
                    JOIN sale_items si ON si.id = sri.sale_item_id
                    WHERE sr.sale_id = ? AND si.product_id = ?
                """, (credit[1], product_id)).fetchone()[0],
                table_name="employee_goods_credit_items",
                record_id=credit_item_id, field="cumulative returned qty",
            ).quantize(quantity_places)
            item_qty = _as_decimal(
                sold_qty, table_name="employee_goods_credit_items",
                record_id=credit_item_id, field="qty",
            ).quantize(quantity_places)
            if returned_qty > item_qty:
                raise SyncConflictError(
                    "employee_goods_credit_items", credit_item_id,
                    f"cumulative returned qty {returned_qty} exceeds credited qty {item_qty}",
                )
            cursor.execute(
                "UPDATE employee_goods_credit_items SET returned_qty = ? WHERE id = ?",
                (str(returned_qty), credit_item_id),
            )

    # Domain CAS rules are checked only for rows newly accepted in this batch,
    # against the trusted pre-batch balance and server receive order. Historical
    # rows must not be re-judged using a later credit limit or skewed client time.
    balances = {
        int(employee_id): _as_decimal(
            value, table_name="employees", record_id=employee_id,
            field="pre-batch current_balance",
        )
        for employee_id, value in validation_state.get("starting_balances", {}).items()
    }
    for entry_id in validation_state.get("new_ledger_entries", []):
        row = cursor.execute("""
            SELECT employee_id, transaction_type, signed_amount
            FROM employee_ledger_entries WHERE id = ?
        """, (entry_id,)).fetchone()
        if not row:
            continue
        employee_id, tx_type = int(row[0]), str(row[1] or "").upper()
        movement = _as_decimal(
            row[2], table_name="employee_ledger_entries",
            record_id=entry_id, field="signed_amount",
        )
        previous = balances.setdefault(employee_id, Decimal("0"))
        current = previous + movement
        if tx_type in {"GOODS_ON_CREDIT", "CASH_ADVANCE"}:
            limit_row = cursor.execute(
                "SELECT credit_limit FROM employees WHERE id = ?", (employee_id,)
            ).fetchone()
            limit = _as_decimal(
                limit_row[0] if limit_row else 0, table_name="employees",
                record_id=employee_id, field="credit_limit",
            )
            if limit <= 0 or current < -limit:
                raise SyncConflictError(
                    "employee_ledger_entries", entry_id,
                    f"employee credit/debt limit {limit} would be exceeded or is disabled",
                )
        if tx_type == "SALARY_PAYMENT" and (previous <= 0 or current < 0):
            raise SyncConflictError(
                "employee_ledger_entries", entry_id,
                "salary payment exceeds the pre-batch employee payable balance",
            )
        if tx_type == "EMPLOYEE_REPAYMENT" and (previous >= 0 or current > 0):
            raise SyncConflictError(
                "employee_ledger_entries", entry_id,
                "employee repayment exceeds the pre-batch employee debt balance",
            )
        balances[employee_id] = current

    for payroll_id in sorted(validation_state.get("payroll_ids", set())):
        columns = _table_columns(cursor, "employee_payrolls")
        if not columns:
            continue
        payroll = _fetch_existing_row(cursor, "employee_payrolls", "id", payroll_id, columns)
        if not payroll:
            raise SyncConflictError("employee_payrolls", payroll_id, "linked payroll row is missing")

        sums = {
            "gross_salary": Decimal("0"),
            "bonus": Decimal("0"),
            "overtime": Decimal("0"),
            "deductions": Decimal("0"),
            "advances": Decimal("0"),
            "goods_credit": Decimal("0"),
            "employee_repayments": Decimal("0"),
            "goods_returns": Decimal("0"),
        }
        active_accrual_ids = []
        ledger_rows = cursor.execute("""
            SELECT id, transaction_type, amount, signed_amount
            FROM employee_ledger_entries
            WHERE payroll_id = ? AND COALESCE(is_reversed, 0) = 0
              AND transaction_type != 'REVERSAL'
        """, (payroll_id,)).fetchall()
        mapping = {
            "SALARY_ACCRUAL": "gross_salary",
            "BONUS": "bonus",
            "OVERTIME": "overtime",
            "DEDUCTION": "deductions",
            "CASH_ADVANCE": "advances",
            "GOODS_ON_CREDIT": "goods_credit",
            "EMPLOYEE_REPAYMENT": "employee_repayments",
            "GOODS_RETURN": "goods_returns",
        }
        for entry_id, tx_type, amount, signed_amount in ledger_rows:
            tx_type = str(tx_type or "").upper()
            amount_value = _as_decimal(
                amount, table_name="employee_payrolls", record_id=payroll_id,
                field=f"ledger {tx_type} amount",
            )
            if tx_type in mapping:
                sums[mapping[tx_type]] += amount_value
            elif tx_type == "MANUAL_ADJUSTMENT":
                if _as_decimal(
                    signed_amount, table_name="employee_payrolls", record_id=payroll_id,
                    field="manual adjustment signed_amount",
                ) > 0:
                    sums["bonus"] += amount_value
                else:
                    sums["deductions"] += amount_value
            if tx_type == "SALARY_ACCRUAL":
                active_accrual_ids.append(int(entry_id))

        if len(active_accrual_ids) > 1:
            raise SyncConflictError(
                "employee_payrolls", payroll_id,
                "more than one active salary accrual exists for this payroll",
            )

        active_paid = _as_decimal(
            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM employee_salary_payments
                WHERE payroll_id = ? AND COALESCE(is_reversed, 0) = 0
            """, (payroll_id,)).fetchone()[0],
            table_name="employee_payrolls", record_id=payroll_id,
            field="active salary payments",
        ).quantize(money_places)
        for field in sums:
            sums[field] = sums[field].quantize(money_places)

        carried = _as_decimal(
            payroll.get("carried_debt_offset"), table_name="employee_payrolls",
            record_id=payroll_id, field="carried_debt_offset",
        ).quantize(money_places)
        raw_net = (
            sums["gross_salary"] + sums["bonus"] + sums["overtime"]
            + sums["employee_repayments"] + sums["goods_returns"]
            - sums["deductions"] - sums["advances"] - sums["goods_credit"]
            - carried
        )
        net_payable = max(Decimal("0.00"), raw_net).quantize(money_places)

        old_accrual_id = payroll.get("accrual_entry_id")
        old_accrual = None
        if old_accrual_id is not None:
            old_accrual = cursor.execute("""
                SELECT employee_id, payroll_id, transaction_type, is_reversed
                FROM employee_ledger_entries WHERE id = ?
            """, (old_accrual_id,)).fetchone()
        is_voided = bool(
            not active_accrual_ids
            and old_accrual
            and str(old_accrual[2] or "").upper() == "SALARY_ACCRUAL"
            and _as_bool(old_accrual[3])
        )

        if is_voided:
            if active_paid != 0:
                raise SyncConflictError(
                    "employee_payrolls", payroll_id,
                    "voided payroll still has active salary payments",
                )
            net_payable = Decimal("0.00")
            status = "voided"
            accrual_entry_id = old_accrual_id
        else:
            if active_paid > net_payable:
                raise SyncConflictError(
                    "employee_payrolls", payroll_id,
                    f"salary payments {active_paid} exceed net payable {net_payable}",
                )
            status = (
                "settled" if net_payable - active_paid <= 0
                else ("partial" if active_paid > 0 else "unpaid")
            )
            accrual_entry_id = active_accrual_ids[0] if active_accrual_ids else None

        derived = {
            **sums,
            "amount_paid": active_paid,
            "net_payable": net_payable,
            "status": status,
            "accrual_entry_id": accrual_entry_id,
        }
        set_clause = ", ".join(f"{_quoted(field)} = ?" for field in derived)
        cursor.execute(
            f"UPDATE employee_payrolls SET {set_clause} WHERE id = ?",
            [str(value) if isinstance(value, Decimal) else value for value in derived.values()]
            + [payroll_id],
        )


def reconcile_touched_employee_balances(cursor, employee_ids):
    """Validate and rebuild balances from the immutable ledger transaction."""
    if not employee_ids:
        return
    employee_columns = _table_columns(cursor, "employees")
    ledger_columns = _table_columns(cursor, "employee_ledger_entries")
    if "current_balance" not in employee_columns or not {"employee_id", "signed_amount"} <= set(ledger_columns):
        return

    for employee_id in sorted({int(value) for value in employee_ids if value is not None}):
        order_by = "created_at, id" if "created_at" in ledger_columns else "id"
        employee = cursor.execute(
            "SELECT 1 FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if not employee:
            raise SyncConflictError("employees", employee_id, "ledger employee row is missing")
        rows = cursor.execute(
            "SELECT id, transaction_type, amount, signed_amount, reversal_of_id "
            "FROM employee_ledger_entries "
            f"WHERE employee_id = ? ORDER BY {order_by}",
            (employee_id,),
        ).fetchall()
        running = Decimal("0")
        has_balance_after = "balance_after" in ledger_columns
        positive_types = {
            "SALARY_ACCRUAL", "BONUS", "OVERTIME",
            "EMPLOYEE_REPAYMENT", "GOODS_RETURN",
        }
        negative_types = {
            "SALARY_PAYMENT", "CASH_ADVANCE",
            "GOODS_ON_CREDIT", "DEDUCTION",
        }
        flexible_types = {"OPENING_BALANCE", "MANUAL_ADJUSTMENT", "REVERSAL"}
        for entry_id, transaction_type, amount, signed_amount, reversal_of_id in rows:
            transaction_type = str(transaction_type or "").upper()
            amount_value = _as_decimal(
                amount,
                table_name="employee_ledger_entries",
                record_id=entry_id,
                field="amount",
            )
            movement = _as_decimal(
                signed_amount,
                table_name="employee_ledger_entries",
                record_id=entry_id,
                field="signed_amount",
            )
            if amount_value <= 0 or movement == 0 or abs(movement) != amount_value:
                raise SyncConflictError(
                    "employee_ledger_entries", entry_id,
                    "amount and signed_amount do not form one valid ledger movement",
                )
            if (
                (transaction_type in positive_types and movement <= 0)
                or (transaction_type in negative_types and movement >= 0)
            ):
                raise SyncConflictError(
                    "employee_ledger_entries", entry_id,
                    f"{transaction_type} has an invalid balance direction",
                )
            if transaction_type not in positive_types | negative_types | flexible_types:
                raise SyncConflictError(
                    "employee_ledger_entries", entry_id,
                    f"unsupported employee ledger transaction type {transaction_type}",
                )
            if transaction_type == "REVERSAL":
                original = cursor.execute(
                    "SELECT employee_id, amount, signed_amount FROM employee_ledger_entries WHERE id = ?",
                    (reversal_of_id,),
                ).fetchone()
                if (
                    not original
                    or _canonical_value(original[0], "INTEGER") != employee_id
                    or _as_decimal(
                        original[1], table_name="employee_ledger_entries",
                        record_id=entry_id, field="reversed amount",
                    ) != amount_value
                    or _as_decimal(
                        original[2], table_name="employee_ledger_entries",
                        record_id=entry_id, field="reversed signed_amount",
                    ) != -movement
                ):
                    raise SyncConflictError(
                        "employee_ledger_entries", entry_id,
                        "REVERSAL does not exactly offset its linked ledger entry",
                    )
            running += movement
            if has_balance_after:
                cursor.execute(
                    "UPDATE employee_ledger_entries SET balance_after = ? "
                    "WHERE id = ? AND (balance_after IS NULL OR balance_after != ?)",
                    (str(running), entry_id, str(running)),
                )
        cursor.execute(
            "UPDATE employees SET current_balance = ? "
            "WHERE id = ? AND (current_balance IS NULL OR current_balance != ?)",
            (str(running), employee_id, str(running)),
        )


def persist_sync_conflict(conn, conflict: SyncConflictError, *, direction: str):
    """Best-effort durable audit written only after the failed batch rollback."""
    try:
        cursor = conn.cursor()
        tables = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'"
        ).fetchone()
        if not tables:
            return
        try:
            entity_id = int(conflict.record_id)
        except (TypeError, ValueError):
            entity_id = None
        payload = json.dumps({
            "table": conflict.table_name,
            "record_id": conflict.record_id,
            "error": conflict.error,
            "direction": direction,
        }, sort_keys=True)
        cursor.execute("""
            INSERT INTO audit_log
                (action, entity_type, entity_id, old_value, new_value, user, ip_note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "SYNC_CONFLICT", conflict.table_name, entity_id, None, payload,
            "sync", f"sync:{direction}"[:100],
        ))
        conn.commit()
    except Exception as audit_error:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[SyncManager] Failed to persist sync conflict audit: {audit_error}")

class SyncManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        # A real Event lets stop() wake the 15-second polling wait immediately.
        # More importantly, stop() does not return while the worker may still
        # own a SQLite connection during an atomic database restore.
        self._stop_event = threading.Event()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def setup_database_triggers(self, *, strict: bool = False):
        """Create the sync queue and all persistent capture triggers."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # 1. Create sync_queue table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                record_id TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # Fresh installs and old backups may not have had sync_queue when
            # database.init_db first attempted trigger registration. Register
            # again now, on the same connection, after the queue exists.
            from database import register_persistent_triggers
            register_persistent_triggers(conn, raise_errors=True)
            conn.commit()
            print("[SyncManager] Database sync queue and triggers initialized.")
        except Exception as e:
            print(f"[SyncManager] Error creating sync_queue table: {e}")
            conn.rollback()
            if strict:
                raise
        finally:
            conn.close()

    def get_setting(self, key: str, default=None) -> str:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default
        except:
            return default
        finally:
            conn.close()

    def save_setting(self, key: str, value: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE sync_control SET skip_sync = 1 WHERE id = 1")
            cursor.execute("SELECT 1 FROM settings WHERE key = ?", (key,))
            if cursor.fetchone():
                cursor.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            else:
                cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
            cursor.execute("UPDATE sync_control SET skip_sync = 0 WHERE id = 1")
            conn.commit()
        except Exception as e:
            print(f"[SyncManager] Failed to save setting {key}: {e}")
            conn.rollback()
        finally:
            conn.close()

    def start(self, *, strict: bool = False):
        with self.lock:
            if self.running:
                return
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("Cannot start SyncManager while its previous worker is still stopping")

            # Trigger registration is part of startup. Restore callers use
            # strict=True so a half-initialized runtime is never reported as
            # successfully resumed.
            self.setup_database_triggers(strict=strict)
            self._stop_event.clear()
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.running = False
                self._stop_event.set()
                self.thread = None
                raise
            print("[SyncManager] Background sync thread started.")

    def stop(self, *, timeout: float = 30.0):
        with self.lock:
            self.running = False
            self._stop_event.set()
            worker = self.thread

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
            if worker.is_alive():
                raise RuntimeError(
                    "SyncManager did not quiesce before the database operation timeout"
                )

        with self.lock:
            if self.thread is worker and (worker is None or not worker.is_alive()):
                self.thread = None
        print("[SyncManager] Background sync thread stopped.")

    def _cleanup_sync_queue(self):
        """Never discard unacknowledged sync events.

        A row remains in ``sync_queue`` precisely because it has not yet been
        acknowledged by its destination (or it is retained after a financial
        conflict for manual resolution).  There is no per-client acknowledgement
        watermark in the current protocol, so age/size-based deletion could lose
        offline accounting data or starve a lagging client.  Safe compaction must
        wait for an explicit acknowledgement design.
        """
        return

    def _run_loop(self):
        loop_count = 0
        while self.running and not self._stop_event.is_set():
            try:
                cloud_url = self.get_setting("cloud_api_url")
                sync_token = self.get_setting("cloud_sync_token", "")
                
                # Only sync when both endpoint and a strong shared token are configured.
                if cloud_url and cloud_url.strip() and sync_token and len(sync_token.strip()) >= 32:
                    self._perform_sync(cloud_url.strip().rstrip('/'), sync_token)
                
                # Auto-cleanup every 100 loops (~25 minutes) to prevent DB bloat
                loop_count += 1
                if loop_count % 100 == 0:
                    self._cleanup_sync_queue()

            except Exception as e:
                print(f"[SyncManager] Unhandled exception in sync loop: {e}")
            
            # Run sync check every 15 seconds, but wake immediately when a
            # restore/shutdown requests a fully quiesced worker.
            self._stop_event.wait(15)


    def _perform_sync(self, base_url: str, token: str):
        # 1. PUSH local changes
        self._push_local_changes(base_url, token)
        # 2. PULL remote changes
        self._pull_remote_changes(base_url, token)

    def _push_local_changes(self, base_url: str, token: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Query up to 50 unsynced operations
            cursor.execute("SELECT * FROM sync_queue ORDER BY id ASC LIMIT 50")
            rows = cursor.fetchall()
            if not rows:
                return

            batch = []
            for row in rows:
                queue_id = row["id"]
                table_name = row["table_name"]
                record_id = row["record_id"]
                action = row["action"]

                if table_name not in SYNC_TABLES or (
                    table_name == "settings" and str(record_id) in SENSITIVE_SETTING_KEYS
                ):
                    cursor.execute("DELETE FROM sync_queue WHERE id = ?", (queue_id,))
                    continue

                if action == "DELETE" and table_name in PROTECTED_EMPLOYEE_SYNC_TABLES:
                    # Never propagate a physical delete for immutable posted
                    # accounting data. Domain workflows post reversals or returns.
                    cursor.execute("DELETE FROM sync_queue WHERE id = ?", (queue_id,))
                    print(f"[SyncManager] Ignored protected DELETE for {table_name}:{record_id}")
                    continue

                record_data = None
                if action in ("INSERT", "UPDATE"):
                    pk = "key" if table_name == "settings" else "id"
                    cursor.execute(f"SELECT * FROM {table_name} WHERE {pk} = ?", (record_id,))
                    record_row = cursor.fetchone()
                    if record_row:
                        record_data = dict(record_row)
                    else:
                        if table_name in PROTECTED_EMPLOYEE_SYNC_TABLES:
                            cursor.execute("DELETE FROM sync_queue WHERE id = ?", (queue_id,))
                            print(f"[SyncManager] Ignored missing protected record {table_name}:{record_id}")
                            continue
                        # Row no longer exists locally, convert to DELETE
                        action = "DELETE"

                batch.append({
                    "id": queue_id,
                    "table_name": table_name,
                    "record_id": str(record_id),
                    "action": action,
                    "data": record_data
                })

            if not batch:
                conn.commit()
                return

            # Send payload to Cloud API
            url = f"{base_url}/api/sync/push"
            req = urllib.request.Request(
                url,
                data=json.dumps(batch).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Sync-Token": token
                },
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    # Successfully pushed! Clear these queue items.
                    queue_ids = [item["id"] for item in batch]
                    placeholders = ",".join("?" for _ in queue_ids)
                    cursor.execute(f"DELETE FROM sync_queue WHERE id IN ({placeholders})", queue_ids)
                    conn.commit()
                    print(f"[SyncManager] Successfully synced {len(batch)} items to cloud.")
        except urllib.error.HTTPError as http_error:
            # A 409 is a durable business conflict, not a transient network
            # failure.  Keep the entire source queue and record why every retry
            # is being rejected instead of silently spinning forever.
            conn.rollback()
            if http_error.code == 409:
                detail = {}
                try:
                    payload = json.loads(http_error.read().decode("utf-8"))
                    detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
                except Exception:
                    detail = {}
                fallback = batch[0] if batch else {}
                conflict = SyncConflictError(
                    detail.get("table", fallback.get("table_name", "sync_batch")),
                    detail.get("record_id", fallback.get("record_id", "unknown")),
                    detail.get("message", "cloud rejected a conflicting sync batch"),
                )
                persist_sync_conflict(conn, conflict, direction="push-source")
                print(f"[SyncManager] Cloud push conflict (queue retained): {conflict}")
            else:
                print(f"[SyncManager] Push HTTP error {http_error.code}; queue retained")
        except urllib.error.URLError:
            # Expected connection drops are transient; leaving the transaction
            # uncommitted retains every queued item for the next attempt.
            pass
        except Exception as e:
            print(f"[SyncManager] Push error: {e}")
        finally:
            conn.close()

    def _pull_remote_changes(self, base_url: str, token: str):
        last_pulled_id = int(self.get_setting("sync_last_pulled_cloud_id", "0"))
        
        try:
            url = f"{base_url}/api/sync/pull?last_id={last_pulled_id}"
            req = urllib.request.Request(
                url,
                headers={"X-Sync-Token": token},
                method="GET"
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    changes = payload.get("changes", [])
                    max_id = payload.get("max_id", last_pulled_id)

                    if changes:
                        self._apply_cloud_changes(changes)
                    # Advance after a successful apply *or* a successful no-op.
                    # The cloud may filter every queued row (for example a
                    # protected tombstone) while still returning a newer
                    # max_id; failing to persist it causes an endless repull.
                    self.save_setting("sync_last_pulled_cloud_id", str(max_id))
                    print(f"[SyncManager] Applied {len(changes)} changes from cloud. Last Pulled ID: {max_id}")
        except urllib.error.URLError:
            pass
        except Exception as e:
            print(f"[SyncManager] Pull error: {e}")

    def _apply_cloud_changes(self, changes: list):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Disable capture triggers in the same transaction so pulled cloud
            # changes are not echoed back into the outbound queue.
            cursor.execute("PRAGMA defer_foreign_keys = ON")
            cursor.execute("UPDATE sync_control SET skip_sync = 1 WHERE id = 1")

            touched_employee_ids = set()
            validation_state = {}
            for change in changes:
                # Pull may contain old unsupported queue rows; retain the
                # established behavior of skipping them.  Every supported row
                # is otherwise fail-closed and shares this one transaction.
                if not isinstance(change, dict) or change.get("table_name") not in SYNC_TABLES:
                    continue
                apply_sync_change(
                    cursor, change, touched_employee_ids, validation_state
                )

            validate_sync_financial_invariants(cursor, validation_state)
            reconcile_touched_employee_balances(cursor, touched_employee_ids)

            cursor.execute("UPDATE sync_control SET skip_sync = 0 WHERE id = 1")
            conn.commit()
        except SyncConflictError as conflict:
            conn.rollback()
            persist_sync_conflict(conn, conflict, direction="pull")
            print(f"[SyncManager] Cloud pull conflict: {conflict}")
            raise
        except Exception as e:
            print(f"[SyncManager] Error applying cloud changes: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
