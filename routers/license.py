"""
SmartVyapar - License Management Router
Handles hardware ID validation, software activation, trial status, export/import, and history.
"""

import os
import json
import logging
import re
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import SessionLocal, get_user_data_root, get_db
from auth import require_admin
import models
import license_utils

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

logger = logging.getLogger("smartvyapar.license")
logger.setLevel(logging.INFO)

router = APIRouter()

DETACH_RESET_KEY = "DETACH_RESET"


# ── Schemas ──────────────────────────────────────────────────────────────

class LicenseActivateRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=100, description="Activation key or DETACH_RESET command")


class LicenseStatusResponse(BaseModel):
    hardware_id: str
    is_activated: bool
    trial_days_left: int
    is_expired: bool
    install_date: Optional[str] = None


class LicenseActivateResponse(BaseModel):
    status: str
    message: str


class LicenseExportResponse(BaseModel):
    hardware_id: str
    is_activated: bool
    activation_key: Optional[str] = None
    installation_date: str
    expiry_date: str
    history: Optional[List[Dict[str, Any]]] = None


class LicenseImportRequest(BaseModel):
    hardware_id: str
    is_activated: bool
    activation_key: Optional[str] = None
    installation_date: str
    expiry_date: str
    history: Optional[List[Dict[str, Any]]] = None


class LicenseImportResponse(BaseModel):
    status: str
    message: str


class LicenseHistoryItem(BaseModel):
    action: str
    timestamp: str
    key_used: Optional[str] = None
    details: Optional[str] = None


class LicenseHistoryResponse(BaseModel):
    hardware_id: str
    history: List[LicenseHistoryItem]


class LicenseVerifyResponse(BaseModel):
    hardware_id: str
    valid: bool
    is_activated: bool
    days_remaining: int
    expiry_date: str


class LicenseRefreshResponse(BaseModel):
    status: str
    message: str
    new_expiry_date: str
    trial_days_left: int


class LicenseHealthResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    endpoints: List[str]


# ── Helper Functions ──────────────────────────────────────────────────────────

def get_license_dir() -> str:
    path = os.path.join(get_user_data_root(), "license")
    os.makedirs(path, exist_ok=True)
    return path


def get_license_file() -> str:
    return os.path.join(get_license_dir(), "license.json")


def load_license_from_file() -> Optional[Dict[str, Any]]:
    path = get_license_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading license from file {path}: {e}")
    return None


def save_license_to_file(data: Dict[str, Any]) -> bool:
    path = get_license_file()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"[License] Save failed: {e}", exc_info=True)
        return False


def _append_license_history(lic_data: Dict[str, Any], action: str, key_used: Optional[str] = None, details: Optional[str] = None):
    if "history" not in lic_data or not isinstance(lic_data["history"], list):
        lic_data["history"] = []
    
    event = {
        "action": action,
        "timestamp": datetime.now().isoformat(),
        "key_used": key_used,
        "details": details or f"Action '{action}' performed."
    }
    lic_data["history"].append(event)


# ── Endpoints ──────────────────────────────────────────────────────────

