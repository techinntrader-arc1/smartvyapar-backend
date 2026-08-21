"""
Safe Backup Service - Atomic single-file database backup management with SQLite VACUUM INTO,
PRAGMA integrity checks, manifest generation, retention policy, background queue worker,
status tracking, and async support.
"""

import os
import shutil
import threading
import time
import json
import sqlite3
import hashlib
import logging
import asyncio
from datetime import datetime, timezone
from queue import Queue, Empty
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("smartvyapar.safe_backup")
logger.setLevel(logging.INFO)

# ── Backup Configuration & Queue ──────────────────────────────────────────────
backup_queue = Queue()

BACKUP_CONFIG: Dict[str, Any] = {
    "retention_count": 5,
    "interval_minutes": 30,
    "debounce_seconds": 20
}

LAST_BACKUP_STATUS: Dict[str, Any] = {
    "status": "idle",
    "last_run_at": None,
    "last_success_at": None,
    "last_error": None,
    "total_backups_created": 0
}

STATUS_LOCK = threading.Lock()
WORKER_LOCK = threading.Lock()
WORKER_STOP_EVENT = threading.Event()
worker_thread = None


# ── Helper Functions ──────────────────────────────────────────────────────────

def get_file_checksum(file_path: str) -> str:
    """Generates a SHA-256 hash for file integrity validation."""
    if not os.path.exists(file_path):
        return ""
    try:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        logger.error(f"Failed to calculate checksum for '{file_path}': {e}")
        return ""


def get_db_stats(db_path: str) -> Dict[str, Any]:
    """Extracts metadata statistics for manifest generation."""
    stats = {
        "company_name": "Unknown",
        "app_version": "1.3.5",
        "database_version": "1.0",
        "products_count": 0,
        "sales_count": 0,
        "sale_items_count": 0,
        "purchases_count": 0,
        "purchase_items_count": 0,
        "accounts_count": 0,
        "ledger_entries_count": 0,
        "customers_count": 0,
        "suppliers_count": 0,
        "payments_count": 0,
        "expenses_count": 0
    }
    if not os.path.exists(db_path):
        return stats

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cursor.fetchall()]

        if 'settings' in tables:
            cursor.execute("SELECT value FROM settings WHERE key='business_name'")
            res = cursor.fetchone()
            if res:
                stats["company_name"] = res[0]

        count_map = {
            "products": "products_count",
            "sales": "sales_count",
            "sale_items": "sale_items_count",
            "purchases": "purchases_count",
            "purchase_items": "purchase_items_count",
            "customers": "customers_count",
            "suppliers": "suppliers_count",
            "payments": "payments_count",
            "expenses": "expenses_count",
            "stock_movements": "ledger_entries_count"
        }

        for table, key in count_map.items():
            if table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                stats[key] = cursor.fetchone()[0]
        
        stats["accounts_count"] = stats["customers_count"] + stats["suppliers_count"]
        conn.close()
    except Exception as e:
        logger.error(f"Metadata extraction error for '{db_path}': {e}")
    return stats


