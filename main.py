"""
SmartVyapar - FastAPI Main Entry Point
Starts the API server on localhost:8765
"""

import os
import sys
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import init_db, get_static_dir
from routers import (
    auth, users, parties, products,
    inventory, sales, purchases,
    expenses, payments, reports, settings,
    license, thermal, fastfood, whatsapp, employees
)
from routers import cashbook, cloud_sync
import migrations
import employee_migrations
from dashboard.router import router as dashboard_router
from fastapi import Request
from fastapi.responses import JSONResponse
import traceback

from fastapi.staticfiles import StaticFiles
from auth import require_admin

# ── App Init ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SmartVyapar API",
    description="Offline business management backend",
    version="1.2.7",
)

# Ensure static directory exists
STATIC_DIR = get_static_dir()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "x-total-count"]
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from database import get_base_data_dir
    log_dir = os.path.join(get_base_data_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "error_trace.log")
        
    # Robust Log Rotation (Keep up to 3 files, 5MB each)
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            for i in range(2, 0, -1):
                src = f"{log_path}.{i}"
                dst = f"{log_path}.{i+1}"
                if os.path.exists(src):
                    if os.path.exists(dst): os.remove(dst)
                    os.rename(src, dst)
            os.rename(log_path, log_path + ".1")
    except Exception:
        pass

    with open(log_path, "a", encoding="utf-8") as f:
        from datetime import datetime
        f.write(f"\n--- Exception at {datetime.now()} | {request.url} ---\n")
        traceback.print_exc(file=f)
    print(f"Logged 500 error to {log_path}: {exc}")
    return JSONResponse(status_code=500, content={"message": "Internal Server Error", "detail": str(exc)})

# ── Register Routers ──────────────────────────────────────────────────────────
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(parties.router, prefix="", tags=["Parties"])
app.include_router(products.router, prefix="", tags=["Products"])
app.include_router(inventory.router, prefix="/inventory", tags=["Inventory"])
app.include_router(sales.router, prefix="/sales", tags=["Sales"])
app.include_router(purchases.router, prefix="/purchases", tags=["Purchases"])
app.include_router(expenses.router, prefix="/expenses", tags=["Expenses"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])
app.include_router(reports.router, prefix="/reports", tags=["Reports"])
app.include_router(settings.router, prefix="", tags=["Settings"])
app.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(license.router, prefix="/license", tags=["License"])
app.include_router(thermal.router, prefix="", tags=["Thermal"])
app.include_router(cashbook.router, prefix="/cashbook", tags=["Cash Book"])
app.include_router(cloud_sync.router, prefix="/api/sync", tags=["Cloud Sync"])
app.include_router(fastfood.router, prefix="", tags=["FastFood"])
app.include_router(whatsapp.router, prefix="/whatsapp", tags=["WhatsApp"])
app.include_router(employees.router, prefix="/employees", tags=["Employees & Payroll"])



