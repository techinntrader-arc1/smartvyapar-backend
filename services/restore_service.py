"""
Restore Service - Database restoration with integrity verification, emergency snapshots,
atomic replacement, rollback protection, history tracking, undo capability, and async support.
"""

import os
import json
import shutil
import sqlite3
import logging
import asyncio
import traceback
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone

logger = logging.getLogger("smartvyapar.restore")
logger.setLevel(logging.INFO)

MAINTENANCE_MODE: bool = False
RESTORE_LOCK = threading.Lock()
_HTTP_REQUEST_GATE = threading.Condition(threading.Lock())
_ACTIVE_HTTP_REQUESTS: int = 0
try:
    _REQUEST_DRAIN_TIMEOUT_SECONDS = max(
        0.0,
        float(os.environ.get("SV_RESTORE_REQUEST_DRAIN_TIMEOUT_SECONDS", "15")),
    )
except (TypeError, ValueError):
    _REQUEST_DRAIN_TIMEOUT_SECONDS = 15.0


def try_admit_http_request(
    *,
    count_request: bool = True,
    allow_during_maintenance: bool = False,
) -> bool:
    """Atomically admit an HTTP request or reject it during a restore.

    The middleware calls this before dispatching the request. Restore-control
    and health/status requests are deliberately uncounted; every ordinary
    request holds one slot until its response (or exception) is complete.
    """
    global _ACTIVE_HTTP_REQUESTS
    with _HTTP_REQUEST_GATE:
        if MAINTENANCE_MODE and not allow_during_maintenance:
            return False
        if count_request:
            _ACTIVE_HTTP_REQUESTS += 1
        return True


def release_http_request() -> None:
    """Release one ordinary request slot and wake a waiting restore."""
    global _ACTIVE_HTTP_REQUESTS
    with _HTTP_REQUEST_GATE:
        if _ACTIVE_HTTP_REQUESTS <= 0:
            logger.error("HTTP request gate release called without an active request")
            _ACTIVE_HTTP_REQUESTS = 0
            return
        _ACTIVE_HTTP_REQUESTS -= 1
        if _ACTIVE_HTTP_REQUESTS == 0:
            _HTTP_REQUEST_GATE.notify_all()


def _begin_maintenance() -> None:
    """Close the request gate atomically before inspecting the active count."""
    global MAINTENANCE_MODE
    with _HTTP_REQUEST_GATE:
        MAINTENANCE_MODE = True


def _wait_for_http_requests_to_drain(timeout: Optional[float] = None) -> bool:
    """Wait a bounded time for requests admitted before maintenance to finish."""
    wait_seconds = (
        _REQUEST_DRAIN_TIMEOUT_SECONDS if timeout is None else max(0.0, float(timeout))
    )
    deadline = time.monotonic() + wait_seconds
    with _HTTP_REQUEST_GATE:
        while _ACTIVE_HTTP_REQUESTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _HTTP_REQUEST_GATE.wait(timeout=remaining)
        return True


def _end_maintenance() -> None:
    """Re-open the request gate after restore success or failure."""
    global MAINTENANCE_MODE
    with _HTTP_REQUEST_GATE:
        MAINTENANCE_MODE = False
        _HTTP_REQUEST_GATE.notify_all()


def get_request_gate_status() -> Dict[str, Any]:
    """Return a consistent snapshot for diagnostics and focused tests."""
    with _HTTP_REQUEST_GATE:
        return {
            "maintenance_mode": MAINTENANCE_MODE,
            "active_http_requests": _ACTIVE_HTTP_REQUESTS,
        }


def _atomic_replace_database(source_path: str, destination_path: str) -> None:
    """Stage, validate, fsync, and atomically promote a SQLite database file."""
    destination_dir = os.path.dirname(os.path.abspath(destination_path))
    os.makedirs(destination_dir, exist_ok=True)
    handle, staged_path = tempfile.mkstemp(
        prefix=".smartvyapar_restore_", suffix=".db", dir=destination_dir
    )
    os.close(handle)
    try:
        shutil.copyfile(source_path, staged_path)
        with open(staged_path, "r+b") as staged_file:
            os.fsync(staged_file.fileno())
        readonly_uri = Path(staged_path).resolve().as_uri() + "?mode=ro"
        validation_conn = sqlite3.connect(readonly_uri, uri=True)
        try:
            validation_result = validation_conn.execute("PRAGMA integrity_check(1)").fetchone()
        finally:
            validation_conn.close()
        if not validation_result or validation_result[0] != "ok":
            raise ValueError(f"Staged database validation failed: {validation_result}")
        os.replace(staged_path, destination_path)
    finally:
        if os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except OSError:
                pass