def perform_atomic_backup(src_path: str, dst_path: str) -> Tuple[bool, str]:
    """
    Performs a safe, atomic single-file SQLite database backup:
    1. Use SQLite's online backup API for a transactionally consistent snapshot.
    2. Flush the target file to disk.
    3. Verify PRAGMA integrity_check(1).
    4. Atomically promote temp file to destination.

    Args:
        src_path (str): Source active SQLite database path.
        dst_path (str): Destination path for final backup file.

    Returns:
        Tuple[bool, str]: Success boolean flag and description message.
    """
    tmp_path = dst_path + ".tmp"
    source_conn = None
    target_conn = None
    try:
        if not os.path.exists(src_path):
            return False, f"Source DB missing at '{src_path}'"

        target_dir = os.path.dirname(dst_path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        # SQLite's backup API includes committed WAL data without mutating or
        # blocking the active database with a forced checkpoint.
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        source_conn = sqlite3.connect(src_path, timeout=30)
        target_conn = sqlite3.connect(tmp_path)
        source_conn.backup(target_conn)
        target_conn.commit()
        target_conn.close()
        target_conn = None
        source_conn.close()
        source_conn = None

        with open(tmp_path, "r+b") as backup_file:
            os.fsync(backup_file.fileno())
        
        # 3. Integrity Check
        if os.path.getsize(tmp_path) < 1024:
            raise ValueError("Backup file size too small or empty")
        
        check_conn = sqlite3.connect(tmp_path)
        res = check_conn.execute("PRAGMA integrity_check(1)").fetchone()
        check_conn.close()
        
        if not res or res[0] != "ok":
            raise ValueError(f"Integrity check failed: {res}")
            
        # 4. Atomic promotion on the destination filesystem
        os.replace(tmp_path, dst_path)
        logger.info(f"Atomic backup created successfully at '{dst_path}'")
        return True, "Success"
            
    except Exception as e:
        logger.error(f"Atomic backup failed: {e}", exc_info=True)
        for connection in (target_conn, source_conn):
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False, str(e)


# ── Backup Execution Worker ───────────────────────────────────────────────────

def run_backup_job(db_path: str, backup_dir: str) -> bool:
    """Executes full backup workflow: atomic copy, manifest update, retention cleanup, and cloud sync."""
    global LAST_BACKUP_STATUS
    now_str = datetime.now(timezone.utc).isoformat()
    
    with STATUS_LOCK:
        LAST_BACKUP_STATUS["status"] = "running"
        LAST_BACKUP_STATUS["last_run_at"] = now_str

    try:
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        latest_file = os.path.join(backup_dir, "latest_backup.db")
        timestamped_file = os.path.join(backup_dir, f"backup_{timestamp}.db")
        manifest_file = os.path.join(backup_dir, "backup_manifest.json")
        
        # 1. Create Timestamped Backup
        success, msg = perform_atomic_backup(db_path, timestamped_file)
        if not success:
            with STATUS_LOCK:
                LAST_BACKUP_STATUS["status"] = "failed"
                LAST_BACKUP_STATUS["last_error"] = msg
            return False
        
        # 2. Update Latest Backup File
        if os.path.exists(latest_file):
            os.remove(latest_file)
        shutil.copy2(timestamped_file, latest_file)
        
        # 3. Write Manifest
        stats = get_db_stats(timestamped_file)
        checksum = get_file_checksum(timestamped_file)
        retention = BACKUP_CONFIG.get("retention_count", 5)
        
        manifest = {
            "latest_backup": {
                "file": os.path.basename(timestamped_file),
                "timestamp": timestamp,
                "size_bytes": os.path.getsize(timestamped_file),
                "checksum": checksum,
                **stats
            },
            "retention_count": retention
        }
        
        with open(manifest_file, "w") as f:
            json.dump(manifest, f, indent=2)

        # 4. Retention Policy Cleanup
        backups = [f for f in os.listdir(backup_dir) if f.startswith("backup_") and f.endswith(".db")]
        backups.sort(reverse=True)
        for old_b in backups[retention:]:
            try:
                os.remove(os.path.join(backup_dir, old_b))
                logger.info(f"Enforced backup retention policy: removed old backup '{old_b}'")
            except Exception as rm_err:
                logger.warning(f"Failed removing old retention backup '{old_b}': {rm_err}")

        # 5. Cleanup Stale Temporary Files
        for f in os.listdir(backup_dir):
            if f.endswith(".tmp") or f.endswith(".tmp-journal") or f.endswith(".old"):
                try:
                    tmp_path = os.path.join(backup_dir, f)
                    if os.path.exists(tmp_path):
                        age_seconds = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(tmp_path))).total_seconds()
                        if age_seconds > 600:
                            os.remove(tmp_path)
                except Exception:
                    pass

        # 6. Cloud Sync (Safe Backup Only)
        try:
            from .google_drive_service import upload_file_to_drive
            from database import SessionLocal
            db = SessionLocal()
            try:
                upload_file_to_drive(db, latest_file)
            finally:
                db.close()
        except Exception as cloud_err:
            logger.warning(f"Cloud sync warning during backup job: {cloud_err}")

        with STATUS_LOCK:
            LAST_BACKUP_STATUS["status"] = "success"
            LAST_BACKUP_STATUS["last_success_at"] = datetime.now(timezone.utc).isoformat()
            LAST_BACKUP_STATUS["last_error"] = None
            LAST_BACKUP_STATUS["total_backups_created"] += 1

        logger.info(f"Safe backup snapshot '{timestamp}' completed successfully.")
        return True
    except Exception as e:
        logger.error(f"Safe backup job execution failed: {e}", exc_info=True)
        with STATUS_LOCK:
            LAST_BACKUP_STATUS["status"] = "failed"
            LAST_BACKUP_STATUS["last_error"] = str(e)
        return False


def backup_worker():
    """Background worker thread with debouncing and interval logic."""
    last_run_time = 0.0
    pending_backup = False
    
    while not WORKER_STOP_EVENT.is_set():
        try:
            try:
                job = backup_queue.get(timeout=5)
                pending_backup = True
            except Empty:
                pass
            
            now = time.time()
            
            # Check Maintenance Mode
            try:
                from services.restore_service import MAINTENANCE_MODE
                if MAINTENANCE_MODE:
                    WORKER_STOP_EVENT.wait(2)
                    continue
            except Exception:
                pass

            debounce_sec = BACKUP_CONFIG.get("debounce_seconds", 20)
            interval_sec = BACKUP_CONFIG.get("interval_minutes", 30) * 60

            if pending_backup:
                if (now - last_run_time) > debounce_sec:
                    from database import get_db_path, get_backup_dir
                    run_backup_job(get_db_path(), get_backup_dir())
                    last_run_time = now
                    pending_backup = False
            elif (now - last_run_time) > interval_sec:
                from database import get_db_path, get_backup_dir
                run_backup_job(get_db_path(), get_backup_dir())
                last_run_time = now
                    
        except Exception as e:
            logger.error(f"Backup worker thread exception: {e}", exc_info=True)
        WORKER_STOP_EVENT.wait(2)


