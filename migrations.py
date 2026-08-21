import os
import sys
import traceback
from sqlalchemy import inspect, text
from database import engine, get_db_path

def get_log_path():
    db_path = get_db_path()
    return os.path.join(os.path.dirname(db_path), "migration.log")

def log(msg, force_print=False):
    log_path = get_log_path()
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[Migration] {msg}\n")
    except Exception:
        pass
    if force_print:
        print(f"[Migration] {msg}")

def apply_migrations():
    """
    Self-healing database migration with robust error handling and logging.
    """
    log("--- Starting Migration Check ---")
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        
        # 0. Create 'brands' table if missing
        if 'brands' not in tables:
            log("Creating 'brands' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE brands (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL UNIQUE,
                        description TEXT
                    )
                """))
            log("'brands' table created successfully.")
            tables.append('brands')
        
        if 'products' in tables:
            columns = [c['name'] for c in inspector.get_columns('products')]
            
            # 1. Add 'code' column if missing
            if 'code' not in columns:
                log("Adding 'code' column to products table...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE products ADD COLUMN code VARCHAR(50)"))
                log("'code' column added successfully.")
                
                # Auto-Populate for existing data
                with engine.begin() as conn:
                    conn.execute(text("UPDATE products SET code = 'ITM-' || substr('000000' || id, -6) WHERE code IS NULL"))
                    log("Generated item codes for existing products.")

            # 2. Add 'location' column if missing
            if 'location' not in columns:
                log("Adding 'location' column to products table... (RACK LOCATION)")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE products ADD COLUMN location VARCHAR(100)"))
                    log("'location' column added successfully.")
                except Exception as e:
                    log(f"FAILED to add 'location' column: {e}")
                    log(traceback.format_exc())

            # 3. Add 'is_service' column if missing
            if 'is_service' not in columns:
                log("Adding 'is_service' column to products table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE products ADD COLUMN is_service BOOLEAN DEFAULT 0"))
                    log("'is_service' column added successfully.")
                except Exception as e:
                    log(f"FAILED to add 'is_service' column: {e}")
                    log(traceback.format_exc())

            # 4. Add 'brand_id' column if missing
            if 'brand_id' not in columns:
                log("Adding 'brand_id' column to products table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE products ADD COLUMN brand_id INTEGER REFERENCES brands(id)"))
                    log("'brand_id' column added successfully.")
                except Exception as e:
                    log(f"FAILED to add 'brand_id' column: {e}")
                    log(traceback.format_exc())

            # 5. Seed 'General Service' item if missing
            with engine.begin() as conn:
                res = conn.execute(text("SELECT id FROM products WHERE code = 'SRVC-01'")).fetchone()
                if not res:
                    log("Seeding 'General Service' (SRVC-01)...")
                    conn.execute(text("""
                        INSERT INTO products (name, code, sell_price, buy_price, is_service, is_active, stock, min_stock, created_at)
                        VALUES ('General Service', 'SRVC-01', 0.0, 0.0, 1, 1, 0, 0, datetime('now'))
                    """))
                    log("'General Service' seeded successfully.")

        if 'customers' in tables:
            columns = [c['name'] for c in inspector.get_columns('customers')]
            if 'is_active' not in columns:
                log("Adding 'is_active' column to customers table...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE customers ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                log("'is_active' column added successfully.")

        if 'suppliers' in tables:
            columns = [c['name'] for c in inspector.get_columns('suppliers')]
            if 'is_active' not in columns:
                log("Adding 'is_active' column to suppliers table...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE suppliers ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                log("'is_active' column added successfully.")

        if 'users' in tables:
            columns = [c['name'] for c in inspector.get_columns('users')]
            if 'permissions' not in columns:
                log("Adding 'permissions' column to users table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT 'billing,sales-list,customers,suppliers,payments,stock-report,license-info,help,labels'"))
                    log("'permissions' column added successfully.")
                except Exception as e:
                    log(f"FAILED to add 'permissions' column: {e}")

        if 'sale_items' in tables:
            columns = [c['name'] for c in inspector.get_columns('sale_items')]
            if 'buy_price' not in columns:
                log("Adding 'buy_price' column to sale_items table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE sale_items ADD COLUMN buy_price FLOAT DEFAULT 0.0"))
                    log("'buy_price' column added successfully.")
                except Exception as e:
                    log(f"FAILED to add 'buy_price' column: {e}")
                    log(traceback.format_exc())

        if 'sales' in tables:
            columns = [c['name'] for c in inspector.get_columns('sales')]
            if 'amount_tendered' not in columns:
                log("Adding 'amount_tendered' column to sales table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE sales ADD COLUMN amount_tendered FLOAT DEFAULT 0.0"))
                    log("'amount_tendered' column added successfully.")
                    # Populate for existing sales
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE sales SET amount_tendered = paid_amount WHERE amount_tendered = 0.0 OR amount_tendered IS NULL"))
                        log("Populated amount_tendered for existing sales.")
                except Exception as e:
                    log(f"FAILED to add 'amount_tendered' column: {e}")
                    log(traceback.format_exc())

            if 'change_returned' not in columns:
                log("Adding 'change_returned' column to sales table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE sales ADD COLUMN change_returned FLOAT DEFAULT 0.0"))
                    log("'change_returned' column added successfully.")
                except Exception as e:
                    log(f"FAILED to add 'change_returned' column: {e}")
                    log(traceback.format_exc())

        # 6. Normalize Enum Values (lowercase)
        log("Normalizing Enum values...")
        with engine.begin() as conn:
            if 'payments' in tables:
                # Normalize known multi-word or legacy method values first
                conn.execute(text("UPDATE payments SET method = 'online' WHERE lower(method) IN ('online payment', 'online transfer', 'easypaisa', 'jazzcash')"))
                conn.execute(text("UPDATE payments SET method = 'bank' WHERE lower(method) IN ('bank transfer', 'bank_transfer', 'bank deposit')"))
                conn.execute(text("UPDATE payments SET method = 'card' WHERE lower(method) IN ('credit card', 'debit card', 'credit_card', 'debit_card')"))
                conn.execute(text("UPDATE payments SET method = 'check' WHERE lower(method) IN ('cheque', 'cheq')"))
                # General lowercase
                conn.execute(text("UPDATE payments SET method = lower(method) WHERE method IS NOT NULL"))
                conn.execute(text("UPDATE payments SET party_type = lower(party_type) WHERE party_type IS NOT NULL"))
                conn.execute(text("UPDATE payments SET payment_type = lower(payment_type) WHERE payment_type IS NOT NULL"))
            if 'sales' in tables:
                conn.execute(text("UPDATE sales SET payment_method = lower(payment_method) WHERE payment_method IS NOT NULL"))
                conn.execute(text("UPDATE sales SET status = lower(status) WHERE status IS NOT NULL"))
            if 'stock_movements' in tables:
                conn.execute(text("UPDATE stock_movements SET movement_type = lower(movement_type) WHERE movement_type IS NOT NULL"))


        # 7. Add Performance Indices
        log("Checking performance indices...")
        with engine.begin() as conn:
            indices = [
                ("idx_sales_created_at", "sales", "created_at"),
                ("idx_sale_items_sale_id", "sale_items", "sale_id"),
                ("idx_sale_items_product_id", "sale_items", "product_id"),
                ("idx_expenses_date", "expenses", "date"),
                ("idx_purchases_created_at", "purchases", "created_at"),
                ("idx_stock_movements_created_at", "stock_movements", "created_at"),
                ("idx_products_category_id", "products", "category_id"),
                ("idx_products_is_active", "products", "is_active")
            ]
            for idx_name, table, col in indices:
                if table in tables:
                    try:
                        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({col})"))
                        log(f"Verified index {idx_name} on {table}({col})")
                    except Exception as e:
                        log(f"Index creation failed for {idx_name}: {e}")

        # ── CASH BOOK MIGRATIONS ──────────────────────────────────────────────

        # 8. Create 'cash_transactions' table if missing
        if 'cash_transactions' not in tables:
            log("Creating 'cash_transactions' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE cash_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tx_type VARCHAR(10) NOT NULL,
                        cash_in_type VARCHAR(50),
                        cash_out_type VARCHAR(50),
                        amount FLOAT NOT NULL,
                        account VARCHAR(30) DEFAULT 'cash_in_hand',
                        received_from VARCHAR(150),
                        paid_to VARCHAR(150),
                        category VARCHAR(100),
                        reference_type VARCHAR(30),
                        reference_id INTEGER,
                        reference_no VARCHAR(50),
                        notes TEXT,
                        created_by VARCHAR(50),
                        created_at DATETIME DEFAULT (datetime('now'))
                    )
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cash_tx_created_at ON cash_transactions(created_at)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cash_tx_type ON cash_transactions(tx_type)"))
            log("'cash_transactions' table created successfully.")
            tables.append('cash_transactions')

        # 9. Create 'day_closings' table if missing
        if 'day_closings' not in tables:
            log("Creating 'day_closings' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE day_closings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date VARCHAR(10) NOT NULL UNIQUE,
                        opening_cash FLOAT DEFAULT 0.0,
                        total_cash_in FLOAT DEFAULT 0.0,
                        total_cash_out FLOAT DEFAULT 0.0,
                        expected_closing_cash FLOAT DEFAULT 0.0,
                        actual_counted_cash FLOAT,
                        difference FLOAT,
                        status VARCHAR(10) DEFAULT 'open',
                        notes TEXT,
                        closed_by VARCHAR(50),
                        closed_at DATETIME,
                        created_at DATETIME DEFAULT (datetime('now'))
                    )
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_day_closings_date ON day_closings(date)"))
            log("'day_closings' table created successfully.")
            tables.append('day_closings')

        # 10. Create 'external_funds' table if missing
        if 'external_funds' not in tables:
            log("Creating 'external_funds' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE external_funds (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fund_type VARCHAR(30) NOT NULL,
                        direction VARCHAR(5) NOT NULL,
                        party_name VARCHAR(150) NOT NULL,
                        amount FLOAT NOT NULL,
                        cash_tx_id INTEGER REFERENCES cash_transactions(id),
                        notes TEXT,
                        created_by VARCHAR(50),
                        created_at DATETIME DEFAULT (datetime('now'))
                    )
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ext_funds_created_at ON external_funds(created_at)"))
            log("'external_funds' table created successfully.")
            tables.append('external_funds')

        # 11. Create 'audit_log' table if missing
        if 'audit_log' not in tables:
            log("Creating 'audit_log' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action VARCHAR(100) NOT NULL,
                        entity_type VARCHAR(50),
                        entity_id INTEGER,
                        old_value TEXT,
                        new_value TEXT,
                        user VARCHAR(50),
                        ip_note VARCHAR(100),
                        created_at DATETIME DEFAULT (datetime('now'))
                    )
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)"))
            log("'audit_log' table created successfully.")
            tables.append('audit_log')

        # 12. Add 'payment_source' to expenses table if missing
        if 'expenses' in tables:
            exp_cols = [c['name'] for c in inspector.get_columns('expenses')]
            if 'payment_source' not in exp_cols:
                log("Adding 'payment_source' column to expenses table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE expenses ADD COLUMN payment_source VARCHAR(30) DEFAULT 'cash_in_hand'"))
                    log("'payment_source' added to expenses.")
                except Exception as e:
                    log(f"FAILED to add payment_source to expenses: {e}")

        # 13. Add 'payment_source' to purchases table if missing
        if 'purchases' in tables:
            pur_cols = [c['name'] for c in inspector.get_columns('purchases')]
            if 'payment_source' not in pur_cols:
                log("Adding 'payment_source' column to purchases table...")
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE purchases ADD COLUMN payment_source VARCHAR(30) DEFAULT 'cash_in_hand'"))
                    log("'payment_source' added to purchases.")
                except Exception as e:
                    log(f"FAILED to add payment_source to purchases: {e}")

        # 14. Cash Book performance indices
        log("Adding cash book indices...")
        with engine.begin() as conn:
            cb_indices = [
                ("idx_cash_tx_ref", "cash_transactions", "reference_type, reference_id"),
                ("idx_cash_tx_account", "cash_transactions", "account"),
                ("idx_day_closings_status", "day_closings", "status"),
            ]
            for idx_name, table, col in cb_indices:
                if table in tables:
                    try:
                        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({col})"))
                    except Exception as e:
                        log(f"Index {idx_name} skipped: {e}")

        # 15. FastFood Module Tables
        if 'ff_categories' not in tables:
            log("Creating 'ff_categories' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_categories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL UNIQUE,
                        description TEXT,
                        icon VARCHAR(10),
                        sort_order INTEGER DEFAULT 0,
                        is_active INTEGER DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_categories' table created.")
            tables.append('ff_categories')

        if 'ff_items' not in tables:
            log("Creating 'ff_items' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(150) NOT NULL,
                        category_id INTEGER REFERENCES ff_categories(id),
                        price REAL DEFAULT 0.0,
                        description TEXT,
                        image_path VARCHAR(255),
                        is_available INTEGER DEFAULT 1,
                        sort_order INTEGER DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_items' table created.")
            tables.append('ff_items')

        if 'ff_orders' not in tables:
            log("Creating 'ff_orders' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_no VARCHAR(30) NOT NULL UNIQUE,
                        order_type VARCHAR(20) DEFAULT 'parcel',
                        table_no VARCHAR(20),
                        customer_name VARCHAR(100),
                        customer_phone VARCHAR(20),
                        subtotal REAL DEFAULT 0.0,
                        discount REAL DEFAULT 0.0,
                        tax_amount REAL DEFAULT 0.0,
                        total REAL DEFAULT 0.0,
                        paid_amount REAL DEFAULT 0.0,
                        payment_method VARCHAR(20) DEFAULT 'cash',
                        status VARCHAR(20) DEFAULT 'completed',
                        cashier VARCHAR(50),
                        notes TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_orders' table created.")
            tables.append('ff_orders')

        if 'ff_order_items' not in tables:
            log("Creating 'ff_order_items' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_order_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id INTEGER NOT NULL REFERENCES ff_orders(id),
                        item_id INTEGER REFERENCES ff_items(id),
                        item_name VARCHAR(150),
                        qty REAL DEFAULT 1.0,
                        price REAL NOT NULL,
                        total REAL NOT NULL,
                        notes TEXT
                    )
                """))
            log("'ff_order_items' table created.")
            tables.append('ff_order_items')

        # 16. Clean up orphaned cash transactions
        log("Cleaning up orphaned cash transactions...")
        with engine.begin() as conn:
            try:
                if 'cash_transactions' in tables and 'purchases' in tables:
                    conn.execute(text("""
                        DELETE FROM cash_transactions 
                        WHERE reference_type = 'purchase' 
                        AND reference_id NOT IN (SELECT id FROM purchases)
                    """))
                if 'cash_transactions' in tables and 'sales' in tables:
                    conn.execute(text("""
                        DELETE FROM cash_transactions 
                        WHERE reference_type = 'sale' 
                        AND reference_id NOT IN (SELECT id FROM sales)
                    """))
            except Exception as e:
                log(f"Failed to clean up orphaned cash transactions: {e}")

        # 16. Auto-heal any corrupt trillion-scale cash values
        try:
            auto_heal_corrupt_data()
        except Exception as e:
            log(f"Auto-heal execution failed: {e}")

        log("--- Migration Check Completed Successfully ---")
        
    except Exception as e:
        log(f"CRITICAL Migration Error: {e}")
        log(traceback.format_exc())

