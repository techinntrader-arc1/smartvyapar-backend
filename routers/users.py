"""
Users Router - Production-Ready User Management Module
Handles CRUD operations, admin user management, self-profile updates,
audit logging, pagination, search, and credential management.
"""

import logging
import traceback
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from database import get_db
import models
from auth import get_current_user, require_admin, hash_password

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

logger = logging.getLogger("smartvyapar.users")
logger.setLevel(logging.INFO)

router = APIRouter()


# ── Audit Logging Helper ──────────────────────────────────────────────────────

def log_audit(action: str, target_user_id: int, performed_by_id: int, details: str = ""):
    """Logs administrative and security audit events for user account mutations."""
    logger.info(
        f"[AUDIT LOG] Action: {action} | Target User ID: {target_user_id} | "
        f"Performed By User ID: {performed_by_id} | Details: {details}"
    )


# ── Schemas ──────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="Unique username handle")
    full_name: Optional[str] = Field(None, max_length=100, description="User full display name")
    password: str = Field(..., min_length=8, max_length=128, description="Raw initial password")
    role: str = Field(default="staff", description="Assigned role (e.g., admin, staff)")
    permissions: Optional[str] = Field(
        "billing,sales-list,customers,suppliers,payments,stock-report,license-info,help,labels",
        description="Comma-separated permission keys"
    )


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, max_length=100)
    role: Optional[str] = Field(None)
    is_active: Optional[bool] = Field(None)
    password: Optional[str] = Field(None, min_length=8, max_length=128)
    permissions: Optional[str] = Field(None)


class UserSelfUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=50)
    full_name: Optional[str] = Field(None, max_length=100)
    password: Optional[str] = Field(None, min_length=8, max_length=128)


class AdminPasswordReset(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128, description="New administrative override password")


