"""
Google Drive Service - Handles OAuth2 authentication, token refresh, file upload/download,
storage quota checks, metadata retrieval, file deletion, and access revocation for SmartVyapar cloud backups.
"""

import os
import io
import time
import json
import logging
from typing import Optional, Dict, Any, List

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from sqlalchemy.orm import Session

import models
import config

logger = logging.getLogger("smartvyapar.google_drive")
logger.setLevel(logging.INFO)

SCOPES = ['https://www.googleapis.com/auth/drive.file']
flow_registry: Dict[str, str] = {}


# ── Configuration & Flow Helpers ──────────────────────────────────────────────

def validate_google_config(db: Session) -> Dict[str, Any]:
    """Validate Google OAuth Client ID and Secret configuration."""
    client_id = getattr(config, "GOOGLE_CLIENT_ID", "") or ""
    client_secret = getattr(config, "GOOGLE_CLIENT_SECRET", "") or ""
    
    is_valid = bool(client_id and client_secret and "PLACEHOLDER" not in client_id)
    return {
        "valid": is_valid,
        "client_id": client_id,
        "client_secret": client_secret
    }


def get_google_config(db: Session) -> Dict[str, str]:
    """Retrieves Google Client ID and Secret."""
    cfg = validate_google_config(db)
    return {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"]
    }


def get_flow(db: Session, redirect_uri: str) -> Flow:
    """Initialize Google OAuth Flow instance."""
    cfg = get_google_config(db)
    client_config = {
        "web": {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri]
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)


def get_auth_url(db: Session, redirect_uri: str) -> str:
    """Generate Google Drive OAuth consent authorization URL."""
    try:
        flow = get_flow(db, redirect_uri)
        auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
        flow_registry[state] = flow.code_verifier
        logger.info("Generated Google OAuth authorization URL.")
        return auth_url
    except Exception as e:
        logger.error(f"Failed to generate Google auth URL: {e}", exc_info=True)
        raise RuntimeError(f"Google OAuth Init Failed: {str(e)}")


def save_token(db: Session, credentials: Any) -> bool:
    """Persist OAuth credentials into Setting table."""
    try:
        token_json = credentials.to_json()
        data = json.loads(token_json)
        
        for key, value in data.items():
            k = f"google_{key}"
            setting = db.query(models.Setting).filter_by(key=k).first()
            if setting:
                setting.value = str(value)
            else:
                db.add(models.Setting(key=k, value=str(value)))
        db.commit()
        logger.info("Saved Google Drive credentials to database.")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to save Google token: {e}", exc_info=True)
        return False


def load_credentials(db: Session) -> Optional[Credentials]:
    """Loads and validates Google OAuth credentials from database settings with auto-refresh."""
    try:
        cfg = get_google_config(db)
        token_keys = ["token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry"]
        token_data = {}
        
        for key in token_keys:
            k = f"google_{key}"
            s = db.query(models.Setting).filter_by(key=k).first()
            if s and s.value:
                token_data[key] = s.value
                
        if not token_data.get("token"):
            logger.debug("No active Google Drive token found.")
            return None
            
        if not token_data.get("client_id") or "PLACEHOLDER" in token_data.get("client_id", ""):
            token_data["client_id"] = cfg["client_id"]
        if not token_data.get("client_secret") or "PLACEHOLDER" in token_data.get("client_secret", ""):
            token_data["client_secret"] = cfg["client_secret"]

        creds = Credentials.from_authorized_user_info(token_data, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                logger.info("Google credentials expired. Refreshing OAuth token...")
                creds.refresh(Request())
                save_token(db, creds)
                logger.info("Google credentials refreshed successfully.")
            except Exception as ref_err:
                logger.error(f"Failed to refresh Google token: {ref_err}")
                return None

        return creds
    except Exception as e:
        logger.error(f"Failed to load Google Drive credentials: {e}", exc_info=True)
        return None


# ── Cloud Operations ──────────────────────────────────────────────────────────

def upload_file_to_drive(db: Session, local_path: str, retries: int = 3) -> str:
    """Upload local backup database file to Google Drive with automatic retries."""
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Local backup file not found: {local_path}")

    creds = load_credentials(db)
    if not creds:
        raise RuntimeError("Google Drive not connected. Please login first.")
        
    service = build('drive', 'v3', credentials=creds)
    
    # 1. Find or create SmartVyapar_Backups folder
    folder_id = None
    results = service.files().list(
        q="name = 'SmartVyapar_Backups' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)"
    ).execute()
    items = results.get('files', [])
    
    if items:
        folder_id = items[0]['id']
    else:
        file_metadata = {
            'name': 'SmartVyapar_Backups',
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        folder_id = folder.get('id')
        logger.info(f"Created new Google Drive backups folder (ID: {folder_id})")

    # 2. Upload or update backup file
    backup_filename = 'smartvyapar_latest_backup.db'
    results = service.files().list(
        q=f"name = '{backup_filename}' and '{folder_id}' in parents and trashed = false",
        fields="files(id, name)"
    ).execute()
    existing_files = results.get('files', [])

    last_error = None
    for attempt in range(retries):
        try:
            media = MediaFileUpload(local_path, mimetype='application/octet-stream', resumable=True)
            if existing_files:
                file_id = existing_files[0]['id']
                logger.info(f"Updating existing Google Drive backup file (ID: {file_id})...")
                file = service.files().update(fileId=file_id, media_body=media, fields='id').execute()
            else:
                logger.info(f"Creating new Google Drive backup file '{backup_filename}'...")
                file_metadata = {
                    'name': backup_filename,
                    'parents': [folder_id]
                }
                file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                
            uploaded_id = file.get('id')
            logger.info(f"Google Drive upload SUCCESS! File ID: {uploaded_id}")
            return uploaded_id
        except Exception as err:
            last_error = err
            logger.warning(f"Google Drive upload attempt {attempt + 1} failed: {err}")
            time.sleep(1)

    logger.error(f"Google Drive upload failed after {retries} retries: {last_error}", exc_info=True)
    raise RuntimeError(f"Google Drive upload failed: {last_error}")


def list_files_in_drive(db: Session) -> List[Dict[str, Any]]:
    """List backup files stored inside SmartVyapar_Backups folder in Google Drive."""
    try:
        creds = load_credentials(db)
        if not creds:
            return []
        
        service = build('drive', 'v3', credentials=creds)
        results = service.files().list(
            q="name = 'SmartVyapar_Backups' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="files(id)"
        ).execute()
        folders = results.get('files', [])
        if not folders:
            return []
        
        folder_id = folders[0]['id']
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, createdTime, size)",
            orderBy="createdTime desc"
        ).execute()
        return results.get('files', [])
    except Exception as e:
        logger.error(f"Failed to list Google Drive files: {e}", exc_info=True)
        return []


