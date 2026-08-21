"""Auth router - login / logout"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from database import get_db
import models, auth as auth_utils
import threading

router = APIRouter()
INITIAL_SETUP_LOCK = threading.Lock()


class InitialAdminSetup(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, regex=r"^[A-Za-z0-9_.-]+$")
    full_name: str = Field("Administrator", min_length=2, max_length=100)
    password: str = Field(..., min_length=10, max_length=128)

try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address)
except ImportError:
    class DummyLimiter:
        def limit(self, _limit_value: str):
            def decorator(func):
                return func
            return decorator

    limiter = DummyLimiter()


@router.get("/setup-status")
def setup_status(db: Session = Depends(get_db)):
    """Report only whether a first administrator still needs to be created."""
    return {"setup_required": db.query(models.User.id).first() is None}


@router.post("/setup", status_code=status.HTTP_201_CREATED)
@limiter.limit("3/minute")
def initial_admin_setup(request: Request, data: InitialAdminSetup, db: Session = Depends(get_db)):
    """Create the first administrator exactly once; subsequent calls are rejected."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Initial setup is available only on the main computer")
    with INITIAL_SETUP_LOCK:
        if db.query(models.User.id).first() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Initial setup is already complete")

        username = data.username.strip()
        user = models.User(
            username=username,
            full_name=data.full_name.strip(),
            password_hash=auth_utils.hash_password(data.password),
            role="admin",
            permissions="*",
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except Exception:
            db.rollback()
            raise
        return {"success": True, "message": "Administrator account created securely"}


@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    if db.query(models.User.id).first() is None:
        raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="Complete initial administrator setup first")
    user = db.query(models.User).filter(models.User.username == form.username).first()
    if not user or not auth_utils.verify_password(form.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"}
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    token = auth_utils.create_access_token({"sub": user.username, "role": user.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "permissions": user.permissions,
        }
    }


@router.get("/me")
def me(current=Depends(auth_utils.get_current_user)):
    return {
        "id": current.id,
        "username": current.username,
        "full_name": current.full_name,
        "role": current.role,
        "permissions": current.permissions,
    }
