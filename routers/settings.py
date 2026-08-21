"""
Settings Router - Handles business settings, backup/restore, Google Drive integration,
FoxPro migration, data path management, network configuration, and system operations.
"""

import os
import json
import shutil
import socket
import logging
import traceback
import io
import csv
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request, Query
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

from database import (
    get_db, engine,
    get_backup_dir as get_backup_path,
    get_base_data_dir, get_db_path, SessionLocal
)
from auth import get_current_user, require_admin, require_admin_or_bootstrap
import models
from services import (
    google_drive_service,
    migration_service,
    backup_service,
    restore_service,
    safe_backup_service,
)

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

logger = logging.getLogger("smartvyapar.settings")
logger.setLevel(logging.INFO)

router = APIRouter()

BACKUP_DIR_DEFAULT = get_backup_path()
LAST_CLOUD_SYNC_TIME = None


# ── Helper Functions ──────────────────────────────────────────────────────────

def get_backup_dir(db: Session) -> str:
    """Get active backup directory path from settings or fallback to default."""
    path_setting = db.query(models.Setting).filter_by(key="backup_path").first()
    if path_setting and path_setting.value:
        try:
            os.makedirs(path_setting.value, exist_ok=True)
            return path_setting.value
        except Exception as e:
            logger.warning(f"Failed to use custom backup path '{path_setting.value}': {e}")
    
    os.makedirs(BACKUP_DIR_DEFAULT, exist_ok=True)
    return BACKUP_DIR_DEFAULT


def find_foxpro_folder() -> Optional[str]:
    """Dynamically locates the FoxPro backup folder in common locations."""
    ROOT_DIR = os.path.dirname(BASE_DIR)
    PARENT_ROOT = os.path.dirname(ROOT_DIR) if ROOT_DIR else None
    
    search_names = ["Fox pro", "Foxpro", "Fox pro data", "Legacy Data", "New folder", "backup", "SmartVyapar"]
    search_paths = []
    
    for base in [BASE_DIR, ROOT_DIR, PARENT_ROOT]:
        if not base:
            continue
        for name in search_names:
            search_paths.append(os.path.join(base, name))
        search_paths.append(base)
        
    user_home = os.path.expanduser("~")
    for sub in ["Documents", "Desktop", "Downloads"]:
        for name in search_names:
            search_paths.append(os.path.join(user_home, sub, name))

    if os.name == 'nt':
        for drive in ['D:\\', 'E:\\', 'F:\\', 'G:\\']:
            if os.path.exists(drive):
                for name in search_names:
                    search_paths.append(os.path.join(drive, name))

    search_paths = list(dict.fromkeys([p for p in search_paths if p]))

    for p in search_paths:
        try:
            if not os.path.exists(p) or not os.path.isdir(p):
                continue
                
            for root, dirs, files in os.walk(p):
                rel_path = os.path.relpath(root, p)
                depth = 0 if rel_path == "." else rel_path.count(os.sep) + 1
                if depth > 4:
                    dirs[:] = []
                    continue
                    
                if any(f.upper() == "ITEM.DBF" for f in files):
                    return root
        except Exception as e:
            logger.debug(f"Error checking FoxPro search path '{p}': {e}")
            continue
            
    return None


def perform_google_upload(db: Session, force: bool = False) -> bool:
    """Internal helper to execute Google Drive backup upload with throttling."""
    global LAST_CLOUD_SYNC_TIME
    
    now = datetime.now()
    if not force and LAST_CLOUD_SYNC_TIME and (now - LAST_CLOUD_SYNC_TIME).total_seconds() < 900:
        logger.info("Cloud sync skipped: recently synchronized within 15 minutes.")
        return True
    
    backup_dir = get_backup_dir(db)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"cloud_backup_{timestamp}.db")
    
    try:
        current_db = get_db_path()
        logger.info(f"Cloud sync uploading from: {current_db}")
        
        if not os.path.exists(current_db):
            logger.error(f"Cloud sync error: DB file not found at {current_db}")
            return False

        from services.safe_backup_service import perform_atomic_backup
        backup_ok, backup_error = perform_atomic_backup(current_db, backup_file)
        if not backup_ok:
            logger.error(f"Cloud sync snapshot failed: {backup_error}")
            return False
            
        file_id = google_drive_service.upload_file_to_drive(db, backup_file)
        logger.info(f"Cloud sync successful! File ID: {file_id}")
        LAST_CLOUD_SYNC_TIME = datetime.now()
        return True
    except Exception as e:
        logger.error(f"Cloud sync failed: {e}", exc_info=True)
        return False
    finally:
        if os.path.exists(backup_file):
            try:
                os.remove(backup_file)
            except OSError as cleanup_error:
                logger.warning(f"Could not remove temporary cloud snapshot: {cleanup_error}")