def download_file_from_drive(db: Session, file_id: str, local_path: str) -> str:
    """Download specified backup file from Google Drive to local disk."""
    try:
        creds = load_credentials(db)
        if not creds:
            raise RuntimeError("Google Drive not connected.")
        
        service = build('drive', 'v3', credentials=creds)
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        
        target_dir = os.path.dirname(local_path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        with open(local_path, 'wb') as f:
            f.write(fh.getbuffer())
        
        logger.info(f"Downloaded Google Drive file ID '{file_id}' to '{local_path}'")
        return local_path
    except Exception as e:
        logger.error(f"Failed to download file from Google Drive: {e}", exc_info=True)
        raise RuntimeError(f"Download failed: {str(e)}")


def delete_file_from_drive(db: Session, file_id: str) -> bool:
    """Delete a backup file from Google Drive."""
    try:
        creds = load_credentials(db)
        if not creds:
            raise RuntimeError("Google Drive not connected.")
        
        service = build('drive', 'v3', credentials=creds)
        service.files().delete(fileId=file_id).execute()
        logger.info(f"Deleted Google Drive file ID '{file_id}'")
        return True
    except Exception as e:
        logger.error(f"Failed to delete Google Drive file ID '{file_id}': {e}", exc_info=True)
        return False


def get_file_metadata(db: Session, file_id: str) -> Dict[str, Any]:
    """Retrieve detailed metadata for a Google Drive file."""
    try:
        creds = load_credentials(db)
        if not creds:
            raise RuntimeError("Google Drive not connected.")
        
        service = build('drive', 'v3', credentials=creds)
        metadata = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, createdTime, modifiedTime"
        ).execute()
        return metadata
    except Exception as e:
        logger.error(f"Failed to get metadata for file ID '{file_id}': {e}", exc_info=True)
        return {}


def check_storage_quota(db: Session) -> Dict[str, Any]:
    """Check user's Google Drive storage quota."""
    try:
        creds = load_credentials(db)
        if not creds:
            return {"connected": False, "quota_mb": 0, "used_mb": 0}
        
        service = build('drive', 'v3', credentials=creds)
        about = service.about().get(fields="storageQuota").execute()
        quota = about.get("storageQuota", {})
        
        limit_bytes = int(quota.get("limit", 0))
        usage_bytes = int(quota.get("usage", 0))
        
        return {
            "connected": True,
            "limit_mb": round(limit_bytes / (1024 * 1024), 2) if limit_bytes > 0 else "Unlimited",
            "used_mb": round(usage_bytes / (1024 * 1024), 2)
        }
    except Exception as e:
        logger.error(f"Failed to fetch Google Drive storage quota: {e}", exc_info=True)
        return {"connected": False, "error": str(e)}


def health_check(db: Session) -> Dict[str, Any]:
    """Check overall Google Drive integration health."""
    try:
        creds = load_credentials(db)
        config_status = validate_google_config(db)
        return {
            "status": "healthy" if creds else "disconnected",
            "config_valid": config_status["valid"],
            "connected": creds is not None,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Google Drive health check error: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e)
        }


def revoke_access(db: Session) -> bool:
    """Revoke Google OAuth access and clean up database token settings."""
    try:
        creds = load_credentials(db)
        if creds:
            try:
                import requests
                requests.post(
                    'https://oauth2.googleapis.com/revoke',
                    params={'token': creds.token},
                    headers={'content-type': 'application/x-www-form-urlencoded'}
                )
            except Exception as req_err:
                logger.warning(f"Google token revoke HTTP call warning: {req_err}")

        token_keys = ["token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry"]
        for key in token_keys:
            k = f"google_{key}"
            setting = db.query(models.Setting).filter_by(key=k).first()
            if setting:
                db.delete(setting)
        db.commit()

        logger.info("Revoked Google Drive OAuth credentials and cleaned tokens.")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to revoke Google Drive access: {e}", exc_info=True)
        return False