@router.get("/health", response_model=LicenseHealthResponse)
@limiter.limit("1200/minute")
def license_health(request: Request):
    """Health check endpoint for license module."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "license",
        "endpoints": [
            "/status",
            "/activate",
            "/export",
            "/import",
            "/history",
            "/verify",
            "/refresh",
            "/health"
        ]
    }


@router.get("/status", response_model=LicenseStatusResponse)
@limiter.limit("1200/minute")
def get_license_status(request: Request, db: Session = Depends(get_db)):
    """
    Retrieves current license status, trial days left, and activation state.
    """
    try:
        hwid = license_utils.get_hardware_id()
        logger.info(f"Checking license status for HWID: {hwid}")
        
        # 1. Authoritative check: license.json
        file_lic = load_license_from_file()
        
        # 2. Secondary check: database (for migration/sync)
        db_lic = db.query(models.License).filter_by(hardware_id=hwid).first()
        
        # --- MIGRATION / SYNC LOGIC ---
        if db_lic and not file_lic:
            # Older permanent licenses stored NULL in expiry_date.  Treat those
            # as non-expiring instead of crashing while building the JSON
            # shadow record.
            migrated_expiry = db_lic.expiry_date
            if migrated_expiry is None:
                migrated_expiry = (
                    datetime(9999, 12, 31, 23, 59, 59)
                    if db_lic.is_activated
                    else datetime.now() + timedelta(days=7)
                )
            file_lic = {
                "hardware_id": hwid,
                "activation_key": db_lic.activation_key,
                "is_activated": db_lic.is_activated,
                "installation_date": db_lic.installation_date.isoformat(),
                "expiry_date": migrated_expiry.isoformat(),
                "history": []
            }
            _append_license_history(file_lic, "migrated", db_lic.activation_key, "Migrated database license to JSON file.")
            save_license_to_file(file_lic)
            logger.info("[License] Migrated database license to JSON shadow file.")
        
        if not file_lic:
            install_date = datetime.now()
            expiry_date = install_date + timedelta(days=7)
            file_lic = {
                "hardware_id": hwid,
                "activation_key": None,
                "is_activated": False,
                "installation_date": install_date.isoformat(),
                "expiry_date": expiry_date.isoformat(),
                "history": []
            }
            _append_license_history(file_lic, "created", None, "Initial trial license created.")
            save_license_to_file(file_lic)
            
            if not db_lic:
                db_lic = models.License(
                    hardware_id=hwid,
                    installation_date=install_date,
                    expiry_date=expiry_date,
                    is_activated=False
                )
                db.add(db_lic)
                db.commit()

        stored_key = file_lic.get("activation_key") or ""
        is_activated = bool(
            file_lic.get("is_activated", False)
            and file_lic.get("hardware_id") == hwid
            and license_utils.verify_activation_key(hwid, stored_key)
        )
        if file_lic.get("is_activated") and not is_activated:
            logger.warning("Stored license failed integrity validation and was deactivated")
            file_lic["is_activated"] = False
            file_lic["activation_key"] = None
            _append_license_history(file_lic, "integrity_failed", None, "Stored activation data failed validation.")
            save_license_to_file(file_lic)
        expiry_str = file_lic.get("expiry_date")
        expiry = (
            datetime.fromisoformat(expiry_str)
            if expiry_str
            else (datetime(9999, 12, 31, 23, 59, 59) if is_activated else datetime.now())
        )
        days_left = (expiry - datetime.now()).days
        if days_left < 0:
            days_left = 0
        
        return {
            "hardware_id": hwid,
            "is_activated": is_activated,
            "trial_days_left": days_left,
            "is_expired": (not is_activated and days_left <= 0),
            "install_date": file_lic.get("installation_date")
        }
    except Exception as e:
        logger.error(f"Error fetching license status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve license status: {str(e)}"
        )


@router.post("/activate", response_model=LicenseActivateResponse)
@limiter.limit("5/minute")
def activate_software(request: Request, payload: LicenseActivateRequest, db: Session = Depends(get_db)):
    """
    Activates software with valid license key or detaches license using DETACH_RESET constant.
    """
    try:
        key = re.sub(r"[\s-]+", "", payload.key).upper()
        hwid = license_utils.get_hardware_id()
        logger.info(f"Activation attempt on HWID {hwid}")
        
        if key == DETACH_RESET_KEY:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="License detach requires administrator authentication")

        if license_utils.verify_activation_key(hwid, key):
            file_lic = load_license_from_file() or {"hardware_id": hwid}
            file_lic.update({
                "hardware_id": hwid,
                "is_activated": True,
                "activation_key": key,
                "installation_date": file_lic.get("installation_date", datetime.now().isoformat())
            })
            _append_license_history(file_lic, "activated", None, "Software successfully activated.")
            if not save_license_to_file(file_lic):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Activation key was valid, but the license file could not be saved. Check AppData permissions."
                )
            
            db_lic = db.query(models.License).filter_by(hardware_id=hwid).first()
            if not db_lic:
                db_lic = models.License(hardware_id=hwid)
                db.add(db_lic)
            
            db_lic.is_activated = True
            db_lic.activation_key = key
            db.commit()
            
            logger.info(f"Software successfully activated for HWID {hwid}")
            return {"status": "success", "message": "Software Activated Successfully!"}
        
        logger.warning(f"Invalid activation key submitted for HWID {hwid}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Activation Key for this PC."
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error activating software: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Activation process failed: {str(e)}"
        )


@router.post("/detach", response_model=LicenseActivateResponse)
@limiter.limit("3/minute")
def detach_license(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin),
):
    hwid = license_utils.get_hardware_id()
    file_lic = load_license_from_file() or {"hardware_id": hwid}
    file_lic["is_activated"] = False
    file_lic["activation_key"] = None
    _append_license_history(file_lic, "detached", None, f"Detached by admin '{current_user.username}'.")
    if not save_license_to_file(file_lic):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not update license file")

    db_lic = db.query(models.License).filter_by(hardware_id=hwid).first()
    if db_lic:
        db_lic.is_activated = False
        db_lic.activation_key = None
        db.commit()
    logger.info(f"Admin '{current_user.username}' detached the license for HWID {hwid}")
    return {"status": "success", "message": "License detached successfully"}


@router.post("/export", response_model=LicenseExportResponse)
@limiter.limit("5/minute")
def export_license(
    request: Request,
    current_user: models.User = Depends(require_admin)
):
    """Returns license configuration for manual backup (Admin only)."""
    try:
        logger.info(f"Admin '{current_user.username}' exporting license configuration")
        lic = load_license_from_file()
        if not lic:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No license configuration file found to export."
            )
        return lic
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting license: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"License export failed: {str(e)}"
        )


@router.post("/import", response_model=LicenseImportResponse)
@limiter.limit("5/minute")
def import_license(
    request: Request,
    payload: LicenseImportRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Imports license configuration from backup (Admin only)."""
    try:
        user_str = current_user.username
        logger.info(f"Admin '{user_str}' importing license configuration")
        hwid = license_utils.get_hardware_id()
        
        if payload.hardware_id != hwid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="License backup hardware ID does not match current hardware ID."
            )

        if payload.is_activated and (
            not payload.activation_key
            or not license_utils.verify_activation_key(hwid, payload.activation_key)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Imported activation data failed integrity validation."
            )

        lic_data = payload.dict()
        _append_license_history(lic_data, "imported", payload.activation_key, f"Imported by admin '{user_str}'.")
        save_license_to_file(lic_data)
        
        db_lic = db.query(models.License).filter_by(hardware_id=hwid).first()
        if not db_lic:
            db_lic = models.License(hardware_id=hwid)
            db.add(db_lic)
        
        db_lic.is_activated = payload.is_activated
        db_lic.activation_key = payload.activation_key
        db_lic.installation_date = datetime.fromisoformat(payload.installation_date)
        db_lic.expiry_date = datetime.fromisoformat(payload.expiry_date)
        db.commit()
        
        logger.info(f"License successfully imported by '{user_str}'")
        return {"status": "success", "message": "License Imported Successfully!"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error importing license: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"License import failed: {str(e)}"
        )