# ── Schemas ──────────────────────────────────────────────────────────────

class GenericSuccessResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    file: Optional[str] = None
    timestamp: Optional[str] = None
    url: Optional[str] = None
    backup: Optional[str] = None


class BackupCreateResponse(BaseModel):
    success: bool
    file: str
    timestamp: str


class BackupListItem(BaseModel):
    name: str
    path: str
    size_mb: float
    created: str


class RestoreRequest(BaseModel):
    file_path: str = Field(..., description="Absolute path to backup database file")


class PathUpdate(BaseModel):
    new_path: str = Field(..., description="Target database or directory path")


class MigrationRequest(BaseModel):
    folder_path: Optional[str] = Field(None, description="Path containing DBF files")
    wipe_first: bool = Field(False, description="Wipe existing business data before migration")


class GoogleAuthUrlResponse(BaseModel):
    url: str


class GoogleStatusResponse(BaseModel):
    connected: bool


class NetworkInfoResponse(BaseModel):
    local_ip: str
    port: int


class LogoUploadResponse(BaseModel):
    success: bool
    url: str


class FoxProCheckResponse(BaseModel):
    exists: bool
    path: str


class DataPathResponse(BaseModel):
    path: str


class SettingsHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Health Check & Export Endpoints ───────────────────────────────────────────

@router.get("/health", response_model=SettingsHealthCheckResponse)
@router.get("/settings/health", response_model=SettingsHealthCheckResponse)
@limiter.limit("1200/minute")
def settings_health(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Health check endpoint for settings and system operations."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "settings",
        "endpoints": [
            "/settings",
            "/settings/export",
            "/backup/create",
            "/backup/list",
            "/backup/diagnostics",
            "/backup/google-drive/status",
            "/system/network",
            "/system/data-path",
            "/health"
        ]
    }