def auto_heal_corrupt_data():
    """Checks and cleans up corrupt trillion-scale entries in the database."""
    log("Running auto-healing check for corrupt cash transactions...")
    try:
        with engine.begin() as conn:
            # Get table names to ensure we do not query non-existent tables
            tables_cursor = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = [row[0] for row in tables_cursor.fetchall()]

            if 'cash_transactions' in tables:
                # 1. Check for cash_transactions with amount > 1,000,000,000 or amount < -1,000,000,000
                corrupt_tx = conn.execute(text(
                    "SELECT id, tx_type, amount, reference_type, reference_id, reference_no FROM cash_transactions WHERE amount > 1000000000 OR amount < -1000000000"
                )).fetchall()
                
                for tx in corrupt_tx:
                    tx_id, tx_type, amount, ref_type, ref_id, ref_no = tx
                    log(f"Auto-Heal: Found corrupt cash transaction ID {tx_id} with amount {amount} (ref: {ref_type} {ref_id} {ref_no})")
                    
                    # IMPORTANT: Only delete the corrupt cash transaction itself.
                    # Do NOT delete linked payments, sales, purchases or expenses —
                    # those records are valid business data. Only the cash transaction
                    # entry has an inflated/corrupt amount and should be removed.
                    
                    # Delete external funds linked to the corrupt cash transaction
                    if 'external_funds' in tables:
                        conn.execute(text("DELETE FROM external_funds WHERE cash_tx_id = :tx_id"), {"tx_id": tx_id})
                    
                    # Delete only the corrupt cash transaction itself
                    conn.execute(text("DELETE FROM cash_transactions WHERE id = :tx_id"), {"tx_id": tx_id})
                    log(f"Auto-Heal: Deleted corrupt cash transaction ID {tx_id} (linked {ref_type} ID {ref_id} preserved)")
                    
            # 2. General cleanup for other tables
            if 'expenses' in tables:
                corrupt_expenses = conn.execute(text("SELECT id, amount FROM expenses WHERE amount > 1000000000 OR amount < -1000000000")).fetchall()
                for exp in corrupt_expenses:
                    exp_id, exp_amount = exp
                    log(f"Auto-Heal: Found corrupt expense ID {exp_id} with amount {exp_amount}")
                    conn.execute(text("DELETE FROM expenses WHERE id = :id"), {"id": exp_id})
                    if 'cash_transactions' in tables:
                        conn.execute(text("DELETE FROM cash_transactions WHERE reference_type = 'expense' AND reference_id = :id"), {"id": exp_id})
                    
            if 'payments' in tables:
                corrupt_payments = conn.execute(text("SELECT id, amount FROM payments WHERE amount > 1000000000 OR amount < -1000000000")).fetchall()
                for pay in corrupt_payments:
                    pay_id, pay_amount = pay
                    log(f"Auto-Heal: Found corrupt payment ID {pay_id} with amount {pay_amount}")
                    conn.execute(text("DELETE FROM payments WHERE id = :id"), {"id": pay_id})
                    if 'cash_transactions' in tables:
                        conn.execute(text("DELETE FROM cash_transactions WHERE reference_type = 'payment' AND reference_id = :id"), {"id": pay_id})

            if 'settings' in tables:
                opening_cash_setting = conn.execute(text("SELECT key, value FROM settings WHERE key = 'opening_cash_manual'")).fetchone()
                if opening_cash_setting:
                    try:
                        val = float(opening_cash_setting[1])
                        if val > 1000000000 or val < -1000000000:
                            conn.execute(text("UPDATE settings SET value = '0.0' WHERE key = 'opening_cash_manual'"))
                            log("Auto-Heal: Corrected opening_cash_manual setting to '0.0'")
                    except:
                        pass

            # WhatsApp Integration Migrations
            if 'whatsapp_history' not in tables:
                log("Creating 'whatsapp_history' table...")
                with engine.begin() as conn:
                    conn.execute(text("""
                        CREATE TABLE whatsapp_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            recipient_phone VARCHAR(50) NOT NULL,
                            doc_type VARCHAR(50) NOT NULL,
                            doc_ref VARCHAR(100),
                            status VARCHAR(50) NOT NULL,
                            message_text TEXT,
                            error_message TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                log("'whatsapp_history' table created successfully.")
                tables.append('whatsapp_history')

            if 'customers' in tables:
                inspector = inspect(engine)
                cust_cols = [c['name'] for c in inspector.get_columns('customers')]
                if 'whatsapp_enabled' not in cust_cols:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE customers ADD COLUMN whatsapp_enabled INTEGER DEFAULT 1"))
                    log("'whatsapp_enabled' added to customers table.")
                if 'auto_send_invoice' not in cust_cols:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE customers ADD COLUMN auto_send_invoice INTEGER DEFAULT 0"))
                    log("'auto_send_invoice' added to customers table.")
                if 'auto_send_ledger' not in cust_cols:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE customers ADD COLUMN auto_send_ledger INTEGER DEFAULT 0"))
                    log("'auto_send_ledger' added to customers table.")

    except Exception as e:
        log(f"FAILED to run auto-healing check: {e}")
        log(traceback.format_exc())

if __name__ == "__main__":
    apply_migrations()
