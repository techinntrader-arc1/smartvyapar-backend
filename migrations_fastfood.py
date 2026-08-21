import os
import sys
import traceback
from sqlalchemy import inspect, text
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from database import engine

def log(msg):
    print(f"[FastFood Migration] {msg}")

def apply_migrations():
    log("--- Starting FastFood Migration Check ---")
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()

        # 1. Create 'ff_tables' table
        if 'ff_tables' not in tables:
            log("Creating 'ff_tables' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_tables (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(50) NOT NULL UNIQUE,
                        status VARCHAR(20) DEFAULT 'available',
                        sort_order INTEGER DEFAULT 0,
                        is_active BOOLEAN DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_tables' created successfully.")
            
            # Seed some initial tables
            with engine.begin() as conn:
                for i in range(1, 11):
                    conn.execute(text(f"INSERT INTO ff_tables (name, sort_order) VALUES ('Table {i}', {i})"))
            log("Seeded Table 1 to Table 10.")
            tables.append('ff_tables')

        # 2. Create 'ff_riders' table
        if 'ff_riders' not in tables:
            log("Creating 'ff_riders' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_riders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL,
                        phone VARCHAR(20),
                        status VARCHAR(20) DEFAULT 'available',
                        is_active BOOLEAN DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_riders' created successfully.")
            
            # Seed some initial riders
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO ff_riders (name, phone) VALUES ('Ali Khan', '0300-1234567')"))
                conn.execute(text("INSERT INTO ff_riders (name, phone) VALUES ('Raza Ahmed', '0312-7654321')"))
            log("Seeded default riders.")
            tables.append('ff_riders')

        # 3. Create 'ff_modifiers' table
        if 'ff_modifiers' not in tables:
            log("Creating 'ff_modifiers' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_modifiers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL,
                        price FLOAT DEFAULT 0.0,
                        sort_order INTEGER DEFAULT 0,
                        is_active BOOLEAN DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_modifiers' created successfully.")
            
            # Seed default modifiers
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO ff_modifiers (name, price) VALUES ('Extra Cheese', 60.0)"))
                conn.execute(text("INSERT INTO ff_modifiers (name, price) VALUES ('Double Patty', 120.0)"))
                conn.execute(text("INSERT INTO ff_modifiers (name, price) VALUES ('Extra Sauce', 20.0)"))
                conn.execute(text("INSERT INTO ff_modifiers (name, price) VALUES ('Add Fries & Drink (Meal)', 180.0)"))
            log("Seeded default modifiers.")
            tables.append('ff_modifiers')

        # 4. Create 'ff_recipes' table
        if 'ff_recipes' not in tables:
            log("Creating 'ff_recipes' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_recipes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id INTEGER NOT NULL REFERENCES ff_items(id),
                        product_id INTEGER NOT NULL REFERENCES products(id),
                        qty FLOAT NOT NULL DEFAULT 1.0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_recipes' created successfully.")
            tables.append('ff_recipes')

        # 5. Create 'ff_order_item_modifiers' table
        if 'ff_order_item_modifiers' not in tables:
            log("Creating 'ff_order_item_modifiers' table...")
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE ff_order_item_modifiers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_item_id INTEGER NOT NULL REFERENCES ff_order_items(id),
                        modifier_id INTEGER NOT NULL REFERENCES ff_modifiers(id),
                        name VARCHAR(100) NOT NULL,
                        price FLOAT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            log("'ff_order_item_modifiers' created successfully.")
            tables.append('ff_order_item_modifiers')

        # 6. Add columns to 'ff_orders' table if missing
        if 'ff_orders' in tables:
            columns = [c['name'] for c in inspector.get_columns('ff_orders')]
            
            if 'rider_id' not in columns:
                log("Adding 'rider_id' column to ff_orders...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE ff_orders ADD COLUMN rider_id INTEGER REFERENCES ff_riders(id)"))
                log("'rider_id' added successfully.")

            if 'delivery_address' not in columns:
                log("Adding 'delivery_address' column to ff_orders...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE ff_orders ADD COLUMN delivery_address TEXT"))
                log("'delivery_address' added successfully.")

            if 'rider_status' not in columns:
                log("Adding 'rider_status' column to ff_orders...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE ff_orders ADD COLUMN rider_status VARCHAR(20)"))
                log("'rider_status' added successfully.")

            if 'kot_printed' not in columns:
                log("Adding 'kot_printed' column to ff_orders...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE ff_orders ADD COLUMN kot_printed BOOLEAN DEFAULT 0"))
                log("'kot_printed' added successfully.")

        log("--- FastFood Migrations Completed Successfully ---")

    except Exception as e:
        log(f"FATAL ERROR DURING MIGRATION: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    apply_migrations()
