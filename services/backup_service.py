"""
Backup Service - Database backup management with safe, debounced backups,
async operations, validation, error handling, and reporting.
"""

import os
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from .safe_backup_service import trigger_safe_backup, force_safe_backup_sync

logger = logging.getLogger("smartvyapar.backup_service")
logger.setLevel(logging.INFO)


class BackupResult:
    """Class representing the result of a backup operation."""

    def __init__(self, success: bool, message: str, backup_path: Optional[str] = None):
        self.success: bool = success
        self.message: str = message
        self.backup_path: Optional[str] = backup_path
        self.timestamp: str = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Convert BackupResult object to dictionary representation."""
        return {
            "success": self.success,
            "message": self.message,
            "backup_path": self.backup_path,
            "timestamp": self.timestamp
        }


def _validate_backup_inputs(db_path: str, backup_dir: str) -> Optional[Dict[str, Any]]:
    """Validate database source file and target backup directory existence."""
    if not db_path or not isinstance(db_path, str):
        logger.error("Database path not specified or invalid type")
        return {
            "success": False,
            "message": "Database path not specified or invalid type",
            "backup_path": None
        }

    if not os.path.exists(db_path) or not os.path.isfile(db_path):
        logger.error(f"Database file not found at path: {db_path}")
        return {
            "success": False,
            "message": f"Database file not found at path: {db_path}",
            "backup_path": None
        }

    if not backup_dir or not isinstance(backup_dir, str):
        logger.error("Backup directory not specified or invalid type")
        return {
            "success": False,
            "message": "Backup directory not specified or invalid type",
            "backup_path": None
        }

    try:
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create or access backup directory '{backup_dir}': {e}")
        return {
            "success": False,
            "message": f"Failed to create or access backup directory '{backup_dir}': {str(e)}",
            "backup_path": None
        }

    return None


def perform_auto_backup(
    db_path: str,
    backup_dir: str,
    debounce_seconds: int = 60
) -> Dict[str, Any]:
    """
    Bridge function for automated, debounced single-file backups.
    Triggers background safe backup execution with input validation.

    Args:
        db_path (str): Absolute file path to active SQLite database.
        backup_dir (str): Absolute directory path to store backup copies.
        debounce_seconds (int): Minimum interval between consecutive automated backups.

    Returns:
        Dict[str, Any]: Backup result dictionary containing success status, message, and path.
    """
    try:
        validation_error = _validate_backup_inputs(db_path, backup_dir)
        if validation_error:
            return validation_error

        result_path = trigger_safe_backup(db_path, backup_dir, debounce_seconds)
        
        logger.info(f"Auto-backup successfully queued/completed: {result_path}")
        return {
            "success": True,
            "message": "Auto-backup triggered successfully",
            "backup_path": result_path
        }
    except Exception as e:
        logger.error(f"Auto-backup execution failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Auto-backup execution failed: {str(e)}",
            "backup_path": None
        }


def perform_manual_backup(
    db_path: str,
    backup_dir: str,
    force: bool = True
) -> Dict[str, Any]:
    """
    Synchronously performs an immediate database backup copy.

    Args:
        db_path (str): Absolute file path to active SQLite database.
        backup_dir (str): Absolute directory path to store backup copies.
        force (bool): Force execution bypassing debounce locks.

    Returns:
        Dict[str, Any]: Backup result dictionary containing success status, message, and path.
    """
    try:
        validation_error = _validate_backup_inputs(db_path, backup_dir)
        if validation_error:
            return validation_error

        result_path = force_safe_backup_sync(db_path, backup_dir)
        
        logger.info(f"Manual synchronous backup completed successfully: {result_path}")
        return {
            "success": True,
            "message": "Manual backup completed successfully",
            "backup_path": result_path
        }
    except Exception as e:
        logger.error(f"Manual synchronous backup failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Manual backup failed: {str(e)}",
            "backup_path": None
        }


async def perform_async_backup(
    db_path: str,
    backup_dir: str,
    auto: bool = True,
    debounce_seconds: int = 60
) -> Dict[str, Any]:
    """
    Asynchronous non-blocking wrapper for database backups using thread delegation.

    Args:
        db_path (str): Absolute file path to active SQLite database.
        backup_dir (str): Absolute directory path to store backup copies.
        auto (bool): If True uses debounced auto-backup; if False runs immediate manual backup.
        debounce_seconds (int): Minimum interval for auto backups.

    Returns:
        Dict[str, Any]: Backup result dictionary containing success status, message, and path.
    """
    try:
        if auto:
            result = await asyncio.to_thread(perform_auto_backup, db_path, backup_dir, debounce_seconds)
        else:
            result = await asyncio.to_thread(perform_manual_backup, db_path, backup_dir, True)
        return result
    except Exception as e:
        logger.error(f"Async backup wrapper failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Async backup failed: {str(e)}",
            "backup_path": None
        }


def list_backups(backup_dir: str) -> Dict[str, Any]:
    """
    List all available database backup files (.db, .sqlite) in target directory.

    Args:
        backup_dir (str): Directory path containing backup files.

    Returns:
        Dict[str, Any]: Dictionary containing backup items, count, and status.
    """
    try:
        if not backup_dir or not os.path.exists(backup_dir):
            logger.warning(f"Backup directory not found: {backup_dir}")
            return {
                "success": False,
                "message": f"Backup directory not found: {backup_dir}",
                "backups": []
            }
        
        backups: List[Dict[str, Any]] = []
        for file in os.listdir(backup_dir):
            if file.endswith('.db') or file.endswith('.sqlite'):
                file_path = os.path.join(backup_dir, file)
                try:
                    stat = os.stat(file_path)
                    try:
                        created_timestamp = stat.st_birthtime
                    except AttributeError:
                        created_timestamp = stat.st_mtime

                    backups.append({
                        "filename": file,
                        "path": file_path,
                        "size_bytes": stat.st_size,
                        "size_mb": round(stat.st_size / (1024 * 1024), 2),
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "created": datetime.fromtimestamp(created_timestamp, tz=timezone.utc).isoformat()
                    })
                except Exception as file_err:
                    logger.warning(f"Skipping unreadable backup file '{file}': {file_err}")

        backups.sort(key=lambda x: x["created"], reverse=True)
        
        logger.info(f"Enumerated {len(backups)} backup files in '{backup_dir}'")
        return {
            "success": True,
            "message": f"Found {len(backups)} backups",
            "backups": backups
        }
    except Exception as e:
        logger.error(f"Failed to list backups: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Failed to list backups: {str(e)}",
            "backups": []
        }


def restore_backup(
    backup_path: str,
    db_path: str,
    create_backup_before: bool = True
) -> Dict[str, Any]:
    """
    Restore database snapshot from backup file to active location.

    Args:
        backup_path (str): File path of backup snapshot to restore.
        db_path (str): Target database destination path.
        create_backup_before (bool): Save pre-restore safety copy of target database.

    Returns:
        Dict[str, Any]: Restoration status dictionary.
    """
    try:
        if not backup_path or not os.path.exists(backup_path) or not os.path.isfile(backup_path):
            logger.error(f"Backup file for restoration not found: {backup_path}")
            return {
                "success": False,
                "message": f"Backup file not found: {backup_path}"
            }
        
        if not db_path or not isinstance(db_path, str):
            logger.error("Target database destination path not specified")
            return {
                "success": False,
                "message": "Target database path not specified"
            }
        
        target_dir = os.path.dirname(db_path)
        if target_dir:
            Path(target_dir).mkdir(parents=True, exist_ok=True)

        if create_backup_before and os.path.exists(db_path):
            backup_dir = target_dir or "."
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            pre_restore_backup = os.path.join(backup_dir, f"pre_restore_{timestamp}.db")
            shutil.copy2(db_path, pre_restore_backup)
            logger.info(f"Pre-restore safety copy created: {pre_restore_backup}")
        
        shutil.copy2(backup_path, db_path)
        
        logger.info(f"Restored database backup from '{backup_path}' to '{db_path}'")
        return {
            "success": True,
            "message": "Restored backup successfully",
            "restore_path": db_path,
            "backup_used": backup_path
        }
    except Exception as e:
        logger.error(f"Database restoration failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Restore failed: {str(e)}"
        }


async def restore_async_backup(
    backup_path: str,
    db_path: str,
    create_backup_before: bool = True
) -> Dict[str, Any]:
    """Asynchronous non-blocking wrapper for database restoration."""
    try:
        result = await asyncio.to_thread(
            restore_backup, backup_path, db_path, create_backup_before
        )
        return result
    except Exception as e:
        logger.error(f"Async database restore failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Async restore failed: {str(e)}"
        }


def delete_old_backups(
    backup_dir: str,
    keep_days: int = 30
) -> Dict[str, Any]:
    """
    Delete backup files older than retention days threshold.

    Args:
        backup_dir (str): Backup storage directory path.
        keep_days (int): Retention window in days.

    Returns:
        Dict[str, Any]: Cleanup statistics dictionary.
    """
    try:
        if not backup_dir or not os.path.exists(backup_dir):
            return {
                "success": False,
                "message": f"Backup directory not found: {backup_dir}",
                "deleted_count": 0
            }
        
        now = datetime.now(timezone.utc)
        deleted_count = 0
        
        for file in os.listdir(backup_dir):
            if file.endswith('.db') or file.endswith('.sqlite'):
                file_path = os.path.join(backup_dir, file)
                try:
                    stat = os.stat(file_path)
                    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    days_old = (now - modified).days
                    
                    if days_old > keep_days:
                        os.remove(file_path)
                        deleted_count += 1
                        logger.info(f"Deleted old backup file: {file} ({days_old} days old)")
                except Exception as del_err:
                    logger.warning(f"Failed to delete old backup file '{file}': {del_err}")
        
        logger.info(f"Backup cleanup completed: deleted {deleted_count} files older than {keep_days} days.")
        return {
            "success": True,
            "message": f"Deleted {deleted_count} old backups",
            "deleted_count": deleted_count
        }
    except Exception as e:
        logger.error(f"Failed to clean old backups: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Failed to delete old backups: {str(e)}",
            "deleted_count": 0
        }


def get_backup_status(backup_dir: str) -> Dict[str, Any]:
    """
    Compute health metrics, file counts, and storage disk usage for backup directory.

    Args:
        backup_dir (str): Backup storage directory path.

    Returns:
        Dict[str, Any]: Health status dictionary.
    """
    try:
        result = list_backups(backup_dir)
        
        if not result["success"]:
            return {
                "status": "error",
                "message": result["message"],
                "total_backups": 0,
                "disk_usage_mb": 0
            }
        
        backups = result["backups"]
        total_size = sum(b["size_bytes"] for b in backups)
        
        return {
            "status": "healthy",
            "message": "Backup system is healthy",
            "total_backups": len(backups),
            "disk_usage_mb": round(total_size / (1024 * 1024), 2),
            "newest_backup": backups[0] if backups else None,
            "oldest_backup": backups[-1] if backups else None
        }
    except Exception as e:
        logger.error(f"Failed to compute backup status: {e}", exc_info=True)
        return {
            "status": "error",
            "message": str(e),
            "total_backups": 0,
            "disk_usage_mb": 0
        }


__all__ = [
    "perform_auto_backup",
    "perform_manual_backup",
    "perform_async_backup",
    "list_backups",
    "restore_backup",
    "restore_async_backup",
    "delete_old_backups",
    "get_backup_status",
    "BackupResult",
]