# ── Restore History Tracking Helper ───────────────────────────────────────────

def _get_history_file_path() -> str:
    from database import get_base_data_dir
    config_dir = os.path.dirname(get_base_data_dir())
    return os.path.join(config_dir, "restore_history.json")


def save_restore_history(entry: Dict[str, Any]) -> bool:
    """Appends an event to the persistent restore history log file."""
    try:
        history_file = _get_history_file_path()
        history: List[Dict[str, Any]] = []
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

        history.append(entry)
        # Keep last 50 history entries
        history = history[-50:]

        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Failed to record restore history: {e}")
        return False


def get_restore_history() -> List[Dict[str, Any]]:
    """Retrieve full restore history tracking log."""
    try:
        history_file = _get_history_file_path()
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return []
    except Exception as e:
        logger.error(f"Failed to read restore history: {e}")
        return []


# ── Preview & Validation Helpers ──────────────────────────────────────────────

def get_preview_stats(file_path: str) -> Dict[str, Any]:
    """
    Validates and extracts detailed statistics for the Restore Preview.

    Args:
        file_path (str): Absolute file path to database backup.

    Returns:
        Dict[str, Any]: Dictionary containing validation status and database statistics.
    """
    if not file_path or not os.path.exists(file_path):
        return {"valid": False, "error": f"Backup file not found: {file_path}"}
    
    try:
        # 1. Header Validation
        with open(file_path, "rb") as f:
            header = f.read(16)
            if header != b"SQLite format 3\x00":
                return {"valid": False, "error": "Invalid SQLite file format header"}
        
        # 2. SQLite PRAGMA Integrity Check
        readonly_uri = Path(file_path).resolve().as_uri() + "?mode=ro"
        # sqlite3.Connection's context manager commits/rolls back but does not
        # close the OS handle; closing() is required before a Windows restore.
        with closing(sqlite3.connect(readonly_uri, uri=True)) as conn:
            res = conn.execute("PRAGMA integrity_check(1)").fetchone()
        if not res or res[0] != "ok":
            return {"valid": False, "error": "Database integrity check failed"}
        
        # 3. Extract Detailed Statistics
        from .safe_backup_service import get_db_stats
        stats = get_db_stats(file_path)
        
        stats["file_size_mb"] = round(os.path.getsize(file_path) / (1024 * 1024), 2)
        stats["backup_date"] = datetime.fromtimestamp(os.path.getctime(file_path), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        
        return {"valid": True, "stats": stats}
    except Exception as e:
        logger.error(f"Preview inspection error for '{file_path}': {e}", exc_info=True)
        return {"valid": False, "error": str(e)}


# ── Core Restoration Logic ───────────────────────────────────────────────────


def _sync_manager_is_active(sync_manager: Any) -> bool:
    """Return whether the supplied manager has a live/running worker."""
    if sync_manager is None:
        return False
    worker = getattr(sync_manager, "thread", None)
    return bool(
        getattr(sync_manager, "running", False)
        or (worker is not None and worker.is_alive())
    )


def _dispose_database_engine() -> None:
    """Close every idle SQLAlchemy handle before a Windows file swap."""
    from database import engine as active_engine
    active_engine.dispose()


def _remove_database_sidecars(active_db_path: str) -> None:
    """Remove SQLite journals only after all known runtime users are stopped."""
    for ext in ("-wal", "-shm", "-journal"):
        sidecar = active_db_path + ext
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError as cleanup_err:
                logger.warning(f"Could not remove database sidecar '{sidecar}': {cleanup_err}")


def _prepare_restored_database(active_db_path: str) -> None:
    """Migrate and register strict triggers on the database now on disk."""
    from migrations import apply_migrations
    from employee_migrations import apply_employee_migrations
    from .sync_manager import SyncManager

    # Disposing both before and after preparation prevents a migration handle
    # from surviving into a rollback or the restarted background worker.
    _dispose_database_engine()
    apply_migrations()
    apply_employee_migrations()
    SyncManager(active_db_path).setup_database_triggers(strict=True)
    _dispose_database_engine()


def _restart_sync_manager(sync_manager: Any, active_db_path: str, should_start: bool) -> None:
    if sync_manager is None or not should_start:
        return
    sync_manager.db_path = active_db_path
    sync_manager.start(strict=True)


def perform_restore(
    backup_path: str,
    active_db_path: str,
    *,
    request_db: Any = None,
    sync_manager: Any = None,
) -> Tuple[bool, str]:
    """
    Executes database restoration with safety checks and atomic rollback protection.

    Args:
        backup_path (str): Absolute file path of backup file to restore.
        active_db_path (str): Absolute destination path of target SQLite database.
        request_db: Optional request-scoped SQLAlchemy Session to close before
            replacing the active SQLite file.
        sync_manager: Optional app-owned SyncManager to quiesce and resume.

    Returns:
        Tuple[bool, str]: Success boolean flag and status description message.
    """
    if not RESTORE_LOCK.acquire(blocking=False):
        return False, "Another database restore is already in progress."

    _begin_maintenance()
    pre_restore_bak: Optional[str] = None
    runtime_quiesced = False
    database_replaced = False
    sync_was_running = False
    sync_restarted = False
    backup_runtime = None
    backup_was_running = False
    backup_restarted = False
    firebase_runtime = None
    firebase_state = None
    firebase_paused = False
    firebase_resumed = False

    try:
        # This request is deliberately excluded from the middleware's active
        # request count, but its dependency can still own a SQLite connection.
        # Close that handle first, then wait for every ordinary request that
        # crossed the gate before maintenance mode was enabled.
        if request_db is not None:
            request_db.close()
        if not _wait_for_http_requests_to_drain():
            active_requests = get_request_gate_status()["active_http_requests"]
            return False, (
                "Restore cancelled because active requests did not finish within "
                f"{_REQUEST_DRAIN_TIMEOUT_SECONDS:g} seconds "
                f"({active_requests} still active)."
            )

        logger.info(f"Initiating database restore: '{backup_path}' -> '{active_db_path}'")
        if not os.path.exists(backup_path):
            return False, f"Backup file missing: {backup_path}"

        # 1. Source Validation
        preview = get_preview_stats(backup_path)
        if not preview["valid"]:
            return False, f"Source Validation Failed: {preview['error']}"

        # The endpoint's dependency is closed and all previously admitted HTTP
        # requests are drained. Next stop every background database owner; only
        # after all of them are quiesced may os.replace touch the SQLite file.
        sync_was_running = _sync_manager_is_active(sync_manager)
        runtime_quiesced = True

        # The automatic backup worker can be inside SQLite's online backup API
        # or a Google Drive Session, and Firebase owns additional short-lived
        # SessionLocal workers. Pause/join both before touching the DB file.
        from . import safe_backup_service as backup_runtime
        from .firebase_sync import firebase_sync as firebase_runtime

        backup_was_running = backup_runtime.is_backup_worker_running()
        backup_runtime.stop_backup_worker()
        firebase_paused = True
        firebase_state = firebase_runtime.pause_for_database_restore()
        if sync_was_running:
            sync_manager.stop()
        _dispose_database_engine()

        # 2. Emergency Pre-Restore Snapshot
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre_restore_bak = active_db_path + f".pre_restore_{timestamp}.bak"
        if os.path.exists(active_db_path):
            from .safe_backup_service import perform_atomic_backup
            snapshot_ok, snapshot_error = perform_atomic_backup(active_db_path, pre_restore_bak)
            if not snapshot_ok:
                raise RuntimeError(
                    f"Could not create pre-restore safety snapshot: {snapshot_error}"
                )
            logger.info(f"Created emergency pre-restore snapshot: {pre_restore_bak}")

        # 3. Cleanup Journal/WAL Files
        _remove_database_sidecars(active_db_path)

        # 4. Atomic Replace
        _atomic_replace_database(backup_path, active_db_path)
        database_replaced = True

        # 5. Apply Database Migrations. A migration failure is a restore
        # failure and must roll back the database before returning.
        _prepare_restored_database(active_db_path)

        # 6. Update database_config.json only after a successful migration.
        from database import load_db_config, save_db_config
        config = load_db_config()
        config.update({
            "active_db_path": active_db_path,
            "last_restore_source": backup_path,
            "last_restore_at": datetime.now(timezone.utc).isoformat(),
            "last_pre_restore_snapshot": pre_restore_bak,
            "db_version": preview["stats"].get("database_version", "1.0")
        })
        save_db_config(config)

        # Resume the exact manager owned by app.state. This preserves runtime
        # ownership and ensures shutdown/another restore can stop it again.
        _restart_sync_manager(sync_manager, active_db_path, sync_was_running)
        sync_restarted = sync_was_running
        firebase_resumed = True
        firebase_runtime.resume_after_database_restore(firebase_state)
        if backup_was_running:
            backup_restarted = True
            backup_runtime.start_backup_worker()

        # 7. Record Restore History Event
        save_restore_history({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "backup_source": backup_path,
            "target_db": active_db_path,
            "pre_restore_snapshot": pre_restore_bak,
            "status": "success"
        })

        logger.info("Database restoration completed successfully.")
        return True, "Restoration complete. Database has been updated and migrated."

    except Exception as e:
        logger.error(f"Database restoration failed: {e}\n{traceback.format_exc()}")

        recovery_errors: List[str] = []
        if runtime_quiesced:
            # A worker restarted against the promoted file must be stopped
            # before that file can itself be replaced during rollback.
            if backup_restarted:
                try:
                    backup_runtime.stop_backup_worker()
                except Exception as stop_err:
                    recovery_errors.append(f"could not stop restarted backup worker: {stop_err}")
            if firebase_resumed:
                try:
                    firebase_state = firebase_runtime.pause_for_database_restore()
                    firebase_paused = True
                except Exception as stop_err:
                    recovery_errors.append(f"could not pause restarted Firebase sync: {stop_err}")
            if sync_restarted:
                try:
                    sync_manager.stop()
                except Exception as stop_err:
                    recovery_errors.append(f"could not stop restarted sync worker: {stop_err}")

            sync_quiesced = not _sync_manager_is_active(sync_manager)
            backup_quiesced = bool(
                backup_runtime is None or not backup_runtime.is_backup_worker_running()
            )
            firebase_quiesced = bool(
                firebase_runtime is None
                or firebase_runtime.database_runtime_is_quiesced()
            )
            worker_quiesced = sync_quiesced and backup_quiesced and firebase_quiesced
            runtime_database_ready = not database_replaced
            if not worker_quiesced:
                recovery_errors.append(
                    "a background database worker is still active; database rollback was not attempted"
                )

            if worker_quiesced and database_replaced:
                engine_disposed = True
                try:
                    _dispose_database_engine()
                except Exception as dispose_err:
                    engine_disposed = False
                    recovery_errors.append(f"could not dispose database engine: {dispose_err}")

                rollback_succeeded = False
                if engine_disposed:
                    _remove_database_sidecars(active_db_path)
                    try:
                        if pre_restore_bak and os.path.exists(pre_restore_bak):
                            _atomic_replace_database(pre_restore_bak, active_db_path)
                            logger.info("Emergency rollback restored the previous database state.")
                        elif os.path.exists(active_db_path):
                            os.remove(active_db_path)
                        rollback_succeeded = True
                    except Exception as rb_err:
                        logger.critical(f"Critical: Emergency rollback failed: {rb_err}")
                        recovery_errors.append(f"database rollback failed: {rb_err}")

                # Re-run runtime preparation on the rolled-back database so its
                # migrations/triggers and all handles are in a known-good state.
                if rollback_succeeded and os.path.exists(active_db_path):
                    try:
                        _prepare_restored_database(active_db_path)
                        runtime_database_ready = True
                    except Exception as prep_err:
                        recovery_errors.append(f"rolled-back database preparation failed: {prep_err}")

            # Resume each component after an untouched failure or a successful
            # rollback. Never create a second worker beside one that timed out.
            if (worker_quiesced or not database_replaced) and runtime_database_ready:
                sync_can_restart = not _sync_manager_is_active(sync_manager)
                try:
                    _restart_sync_manager(
                        sync_manager,
                        active_db_path,
                        sync_was_running
                        and sync_can_restart
                        and os.path.exists(active_db_path),
                    )
                except Exception as restart_err:
                    recovery_errors.append(f"sync restart failed: {restart_err}")
                if firebase_runtime is not None and firebase_paused and firebase_quiesced:
                    try:
                        firebase_runtime.resume_after_database_restore(firebase_state)
                    except Exception as restart_err:
                        recovery_errors.append(f"Firebase restart failed: {restart_err}")
                if (
                    backup_runtime is not None
                    and backup_was_running
                    and backup_quiesced
                ):
                    try:
                        backup_runtime.start_backup_worker()
                    except Exception as restart_err:
                        recovery_errors.append(f"backup worker restart failed: {restart_err}")

        recovery_suffix = ""
        if recovery_errors:
            recovery_suffix = " Runtime recovery errors: " + "; ".join(recovery_errors)
        return False, f"Restore failed: {str(e)}.{recovery_suffix}"
    finally:
        _end_maintenance()
        RESTORE_LOCK.release()


# ── New Features ─────────────────────────────────────────────────────────────

async def perform_async_restore(
    backup_path: str,
    active_db_path: str,
    *,
    request_db: Any = None,
    sync_manager: Any = None,
) -> Tuple[bool, str]:
    """Asynchronous non-blocking wrapper for database restoration."""
    try:
        return await asyncio.to_thread(
            perform_restore,
            backup_path,
            active_db_path,
            request_db=request_db,
            sync_manager=sync_manager,
        )
    except Exception as e:
        logger.error(f"Async restore failed: {e}", exc_info=True)
        return False, f"Async restore error: {str(e)}"


def list_restore_points(active_db_path: str) -> List[Dict[str, Any]]:
    """List available `.pre_restore_*.bak` snapshots available for undoing restores."""
    try:
        db_dir = os.path.dirname(active_db_path)
        base_name = os.path.basename(active_db_path)
        restore_points: List[Dict[str, Any]] = []

        if os.path.exists(db_dir):
            for file in os.listdir(db_dir):
                if file.startswith(base_name + ".pre_restore_") and file.endswith(".bak"):
                    file_path = os.path.join(db_dir, file)
                    stat = os.stat(file_path)
                    restore_points.append({
                        "filename": file,
                        "path": file_path,
                        "size_mb": round(stat.st_size / (1024 * 1024), 2),
                        "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                    })

        restore_points.sort(key=lambda x: x["created"], reverse=True)
        return restore_points
    except Exception as e:
        logger.error(f"Failed to list restore points: {e}", exc_info=True)
        return []


def undo_last_restore(
    active_db_path: str,
    *,
    request_db: Any = None,
    sync_manager: Any = None,
) -> Tuple[bool, str]:
    """Reverts database to the most recent pre-restore safety snapshot."""
    try:
        restore_points = list_restore_points(active_db_path)
        if not restore_points:
            return False, "No pre-restore snapshots available for undo."

        latest_snapshot = restore_points[0]["path"]
        logger.info(f"Undoing restore using latest snapshot: '{latest_snapshot}'")

        success, msg = perform_restore(
            latest_snapshot,
            active_db_path,
            request_db=request_db,
            sync_manager=sync_manager,
        )
        if success:
            save_restore_history({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "backup_source": latest_snapshot,
                "target_db": active_db_path,
                "status": "undo_success"
            })
            return True, f"Undo successful. Reverted to snapshot '{restore_points[0]['filename']}'."
        else:
            return False, f"Undo failed: {msg}"
    except Exception as e:
        logger.error(f"Undo restore failed: {e}", exc_info=True)
        return False, f"Undo restore failed: {str(e)}"


def health_check(active_db_path: Optional[str] = None) -> Dict[str, Any]:
    """Check status of restore subsystem and active maintenance mode."""
    try:
        from database import get_db_path
        target_path = active_db_path or get_db_path()
        points = list_restore_points(target_path)
        return {
            "status": "maintenance" if MAINTENANCE_MODE else "healthy",
            "maintenance_mode": MAINTENANCE_MODE,
            "active_db": target_path,
            "available_undo_snapshots": len(points),
            "latest_undo_snapshot": points[0]["filename"] if points else None,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Restore health check error: {e}", exc_info=True)
        return {
            "status": "error",
            "maintenance_mode": MAINTENANCE_MODE,
            "error": str(e)
        }