@router.get("/history", response_model=LicenseHistoryResponse)
@limiter.limit("1200/minute")
def get_license_history(
    request: Request,
    current_user: models.User = Depends(require_admin),
):
    """Retrieves license activation and state change history."""
    try:
        hwid = license_utils.get_hardware_id()
        logger.info(f"Fetching license history for HWID {hwid}")
        file_lic = load_license_from_file() or {}
        raw_history = file_lic.get("history", [])
        
        history_items = [
            {
                "action": h.get("action", "unknown"),
                "timestamp": h.get("timestamp", datetime.now().isoformat()),
                "key_used": h.get("key_used"),
                "details": h.get("details")
            }
            for h in raw_history
        ]
        
        return {
            "hardware_id": hwid,
            "history": history_items
        }
    except Exception as e:
        logger.error(f"Error fetching license history: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve license history: {str(e)}"
        )


@router.get("/verify", response_model=LicenseVerifyResponse)
@limiter.limit("1200/minute")
def verify_license_status(request: Request):
    """Verifies whether the current license is valid and active."""
    try:
        hwid = license_utils.get_hardware_id()
        file_lic = load_license_from_file() or {}
        is_activated = file_lic.get("is_activated", False)
        expiry_str = file_lic.get("expiry_date", datetime.now().isoformat())
        expiry = datetime.fromisoformat(expiry_str)
        days_left = (expiry - datetime.now()).days
        if days_left < 0:
            days_left = 0
            
        valid = is_activated or (days_left > 0)
        
        return {
            "hardware_id": hwid,
            "valid": valid,
            "is_activated": is_activated,
            "days_remaining": days_left,
            "expiry_date": expiry_str
        }
    except Exception as e:
        logger.error(f"Error verifying license status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify license status: {str(e)}"
        )


@router.post("/refresh", response_model=LicenseRefreshResponse)
@limiter.limit("5/minute")
def refresh_trial_period(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin),
):
    """Refreshes trial period if software is not yet permanently activated."""
    try:
        hwid = license_utils.get_hardware_id()
        logger.info(f"Trial refresh requested for HWID {hwid}")
        file_lic = load_license_from_file() or {}
        
        if file_lic.get("is_activated", False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Software is already permanently activated."
            )
            
        new_expiry = datetime.now() + timedelta(days=7)
        file_lic["expiry_date"] = new_expiry.isoformat()
        _append_license_history(file_lic, "refreshed", None, "Trial period refreshed for 7 additional days.")
        save_license_to_file(file_lic)
        
        db_lic = db.query(models.License).filter_by(hardware_id=hwid).first()
        if db_lic:
            db_lic.expiry_date = new_expiry
            db.commit()
            
        return {
            "status": "success",
            "message": "Trial period refreshed successfully!",
            "new_expiry_date": new_expiry.isoformat(),
            "trial_days_left": 7
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error refreshing trial period: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to refresh trial period: {str(e)}"
        )