@router.get("/export")
@router.get("/settings/export")
@limiter.limit("10/minute")
def export_settings(
    request: Request,
    format: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Export all system and business settings to CSV or JSON format."""
    try:
        logger.info(f"Admin '{current_user.username}' exporting settings in {format} format")
        settings = db.query(models.Setting).order_by(models.Setting.key).all()
        data = [{"key": s.key, "value": s.value} for s in settings]

        if not data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No settings records found to export")

        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

        if format == "json":
            json_str = json.dumps(data, indent=2)
            return StreamingResponse(
                io.BytesIO(json_str.encode('utf-8')),
                media_type="application/json",
                headers={
                    "Content-Disposition": f"attachment; filename=settings_{timestamp_str}.json"
                }
            )
        else:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["key", "value"])
            writer.writeheader()
            writer.writerows(data)
            output.seek(0)
            return StreamingResponse(
                io.BytesIO(output.getvalue().encode('utf-8')),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=settings_{timestamp_str}.csv"
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting settings: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export settings: {str(e)}"
        )


# ── Core Settings CRUD ────────────────────────────────────────────────────────

@router.get("/settings/pos", response_model=Dict[str, str])
@limiter.limit("1200/minute")
def get_pos_runtime_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Expose only non-sensitive POS behavior settings to authenticated cashiers."""
    allowed_keys = {
        "employee_price_mode",
        "negative_stock",
        "currency",
        "currency_symbol",
        "tax_name",
        "tax_rate",
    }
    settings = db.query(models.Setting).filter(models.Setting.key.in_(allowed_keys)).all()
    result = {setting.key: setting.value for setting in settings}
    result["employee_price_mode"] = (
        "employee"
        if str(result.get("employee_price_mode", "retail")).lower() == "employee"
        else "retail"
    )
    return result

@router.get("/settings", response_model=Dict[str, str])
@limiter.limit("1200/minute")
def get_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Fetch all key-value business settings."""
    try:
        logger.info(f"Admin '{current_user.username}' reading system settings")
        settings = db.query(models.Setting).all()
        return {s.key: s.value for s in settings}
    except Exception as e:
        logger.error(f"Error fetching settings: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch settings: {str(e)}"
        )


@router.put("/settings", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def update_settings(
    request: Request,
    data: Dict[str, str],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Update key-value business settings and trigger auto-backup."""
    try:
        logger.info(f"Admin '{current_user.username}' updating settings keys: {list(data.keys())}")
        for key, value in data.items():
            setting = db.query(models.Setting).filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                db.add(models.Setting(key=key, value=value))
        db.commit()
        
        try:
            from database import get_db_path, get_backup_dir as gbd
            from services import backup_service as bs
            bs.perform_auto_backup(get_db_path(), gbd(db))
        except Exception as e:
            logger.warning(f"Post-settings update auto-backup warning: {e}")

        return {"success": True, "message": "Settings updated successfully"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating settings: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update settings: {str(e)}"
        )


# ── Backup & Restore Endpoints ────────────────────────────────────────────────

@router.post("/backup/create", response_model=BackupCreateResponse)
@limiter.limit("10/minute")
def create_backup(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Create a manual database backup snapshot."""
    try:
        backup_dir = get_backup_dir(db)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(backup_dir, f"smartvyapar_backup_{timestamp}.db")
        logger.info(f"Admin '{current_user.username}' creating manual backup: {backup_file}")
        
        success, message = safe_backup_service.perform_atomic_backup(get_db_path(), backup_file)
        if not success:
            raise RuntimeError(message)
        return {"success": True, "file": backup_file, "timestamp": timestamp}
    except Exception as e:
        logger.error(f"Manual backup creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backup failed: {str(e)}"
        )


@router.post("/backup/auto", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def trigger_auto_backup_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Trigger automated background backup."""
    try:
        backup_dir = get_backup_dir(db)
        logger.info(f"Admin '{current_user.username}' triggering automated backup")
        backup_service.perform_auto_backup(get_db_path(), backup_dir)
        return {"success": True, "message": "Auto-backup triggered successfully"}
    except Exception as e:
        logger.error(f"Auto-backup trigger failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Auto-backup trigger failed: {str(e)}"
        )


@router.get("/backup/list", response_model=List[BackupListItem])
@limiter.limit("1200/minute")
def list_backups(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """List available local database backup files."""
    try:
        backup_dir = get_backup_dir(db)
        files = []
        if not os.path.exists(backup_dir):
            return []
        for f in os.listdir(backup_dir):
            if f.endswith(".db"):
                fp = os.path.join(backup_dir, f)
                files.append({
                    "name": f,
                    "path": fp,
                    "size_mb": round(os.path.getsize(fp) / (1024 * 1024), 2),
                    "created": datetime.fromtimestamp(os.path.getctime(fp)).isoformat()
                })
        return sorted(files, key=lambda x: x["created"], reverse=True)
    except Exception as e:
        logger.error(f"Error listing backups: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list backups: {str(e)}"
        )


@router.post("/backup/restore/{filename}", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def restore_backup(
    request: Request,
    filename: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Restore database from a local backup file by filename."""
    try:
        logger.info(f"Admin '{current_user.username}' restoring backup file '{filename}'")
        backup_dir = get_backup_dir(db)
        safe_name = os.path.basename(filename)
        if safe_name != filename or not safe_name.lower().endswith((".db", ".sqlite", ".sqlite3")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup filename")
        backup_root = os.path.realpath(backup_dir)
        backup_file = os.path.realpath(os.path.join(backup_root, safe_name))
        if os.path.commonpath([backup_root, backup_file]) != backup_root:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup path")
        
        active_db = get_db_path()
        success, msg = restore_service.perform_restore(
            backup_file,
            active_db,
            request_db=db,
            sync_manager=getattr(request.app.state, "sync_manager", None),
        )
        
        if success:
            return {"success": True, "message": "Restore successful. Software must now restart."}
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Restore failed: {msg}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error restoring backup '{filename}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restore failed: {str(e)}"
        )


@router.post("/backup/restore-manual", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def restore_manual_file(
    request: Request,
    data: RestoreRequest,
    db: Session = Depends(get_db),
    current_user = Depends(require_admin_or_bootstrap)
):
    """Restore database from a custom external file path."""
    try:
        logger.info(f"Admin '{current_user.username}' restoring manual file '{data.file_path}'")
        file_path = data.file_path
        active_db = get_db_path()
        success, msg = restore_service.perform_restore(
            file_path,
            active_db,
            request_db=db,
            sync_manager=getattr(request.app.state, "sync_manager", None),
        )
        
        if success:
            return {"success": True, "message": "Database restored! App will now restart."}
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Manual Restore Failed: {msg}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error performing manual restore: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Manual Restore Failed: {str(e)}"
        )


@router.get("/backup/diagnostics", response_model=Dict[str, Any])
@limiter.limit("1200/minute")
def get_backup_diagnostics(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Returns detailed database, storage, and backup statistics."""
    try:
        from services.safe_backup_service import get_db_stats
        db_path = get_db_path()
        current_stats = get_db_stats(db_path)
        
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            journal_mode = cursor.fetchone()[0]
            cursor.execute("SELECT sqlite_version()")
            sqlite_ver = cursor.fetchone()[0]
            conn.close()
        except Exception as e:
            logger.warning(f"SQLite PRAGMA inspection warning: {e}")
            journal_mode = "unknown"
            sqlite_ver = "unknown"

        backup_dir = get_backup_dir(db)
        manifest_path = os.path.join(backup_dir, "backup_manifest.json")
        manifest = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
            except Exception as e:
                logger.warning(f"Manifest read error: {e}")

        can_write = os.access(os.path.dirname(db_path), os.W_OK)
        cloud_warning = any(x in db_path for x in ["Google Drive", "OneDrive", "Dropbox", "iCloud"])

        from database import load_db_config, get_config_path
        config = load_db_config()
        
        return {
            "active_db": {
                "path": db_path,
                "size_mb": round(os.path.getsize(db_path) / (1024 * 1024), 2),
                "journal_mode": journal_mode,
                "sqlite_version": sqlite_ver,
                "can_write": can_write,
                "cloud_warning": cloud_warning,
                **current_stats
            },
            "config": {
                "path": get_config_path(),
                "last_restore_source": config.get("last_restore_source"),
                "last_restore_at": config.get("last_restore_at"),
                "auto_backup_enabled": config.get("auto_backup_enabled", True)
            },
            "backup_dir": backup_dir,
            "manifest": manifest
        }
    except Exception as e:
        logger.error(f"Error compiling backup diagnostics: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch backup diagnostics: {str(e)}"
        )


@router.get("/backup/preview", response_model=Dict[str, Any])
@limiter.limit("1200/minute")
def get_backup_preview(
    request: Request,
    file_path: str = Query(..., description="Backup file path to inspect"),
    current_user: models.User = Depends(require_admin)
):
    """Preview database stats of a backup file prior to restoration."""
    try:
        return restore_service.get_preview_stats(file_path)
    except Exception as e:
        logger.error(f"Error inspecting backup preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to preview backup: {str(e)}"
        )


# ── Google Drive Integration Endpoints ────────────────────────────────────────

@router.get("/backup/google-drive/auth-url", response_model=GoogleAuthUrlResponse)
@limiter.limit("1200/minute")
def get_google_auth_url(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Generate Google Drive OAuth consent authorization URL."""
    redirect_uri = "http://127.0.0.1:8765/backup/google-drive/callback"
    try:
        url = google_drive_service.get_auth_url(db, redirect_uri)
        return {"url": url}
    except Exception as e:
        logger.error(f"Google OAuth init failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google OAuth Init Failed: {e}")


@router.get("/backup/google-drive/callback")
def google_callback(
    code: str,
    state: str,
    db: Session = Depends(get_db)
):
    """OAuth callback handler for Google Drive integration."""
    redirect_uri = "http://127.0.0.1:8765/backup/google-drive/callback"
    try:
        flow = google_drive_service.get_flow(db, redirect_uri)
        verifier = google_drive_service.flow_registry.get(state)
        flow.fetch_token(code=code, code_verifier=verifier)
        google_drive_service.save_token(db, flow.credentials)
        
        if state in google_drive_service.flow_registry:
            del google_drive_service.flow_registry[state]
        
        return HTMLResponse(content="""
        <html>
            <body style="font-family:sans-serif; text-align:center; padding:100px;">
                <h1 style="color:#10b981;">Google Drive Connected!</h1>
                <p>You can close this window now and return to SmartVyapar.</p>
                <script>setTimeout(() => window.close(), 3000);</script>
            </body>
        </html>
        """)
    except Exception as e:
        logger.error(f"Google Drive callback error: {e}", exc_info=True)
        return f"Authentication failed: {e}"


@router.get("/backup/google-drive/status", response_model=GoogleStatusResponse)
@limiter.limit("1200/minute")
def google_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Check if active Google Drive credentials are linked."""
    try:
        creds = google_drive_service.load_credentials(db)
        return {"connected": creds is not None}
    except Exception as e:
        logger.error(f"Error checking Google Drive status: {e}", exc_info=True)
        return {"connected": False}


@router.post("/backup/google-drive/upload", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def google_upload(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Upload database backup to Google Drive."""
    logger.info(f"Admin '{current_user.username}' triggering manual Google Drive cloud upload")
    success = perform_google_upload(db, force=True)
    if success:
        return {"success": True, "message": "Cloud backup uploaded successfully"}
    else:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Cloud Sync Failed. Check system logs.")


@router.get("/backup/google-drive/list", response_model=List[Dict[str, Any]])
@limiter.limit("1200/minute")
def list_cloud_backups(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """List backups stored in Google Drive."""
    try:
        return google_drive_service.list_files_in_drive(db)
    except Exception as e:
        logger.error(f"Failed to list Google Drive cloud backups: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to list cloud backups: {e}")


@router.post("/backup/google-drive/restore", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def restore_cloud_backup(
    request: Request,
    file_id: str = Query(..., description="Google Drive File ID"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Download and restore database from a Google Drive file."""
    try:
        logger.info(f"Admin '{current_user.username}' restoring Google Drive file ID '{file_id}'")
        backup_dir = get_backup_dir(db)
        local_temp = os.path.join(backup_dir, "cloud_restore_temp.db")
        
        google_drive_service.download_file_from_drive(db, file_id, local_temp)
        
        active_db = get_db_path()
        success, msg = restore_service.perform_restore(
            local_temp,
            active_db,
            request_db=db,
            sync_manager=getattr(request.app.state, "sync_manager", None),
        )
        
        if success:
            return {"success": True, "message": "Cloud backup restored successfully! System will now restart."}
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Cloud Restore Failed: {msg}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cloud restore failed for file ID '{file_id}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Cloud Restore Failed: {e}")


# ── System Operations & Migration Endpoints ────────────────────────────────────

@router.get("/system/network", response_model=NetworkInfoResponse)
@limiter.limit("1200/minute")
def get_network_info(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Returns local IP address and port of this server PC for multi-device setup."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    
    current_port = int(os.environ.get("SV_PORT", 8765))
    return {"local_ip": ip, "port": current_port}


@router.post("/system/init-new-db", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def init_new_database(
    request: Request,
    current_user = Depends(require_admin_or_bootstrap)
):
    """Forces fresh database initialization."""
    from database import init_db
    try:
        logger.info(f"Admin '{current_user.username}' initiating fresh database creation")
        init_db(force_create=True)
        return {"success": True, "message": "Fresh database initialized"}
    except Exception as e:
        logger.error(f"Database initialization failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Initialization failed: {e}")


@router.post("/system/emergency-data-path", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def emergency_data_path(
    request: Request,
    req: PathUpdate,
    current_user = Depends(require_admin_or_bootstrap)
):
    """Updates database path redirect configurations in emergency mode."""
    new_path = req.new_path.strip().replace('"', '').replace("'", "")
    new_path = os.path.abspath(new_path)
    try:
        logger.warning(f"Admin '{current_user.username}' setting emergency data path: {new_path}")
        from database import load_db_config, save_db_config
        config = load_db_config()
        config["active_db_path"] = new_path
        save_db_config(config)

        for env_var in ['APPDATA', 'LOCALAPPDATA']:
            base = os.environ.get(env_var)
            if not base:
                continue
            target_config_dir = os.path.join(base, "SmartVyapar")
            os.makedirs(target_config_dir, exist_ok=True)
            redirect_file = os.path.join(target_config_dir, "datapath.txt")
            with open(redirect_file, "w") as f:
                f.write(new_path)
        return {"success": True, "message": f"Data path updated to '{new_path}'"}
    except Exception as e:
        logger.error(f"Emergency data path update failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Path update failed: {str(e)}")


@router.post("/system/wipe", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def wipe_system_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Destructive operation: Clears all transaction and inventory data while preserving license."""
    try:
        current_db = get_db_path()
        logger.warning(f"Admin '{current_user.username}' initiating system wipe on target database: {current_db}")
        
        backup_dir = get_backup_dir(db)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        emergency_file = os.path.join(backup_dir, f"emergency_before_wipe_{timestamp}.db")
        
        if os.path.exists(current_db):
            shutil.copy2(current_db, emergency_file)
        
        lic = db.query(models.License).first()
        db.execute(text("PRAGMA foreign_keys = OFF"))
        
        tables_to_wipe = [
            "sale_items", "sales", "purchase_items", "purchases", 
            "payments", "expenses", "stock_movements", "products", 
            "customers", "suppliers", "brands", "categories", "units"
        ]
        
        for table in tables_to_wipe:
            try:
                db.execute(text(f"DELETE FROM {table}"))
                db.execute(text(f"DELETE FROM sqlite_sequence WHERE name='{table}'"))
            except Exception as e:
                logger.warning(f"Wipe warning on table '{table}': {e}")

        if lic:
            db.merge(lic)
        
        db.execute(text("PRAGMA foreign_keys = ON"))
        db.commit()
        
        if lic and lic.activation_key:
            try:
                config_dir = os.path.dirname(get_base_data_dir()) 
                shadow_file = os.path.join(config_dir, "license.shadow")
                with open(shadow_file, "w") as f:
                    f.write(lic.activation_key)
            except Exception as e:
                logger.warning(f"License shadow creation warning: {e}")

        return {
            "success": True, 
            "message": "System data wiped successfully. License was PRESERVED.",
            "backup": emergency_file
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Nuclear Wipe failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Nuclear Wipe failed: {str(e)}")


@router.post("/settings/upload-logo", response_model=LogoUploadResponse)
@limiter.limit("10/minute")
async def upload_logo(
    request: Request,
    file: UploadFile = File(...),
    current_user: models.User = Depends(require_admin)
):
    """Uploads and saves company logo image."""
    from database import get_static_dir
    static_dir = get_static_dir()
    file_path = os.path.join(static_dir, "logo.png")
    
    try:
        import io
        from PIL import Image, UnidentifiedImageError

        allowed_types = {"image/png", "image/jpeg", "image/webp"}
        if file.content_type not in allowed_types:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Logo must be a PNG, JPEG, or WebP image")

        max_logo_bytes = 5 * 1024 * 1024
        content = await file.read(max_logo_bytes + 1)
        if len(content) > max_logo_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Logo file cannot exceed 5 MB")

        try:
            source_image = Image.open(io.BytesIO(content))
            source_image.verify()
            source_image = Image.open(io.BytesIO(content))
            if source_image.width * source_image.height > 16_000_000:
                raise ValueError("Image dimensions are too large")
            source_image.thumbnail((2000, 2000))
            normalized = source_image.convert("RGBA")
        except (UnidentifiedImageError, OSError, ValueError) as image_error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is not a valid image") from image_error

        logger.info(f"Admin '{current_user.username}' uploading logo '{file.filename}'")
        temp_logo_path = file_path + ".tmp"
        normalized.save(temp_logo_path, format="PNG", optimize=True)
        os.replace(temp_logo_path, file_path)
        
        db = SessionLocal()
        try:
            setting = db.query(models.Setting).filter_by(key="has_custom_logo").first()
            if setting:
                setting.value = "true"
            else:
                db.add(models.Setting(key="has_custom_logo", value="true"))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return {"success": True, "url": "/static/logo.png"}
    except HTTPException:
        raise
    except Exception as e:
        temp_logo_path = file_path + ".tmp"
        if os.path.exists(temp_logo_path):
            try:
                os.remove(temp_logo_path)
            except OSError:
                pass
        logger.error(f"Logo upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Logo upload failed: {str(e)}")


@router.post("/settings/remove-logo", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
async def remove_logo(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Removes existing company logo image."""
    from database import get_static_dir
    static_dir = get_static_dir()
    file_path = os.path.join(static_dir, "logo.png")
    
    try:
        logger.info(f"Admin '{current_user.username}' removing logo")
        if os.path.exists(file_path):
            os.remove(file_path)
        
        db = SessionLocal()
        try:
            setting = db.query(models.Setting).filter_by(key="has_custom_logo").first()
            if setting:
                setting.value = "false"
            else:
                db.add(models.Setting(key="has_custom_logo", value="false"))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return {"success": True, "message": "Logo removed successfully"}
    except Exception as e:
        logger.error(f"Logo removal failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Logo removal failed: {str(e)}")


# ── Legacy Migration (FoxPro) Endpoints ────────────────────────────────────────

@router.post("/system/migrate-foxpro", response_model=Dict[str, Any])
@limiter.limit("10/minute")
def run_foxpro_migration(
    request: Request,
    req: MigrationRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Triggers legacy FoxPro DBF migration service into SQLite."""
    try:
        logger.info(f"Admin '{current_user.username}' triggering FoxPro data migration")
        target_path = req.folder_path or find_foxpro_folder()
            
        if not target_path or not os.path.exists(target_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="FoxPro backup folder not detected. Please specify valid directory."
            )
            
        result = migration_service.migrate_foxpro_data(db, target_path, wipe=req.wipe_first)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FoxPro migration failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Migration Failed: {str(e)}")


@router.get("/system/check-foxpro", response_model=FoxProCheckResponse)
@limiter.limit("1200/minute")
def check_foxpro_status(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Identifies if a valid legacy FoxPro data folder is detected on disk."""
    try:
        path = find_foxpro_folder()
        return {
            "exists": path is not None,
            "path": path or "Not detected"
        }
    except Exception as e:
        logger.error(f"Error checking FoxPro status: {e}", exc_info=True)
        return {"exists": False, "path": "Not detected"}


# ── Data Path Management Endpoints ────────────────────────────────────────────

@router.post("/system/change-data-path", response_model=GenericSuccessResponse)
@limiter.limit("10/minute")
def change_data_path(
    request: Request,
    req: PathUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Relocates database, backups, and static assets to a new directory path."""
    new_path = req.new_path.strip()
    if not new_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Target path cannot be empty")

    new_path = os.path.abspath(new_path)
    if not os.path.exists(new_path):
        try:
            os.makedirs(new_path, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot create or access target path: {e}")
            
    current_base = get_base_data_dir()
    if os.path.abspath(current_base) == new_path:
        return {"success": True, "message": "Already using this path."}

    def get_db_data_score(db_path):
        import sqlite3
        if not db_path or not os.path.exists(db_path) or os.path.isdir(db_path):
            return -1
        try:
            with open(db_path, "rb") as f:
                if f.read(16) != b"SQLite format 3\x00":
                    return -1
            
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
            if not cur.fetchone():
                return 0
            
            cur.execute("SELECT COUNT(*) FROM products")
            p = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sales")
            s = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sale_items")
            si = cur.fetchone()[0]
            conn.close()
            return p + s + si
        except Exception:
            return 0

    active_db = get_db_path()
    db_filename = os.path.basename(active_db)
    
    target_db_dir = os.path.join(new_path, "database")
    target_db_subdir = os.path.join(target_db_dir, db_filename)
    target_static = os.path.join(new_path, "static")
    target_backup = os.path.join(new_path, "backup")

    target_score = get_db_data_score(target_db_subdir)
    logger.info(f"Target DB data score: {target_score}")

    default_base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), "SmartVyapar")
    default_db = os.path.join(default_base, "database", db_filename)
    if not os.path.exists(default_db): 
        default_db = os.path.join(default_base, db_filename)
        
    # Resolve the project root locally instead of relying on the undefined
    # BASE_DIR global.  This endpoint also runs from packaged builds where the
    # current working directory is not guaranteed to be the project folder.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root_db = os.path.join(project_root, "database", db_filename)

    scores = {
        active_db: get_db_data_score(active_db),
        default_db: get_db_data_score(default_db),
        root_db: get_db_data_score(root_db)
    }
    
    logger.info(f"Source database scores: {scores}")
    
    if target_score > 0:
        logger.info("Target directory already contains data. Updating path redirect without overwriting.")
        src_db = None
    else:
        src_db = max(scores, key=scores.get)
        if scores[src_db] <= 0:
            src_db = active_db
        logger.info(f"Selected source database to copy: {src_db} (Score: {scores[src_db]})")

    if src_db:
        src_base = os.path.dirname(os.path.dirname(src_db))
        src_static = os.path.join(src_base, "static")
        src_backup = os.path.join(src_base, "backup")
    else:
        src_static = src_backup = None
    
    try:
        logger.info(f"Checkpointing active database: {src_db}")
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(FULL)"))
        except Exception as cp_err:
            logger.warning(f"Checkpoint warning: {cp_err}")

        engine.dispose()
        os.makedirs(target_db_dir, exist_ok=True)
        
        if src_db:
            if os.path.exists(target_db_subdir):
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                target_backup_file = target_db_subdir + f".existing_{timestamp}.bak"
                logger.info(f"Target DB exists. Renaming to '{target_backup_file}'")
                try:
                    os.rename(target_db_subdir, target_backup_file)
                except Exception as e:
                    logger.warning(f"Could not rename existing target DB: {e}. Attempting direct overwrite.")

            logger.info(f"Copying database: {src_db} -> {target_db_subdir}")
            shutil.copy2(src_db, target_db_subdir)
        else:
            logger.info(f"Using existing database in target directory: {target_db_subdir}")
        
        if src_static and os.path.exists(src_static):
            os.makedirs(target_static, exist_ok=True)
            for item in os.listdir(src_static):
                s = os.path.join(src_static, item)
                d = os.path.join(target_static, item)
                if os.path.isfile(s):
                    if not os.path.exists(d) or os.path.getsize(s) != os.path.getsize(d):
                        shutil.copy2(s, d)

        if src_backup and os.path.exists(src_backup):
            os.makedirs(target_backup, exist_ok=True)
            for item in os.listdir(src_backup):
                s = os.path.join(src_backup, item)
                d = os.path.join(target_backup, item)
                if os.path.isfile(s):
                    if not os.path.exists(d):
                        shutil.copy2(s, d)
            
        from database import load_db_config, save_db_config
        config = load_db_config()
        config["active_db_path"] = target_db_subdir
        save_db_config(config)

        for env_var in ['APPDATA', 'LOCALAPPDATA']:
            base = os.environ.get(env_var)
            if not base:
                continue
            target_config_dir = os.path.join(base, "SmartVyapar")
            os.makedirs(target_config_dir, exist_ok=True)
            redirect_file = os.path.join(target_config_dir, "datapath.txt")
            with open(redirect_file, "w") as f:
                f.write(new_path)
            
        return {
            "success": True, 
            "message": "Data & License Successfully Migrated! Please RESTART the software to see your items."
        }
    except Exception as e:
        logger.error(f"Failed to relocate data path: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to migrate data: {str(e)}"
        )


@router.get("/system/data-path", response_model=DataPathResponse)
@limiter.limit("1200/minute")
def get_current_data_path(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Return active base data storage directory path."""
    try:
        return {"path": get_base_data_dir()}
    except Exception as e:
        logger.error(f"Error fetching base data dir: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get data path: {str(e)}")
