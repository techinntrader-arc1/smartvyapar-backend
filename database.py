"""
SmartVyapar - Database Configuration
SQLite database with SQLAlchemy ORM
"""

import os
import sys
import shutil
import json
import logging
import sqlite3
from datetime import datetime

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

# Set up module-level logger
logger = logging.getLogger(__name__)


# ── Project Root Detection (Backward Compatibility) ───────────────────────────
if getattr(sys, 'frozen', False):
    EXE_DIR = os.path.dirname(sys.executable)
    BASE_DIR = os.path.dirname(EXE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Configuration Paths ────────────────────────────────────────────────────────
def get_user_data_root():
    """Returns the platform-specific userData/Roaming root for SmartVyapar."""
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA')
        if not appdata:
            appdata = os.path.expanduser('~\\AppData\\Roaming')
        return os.path.join(appdata, "SmartVyapar")
    return os.path.join(os.path.expanduser('~'), ".SmartVyapar")


def get_config_path():
    """Returns path to database_config.json without side-effects."""
    return os.path.join(get_user_data_root(), "config", "database_config.json")


def load_db_config():
    """Loads database_config.json if present without filesystem side-effects."""
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to read database config at %s: %s", path, e)
    return {}


def save_db_config(config):
    """Saves database_config.json, creating target directory if needed."""
    path = get_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error("Failed to save database config to %s: %s", path, e)


def _is_drive_root(path_str):
    """Returns True if the path represents a drive root (e.g. 'C:\\' or 'D:\\')."""
    if not path_str:
        return True
    norm = os.path.normpath(path_str)
    parent = os.path.dirname(norm)
    return norm == parent or os.path.splitdrive(norm)[1] in ('\\', '/', '')


def get_base_data_dir():
    """Intelligently calculates base data directory without side-effects."""
    # 1. SV_DB_PATH Environment Variable (Priority 1)
    env_path = os.environ.get("SV_DB_PATH")
    if env_path and os.path.isdir(env_path):
        return os.path.normpath(env_path)

    # 2. database_config.json (Priority 2)
    config = load_db_config()
    custom_path = config.get("active_db_path")
    if custom_path:
        if os.path.isdir(custom_path):
            base_dir = custom_path
        else:
            parent = os.path.dirname(custom_path)
            if os.path.basename(parent).lower() == "database":
                base_dir = os.path.dirname(parent)
            else:
                base_dir = parent

        if not _is_drive_root(base_dir):
            return os.path.normpath(base_dir)
        elif not _is_drive_root(os.path.dirname(custom_path)):
            return os.path.normpath(os.path.dirname(custom_path))

    # 3. Default Fallback
    return get_user_data_root()


def get_db_path():
    """Resolves active database file location as a string path without side-effects."""
    config = load_db_config()
    full_path = config.get("active_db_path")

    if full_path:
        if os.path.isdir(full_path):
            db_path = os.path.join(full_path, "database", "smartvyapar.db")
        else:
            db_path = os.path.normpath(full_path)
    else:
        base_dir = get_base_data_dir()
        db_path = os.path.join(base_dir, "database", "smartvyapar.db")

    return os.path.normpath(db_path)


# Database connection string (defaults to SQLite, supports MySQL/PostgreSQL via env)
ENV_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if ENV_DATABASE_URL:
    DATABASE_URL = ENV_DATABASE_URL
    DB_PATH_LOG = ENV_DATABASE_URL
else:
    DB_PATH_LOG = get_db_path()
    DATABASE_URL = f"sqlite:///{DB_PATH_LOG}"

# ── Engine Initialization ──────────────────────────────────────────────────────
is_sqlite = DATABASE_URL.startswith("sqlite")
engine_kwargs = {}
if is_sqlite:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

engine = create_engine(DATABASE_URL, **engine_kwargs)

if is_sqlite:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")  # 30 seconds timeout
        cursor.execute("PRAGMA cache_size=-64000;")   # 64MB Cache
        cursor.execute("PRAGMA mmap_size=268435456;")  # 256MB Memory Map
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: provides a DB session with guaranteed cleanup.
    Rolls back on any exception so connection is never left in bad state.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_pool_status() -> dict:
    """Returns diagnostic info about current engine pool."""
    pool = engine.pool
    info = {"pool_class": type(pool).__name__}
    for attr in ("size", "checkedout", "overflow", "checkedin"):
        fn = getattr(pool, attr, None)
        if callable(fn):
            try:
                info[attr] = fn()
            except Exception:
                info[attr] = "n/a"
    return info


# ── Persistent Triggers Management ────────────────────────────────────────────
def register_persistent_triggers(conn, *, raise_errors=False):
    """Register guarded sync triggers and immutable-financial delete guards."""
    cursor = None
    try:
        cursor = conn.connection.cursor() if hasattr(conn, 'connection') else conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sync_queue'")
        if not cursor.fetchone():
            return
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_control (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                skip_sync INTEGER NOT NULL DEFAULT 0 CHECK(skip_sync IN (0, 1))
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO sync_control(id, skip_sync) VALUES (1, 0)")

        tables = [
            "users", "categories", "units", "brands", "customers", "suppliers",
            "products", "sales", "sale_items", "purchases", "purchase_items",
            "payments", "expenses", "stock_movements", "settings",
            "cash_transactions", "day_closings", "external_funds",
            "employees", "employee_ledger_entries", "employee_payrolls",
            "employee_salary_payments", "employee_goods_credits",
            "employee_goods_credit_items", "sale_returns",
            "sale_return_items"
        ]
        protected_delete_tables = {
            "employees", "employee_ledger_entries", "employee_payrolls",
            "employee_salary_payments", "employee_goods_credits",
            "employee_goods_credit_items", "sale_returns", "sale_return_items",
            "stock_movements",
        }

        for table in tables:
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if not cursor.fetchone():
                continue

            pk = "key" if table == "settings" else "id"

            # Recreate legacy triggers because older versions enqueued remote
            # upserts again: their temporary skip_sync table was never read by
            # the persistent trigger body.
            for suffix in ("insert", "update", "delete"):
                cursor.execute(f"DROP TRIGGER IF EXISTS trg_{table}_{suffix}")

            cursor.execute(f"""
            CREATE TRIGGER trg_{table}_insert
            AFTER INSERT ON {table}
            WHEN COALESCE((SELECT skip_sync FROM sync_control WHERE id = 1), 0) = 0
            BEGIN
                INSERT INTO sync_queue (table_name, record_id, action)
                VALUES ('{table}', NEW.{pk}, 'INSERT');
            END;
            """)

            cursor.execute(f"""
            CREATE TRIGGER trg_{table}_update
            AFTER UPDATE ON {table}
            WHEN COALESCE((SELECT skip_sync FROM sync_control WHERE id = 1), 0) = 0
            BEGIN
                INSERT INTO sync_queue (table_name, record_id, action)
                VALUES ('{table}', NEW.{pk}, 'UPDATE');
            END;
            """)

            if table == "sales":
                cursor.execute(f"""
                CREATE TRIGGER trg_{table}_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT CASE WHEN
                        COALESCE(OLD.status, '') <> 'held'
                        OR EXISTS (SELECT 1 FROM employee_goods_credits WHERE sale_id = OLD.id)
                        OR EXISTS (SELECT 1 FROM employee_ledger_entries WHERE sale_id = OLD.id)
                        OR EXISTS (SELECT 1 FROM sale_returns WHERE sale_id = OLD.id)
                    THEN RAISE(ABORT, 'Posted sale history cannot be deleted; use a return') END;
                    INSERT INTO sync_queue (table_name, record_id, action)
                    SELECT '{table}', OLD.{pk}, 'DELETE'
                    WHERE COALESCE((SELECT skip_sync FROM sync_control WHERE id = 1), 0) = 0;
                END;
                """)
            elif table == "sale_items":
                cursor.execute(f"""
                CREATE TRIGGER trg_{table}_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT CASE WHEN
                        EXISTS (
                            SELECT 1 FROM sales
                            WHERE sales.id = OLD.sale_id AND COALESCE(sales.status, '') <> 'held'
                        )
                        OR EXISTS (SELECT 1 FROM sale_return_items WHERE sale_item_id = OLD.id)
                        OR EXISTS (
                            SELECT 1 FROM employee_goods_credits
                            WHERE employee_goods_credits.sale_id = OLD.sale_id
                        )
                    THEN RAISE(ABORT, 'Posted sale item history cannot be deleted; use a return') END;
                    INSERT INTO sync_queue (table_name, record_id, action)
                    SELECT '{table}', OLD.{pk}, 'DELETE'
                    WHERE COALESCE((SELECT skip_sync FROM sync_control WHERE id = 1), 0) = 0;
                END;
                """)
            elif table == "cash_transactions":
                cursor.execute(f"""
                CREATE TRIGGER trg_{table}_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT CASE WHEN
                        COALESCE(OLD.reference_type, '') NOT IN ('', 'manual')
                        OR EXISTS (
                            SELECT 1 FROM employee_ledger_entries
                            WHERE cash_transaction_id = OLD.id
                        )
                        OR EXISTS (
                            SELECT 1 FROM employee_salary_payments
                            WHERE cash_transaction_id = OLD.id
                        )
                        OR EXISTS (
                            SELECT 1 FROM sale_returns
                            WHERE cash_transaction_id = OLD.id
                        )
                    THEN RAISE(ABORT, 'Posted cash history cannot be deleted; use a reversal') END;
                    INSERT INTO sync_queue (table_name, record_id, action)
                    SELECT '{table}', OLD.{pk}, 'DELETE'
                    WHERE COALESCE((SELECT skip_sync FROM sync_control WHERE id = 1), 0) = 0;
                END;
                """)
            elif table in protected_delete_tables:
                cursor.execute(f"""
                CREATE TRIGGER trg_{table}_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'Posted financial history cannot be deleted');
                END;
                """)
            else:
                cursor.execute(f"""
                CREATE TRIGGER trg_{table}_delete
                BEFORE DELETE ON {table}
                WHEN COALESCE((SELECT skip_sync FROM sync_control WHERE id = 1), 0) = 0
                BEGIN
                    INSERT INTO sync_queue (table_name, record_id, action)
                    VALUES ('{table}', OLD.{pk}, 'DELETE');
                END;
                """)
    except Exception as e:
        logger.error("Error registering persistent triggers: %s", e)
        if raise_errors:
            raise
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass


# ── Recovery & Corruption Handlers ─────────────────────────────────────────────
def trigger_auto_recovery() -> bool:
    """Finds best healthy backup and promotes it to primary. Returns True if successful."""
    base = get_base_data_dir()
    db_dir = os.path.join(base, "database")
    backup_dir = os.path.join(base, "backup")
    db_path = get_db_path()

    logger.critical("Initiating Forced Auto-Recovery...")
    candidates = []
    for s_dir in [db_dir, backup_dir]:
        if not os.path.exists(s_dir):
            continue
        for f in os.listdir(s_dir):
            if (f.endswith(".db") or ".db.corrupt" in f or ".db.before" in f) and "malformed" not in f and "empty_backup" not in f:
                fp = os.path.join(s_dir, f)
                if os.path.isfile(fp) and os.path.abspath(fp) != os.path.abspath(db_path):
                    try:
                        size = os.path.getsize(fp)
                        if size > 1000000:
                            candidates.append((size, fp))
                    except Exception:
                        pass

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        for best_size, best_path in candidates:
            conn = None
            try:
                conn = sqlite3.connect(best_path)
                res = conn.execute("PRAGMA integrity_check(1)").fetchone()
                if res and res[0] == "ok":
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None

                    logger.info("Promoting healthy backup: %s", os.path.basename(best_path))
                    global engine
                    try:
                        engine.dispose()
                    except Exception:
                        pass

                    ts = int(datetime.now().timestamp())
                    if os.path.exists(db_path):
                        try:
                            os.rename(db_path, db_path + f".malformed_{ts}")
                        except Exception:
                            pass
                    shutil.copy2(best_path, db_path)
                    logger.info("Auto-Recovery SUCCESSFUL.")
                    return True
            except Exception as e:
                logger.warning("Candidate backup check failed for %s: %s", best_path, e)
                continue
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    logger.error("No healthy recovery candidates found.")
    return False


def handle_corruption(e) -> bool:
    """Handles database corruption by attempting recovery or fresh database creation.
    Raises exception if database is busy/locked or if recovery fails.
    """
    err_msg = str(e).lower()
    if any(k in err_msg for k in ["busy", "locked", "permission", "access is denied"]):
        logger.error("Connection blocked by OS/Lock: %s. Re-raising failure.", e)
        raise e

    if any(x in err_msg for x in ["not a database", "malformed", "integrity"]):
        logger.error("Database corruption detected: %s", e)
        if trigger_auto_recovery():
            logger.info("Auto-Recovery completed successfully.")
            return True
        else:
            logger.error("No healthy candidates found for recovery.")
            global engine
            try:
                engine.dispose()
            except Exception:
                pass

            db_path = get_db_path()
            if os.path.exists(db_path):
                corrupt_rename = db_path + f".unrecoverable_{int(datetime.now().timestamp())}"
                try:
                    os.rename(db_path, corrupt_rename)
                    logger.info("Renamed unrecoverable corrupt database to: %s", corrupt_rename)
                except Exception as rename_err:
                    logger.error("Failed to rename corrupt database: %s", rename_err)

            logger.info("Starting fresh with new database creation...")
            return init_db(force_create=True)
    else:
        raise e


# ── Initialization & Directory Helpers ─────────────────────────────────────────
def ensure_db_directories():
    """Ensures directory structure and saves default config if unconfigured."""
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    config = load_db_config()
    if not config:
        config["active_db_path"] = db_path
        config["auto_backup_enabled"] = True
        save_db_config(config)


def init_db(force_create=False) -> bool:
    """Initialize database with schema, persistent triggers, and integrity checks.
    Returns:
        True: Database is initialized and ready.
        False: Database missing and auto-creation was skipped.
    Raises:
        Exception: On unrecoverable error or locked connection.
    """
    import models

    db_path = get_db_path()
    db_exists = os.path.exists(db_path)
    config = load_db_config()

    if not db_exists and not force_create:
        default_appdata_db = os.path.normpath(os.path.join(get_user_data_root(), "database", "smartvyapar.db"))
        is_default_path = (os.path.abspath(db_path) == os.path.abspath(default_appdata_db))
        is_configured_custom = bool(config.get("active_db_path"))

        if is_default_path or is_configured_custom:
            logger.info("Auto-creating missing database at %s", db_path)
        else:
            logger.warning("Skipping auto-creation of missing DB at %s. Recovery Wizard required.", db_path)
            return False

    ensure_db_directories()
    logger.info("Initializing Active DB at: %s", db_path)

    try:
        if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
            with engine.connect() as conn:
                res = conn.execute(text("PRAGMA integrity_check(1)")).fetchone()
                if res and res[0] != "ok":
                    raise Exception(f"Integrity check failed: {res[0]}")

                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(text("PRAGMA synchronous=NORMAL"))

                cursor = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='products'"))
                if cursor.fetchone():
                    count = conn.execute(text("SELECT COUNT(*) FROM products")).fetchone()[0]
                    if count == 0:
                        logger.warning("Database exists but 'products' table is EMPTY. This may be a new or corrupt file.")

        Base.metadata.create_all(bind=engine)

        with engine.connect() as conn:
            register_persistent_triggers(conn)
            conn.commit()

        logger.info("Database initialization complete.")
        return True

    except Exception as e:
        logger.error("Init failed: %s", e)
        recovered = handle_corruption(e)
        if recovered:
            return True
        raise


def get_static_dir():
    """Returns static directory path, creating it if needed."""
    base = get_base_data_dir()
    path = os.path.join(base, "static")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def get_backup_dir():
    """Returns backup directory path, creating it if needed."""
    base = get_base_data_dir()
    path = os.path.join(base, "backup")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path
