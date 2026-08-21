"""
SmartVyapar - Authentication Utilities
JWT token generation + password hashing
"""

import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from database import get_db, get_user_data_root
import models

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    _external_env = Path(get_user_data_root()) / "config" / ".env"
    if _external_env.exists():
        load_dotenv(_external_env, override=False)
except Exception:
    pass


def _load_or_create_secret_key() -> str:
    """Return an installation-specific JWT key without shipping a shared secret."""
    configured = (os.environ.get("SV_SECRET") or os.environ.get("SECRET_KEY") or "").strip()
    insecure_placeholders = {
        "smartvyapar_default_secret_key",
        "smartvyapar-secret-key-2024-xK9mP",
        "your-production-secret-key-here",
    }
    if configured and configured not in insecure_placeholders and len(configured) >= 32:
        return configured

    secret_path = Path(get_user_data_root()) / "config" / "jwt_secret"
    try:
        if secret_path.exists():
            saved = secret_path.read_text(encoding="utf-8").strip()
            if len(saved) >= 32:
                return saved

        secret_path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        secret_path.write_text(generated, encoding="utf-8")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        return generated
    except OSError as exc:
        raise RuntimeError(
            "A secure JWT secret could not be loaded or persisted. Set SV_SECRET to a random value of at least 32 characters."
        ) from exc


SECRET_KEY = _load_or_create_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12  # 12 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    to_encode["type"] = "access"
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_token(token)
    if not payload:
        raise credentials_exception
        
    username: Optional[str] = payload.get("sub")
    if not username:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not user.is_active:
        raise credentials_exception

    return user


def require_admin(current_user: models.User = Depends(get_current_user)):
    if (current_user.role or "").lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin access required"
        )
    return current_user


@dataclass(frozen=True)
class BootstrapPrincipal:
    """Minimal principal used only for loopback recovery before login is possible."""

    username: str = "local-recovery"
    role: str = "admin"
    id: int = 0
    permissions: str = ""


def require_admin_or_bootstrap(
    request: Request,
    x_bootstrap_token: Optional[str] = Header(None, alias="X-Bootstrap-Token"),
    token: Optional[str] = Depends(optional_oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Allow an admin JWT or Electron's per-launch token from loopback only."""
    expected = os.environ.get("SV_BOOTSTRAP_TOKEN", "")
    client_host = request.client.host if request.client else ""
    is_loopback = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if expected and x_bootstrap_token and is_loopback:
        if hmac.compare_digest(x_bootstrap_token, expected):
            return BootstrapPrincipal()

    if token:
        payload = decode_token(token)
        username = payload.get("sub") if payload else None
        if username:
            user = db.query(models.User).filter(models.User.username == username).first()
            if user and user.is_active and (user.role or "").lower() == "admin":
                return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Administrator authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