def start_backup_worker() -> None:
    """Start the backup worker once, after database startup has completed."""
    global worker_thread
    with WORKER_LOCK:
        if worker_thread and worker_thread.is_alive():
            return
        WORKER_STOP_EVENT.clear()
        worker_thread = threading.Thread(
            target=backup_worker,
            name="smartvyapar-backup-worker",
            daemon=True,
        )
        worker_thread.start()


def is_backup_worker_running() -> bool:
    """Return whether the singleton backup worker is currently alive."""
    with WORKER_LOCK:
        return bool(worker_thread and worker_thread.is_alive())


def stop_backup_worker(timeout: float = 30.0) -> bool:
    """Signal the worker and do not return until its DB handles are released.

    Returns whether a live worker was stopped. A timeout raises instead of
    discarding the thread reference; restore callers must never swap SQLite
    while an in-flight backup or Google upload still owns a Session.
    """
    global worker_thread
    with WORKER_LOCK:
        WORKER_STOP_EVENT.set()
        worker = worker_thread
        was_running = bool(worker and worker.is_alive())

    if worker and worker.is_alive():
        worker.join(timeout=timeout)
        if worker.is_alive():
            raise RuntimeError(
                "Backup worker did not quiesce before the database operation timeout"
            )

    with WORKER_LOCK:
        if worker_thread is worker:
            worker_thread = None
    return was_running


# ── Public API & Control Functions ────────────────────────────────────────────

def trigger_safe_backup(db_path: Optional[str] = None, backup_dir: Optional[str] = None, debounce_seconds: int = 60) -> str:
    """Enqueues automated debounced backup request into background queue."""
    backup_queue.put(True)
    return "Queued"


def force_safe_backup_sync(db_path: str, backup_dir: str) -> str:
    """Forces immediate synchronous backup execution."""
    success = run_backup_job(db_path, backup_dir)
    if not success:
        raise RuntimeError("Immediate synchronous backup failed. Check system logs.")
    return os.path.join(backup_dir, "latest_backup.db")


async def trigger_async_safe_backup(db_path: Optional[str] = None, backup_dir: Optional[str] = None) -> str:
    """Asynchronous non-blocking wrapper for queueing safe backup."""
    return await asyncio.to_thread(trigger_safe_backup, db_path, backup_dir)


async def force_async_safe_backup(db_path: str, backup_dir: str) -> str:
    """Asynchronous non-blocking wrapper for immediate forced backup."""
    return await asyncio.to_thread(force_safe_backup_sync, db_path, backup_dir)


def get_backup_status() -> Dict[str, Any]:
    """Get current backup operational tracking status and configuration."""
    with STATUS_LOCK:
        return {
            **LAST_BACKUP_STATUS,
            "config": dict(BACKUP_CONFIG)
        }


def get_latest_manifest(backup_dir: str) -> Dict[str, Any]:
    """Retrieve contents of the latest backup manifest JSON file."""
    manifest_path = os.path.join(backup_dir, "backup_manifest.json")
    if not os.path.exists(manifest_path):
        return {"error": "Manifest file not found"}
    try:
        with open(manifest_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read backup manifest: {e}")
        return {"error": f"Failed to read manifest: {str(e)}"}


def update_backup_config(config_updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update active backup configuration settings."""
    try:
        if "retention_count" in config_updates and isinstance(config_updates["retention_count"], int):
            if config_updates["retention_count"] >= 1:
                BACKUP_CONFIG["retention_count"] = config_updates["retention_count"]
        if "interval_minutes" in config_updates and isinstance(config_updates["interval_minutes"], int):
            if config_updates["interval_minutes"] >= 5:
                BACKUP_CONFIG["interval_minutes"] = config_updates["interval_minutes"]
        if "debounce_seconds" in config_updates and isinstance(config_updates["debounce_seconds"], int):
            if config_updates["debounce_seconds"] >= 5:
                BACKUP_CONFIG["debounce_seconds"] = config_updates["debounce_seconds"]

        logger.info(f"Updated backup config: {BACKUP_CONFIG}")
        return {"success": True, "config": dict(BACKUP_CONFIG)}
    except Exception as e:
        logger.error(f"Failed to update backup config: {e}")
        return {"success": False, "error": str(e)}


def health_check(db_path: str, backup_dir: str) -> Dict[str, Any]:
    """Check health and write capability of safe backup service."""
    try:
        db_exists = os.path.exists(db_path)
        dir_writable = os.access(os.path.dirname(backup_dir), os.W_OK) if os.path.exists(backup_dir) else True
        manifest_exists = os.path.exists(os.path.join(backup_dir, "backup_manifest.json"))
        
        status_dict = get_backup_status()
        
        return {
            "status": "healthy" if (db_exists and dir_writable) else "degraded",
            "db_exists": db_exists,
            "backup_dir_writable": dir_writable,
            "manifest_exists": manifest_exists,
            "service_status": status_dict,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Safe backup health check error: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