# ── Firebase Manual Sync Endpoint (called by mobile pull-to-refresh) ──────────
@app.post("/firebase/sync-now", tags=["Firebase"])
async def firebase_sync_now():
    """
    Immediately push latest data to Firestore.
    Called by mobile app's pull-to-refresh gesture.
    """
    try:
        from services.firebase_sync import firebase_sync as fb
        if not fb.is_enabled():
            return {"status": "skipped", "reason": "Firebase not configured"}
        from database import SessionLocal
        _fb_db = SessionLocal()
        fb.trigger_dashboard_sync(_fb_db)
        return {"status": "ok", "message": "Sync triggered"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.on_event("startup")
def on_startup():
    try:
        try:
            init_db(force_create=True)
            migrations.apply_migrations()
            print("[Core Startup] Database initialized & ready.")
        except Exception as e:
            print("Migration startup error:", e)
        
        # Start Background Sync Engine
        try:
            from services.sync_manager import SyncManager
            from database import DB_PATH_LOG
            sync_mgr = SyncManager(DB_PATH_LOG)
            sync_mgr.start()
            app.state.sync_manager = sync_mgr
        except Exception as sync_err:
            print(f"[Core Startup] Failed to start SyncManager: {sync_err}")

        # Start Firebase Firestore sync (for mobile dashboard)
        try:
            from services.firebase_sync import firebase_sync
            if firebase_sync.is_enabled():
                firebase_sync.start_periodic_sync(interval_minutes=10)
                print("[Core Startup] Firebase mobile sync started")
            else:
                print("[Core Startup] Firebase not configured — mobile cloud sync disabled")
        except Exception as fb_err:
            print(f"[Core Startup] Firebase init skipped: {fb_err}")

        # --- NEW: PERSISTENT LICENSE RECOVERY ---
        from database import SessionLocal
        from routers.license import load_license_from_file
        import models
        db = SessionLocal()
        try:
            file_lic = load_license_from_file()
            if file_lic and file_lic.get("is_activated"):
                key = file_lic.get("activation_key")
                hwid = file_lic.get("hardware_id")
                
                # Ensure DB is in sync
                db_lic = db.query(models.License).filter_by(hardware_id=hwid).first()
                if not db_lic:
                    db_lic = models.License(hardware_id=hwid)
                    db.add(db_lic)
                
                if not db_lic.is_activated or db_lic.activation_key != key:
                    print(f"[Core Startup] Restoring license from persistent shadow...")
                    db_lic.is_activated = True
                    db_lic.activation_key = key
                    db.commit()
        except Exception as lic_err:
            print(f"[Core Startup] License recovery failed: {lic_err}")
        finally: db.close()

        # Seed defaults if required
        _seed_defaults()
    except Exception as e:
        print(f"[Core Startup] Startup Error: {e}")


def _seed_defaults():
    """Insert default settings and admin user if not present."""
    from database import SessionLocal
    import models
    from auth import hash_password
    db = SessionLocal()
    try:
        # Default admin
        if not db.query(models.User).first():
            db.add(models.User(
                username="admin",
                full_name="Administrator",
                password_hash=hash_password("admin123"),
                role="admin",
            ))

        # Default units
        default_units = ["pcs", "kg", "box", "liter", "dozen", "meter", "gram"]
        for u in default_units:
            if not db.query(models.Unit).filter_by(name=u).first():
                db.add(models.Unit(name=u))

        # Default items
        if not db.query(models.Product).filter_by(code="SRVC-01").first():
            db.add(models.Product(
                name="General Service",
                code="SRVC-01",
                sell_price=0.0,
                buy_price=0.0,
                is_service=True,
                is_active=True
            ))

        db.commit()
        defaults = {
            "business_name": "My Shop",
            "business_address": "123 Main Street",
            "business_phone": "03001234567",
            "currency": "PKR",
            "currency_symbol": "Rs.",
            "tax_name": "GST",
            "tax_rate": "17",
            "invoice_prefix": "INV",
            "purchase_prefix": "PUR",
            "theme": "light",
            "thermal_width": "80",
            "print_preview": "false",
            "footer_text": "Thank you for shopping with us!",
            "has_custom_logo": "false",
            "negative_stock": "false",
            "allow_negative_cash": "false",  # Cash book: allow cash out even if balance is 0
        }
        for k, v in defaults.items():
            if not db.query(models.Setting).filter_by(key=k).first():
                db.add(models.Setting(key=k, value=v))

        # FastFood defaults
        ff_defaults = {
            "fastfood_enabled": "false",
            "ff_order_prefix": "KOT",
            "ff_kitchen_footer": "Please prepare immediately!",
        }
        for k, v in ff_defaults.items():
            if not db.query(models.Setting).filter_by(key=k).first():
                db.add(models.Setting(key=k, value=v))

        db.commit()
    finally:
        db.close()


@app.on_event("shutdown")
def on_shutdown():
    try:
        sync_mgr = getattr(app.state, "sync_manager", None)
        if sync_mgr:
            sync_mgr.stop()
    except Exception as e:
        print(f"[Shutdown] Error stopping sync manager: {e}")


@app.get("/")
def root():
    return {"app": "SmartVyapar", "status": "running", "version": "1.2.7"}


@app.post("/shutdown")
def shutdown_app():
    import threading
    import time
    from database import engine
    
    def _shutdown():
        print("[Shutdown] Running final safe backup...")
        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_manual_backup(get_db_path(), get_backup_dir())
        except Exception as e:
            print(f"[Shutdown] Backup error: {e}")

        print("[Shutdown] Flushing database connections...")
        try:
            engine.dispose()
        except Exception as e:
            print(f"[Shutdown] Engine dispose error: {e}")
        time.sleep(1.0)
        os._exit(0)
        
    threading.Thread(target=_shutdown).start()
    return {"status": "shutting down", "message": "Database flushed."}


@app.get("/health")
def health_check():
    from database import DB_PATH_LOG, get_pool_status
    db_exists = os.path.exists(DB_PATH_LOG)
    db_size = os.path.getsize(DB_PATH_LOG) if db_exists else 0
    
    status = "ok"
    if not db_exists or db_size == 0:
        status = "db_missing"
    
    return {
        "status": status,
        "db_path": DB_PATH_LOG,
        "db_exists": db_exists,
        "db_size": db_size,
        "version": "1.2.7",
        "pool": get_pool_status()
    }


@app.get("/pool-status")
def pool_status_endpoint():
    """Diagnostic endpoint: shows current DB pool configuration and usage.
    After the NullPool fix, pool_class should always be 'NullPool'.
    A NullPool means zero connection exhaustion is possible.
    """
    from database import get_pool_status
    info = get_pool_status()
    info["recommendation"] = (
        "OK - NullPool is correct for SQLite + FastAPI"
        if info.get("pool_class") == "NullPool"
        else "WARNING - QueuePool detected! Session leaks may occur."
    )
    return info


@app.on_event("shutdown")
def on_shutdown():
    try:
        from services.safe_backup_service import stop_backup_worker
        stop_backup_worker()
    except Exception as e:
        print(f"[Shutdown] Error stopping backup worker: {e}")

    try:
        sync_mgr = getattr(app.state, "sync_manager", None)
        if sync_mgr:
            sync_mgr.stop()
    except Exception as e:
        print(f"[Shutdown] Error stopping sync manager: {e}")

    try:
        from services.firebase_sync import firebase_sync
        firebase_sync.pause_for_database_restore()
    except Exception as e:
        print(f"[Shutdown] Error stopping Firebase sync: {e}")


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import time
    # Add project root to path for bundled execution
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    
    port = int(os.environ.get("SV_PORT", 8765))
    print(f"[Backend] Starting on port: {port}")
    
    # Small delay to ensure port is released by OS if just killed
    time.sleep(0.5)
    
    # Pass the app object directly instead of "main:app" for PyInstaller compatibility
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False, log_level="info")


