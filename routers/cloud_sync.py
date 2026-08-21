"""
SmartVyapar - Cloud Sync Router
Provides REST endpoints for two-way offline-first database synchronization.
"""

from fastapi import APIRouter, Depends, HTTPException, Header, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
import hmac
import json
import sqlite3
from database import get_db
from services.sync_manager import (
    PROTECTED_EMPLOYEE_SYNC_TABLES,
    SYNC_TABLES,
    SyncConflictError,
    apply_sync_change,
    persist_sync_conflict,
    reconcile_touched_employee_balances,
    validate_sync_financial_invariants,
    sync_delete_allowed,
)

router = APIRouter()

ALLOWED_SYNC_TABLES = frozenset(SYNC_TABLES)
SENSITIVE_SETTING_KEYS = frozenset({
    "cloud_sync_token",
    "firebase_cloud_password",
    "google_token",
    "google_refresh_token",
    "google_client_id",
    "google_client_secret",
})

# Simple verification header
def verify_sync_token(db: Session = Depends(get_db), x_sync_token: str = Header(None)):
    if not x_sync_token:
        raise HTTPException(status_code=401, detail="X-Sync-Token header missing")
    
    # Get secret token from settings
    token_row = db.execute(
        text("SELECT value FROM settings WHERE key = 'cloud_sync_token'")
    ).fetchone()
    
    expected_token = str(token_row[0]).strip() if token_row and token_row[0] else ""
    if len(expected_token) < 32 or expected_token == "default_secret":
        raise HTTPException(
            status_code=503,
            detail="Cloud sync is disabled until a strong sync token is configured",
        )
    
    if not hmac.compare_digest(str(x_sync_token), expected_token):
        raise HTTPException(status_code=403, detail="Invalid Sync Token")

@router.post("/push")
async def push_changes(
    request: Request,
    db: Session = Depends(get_db),
    _ = Depends(verify_sync_token)
):
    """
    Accepts local changes from Desktop POS, applies them to Cloud DB.
    The cloud capture triggers remain enabled so a change pushed by Client A is
    available to Client B through /pull. Clients suppress their own triggers
    while applying pulled rows, which prevents echo loops at the correct edge.
    """
    try:
        batch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if not isinstance(batch, list) or len(batch) > 500:
        raise HTTPException(status_code=400, detail="Sync payload must be a list of at most 500 items")

    # Token verification opened a read transaction through SQLAlchemy. End
    # that read-only snapshot before acquiring the serialized write lock.
    db.commit()
    conn = db.connection().connection
    cursor = conn.cursor()

    try:
        # Two desktop clients cannot now both observe the same PK as absent
        # before either INSERT commits.
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("PRAGMA defer_foreign_keys = ON")
        touched_employee_ids = set()
        validation_state = {}
        for change in batch:
            if not isinstance(change, dict):
                raise HTTPException(status_code=400, detail="Each sync item must be an object")
            try:
                table_name = change["table_name"]
                record_id = change["record_id"]
                action = str(change["action"]).upper()
                data = change.get("data")
            except (KeyError, TypeError):
                raise HTTPException(status_code=400, detail="Malformed synchronization item")

            if table_name not in ALLOWED_SYNC_TABLES:
                raise HTTPException(status_code=400, detail="Table is not allowed for synchronization")
            if action not in {"DELETE", "INSERT", "UPDATE"}:
                raise HTTPException(status_code=400, detail="Unsupported synchronization action")
            if action == "DELETE" and not sync_delete_allowed(cursor, table_name, record_id):
                raise SyncConflictError(
                    table_name,
                    record_id,
                    "posted financial records cannot be deleted; use the reversal, return, or inactive workflow",
                )
            if table_name == "settings" and str(record_id) in SENSITIVE_SETTING_KEYS:
                raise HTTPException(status_code=400, detail="Sensitive settings cannot be synchronized")
            if data is not None and not isinstance(data, dict):
                raise HTTPException(status_code=400, detail="Sync item data must be an object")

            apply_sync_change(
                cursor, change, touched_employee_ids, validation_state
            )

        # Employee.current_balance is a local projection, never client input.
        # Rebuild it from immutable ledger movements before this batch commits.
        validate_sync_financial_invariants(cursor, validation_state)
        reconcile_touched_employee_balances(cursor, touched_employee_ids)

        conn.commit()
        return {"status": "success", "message": f"Processed {len(batch)} sync items"}

    except SyncConflictError as conflict:
        conn.rollback()
        persist_sync_conflict(conn, conflict, direction="push")
        raise HTTPException(status_code=409, detail=conflict.detail())
    except HTTPException:
        conn.rollback()
        raise
    except ValueError as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        conn.rollback()
        print(f"[Cloud Sync] Push failed: {e}")
        raise HTTPException(status_code=500, detail="Database error during sync push")
@router.get("/pull")
def pull_changes(
    last_id: int = Query(0),
    db: Session = Depends(get_db),
    _ = Depends(verify_sync_token)
):
    """
    Returns changes recorded in cloud sync_queue that happened after `last_id`.
    This is called by PC to download mobile entries.
    """
    conn = db.connection().connection
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row

    try:
        # Fetch cloud sync queue items
        cursor.execute(
            "SELECT * FROM sync_queue WHERE id > ? ORDER BY id ASC LIMIT 100",
            (last_id,)
        )
        rows = cursor.fetchall()

        if not rows:
            return {"max_id": last_id, "changes": []}

        changes = []
        max_id = last_id

        for row in rows:
            qid = row["id"]
            table_name = row["table_name"]
            record_id = row["record_id"]
            action = row["action"]

            if qid > max_id:
                max_id = qid

            if table_name not in ALLOWED_SYNC_TABLES:
                continue
            if table_name == "settings" and str(record_id) in SENSITIVE_SETTING_KEYS:
                continue
            if action == "DELETE" and table_name in PROTECTED_EMPLOYEE_SYNC_TABLES:
                continue

            record_data = None
            if action in ("INSERT", "UPDATE"):
                pk = "key" if table_name == "settings" else "id"
                cursor.execute(f"SELECT * FROM {table_name} WHERE {pk} = ?", (record_id,))
                rec_row = cursor.fetchone()
                if rec_row:
                    record_data = dict(rec_row)
                else:
                    if table_name in PROTECTED_EMPLOYEE_SYNC_TABLES:
                        continue
                    action = "DELETE"

            changes.append({
                "id": qid,
                "table_name": table_name,
                "record_id": str(record_id),
                "action": action,
                "data": record_data
            })

        return {"max_id": max_id, "changes": changes}

    except Exception as e:
        print(f"[Cloud Sync] Pull failed: {e}")
        raise HTTPException(status_code=500, detail="Database error during sync pull")
    finally:
        conn.close()