class UserResponse(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    role: str
    is_active: bool
    permissions: Optional[str] = None

    class Config:
        orm_mode = True


class UserListResponse(BaseModel):
    success: bool = True
    total: int
    page: int
    limit: int
    users: List[UserResponse]


class UserCreateResponse(BaseModel):
    success: bool = True
    message: str
    user: UserResponse


class UserUpdateResponse(BaseModel):
    success: bool = True
    message: str
    user: UserResponse


class UserSelfUpdateResponse(BaseModel):
    success: bool = True
    message: str
    user: UserResponse


class UserDeleteResponse(BaseModel):
    success: bool = True
    message: str
    deleted_user_id: int


class UserHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    total_users: int
    active_users: int


# ── Health & Self Profile Endpoints ───────────────────────────────────────────

@router.get("/health", response_model=UserHealthCheckResponse)
@router.get("/users/health", response_model=UserHealthCheckResponse)
@limiter.limit("1200/minute")
def users_health_check(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Module health check endpoint returning user count metrics."""
    try:
        total = db.query(models.User).count()
        active = db.query(models.User).filter_by(is_active=True).count()
        return {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_users": total,
            "active_users": active
        }
    except Exception as e:
        logger.error(f"Users health check failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Users module health check failed: {str(e)}"
        )


@router.get("/me", response_model=UserResponse)
@router.get("/users/me", response_model=UserResponse)
@limiter.limit("1200/minute")
def get_current_user_profile(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves the authenticated user's own account profile information."""
    try:
        return current_user
    except Exception as e:
        logger.error(f"Failed to fetch profile for user ID {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve profile: {str(e)}"
        )


@router.put("/me", response_model=UserSelfUpdateResponse)
@router.put("/users/me", response_model=UserSelfUpdateResponse)
@limiter.limit("10/minute")
def update_self(
    request: Request,
    data: UserSelfUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Allows logged-in user to update their username, display name, or password."""
    try:
        updated_fields = []
        if data.username and data.username.strip() != current_user.username:
            new_username = data.username.strip()
            existing = db.query(models.User).filter_by(username=new_username).first()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username already taken by another account."
                )
            current_user.username = new_username
            updated_fields.append("username")

        if data.full_name is not None:
            current_user.full_name = data.full_name.strip()
            updated_fields.append("full_name")

        if data.password:
            current_user.password_hash = hash_password(data.password)
            updated_fields.append("password")

        db.commit()
        db.refresh(current_user)

        log_audit(
            action="SELF_UPDATE",
            target_user_id=current_user.id,
            performed_by_id=current_user.id,
            details=f"Updated fields: {', '.join(updated_fields) if updated_fields else 'None'}"
        )

        return {
            "success": True,
            "message": "Profile updated successfully.",
            "user": current_user
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Self update failed for user ID {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Self-update failed: {str(e)}"
        )


# ── Collection & CRUD Endpoints ───────────────────────────────────────────────

@router.get("", response_model=List[UserResponse])
@router.get("/", response_model=List[UserResponse])
@limiter.limit("1200/minute")
def list_users(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    search: Optional[str] = Query(None, description="Search term for username or full name"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Lists system users with search filter and pagination support (Admin only)."""
    try:
        logger.info(f"Admin '{current_user.username}' listing users (search={search}, page={page}, limit={limit})")
        query = db.query(models.User)
        if search:
            search_pattern = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    models.User.username.ilike(search_pattern),
                    models.User.full_name.ilike(search_pattern)
                )
            )

        offset = (page - 1) * limit
        users = query.order_by(models.User.id.asc()).offset(offset).limit(limit).all()
        return users
    except Exception as e:
        logger.error(f"Failed to query user listing: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user list: {str(e)}"
        )


@router.post("", response_model=UserCreateResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=UserCreateResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def create_user(
    request: Request,
    data: UserCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Creates a new user account (Admin only)."""
    try:
        clean_username = data.username.strip()
        logger.info(f"Admin '{current_user.username}' creating user '{clean_username}' with role '{data.role}'")
        if db.query(models.User).filter_by(username=clean_username).first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists."
            )

        user = models.User(
            username=clean_username,
            full_name=data.full_name.strip() if data.full_name else clean_username,
            password_hash=hash_password(data.password),
            role=data.role.strip().lower(),
            permissions=data.permissions,
            is_active=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        log_audit(
            action="CREATE_USER",
            target_user_id=user.id,
            performed_by_id=current_user.id,
            details=f"Created user '{user.username}' with role '{user.role}'"
        )

        return {
            "success": True,
            "message": "User created successfully.",
            "user": user
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed creating user '{data.username}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"User creation failed: {str(e)}"
        )


# ── Parameterized User Instance Endpoints ───────────────────────────────────────

@router.get("/{user_id}", response_model=UserResponse)
@limiter.limit("1200/minute")
def get_user_by_id(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Fetches detailed account information for a single user by ID (Admin only)."""
    try:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found."
            )
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading details for user ID {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve user details: {str(e)}"
        )


@router.put("/{user_id}", response_model=UserUpdateResponse)
@limiter.limit("10/minute")
def update_user(
    request: Request,
    user_id: int,
    data: UserUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Updates administrative attributes of a user account (Admin only)."""
    try:
        logger.info(f"Admin '{current_user.username}' updating user ID {user_id}")
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found."
            )

        changes = []
        if data.full_name is not None:
            user.full_name = data.full_name.strip()
            changes.append("full_name")
        if data.role is not None:
            new_role = data.role.strip().lower()
            if user.role != new_role:
                changes.append(f"role ({user.role} -> {new_role})")
                user.role = new_role
        if data.is_active is not None:
            if user.id == current_user.id and not data.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You cannot deactivate your own account."
                )
            if user.is_active != data.is_active:
                changes.append(f"is_active ({user.is_active} -> {data.is_active})")
                user.is_active = data.is_active
        if data.password:
            user.password_hash = hash_password(data.password)
            changes.append("password")
        if data.permissions is not None:
            user.permissions = data.permissions
            changes.append("permissions")

        db.commit()
        db.refresh(user)

        log_audit(
            action="UPDATE_USER",
            target_user_id=user.id,
            performed_by_id=current_user.id,
            details=f"Modified: {', '.join(changes) if changes else 'None'}"
        )

        return {
            "success": True,
            "message": "User updated successfully.",
            "user": user
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update user ID {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"User update failed: {str(e)}"
        )


@router.post("/{user_id}/reset-password", response_model=UserUpdateResponse)
@limiter.limit("10/minute")
def reset_user_password(
    request: Request,
    user_id: int,
    data: AdminPasswordReset,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Resets a specified user's password directly (Admin only)."""
    try:
        logger.info(f"Admin '{current_user.username}' resetting password for user ID {user_id}")
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found."
            )

        user.password_hash = hash_password(data.new_password)
        db.commit()
        db.refresh(user)

        log_audit(
            action="ADMIN_RESET_PASSWORD",
            target_user_id=user.id,
            performed_by_id=current_user.id,
            details="Password forcibly reset by admin"
        )

        return {
            "success": True,
            "message": f"Password reset successfully for user '{user.username}'.",
            "user": user
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to reset password for user ID {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Password reset failed: {str(e)}"
        )


@router.delete("/{user_id}", response_model=UserDeleteResponse)
@limiter.limit("10/minute")
def delete_user(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin)
):
    """Deletes a user account (Admin only). Prevents self-deletion."""
    try:
        logger.info(f"Admin '{current_user.username}' deleting user ID {user_id}")
        if user_id == current_user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot delete your own account."
            )

        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found."
            )

        deleted_username = user.username
        db.delete(user)
        db.commit()

        log_audit(
            action="DELETE_USER",
            target_user_id=user_id,
            performed_by_id=current_user.id,
            details=f"Deleted user handle '{deleted_username}'"
        )

        return {
            "success": True,
            "message": f"User '{deleted_username}' deleted successfully.",
            "deleted_user_id": user_id
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed deleting user ID {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"User deletion failed: {str(e)}"
        )
