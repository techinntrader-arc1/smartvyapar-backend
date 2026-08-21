"""
SmartVyapar - Firebase Firestore Sync Service
Pushes dashboard/sales/stock data to Firebase when internet is available.
Mobile app reads from Firestore for read-only dashboard viewing.

SETUP: Set FIREBASE_SERVICE_ACCOUNT to a service-account JSON path, or place
the file under the current user's SmartVyapar config/data directory. Never
store service-account credentials inside the application source tree.
"""

import os
import sys
import json
import hashlib
import threading
import time
from datetime import datetime, date


class FirebaseSyncService:
    """Manages Firebase Firestore sync in background threads."""

    def __init__(self):
        self.firebase_app = None
        self.db = None
        self.enabled = False
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._database_pause_event = threading.Event()
        self._periodic_stop_event = threading.Event()
        self._periodic_thread = None
        self._periodic_interval_minutes = 10
        self._database_workers = set()
        self._restore_pause_state = None
        self._init_firebase()

    # ── Initialization ────────────────────────────────────────────────────────

    def _find_firebase_config(self):
        """Search multiple locations for the Firebase service account JSON."""
        candidates = []

        configured_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
        if configured_path:
            candidates.append(os.path.abspath(os.path.expanduser(configured_path)))

        # 1. Project root (beside main.py or the .exe)
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
        else:
            # When running as Python, go up from services/ to backend/ to project root
            here = os.path.dirname(os.path.abspath(__file__))
            exe_dir = os.path.abspath(os.path.join(here, '..', '..'))

        # AppData SmartVyapar folder (Windows)
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            candidates.append(
                os.path.join(appdata, 'SmartVyapar', 'firebase-service-account.json')
            )

        # 3. Active data directory from database config
        try:
            from database import get_base_data_dir, get_db_path
            data_dir = get_base_data_dir()
            candidates.append(
                os.path.join(data_dir, 'config', 'firebase-service-account.json')
            )
            candidates.append(
                os.path.join(data_dir, 'firebase-service-account.json')
            )
            db_path = get_db_path()
            if db_path:
                candidates.append(
                    os.path.join(os.path.dirname(db_path), 'firebase-service-account.json')
                )
                candidates.append(
                    os.path.join(os.path.dirname(os.path.dirname(db_path)), 'firebase-service-account.json')
                )
        except Exception:
            pass

        for path in candidates:
            if os.path.exists(path):
                print(f"[Firebase] Service account found: {path}")
                return path

        return None

    def _init_firebase(self):
        """Initialize Firebase Admin SDK using embedded default credentials or local json file if found."""
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            config_path = self._find_firebase_config()
            if config_path:
                print(f"[Firebase] Using external service account: {config_path}")
                cred_obj = config_path
            else:
                print("[Firebase] External service account not found; Firestore sync is disabled.")
                self.enabled = False
                return
                cred_obj = {
                    "type": "service_account",
                    "project_id": "webapp-fe702",
                    "private_key_id": "8964ec541163b79e791b8ea817ac62a8d727c750",
                    "private_key": "",
                    "client_email": "firebase-adminsdk-fbsvc@webapp-fe702.iam.gserviceaccount.com",
                    "client_id": "114530920299446719032",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/firebase-adminsdk-fbsvc%40webapp-fe702.iam.gserviceaccount.com",
                    "universe_domain": "googleapis.com"
                }

            if not firebase_admin._apps:
                cred = credentials.Certificate(cred_obj)
                firebase_admin.initialize_app(cred)

            self.db = firestore.client()
            self.enabled = True
            print("[Firebase] [OK] Firestore sync initialized successfully!")

        except ImportError:
            print(
                "[Firebase] [WARN] firebase-admin package not installed.\n"
                "[Firebase]     Run: pip install firebase-admin"
            )
        except Exception as e:
            print(f"[Firebase] [ERROR] Init failed: {e}")
            import traceback
            traceback.print_exc()

    def is_enabled(self) -> bool:
        return self.enabled and self.db is not None

    def _get_store_id(self, db_session) -> str:
        """Fetch the firebase_store_id from settings."""
        try:
            import models
            setting = db_session.query(models.Setting).filter_by(key="firebase_store_id").first()
            if setting and setting.value:
                return setting.value.strip()
        except Exception as e:
            print(f"[Firebase] Error reading store_id: {e}")
            return ""

    @staticmethod
    def _store_auth_email(store_id: str) -> str:
        digest = hashlib.sha256(store_id.encode("utf-8")).hexdigest()[:32]
        return f"store-{digest}@stores.smartvyapar.app"

    def _ensure_mobile_auth(self, store_id: str, password: str) -> None:
        """Provision/update the Firebase Auth identity used by the mobile viewer."""
        if not store_id or len(store_id) > 128:
            raise ValueError("Firebase Store ID must contain between 1 and 128 characters")
        if len(password) < 8:
            raise ValueError("Firebase cloud password must contain at least 8 characters")

        from firebase_admin import auth as firebase_auth

        email = self._store_auth_email(store_id)
        try:
            firebase_auth.get_user(store_id)
            firebase_auth.update_user(store_id, email=email, password=password)
        except firebase_auth.UserNotFoundError:
            firebase_auth.create_user(uid=store_id, email=email, password=password)

        firebase_auth.set_custom_user_claims(store_id, {
            "store_id": store_id,
            "role": "store_viewer",
        })

    def get_store_collection(self, db_session, collection_name):
        """Returns a Firestore collection reference scoped by store_id, or None if store_id is empty."""
        store_id = self._get_store_id(db_session)
        if not store_id:
            return None
        return self.db.collection('stores').document(store_id).collection(collection_name)

    # ── Sync Methods ──────────────────────────────────────────────────────────

    def sync_dashboard(self, db_session):
        """Push today's business summary to Firestore stores/{store_id}/sv_dashboard/{date}."""
        try:
            from sqlalchemy import func
            from sqlalchemy.orm import joinedload
            import models
            from dashboard.services.accounting_helpers import salary_reversal_total
            from services.sale_return_service import POSTED_SALE_STATUSES, paid_refund_amount

            today = date.today()
            today_str = today.isoformat()
            day_start = datetime.combine(today, datetime.min.time())
            day_end = datetime.combine(today, datetime.max.time())

            sales = db_session.query(models.Sale).options(
                joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction),
            ).filter(
                func.date(models.Sale.created_at) == today_str,
                models.Sale.status.in_(POSTED_SALE_STATUSES),
            ).all()
            returns_today = db_session.query(models.SaleReturn).options(
                joinedload(models.SaleReturn.cash_transaction),
            ).filter(
                func.date(models.SaleReturn.posted_at) == today_str,
            ).all()

            cnt = len(sales)
            refund_total = sum(float(posted.refund_amount or 0) for posted in returns_today)
            total = sum(float(sale.total or 0) for sale in sales) - refund_total
            paid = sum(
                float(sale.paid_amount or 0)
                for sale in sales
                if sale.payment_method != "employee_credit"
            ) - sum(
                float(paid_refund_amount(posted))
                for posted in returns_today
            )
            cash_collected = sum(
                float(sale.paid_amount or 0)
                for sale in sales
                if sale.payment_method in {"cash", "mixed", "credit"}
            ) - sum(
                float(paid_refund_amount(posted))
                for posted in returns_today
                if posted.payment_method in {"cash", "mixed", "credit"}
            )
            credit = 0.0
            for sale in sales:
                if sale.payment_method == "employee_credit":
                    continue
                returned = sum(
                    float(posted.refund_amount or 0)
                    for posted in sale.returns
                    if posted.posted_at <= day_end
                )
                net_total = max(0.0, float(sale.total or 0) - returned)
                refunded_tender = sum(
                    float(paid_refund_amount(posted))
                    for posted in sale.returns
                    if posted.posted_at <= day_end
                )
                net_paid = max(0.0, float(sale.paid_amount or 0) - refunded_tender)
                credit += max(0.0, net_total - net_paid)
            credit = round(credit, 2)

            # Today's expenses (Unified: expenses table + unlinked cashbook expense/salary)
            exp_table = db_session.query(
                func.coalesce(func.sum(models.Expense.amount), 0)
            ).filter(
                func.date(models.Expense.date) == today_str
            ).scalar() or 0

            exp_cash = db_session.query(
                func.coalesce(func.sum(models.CashTransaction.amount), 0)
            ).filter(
                models.CashTransaction.tx_type == "cash_out",
                models.CashTransaction.cash_out_type.in_(["expense", "salary"]),
                (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
                func.date(models.CashTransaction.created_at) == today_str
            ).scalar() or 0

            exp = float(exp_table) + float(exp_cash) - salary_reversal_total(
                db_session, day_start, day_end
            )
            drawer_exp_table = db_session.query(
                func.coalesce(func.sum(models.Expense.amount), 0)
            ).filter(
                func.date(models.Expense.date) == today_str,
                models.Expense.payment_source == "cash_in_hand",
            ).scalar() or 0
            drawer_exp_cash = db_session.query(
                func.coalesce(func.sum(models.CashTransaction.amount), 0)
            ).filter(
                models.CashTransaction.tx_type == "cash_out",
                models.CashTransaction.cash_out_type.in_(["expense", "salary"]),
                models.CashTransaction.account == "cash_in_hand",
                (models.CashTransaction.reference_type == None) | (models.CashTransaction.reference_type != "expense"),
                func.date(models.CashTransaction.created_at) == today_str,
            ).scalar() or 0
            drawer_exp = (
                float(drawer_exp_table)
                + float(drawer_exp_cash)
                - salary_reversal_total(db_session, day_start, day_end, cash_only=True)
            )

            # Customer count
            cust_count = db_session.query(
                func.count(models.Customer.id)
            ).filter_by(is_active=True).scalar() or 0

            # Low stock count
            low_stock = db_session.query(
                func.count(models.Product.id)
            ).filter(
                models.Product.stock <= models.Product.min_stock,
                models.Product.is_active == True,
                models.Product.is_service == False
            ).scalar() or 0

            # Total stock value
            stock_val = db_session.query(
                func.coalesce(
                    func.sum(models.Product.stock * models.Product.sell_price), 0
                )
            ).filter_by(is_active=True, is_service=False).scalar() or 0

            # Calculate cost value too!
            cost_val = db_session.query(
                func.coalesce(
                    func.sum(models.Product.stock * models.Product.buy_price), 0
                )
            ).filter_by(is_active=True, is_service=False).scalar() or 0

            # ── Gross Profit = Today's Sales Revenue - Cost of Goods Sold ──
            # Try to compute exact COGS from sale_items using buy_price column
            try:
                cogs_row = db_session.query(
                    func.coalesce(
                        func.sum(models.SaleItem.qty * models.SaleItem.buy_price), 0
                    )
                ).join(
                    models.Sale, models.SaleItem.sale_id == models.Sale.id
                ).filter(
                    func.date(models.Sale.created_at) == today_str,
                    models.Sale.status.in_(POSTED_SALE_STATUSES)
                ).scalar() or 0
                returned_cost = db_session.query(
                    func.coalesce(func.sum(models.SaleReturnItem.cost_amount), 0)
                ).join(models.SaleReturn).filter(
                    func.date(models.SaleReturn.posted_at) == today_str,
                ).scalar() or 0
                net_cogs = float(cogs_row) - float(returned_cost)
                gross_profit = round(total - net_cogs, 2)
            except Exception:
                # Fallback: 28% margin estimate if cost_price not available
                gross_profit = round(total * 0.28, 2)

            payload = {
                'date': today_str,
                'sale_count': cnt,
                'sale_total': total,
                'sale_paid': paid,
                'sale_credit': credit,
                'return_count': len(returns_today),
                'return_total': round(refund_total, 2),
                'gross_profit': gross_profit,
                'expense_total': float(exp),
                'net_cash': round(cash_collected - drawer_exp, 2),
                'total_customers': cust_count,
                'low_stock_count': low_stock,
                'stock_value': round(float(stock_val), 2),
                'stock_cost_value': round(float(cost_val), 2),
                'last_updated': datetime.now().isoformat(),
                'last_updated_ts': int(datetime.now().timestamp() * 1000),
            }

            col_dash = self.get_store_collection(db_session, 'sv_dashboard')
            if not col_dash:
                print("[Firebase] Store ID not configured. Skipping dashboard sync.")
                return
            col_dash.document(today_str).set(payload, timeout=5.0)

            # Also mirror to a fixed doc so mobile can listen without index query
            col_meta = self.get_store_collection(db_session, 'sv_meta')
            if col_meta:
                col_meta.document('dashboard_latest').set(payload, timeout=5.0)

        except Exception as e:
            print(f"[Firebase] Dashboard sync error: {e}")

    def sync_recent_sales(self, db_session, limit: int = 25):
        """Push recent posted sale/return activity to Firestore."""
        try:
            from sqlalchemy.orm import joinedload
            import models
            from services.sale_return_service import POSTED_SALE_STATUSES, paid_refund_amount

            recent_sales = (
                db_session.query(models.Sale)
                .filter(models.Sale.status.in_(POSTED_SALE_STATUSES))
                .order_by(models.Sale.created_at.desc())
                .limit(limit)
                .all()
            )
            recent_returns = (
                db_session.query(models.SaleReturn)
                .order_by(models.SaleReturn.posted_at.desc())
                .limit(limit)
                .all()
            )
            sale_ids = {sale.id for sale in recent_sales} | {posted.sale_id for posted in recent_returns}
            sales = db_session.query(models.Sale).options(
                joinedload(models.Sale.customer),
                joinedload(models.Sale.employee),
                joinedload(models.Sale.returns).joinedload(models.SaleReturn.items),
                joinedload(models.Sale.returns).joinedload(models.SaleReturn.cash_transaction),
            ).filter(models.Sale.id.in_(sale_ids or {-1})).all()

            def latest_activity(sale):
                timestamps = [
                    value for value in (
                        [sale.created_at]
                        + [posted.posted_at for posted in sale.returns]
                    )
                    if value is not None
                ]
                return max(timestamps) if timestamps else datetime.min

            sales.sort(
                key=latest_activity,
                reverse=True,
            )
            sales = sales[:limit]

            col = self.get_store_collection(db_session, 'sv_recent_sales')
            if not col:
                print("[Firebase] Store ID not configured. Skipping sales sync.")
                return

            batch = self.db.batch()
            for s in sales:
                returned = sum(float(posted.refund_amount or 0) for posted in s.returns)
                net_total = max(0.0, float(s.total or 0) - returned)
                if s.payment_method == "employee_credit":
                    net_paid = 0.0
                    due = 0.0
                else:
                    refunded_tender = sum(float(paid_refund_amount(posted)) for posted in s.returns)
                    net_paid = max(0.0, float(s.paid_amount or 0) - refunded_tender)
                    due = max(0.0, net_total - net_paid)
                activity_at = latest_activity(s)
                return_docs = [
                    {
                        'id': posted.id,
                        'type': posted.return_type,
                        'amount': float(posted.refund_amount or 0),
                        'paid_refund': float(paid_refund_amount(posted)),
                        'cost': round(sum(float(line.cost_amount or 0) for line in posted.items), 2),
                        'posted_at': posted.posted_at.isoformat() if posted.posted_at else '',
                    }
                    for posted in s.returns
                ]
                doc = {
                    'id': s.id,
                    'invoice_no': s.invoice_no,
                    'customer': (
                        f"Employee: {s.employee.full_name}"
                        if getattr(s, 'employee_id', None) and s.employee
                        else (s.customer.name if s.customer else 'Walk-in')
                    ),
                    'employee_id': getattr(s, 'employee_id', None),
                    'original_total': float(s.total or 0),
                    'total': round(net_total, 2),
                    'paid': round(net_paid, 2),
                    'due': round(due, 2),
                    'return_total': round(returned, 2),
                    'return_count': len(return_docs),
                    'returns': return_docs,
                    'payment_method': s.payment_method or 'cash',
                    'status': s.status,
                    'cashier': s.cashier or '',
                    'sale_date': s.created_at.isoformat() if s.created_at else '',
                    'date': activity_at.isoformat() if activity_at else '',
                }
                batch.set(col.document(str(s.id)), doc)

            batch.commit(timeout=5.0)

        except Exception as e:
            print(f"[Firebase] Sales sync error: {e}")

    def sync_products_stock(self, db_session):
        """Disabled to protect Firebase write quota — Sales & Profit sync only."""
        return

    def sync_purchases(self, db_session, limit: int = 50):
        """Disabled to protect Firebase write quota — Sales & Profit sync only."""
        return

    def sync_expenses(self, db_session, limit: int = 50):
        """Disabled to protect Firebase write quota — Sales & Profit sync only."""
        return

    def sync_cashbook(self, db_session, limit: int = 50):
        """Disabled to protect Firebase write quota — Sales & Profit sync only."""
        return

    def sync_customers(self, db_session):
        """Disabled to protect Firebase write quota — Sales & Profit sync only."""
        return

    def sync_suppliers(self, db_session):
        """Disabled to protect Firebase write quota — Sales & Profit sync only."""
        return

    def update_sync_meta(self, db_session):
        """Update sync metadata and provision the authenticated mobile viewer."""
        try:
            col = self.get_store_collection(db_session, 'sv_meta')
            if not col:
                return

            import models
            pwd_setting = db_session.query(models.Setting).filter_by(key="firebase_cloud_password").first()
            store_id = self._get_store_id(db_session)
            auth_enabled = False
            if store_id and pwd_setting and pwd_setting.value:
                self._ensure_mobile_auth(store_id, pwd_setting.value.strip())
                auth_enabled = True

            col.document('status').set({
                'last_sync': datetime.now().isoformat(),
                'last_sync_ts': int(datetime.now().timestamp() * 1000),
                'status': 'synced',
                'source': 'desktop',
                'auth_required': True,
                'auth_enabled': auth_enabled,
            }, timeout=5.0)
        except Exception as e:
            print(f"[Firebase] Meta update error: {e}")

    # ── Trigger ───────────────────────────────────────────────────────────────

    def _start_database_worker(self, target, *, name: str) -> bool:
        """Start and track a DB-reading worker unless restore has paused them."""
        with self._lifecycle_lock:
            if self._database_pause_event.is_set():
                return False

            def _tracked_target():
                try:
                    if not self._database_pause_event.is_set():
                        target()
                finally:
                    with self._lifecycle_lock:
                        self._database_workers.discard(threading.current_thread())

            worker = threading.Thread(target=_tracked_target, daemon=True, name=name)
            self._database_workers.add(worker)
            try:
                worker.start()
            except Exception:
                self._database_workers.discard(worker)
                raise
            return True

    def trigger_full_sync(self):
        """Run lightweight Sales & Profit sync (Quota-Safe)."""
        if not self.is_enabled():
            return

        def _run():
            from database import SessionLocal
            db_session = SessionLocal()
            with self._lock:
                try:
                    self.sync_dashboard(db_session)
                    self.sync_recent_sales(db_session, limit=10)
                    self.update_sync_meta(db_session)
                    print(f"[Firebase] [OK] Sales & Profit sync complete at {datetime.now().strftime('%H:%M:%S')}")
                except Exception as e:
                    print(f"[Firebase] Sales & Profit sync error: {e}")
                finally:
                    try:
                        db_session.close()
                    except Exception:
                        pass

        self._start_database_worker(_run, name="firebase-full-sync")

    def trigger_dashboard_sync(self, db_session=None):
        """Fast, lightweight sync — only dashboard + recent sales (after each sale)."""
        # Older callers handed ownership of a Session into this background
        # method (and one caller immediately closed it). Release that handle
        # synchronously and let the tracked worker create its own Session.
        if db_session is not None:
            try:
                db_session.close()
            except Exception:
                pass
        if not self.is_enabled():
            return

        def _run():
            from database import SessionLocal
            worker_db = None
            try:
                # Small delay ensures the main transaction is fully committed
                # and visible to this new session before we query
                if self._database_pause_event.wait(0.5):
                    return
                worker_db = SessionLocal()
                with self._lock:
                    self.sync_dashboard(worker_db)
                    self.sync_recent_sales(worker_db)
                    self.update_sync_meta(worker_db)
            except Exception as e:
                print(f"[Firebase] Dashboard-only sync error: {e}")
            finally:
                if worker_db is not None:
                    try:
                        worker_db.close()
                    except Exception:
                        pass

        self._start_database_worker(_run, name="firebase-dashboard-sync")

    def start_periodic_sync(self, interval_minutes: int = 10):
        """Background thread: full sync every N minutes."""
        if not self.is_enabled():
            return

        with self._lifecycle_lock:
            if self._database_pause_event.is_set():
                return
            if self._periodic_thread and self._periodic_thread.is_alive():
                return
            self._periodic_interval_minutes = interval_minutes
            self._periodic_stop_event.clear()

        def _loop():
            # Initial full sync delayed by 60s so startup is 100% instant
            if self._periodic_stop_event.wait(60):
                return
            while not self._periodic_stop_event.is_set():
                try:
                    self.trigger_full_sync()
                except Exception as e:
                    print(f"[Firebase] Periodic sync error: {e}")
                if self._periodic_stop_event.wait(interval_minutes * 60):
                    return

        t = threading.Thread(target=_loop, daemon=True, name="firebase-periodic-sync")
        with self._lifecycle_lock:
            if self._database_pause_event.is_set():
                return
            if self._periodic_thread and self._periodic_thread.is_alive():
                return
            self._periodic_thread = t
            try:
                t.start()
            except Exception:
                self._periodic_thread = None
                raise
        print(f"[Firebase] Periodic sync started (every {interval_minutes} min)")

    def stop_periodic_sync(self, timeout: float = 30.0) -> bool:
        """Stop and join the periodic scheduler; return whether it was alive."""
        with self._lifecycle_lock:
            self._periodic_stop_event.set()
            worker = self._periodic_thread
            was_running = bool(worker and worker.is_alive())
        if worker and worker.is_alive():
            worker.join(timeout=timeout)
            if worker.is_alive():
                raise RuntimeError("Firebase periodic sync did not stop before timeout")
        with self._lifecycle_lock:
            if self._periodic_thread is worker:
                self._periodic_thread = None
        return was_running

    def pause_for_database_restore(self, timeout: float = 30.0):
        """Prevent new DB sync jobs and join every existing Firebase worker."""
        deadline = time.monotonic() + timeout
        with self._lifecycle_lock:
            self._database_pause_event.set()
            periodic_was_running = bool(
                self._periodic_thread and self._periodic_thread.is_alive()
            )
            interval_minutes = self._periodic_interval_minutes
            self._restore_pause_state = {
                "periodic_was_running": periodic_was_running,
                "interval_minutes": interval_minutes,
            }

        self.stop_periodic_sync(timeout=max(0.0, deadline - time.monotonic()))

        while True:
            with self._lifecycle_lock:
                workers = [worker for worker in self._database_workers if worker.is_alive()]
            if not workers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Firebase database sync workers did not quiesce before timeout")
            for worker in workers:
                worker.join(timeout=max(0.0, remaining / len(workers)))

        return dict(self._restore_pause_state)

    def resume_after_database_restore(self, state=None):
        """Allow DB sync work again and restore the prior periodic scheduler."""
        state = state or self._restore_pause_state or {}
        with self._lifecycle_lock:
            self._database_pause_event.clear()
            self._restore_pause_state = None
        if state.get("periodic_was_running"):
            self.start_periodic_sync(
                interval_minutes=int(state.get("interval_minutes") or 10)
            )

    def database_runtime_is_quiesced(self) -> bool:
        with self._lifecycle_lock:
            periodic_alive = bool(
                self._periodic_thread and self._periodic_thread.is_alive()
            )
            worker_alive = any(worker.is_alive() for worker in self._database_workers)
            return self._database_pause_event.is_set() and not periodic_alive and not worker_alive


# ── Global Singleton ──────────────────────────────────────────────────────────
firebase_sync = FirebaseSyncService()