@app.get("/")
def root():
    return {"app": "SmartVyapar", "status": "running", "version": "1.3.5"}


@app.post("/shutdown")
def shutdown_app(_current_user=Depends(require_admin)):
    import threading
    import time
    from database import engine
    
    def _shutdown():
        print("[Shutdown] Running final safe backup...")
        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_manual_backup(get_db_path(), get_backup_dir())
        except Exception as e:
            print(f"[Shutdown] Backup error: {e}")

        print("[Shutdown] Flushing database connections...")
        try:
            engine.dispose()
        except Exception as e:
            print(f"[Shutdown] Engine dispose error: {e}")
        time.sleep(1.0)
        os._exit(0)
        
    threading.Thread(target=_shutdown).start()
    return {"status": "shutting down", "message": "Database flushed."}


@app.get("/health")
def health_check():
    from database import DB_PATH_LOG, get_pool_status
    db_exists = os.path.exists(DB_PATH_LOG)
    db_size = os.path.getsize(DB_PATH_LOG) if db_exists else 0
    
    status = "ok"
    if not db_exists or db_size == 0:
        status = "db_missing"
    
    return {
        "status": status,
        "db_exists": db_exists,
        "db_size": db_size,
        "version": "1.3.5",
    }


@app.get("/pool-status")
def pool_status_endpoint(_current_user=Depends(require_admin)):
    """Diagnostic endpoint: shows current DB pool configuration and usage.
    After the NullPool fix, pool_class should always be 'NullPool'.
    A NullPool means zero connection exhaustion is possible.
    """
    from database import get_pool_status
    info = get_pool_status()
    info["recommendation"] = (
        "OK - NullPool is correct for SQLite + FastAPI"
        if info.get("pool_class") == "NullPool"
        else "WARNING - QueuePool detected! Session leaks may occur."
    )
    return info


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import time
    # Add project root to path for bundled execution
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    
    port = int(os.environ.get("SV_PORT", 8765))
    print(f"[Backend] Starting on port: {port}")
    
    # Small delay to ensure port is released by OS if just killed
    time.sleep(0.5)
    
    # Pass the app object directly instead of "main:app" for PyInstaller compatibility
    host = os.environ.get("SV_HOST", "127.0.0.1").strip() or "127.0.0.1"
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")
