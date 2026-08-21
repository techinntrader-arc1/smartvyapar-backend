"""Idempotent, non-destructive schema migrations for employee accounting."""

from decimal import Decimal, ROUND_HALF_UP
import threading

from sqlalchemy import inspect, text

from database import engine


_EMPLOYEE_MIGRATION_LOCK = threading.RLock()


def _columns(conn, table_name):
    # Introspect through the same connection that owns the migration
    # transaction.  Opening a second SQLite connection while ALTER TABLE is
    # uncommitted can fail with "database schema is locked" on legacy data.
    return {column["name"] for column in inspect(conn).get_columns(table_name)}


def _add_column_if_missing(conn, table_name, column_name, ddl):
    inspector = inspect(conn)
    if inspector.has_table(table_name) and column_name not in _columns(conn, table_name):
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))


def _migrate_legacy_employee_financials(conn):
    """Copy the prototype employee ledger/payroll into the final append-only schema.

    The prototype used ``employee_ledger`` and ``payrolls``.  Stable migration
    idempotency keys retain every posted row without relying on legacy/new
    auto-increment IDs being equal.
    """
    tables = set(inspect(conn).get_table_names())

    if "payrolls" in tables:
        conn.execute(text("""
            INSERT INTO employee_payrolls(
                employee_id, salary_period, gross_salary, bonus, overtime,
                deductions, advances, goods_credit, employee_repayments,
                goods_returns, carried_debt_offset, amount_paid, net_payable,
                status, generated_by, created_at, updated_at
            )
            SELECT p.employee_id, SUBSTR(p.payroll_month, 1, 7),
                   MAX(COALESCE(p.gross_salary, 0), 0),
                   COALESCE(p.bonus, 0), COALESCE(p.overtime, 0),
                   COALESCE(p.fines_deductions, 0),
                   COALESCE(p.advances_deducted, 0),
                   COALESCE(p.goods_credit_deducted, 0), 0, 0, 0,
                   MAX(COALESCE(p.amount_paid, 0), 0),
                   MAX(COALESCE(
                       p.net_salary,
                       COALESCE(p.gross_salary, 0) + COALESCE(p.bonus, 0)
                       + COALESCE(p.overtime, 0)
                       - COALESCE(p.fines_deductions, 0)
                       - COALESCE(p.advances_deducted, 0)
                       - COALESCE(p.goods_credit_deducted, 0)
                   ), 0),
                   CASE LOWER(COALESCE(p.status, ''))
                       WHEN 'paid' THEN 'settled'
                       WHEN 'settled' THEN 'settled'
                       WHEN 'partial' THEN 'partial'
                       WHEN 'partially_paid' THEN 'partial'
                       WHEN 'voided' THEN 'voided'
                       ELSE 'unpaid'
                   END,
                   COALESCE(NULLIF(p.created_by, ''), 'migration'),
                   COALESCE(p.created_at, CURRENT_TIMESTAMP),
                   COALESCE(p.created_at, CURRENT_TIMESTAMP)
              FROM payrolls p
              JOIN employees e ON e.id = p.employee_id
             WHERE LENGTH(COALESCE(p.payroll_month, '')) >= 7
               AND NOT EXISTS (
                   SELECT 1 FROM employee_payrolls np
                    WHERE np.employee_id = p.employee_id
                      AND np.salary_period = SUBSTR(p.payroll_month, 1, 7)
               )
        """))

    if "employee_ledger" not in tables:
        return

    payroll_join = """
        LEFT JOIN payrolls lp
          ON lp.id = l.reference_id
         AND LOWER(COALESCE(l.reference_type, '')) IN ('payroll', 'payroll_payment', 'salary_payment')
        LEFT JOIN employee_payrolls np
          ON np.employee_id = l.employee_id
         AND np.salary_period = SUBSTR(lp.payroll_month, 1, 7)
    """ if "payrolls" in tables else ""
    payroll_period = "SUBSTR(lp.payroll_month, 1, 7)" if "payrolls" in tables else "NULL"
    payroll_id = "np.id" if "payrolls" in tables else "NULL"

    conn.execute(text(f"""
        INSERT INTO employee_ledger_entries(
            employee_id, transaction_type, amount, signed_amount,
            balance_after, reference_no, description, salary_period,
            sale_id, payroll_id, cash_transaction_id, source_type, source_id,
            idempotency_key, reversal_of_id, is_reversed, reversal_reason,
            metadata_json, created_by, created_at
        )
        SELECT l.employee_id,
               UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')),
               ABS(l.amount),
               CASE LOWER(COALESCE(l.type_direction, ''))
                   WHEN 'credit' THEN ABS(l.amount)
                   WHEN 'debit' THEN -ABS(l.amount)
                   ELSE CASE UPPER(REPLACE(TRIM(l.tx_type), ' ', '_'))
                       WHEN 'SALARY_ACCRUAL' THEN ABS(l.amount)
                       WHEN 'BONUS' THEN ABS(l.amount)
                       WHEN 'OVERTIME' THEN ABS(l.amount)
                       WHEN 'EMPLOYEE_REPAYMENT' THEN ABS(l.amount)
                       WHEN 'GOODS_RETURN' THEN ABS(l.amount)
                       WHEN 'OPENING_BALANCE' THEN l.amount
                       ELSE -ABS(l.amount)
                   END
               END,
               COALESCE(l.running_balance, 0),
               COALESCE(NULLIF(l.reference_no, ''), 'LEGACY-EMP-' || l.id),
               l.description, {payroll_period},
               CASE WHEN LOWER(COALESCE(l.reference_type, '')) = 'sale'
                    THEN s.id ELSE NULL END,
               {payroll_id}, ct.id, NULLIF(l.reference_type, ''), l.reference_id,
               'legacy-employee-ledger:' || l.id, NULL,
               COALESCE(l.is_reversed, 0), l.reversal_reason,
               '{{"legacy_employee_ledger_id":' || l.id || '}}',
               COALESCE(NULLIF(l.created_by, ''), 'migration'),
               COALESCE(l.tx_date, l.created_at, CURRENT_TIMESTAMP)
          FROM employee_ledger l
          JOIN employees e ON e.id = l.employee_id
          LEFT JOIN sales s
            ON LOWER(COALESCE(l.reference_type, '')) = 'sale'
           AND s.id = l.reference_id
          LEFT JOIN cash_transactions ct
            ON ct.id = (
                SELECT MIN(ct2.id) FROM cash_transactions ct2
                 WHERE UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')) IN (
                           'EMPLOYEE_REPAYMENT', 'CASH_ADVANCE', 'SALARY_PAYMENT'
                       )
                   AND NULLIF(TRIM(ct2.reference_no), '') = NULLIF(TRIM(l.reference_no), '')
                   AND ROUND(ABS(COALESCE(ct2.amount, 0)), 2) =
                       ROUND(ABS(COALESCE(l.amount, 0)), 2)
                   AND LOWER(COALESCE(ct2.tx_type, '')) = CASE
                       WHEN UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')) =
                            'EMPLOYEE_REPAYMENT' THEN 'cash_in'
                       ELSE 'cash_out'
                   END
                HAVING COUNT(*) = 1
            )
          {payroll_join}
         WHERE ABS(COALESCE(l.amount, 0)) > 0
           AND NOT EXISTS (
               SELECT 1 FROM employee_ledger_entries ne
                WHERE ne.idempotency_key = 'legacy-employee-ledger:' || l.id
           )
         ORDER BY l.id
    """))

    # Restore reversal links after all legacy rows have stable new IDs.
    conn.execute(text("""
        UPDATE employee_ledger_entries
           SET reversal_of_id = (
               SELECT original_new.id
                 FROM employee_ledger original_legacy
                 JOIN employee_ledger_entries original_new
                   ON original_new.idempotency_key =
                      'legacy-employee-ledger:' || original_legacy.id
                WHERE original_legacy.reversal_ledger_id = (
                    SELECT reversal_legacy.id
                      FROM employee_ledger reversal_legacy
                     WHERE employee_ledger_entries.idempotency_key =
                           'legacy-employee-ledger:' || reversal_legacy.id
                )
                LIMIT 1
           )
         WHERE idempotency_key LIKE 'legacy-employee-ledger:%'
           AND reversal_of_id IS NULL
           AND EXISTS (
               SELECT 1
                 FROM employee_ledger original_legacy
                 JOIN employee_ledger reversal_legacy
                   ON reversal_legacy.id = original_legacy.reversal_ledger_id
                WHERE employee_ledger_entries.idempotency_key =
                      'legacy-employee-ledger:' || reversal_legacy.id
           )
    """))

    if "payrolls" in tables:
        conn.execute(text("""
            UPDATE employee_payrolls
               SET accrual_entry_id = (
                    SELECT ne.id
                      FROM payrolls p
                      JOIN employee_ledger l
                        ON l.employee_id = p.employee_id
                       AND l.reference_id = p.id
                       AND LOWER(COALESCE(l.reference_type, '')) = 'payroll'
                       AND UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')) =
                           'SALARY_ACCRUAL'
                      JOIN employee_ledger_entries ne
                        ON ne.idempotency_key = 'legacy-employee-ledger:' || l.id
                     WHERE p.employee_id = employee_payrolls.employee_id
                       AND SUBSTR(p.payroll_month, 1, 7) = employee_payrolls.salary_period
                     ORDER BY l.id LIMIT 1
               )
             WHERE accrual_entry_id IS NULL
               AND EXISTS (
                    SELECT 1
                      FROM payrolls p
                      JOIN employee_ledger l
                        ON l.employee_id = p.employee_id
                       AND l.reference_id = p.id
                       AND LOWER(COALESCE(l.reference_type, '')) = 'payroll'
                       AND UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')) =
                           'SALARY_ACCRUAL'
                      JOIN employee_ledger_entries ne
                        ON ne.idempotency_key = 'legacy-employee-ledger:' || l.id
                     WHERE p.employee_id = employee_payrolls.employee_id
                       AND SUBSTR(p.payroll_month, 1, 7) = employee_payrolls.salary_period
                )
        """))

        conn.execute(text("""
            INSERT INTO employee_salary_payments(
                employee_id, payroll_id, ledger_entry_id,
                cash_transaction_id, amount, method, account, note,
                idempotency_key, is_reversed, reversed_by_entry_id,
                reversal_reason, created_by, created_at
            )
            SELECT l.employee_id, np.id, ne.id, ct.id, ABS(l.amount),
                   CASE LOWER(COALESCE(p.payment_method, ct.account, 'cash'))
                       WHEN 'bank' THEN 'bank'
                       WHEN 'bank transfer' THEN 'bank'
                       WHEN 'online' THEN 'online'
                       WHEN 'card' THEN 'online'
                       ELSE 'cash'
                   END,
                   CASE LOWER(COALESCE(p.payment_method, ct.account, 'cash'))
                       WHEN 'bank' THEN 'bank'
                       WHEN 'bank transfer' THEN 'bank'
                       WHEN 'online' THEN 'online'
                       WHEN 'card' THEN 'online'
                       ELSE 'cash_in_hand'
                   END,
                   l.description, 'legacy-salary-payment:' || l.id,
                   COALESCE(l.is_reversed, 0), reversal_new.id,
                   l.reversal_reason,
                   COALESCE(NULLIF(l.created_by, ''), 'migration'),
                   COALESCE(l.tx_date, l.created_at, CURRENT_TIMESTAMP)
              FROM employee_ledger l
              JOIN payrolls p ON p.id = l.reference_id
              JOIN employee_payrolls np
                ON np.employee_id = l.employee_id
               AND np.salary_period = SUBSTR(p.payroll_month, 1, 7)
              JOIN employee_ledger_entries ne
                ON ne.idempotency_key = 'legacy-employee-ledger:' || l.id
              JOIN cash_transactions ct
                ON ct.id = (
                    SELECT MIN(ct2.id) FROM cash_transactions ct2
                     WHERE NULLIF(TRIM(ct2.reference_no), '') = NULLIF(TRIM(l.reference_no), '')
                       AND ROUND(ABS(COALESCE(ct2.amount, 0)), 2) =
                           ROUND(ABS(COALESCE(l.amount, 0)), 2)
                       AND LOWER(COALESCE(ct2.tx_type, '')) = 'cash_out'
                    HAVING COUNT(*) = 1
                )
              LEFT JOIN employee_ledger_entries reversal_new
                ON reversal_new.idempotency_key =
                   'legacy-employee-ledger:' || l.reversal_ledger_id
             WHERE UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')) = 'SALARY_PAYMENT'
               AND NOT EXISTS (
                   SELECT 1 FROM employee_salary_payments sp
                    WHERE sp.idempotency_key = 'legacy-salary-payment:' || l.id
                       OR sp.ledger_entry_id = ne.id
                       OR sp.cash_transaction_id = ct.id
               )
        """))

    if {"sales", "sale_items"}.issubset(tables):
        conn.execute(text("""
            INSERT INTO employee_goods_credits(
                employee_id, sale_id, invoice_no, total, returned_amount,
                price_mode, status, ledger_entry_id, created_at
            )
            SELECT l.employee_id, s.id, s.invoice_no, ABS(l.amount),
                   MIN(ABS(l.amount), COALESCE((
                       SELECT SUM(ABS(gr.amount)) FROM employee_ledger gr
                         WHERE UPPER(REPLACE(TRIM(gr.tx_type), ' ', '_')) = 'GOODS_RETURN'
                           AND gr.employee_id = l.employee_id
                           AND LOWER(COALESCE(gr.reference_type, '')) = 'sale'
                           AND gr.reference_id = s.id
                   ), 0)),
                   'retail',
                   CASE
                       WHEN COALESCE((
                           SELECT SUM(ABS(gr.amount)) FROM employee_ledger gr
                             WHERE UPPER(REPLACE(TRIM(gr.tx_type), ' ', '_')) = 'GOODS_RETURN'
                               AND gr.employee_id = l.employee_id
                               AND LOWER(COALESCE(gr.reference_type, '')) = 'sale'
                               AND gr.reference_id = s.id
                       ), 0) >= ABS(l.amount) THEN 'returned'
                       WHEN COALESCE((
                           SELECT SUM(ABS(gr.amount)) FROM employee_ledger gr
                             WHERE UPPER(REPLACE(TRIM(gr.tx_type), ' ', '_')) = 'GOODS_RETURN'
                               AND gr.employee_id = l.employee_id
                               AND LOWER(COALESCE(gr.reference_type, '')) = 'sale'
                               AND gr.reference_id = s.id
                       ), 0) > 0 THEN 'partially_returned'
                       ELSE 'active'
                   END,
                   ne.id, COALESCE(l.tx_date, l.created_at, s.created_at, CURRENT_TIMESTAMP)
              FROM employee_ledger l
              JOIN sales s
                ON LOWER(COALESCE(l.reference_type, '')) = 'sale'
               AND s.id = l.reference_id
              JOIN employee_ledger_entries ne
                ON ne.idempotency_key = 'legacy-employee-ledger:' || l.id
             WHERE UPPER(REPLACE(TRIM(l.tx_type), ' ', '_')) = 'GOODS_ON_CREDIT'
               AND NOT EXISTS (
                   SELECT 1 FROM employee_goods_credits gc
                    WHERE gc.sale_id = s.id OR gc.ledger_entry_id = ne.id
               )
        """))

        conn.execute(text("""
            INSERT INTO employee_goods_credit_items(
                goods_credit_id, product_id, product_name, qty,
                price, cost, total, returned_qty
            )
            SELECT gc.id, si.product_id, COALESCE(si.product_name, 'Unknown item'),
                   si.qty, si.price, COALESCE(si.buy_price, 0), si.total,
                   COALESCE(si.returned_qty, 0)
              FROM employee_goods_credits gc
              JOIN sales s ON s.id = gc.sale_id
              JOIN sale_items si ON si.sale_id = s.id
             WHERE NOT EXISTS (
                 SELECT 1 FROM employee_goods_credit_items gi
                  WHERE gi.goods_credit_id = gc.id
             )
        """))

    # The append-only ledger is authoritative after migration.  This yields the
    # exact latest legacy running balance for a consistent legacy ledger while
    # continuing to include any later final-schema transactions on every start.
    conn.execute(text("""
        UPDATE employees
           SET current_balance = ROUND(COALESCE((
               SELECT SUM(le.signed_amount)
                 FROM employee_ledger_entries le
                WHERE le.employee_id = employees.id
           ), opening_balance, 0), 2)
         WHERE EXISTS (
             SELECT 1 FROM employee_ledger_entries le
              WHERE le.employee_id = employees.id
         )
           AND ROUND(COALESCE(current_balance, 0), 2) != ROUND(COALESCE((
               SELECT SUM(le.signed_amount)
                 FROM employee_ledger_entries le
                WHERE le.employee_id = employees.id
           ), opening_balance, 0), 2)
    """))


def apply_employee_migrations():
    """Apply employee schema changes once at a time within this process.

    A legacy database can first be discovered by a request after a restore.  In
    that case more than one dashboard request may try to repair the schema at
    once, so the public entry point must be process-serialized.
    """
    with _EMPLOYEE_MIGRATION_LOCK:
        _apply_employee_migrations_unlocked()


def _apply_employee_migrations_unlocked():
    """Add missing objects and copy legacy rows without deleting source data."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_code VARCHAR(40) NOT NULL UNIQUE,
                full_name VARCHAR(150) NOT NULL,
                phone VARCHAR(30), cnic VARCHAR(30) UNIQUE, address TEXT,
                designation VARCHAR(100), joining_date DATE,
                monthly_salary NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(monthly_salary >= 0),
                opening_balance NUMERIC(18,2) NOT NULL DEFAULT 0,
                credit_limit NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(credit_limit >= 0),
                current_balance NUMERIC(18,2) NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1, notes TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employee_payrolls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                salary_period VARCHAR(7) NOT NULL,
                gross_salary NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(gross_salary >= 0),
                bonus NUMERIC(18,2) NOT NULL DEFAULT 0,
                overtime NUMERIC(18,2) NOT NULL DEFAULT 0,
                deductions NUMERIC(18,2) NOT NULL DEFAULT 0,
                advances NUMERIC(18,2) NOT NULL DEFAULT 0,
                goods_credit NUMERIC(18,2) NOT NULL DEFAULT 0,
                employee_repayments NUMERIC(18,2) NOT NULL DEFAULT 0,
                goods_returns NUMERIC(18,2) NOT NULL DEFAULT 0,
                carried_debt_offset NUMERIC(18,2) NOT NULL DEFAULT 0,
                amount_paid NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(amount_paid >= 0),
                net_payable NUMERIC(18,2) NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'unpaid',
                accrual_entry_id INTEGER UNIQUE REFERENCES employee_ledger_entries(id),
                generated_by VARCHAR(50) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_employee_payroll_period UNIQUE(employee_id, salary_period)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employee_ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                transaction_type VARCHAR(40) NOT NULL,
                amount NUMERIC(18,2) NOT NULL CHECK(amount > 0),
                signed_amount NUMERIC(18,2) NOT NULL CHECK(signed_amount != 0),
                balance_after NUMERIC(18,2) NOT NULL,
                reference_no VARCHAR(80) NOT NULL,
                description TEXT, salary_period VARCHAR(7),
                sale_id INTEGER REFERENCES sales(id),
                payroll_id INTEGER REFERENCES employee_payrolls(id),
                cash_transaction_id INTEGER REFERENCES cash_transactions(id),
                source_type VARCHAR(40), source_id INTEGER,
                idempotency_key VARCHAR(160) UNIQUE,
                reversal_of_id INTEGER UNIQUE REFERENCES employee_ledger_entries(id),
                is_reversed BOOLEAN NOT NULL DEFAULT 0,
                reversal_reason TEXT, metadata_json TEXT,
                created_by VARCHAR(50) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employee_salary_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                payroll_id INTEGER NOT NULL REFERENCES employee_payrolls(id),
                ledger_entry_id INTEGER NOT NULL UNIQUE REFERENCES employee_ledger_entries(id),
                cash_transaction_id INTEGER NOT NULL UNIQUE REFERENCES cash_transactions(id),
                amount NUMERIC(18,2) NOT NULL CHECK(amount > 0),
                method VARCHAR(20) NOT NULL, account VARCHAR(30) NOT NULL,
                note TEXT, idempotency_key VARCHAR(160) UNIQUE,
                is_reversed BOOLEAN NOT NULL DEFAULT 0,
                reversed_by_entry_id INTEGER UNIQUE REFERENCES employee_ledger_entries(id),
                reversal_reason TEXT, created_by VARCHAR(50) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employee_goods_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                sale_id INTEGER NOT NULL UNIQUE REFERENCES sales(id),
                invoice_no VARCHAR(80) NOT NULL,
                total NUMERIC(18,2) NOT NULL CHECK(total > 0),
                returned_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(returned_amount >= 0),
                price_mode VARCHAR(20) NOT NULL DEFAULT 'retail',
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                ledger_entry_id INTEGER NOT NULL UNIQUE REFERENCES employee_ledger_entries(id),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employee_goods_credit_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goods_credit_id INTEGER NOT NULL REFERENCES employee_goods_credits(id),
                product_id INTEGER REFERENCES products(id), product_name VARCHAR(150) NOT NULL,
                qty FLOAT NOT NULL, price NUMERIC(18,2) NOT NULL,
                cost NUMERIC(18,2) NOT NULL DEFAULT 0, total NUMERIC(18,2) NOT NULL,
                returned_qty FLOAT NOT NULL DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sale_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL REFERENCES sales(id),
                idempotency_key VARCHAR(160) NOT NULL UNIQUE,
                return_type VARCHAR(20) NOT NULL,
                refund_amount NUMERIC(18,2) NOT NULL CHECK(refund_amount >= 0),
                payment_method VARCHAR(30) NOT NULL,
                cash_transaction_id INTEGER UNIQUE REFERENCES cash_transactions(id),
                created_by VARCHAR(50) NOT NULL,
                notes TEXT,
                posted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(return_type IN ('full', 'partial', 'legacy'))
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sale_return_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_return_id INTEGER NOT NULL REFERENCES sale_returns(id),
                sale_item_id INTEGER NOT NULL REFERENCES sale_items(id),
                product_id INTEGER REFERENCES products(id),
                product_name VARCHAR(150) NOT NULL,
                qty NUMERIC(18,4) NOT NULL CHECK(qty > 0),
                allocated_amount NUMERIC(18,2) NOT NULL CHECK(allocated_amount >= 0),
                allocated_tax_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(allocated_tax_amount >= 0),
                cost_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(cost_amount >= 0),
                CONSTRAINT uq_sale_return_item_line UNIQUE(sale_return_id, sale_item_id)
            )
        """))

        # Compatibility with the employee prototype shipped before the final
        # ledger module.  That table used ``job_title`` and did not carry the
        # balance projection or update timestamp.  CREATE TABLE IF NOT EXISTS
        # cannot evolve it, so every change below is additive and idempotent.
        _add_column_if_missing(conn, "employees", "designation", "designation VARCHAR(100)")
        _add_column_if_missing(
            conn,
            "employees",
            "current_balance",
            "current_balance NUMERIC(18,2) NOT NULL DEFAULT 0",
        )
        # SQLite rejects ALTER TABLE with a non-constant CURRENT_TIMESTAMP
        # default.  Add the column first, then populate all legacy rows.
        _add_column_if_missing(conn, "employees", "updated_at", "updated_at DATETIME")
        _add_column_if_missing(
            conn,
            "employee_payrolls",
            "employee_repayments",
            "employee_repayments NUMERIC(18,2) NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            conn,
            "employee_payrolls",
            "goods_returns",
            "goods_returns NUMERIC(18,2) NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            conn,
            "employee_payrolls",
            "carried_debt_offset",
            "carried_debt_offset NUMERIC(18,2) NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(conn, "products", "employee_price", "employee_price NUMERIC(18,2)")
        _add_column_if_missing(conn, "sales", "employee_id", "employee_id INTEGER REFERENCES employees(id)")
        _add_column_if_missing(conn, "sales", "idempotency_key", "idempotency_key VARCHAR(160)")

        employee_columns_after = _columns(conn, "employees")
        if "job_title" in employee_columns_after:
            conn.execute(text("""
                UPDATE employees
                   SET designation = COALESCE(NULLIF(designation, ''), job_title)
                 WHERE (designation IS NULL OR TRIM(designation) = '')
                   AND NULLIF(TRIM(job_title), '') IS NOT NULL
            """))
        if "joining_date" in employee_columns_after:
            # The prototype stored this Date column as a full timestamp.  The
            # final SQLAlchemy Date processor accepts only YYYY-MM-DD.
            conn.execute(text("""
                UPDATE employees
                   SET joining_date = SUBSTR(joining_date, 1, 10)
                 WHERE joining_date GLOB '????-??-?? *'
                    OR joining_date GLOB '????-??-??T*'
            """))
        conn.execute(text("""
            UPDATE employees
               SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
             WHERE updated_at IS NULL
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS employees_fill_updated_at_after_insert
            AFTER INSERT ON employees
            WHEN NEW.updated_at IS NULL
            BEGIN
                UPDATE employees
                   SET updated_at = CURRENT_TIMESTAMP
                 WHERE id = NEW.id;
            END
        """))
        _migrate_legacy_employee_financials(conn)

        # Give any pre-ledger opening balance an immutable source row.  Run this
        # idempotent repair on every start so an interrupted older migration is
        # also healed when current_balance already exists.
        conn.execute(text("""
            INSERT OR IGNORE INTO employee_ledger_entries(
                employee_id, transaction_type, amount, signed_amount,
                balance_after, reference_no, description, source_type,
                source_id, idempotency_key, is_reversed, created_by,
                created_at
            )
            SELECT e.id, 'OPENING_BALANCE', ABS(e.opening_balance),
                   e.opening_balance, e.opening_balance,
                   'OPEN-' || e.employee_code,
                   'Legacy employee opening balance migration',
                   'employee', e.id, 'legacy-opening:' || e.id,
                   0, 'migration', COALESCE(e.created_at, CURRENT_TIMESTAMP)
              FROM employees e
             WHERE COALESCE(e.opening_balance, 0) != 0
               AND NOT EXISTS (
                   SELECT 1 FROM employee_ledger_entries l
                    WHERE l.employee_id = e.id
               )
        """))
        conn.execute(text("""
            UPDATE employees
               SET current_balance = ROUND(COALESCE((
                   SELECT SUM(le.signed_amount)
                     FROM employee_ledger_entries le
                    WHERE le.employee_id = employees.id
               ), opening_balance, 0), 2)
             WHERE ROUND(COALESCE(current_balance, 0), 2) != ROUND(COALESCE((
                   SELECT SUM(le.signed_amount)
                     FROM employee_ledger_entries le
                    WHERE le.employee_id = employees.id
               ), opening_balance, 0), 2)
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS protect_sale_returns_accounting_update
            BEFORE UPDATE ON sale_returns
            WHEN NOT (
                NEW.sale_id IS OLD.sale_id
                AND NEW.idempotency_key IS OLD.idempotency_key
                AND NEW.return_type IS OLD.return_type
                AND NEW.refund_amount IS OLD.refund_amount
                AND NEW.payment_method IS OLD.payment_method
                AND NEW.created_by IS OLD.created_by
                AND NEW.notes IS OLD.notes
                AND NEW.posted_at IS OLD.posted_at
            )
            BEGIN
                SELECT RAISE(ABORT, 'posted sale return accounting fields are immutable');
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS protect_sale_return_items_update
            BEFORE UPDATE ON sale_return_items
            WHEN NOT (
                NEW.sale_return_id IS OLD.sale_return_id
                AND NEW.sale_item_id IS OLD.sale_item_id
                AND NEW.product_id IS OLD.product_id
                AND NEW.product_name IS OLD.product_name
                AND NEW.qty IS OLD.qty
                AND NEW.allocated_amount IS OLD.allocated_amount
                AND NEW.allocated_tax_amount IS OLD.allocated_tax_amount
                AND NEW.cost_amount IS OLD.cost_amount
            )
            BEGIN
                SELECT RAISE(ABORT, 'posted sale return lines are immutable');
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS protect_sale_returns_delete
            BEFORE DELETE ON sale_returns
            BEGIN
                SELECT RAISE(ABORT, 'posted sale returns cannot be deleted');
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS protect_sale_return_items_delete
            BEFORE DELETE ON sale_return_items
            BEGIN
                SELECT RAISE(ABORT, 'posted sale return lines cannot be deleted');
            END
        """))

        # POS integration fields.  ALTER TABLE is additive and preserves every
        # existing sale/product row (NULL means retail/default behavior).
        _add_column_if_missing(conn, "products", "employee_price", "employee_price NUMERIC(18,2)")
        _add_column_if_missing(conn, "sales", "employee_id", "employee_id INTEGER REFERENCES employees(id)")
        _add_column_if_missing(conn, "sales", "idempotency_key", "idempotency_key VARCHAR(160)")
        _add_column_if_missing(conn, "employee_payrolls", "employee_repayments", "employee_repayments NUMERIC(18,2) NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "employee_payrolls", "goods_returns", "goods_returns NUMERIC(18,2) NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "employee_payrolls", "carried_debt_offset", "carried_debt_offset NUMERIC(18,2) NOT NULL DEFAULT 0")

        indexes = (
            ("idx_employees_name", "employees", "full_name"),
            ("idx_employees_active", "employees", "is_active"),
            ("idx_employee_ledger_employee_date", "employee_ledger_entries", "employee_id, created_at"),
            ("idx_employee_ledger_type", "employee_ledger_entries", "transaction_type"),
            ("idx_employee_ledger_period", "employee_ledger_entries", "salary_period"),
            ("idx_employee_payroll_period", "employee_payrolls", "salary_period"),
            ("idx_employee_salary_payments_employee", "employee_salary_payments", "employee_id, created_at"),
            ("idx_employee_goods_employee", "employee_goods_credits", "employee_id, created_at"),
            ("idx_employee_goods_items_credit", "employee_goods_credit_items", "goods_credit_id"),
            ("idx_sale_returns_sale_date", "sale_returns", "sale_id, posted_at"),
            ("idx_sale_return_items_return", "sale_return_items", "sale_return_id"),
            ("idx_sale_return_items_product", "sale_return_items", "product_id"),
            ("idx_sales_employee_id", "sales", "employee_id"),
            ("uq_sales_idempotency_key", "sales", "idempotency_key"),
        )
        known_tables = set(inspect(conn).get_table_names()) | {
            "employees", "employee_ledger_entries", "employee_payrolls",
            "employee_salary_payments", "employee_goods_credits",
            "employee_goods_credit_items", "sale_returns", "sale_return_items",
        }
        for index_name, table_name, columns in indexes:
            if table_name in known_tables:
                unique = "UNIQUE " if index_name == "uq_sales_idempotency_key" else ""
                conn.execute(text(f"CREATE {unique}INDEX IF NOT EXISTS {index_name} ON {table_name}({columns})"))

        if "settings" in known_tables:
            conn.execute(text("""
                INSERT OR IGNORE INTO settings(key, value)
                VALUES ('employee_price_mode', 'retail')
            """))

        # Preserve historical returns that pre-date immutable return documents.
        # Their true posting time was never stored, so sale.created_at is the
        # only auditable timestamp available and is retained as a legacy value.
        if {"sales", "sale_items", "sale_returns", "sale_return_items"}.issubset(known_tables):
            rows = conn.execute(text("""
                SELECT s.id AS sale_id, s.total AS sale_total, COALESCE(s.tax_amount, 0) AS sale_tax,
                       COALESCE(s.payment_method, 'cash') AS payment_method,
                       COALESCE(s.cashier, 'migration') AS created_by,
                       s.created_at AS posted_at,
                       si.id AS sale_item_id, si.product_id, si.product_name,
                       si.qty, si.returned_qty, si.total AS line_total,
                       COALESCE(si.tax_pct, 0) AS tax_pct,
                       COALESCE(si.buy_price, 0) AS buy_price,
                       (SELECT COALESCE(SUM(si2.total), 0)
                          FROM sale_items si2 WHERE si2.sale_id = s.id) AS invoice_line_total,
                       (SELECT COUNT(*) FROM sale_items si3
                          WHERE si3.sale_id = s.id AND COALESCE(si3.returned_qty, 0) < si3.qty) AS open_line_count
                  FROM sales s
                  JOIN sale_items si ON si.sale_id = s.id
                 WHERE COALESCE(si.returned_qty, 0) > 0
                 ORDER BY s.id, si.id
            """)).mappings().all()

            grouped = {}
            for row in rows:
                grouped.setdefault(row["sale_id"], []).append(row)

            cents = Decimal("0.01")
            for sale_id, sale_rows in grouped.items():
                idem = f"legacy-sale-return:{sale_id}"
                # A modern immutable return document already accounts for this
                # legacy returned_qty snapshot.  Backfilling again on a later
                # application startup would double every refund/report total.
                if conn.execute(
                    text("SELECT 1 FROM sale_returns WHERE sale_id = :sale_id LIMIT 1"),
                    {"sale_id": sale_id},
                ).first():
                    continue

                sale_total = Decimal(str(sale_rows[0]["sale_total"] or 0))
                invoice_line_total = Decimal(str(sale_rows[0]["invoice_line_total"] or 0))
                factor = sale_total / invoice_line_total if invoice_line_total > 0 else Decimal("0")
                raw_lines = []
                for row in sale_rows:
                    sold_qty = Decimal(str(row["qty"] or 0))
                    returned_qty = Decimal(str(row["returned_qty"] or 0))
                    line_total = Decimal(str(row["line_total"] or 0))
                    raw_amount = (line_total / sold_qty) * returned_qty * factor if sold_qty > 0 else Decimal("0")
                    raw_lines.append((row, returned_qty, raw_amount))

                is_full = int(sale_rows[0]["open_line_count"] or 0) == 0
                refund_amount = sale_total if is_full else sum((line[2] for line in raw_lines), Decimal("0"))
                refund_amount = refund_amount.quantize(cents, rounding=ROUND_HALF_UP)
                if refund_amount < 0:
                    continue

                result = conn.execute(text("""
                    INSERT INTO sale_returns(
                        sale_id, idempotency_key, return_type, refund_amount,
                        payment_method, created_by, notes, posted_at
                    ) VALUES (
                        :sale_id, :key, 'legacy', :amount,
                        :method, :created_by, :notes, COALESCE(:posted_at, CURRENT_TIMESTAMP)
                    )
                """), {
                    "sale_id": sale_id,
                    "key": idem,
                    "amount": str(refund_amount),
                    "method": sale_rows[0]["payment_method"],
                    "created_by": sale_rows[0]["created_by"],
                    "notes": "Backfilled from historical returned_qty; original return timestamp unavailable",
                    "posted_at": sale_rows[0]["posted_at"],
                })
                return_id = result.lastrowid
                raw_taxes = []
                for row, returned_qty, _raw_amount in raw_lines:
                    tax_pct = Decimal(str(row["tax_pct"] or 0))
                    sold_qty = Decimal(str(row["qty"] or 0))
                    line_total = Decimal(str(row["line_total"] or 0))
                    item_tax = line_total * tax_pct / (Decimal("100") + tax_pct) if tax_pct > 0 else Decimal("0")
                    raw_taxes.append((item_tax / sold_qty) * returned_qty if sold_qty > 0 else Decimal("0"))
                return_tax = Decimal(str(sale_rows[0]["sale_tax"] or 0)) if is_full else sum(raw_taxes, Decimal("0"))
                return_tax = return_tax.quantize(cents, rounding=ROUND_HALF_UP)

                allocated = Decimal("0")
                allocated_tax = Decimal("0")
                for index, (row, returned_qty, raw_amount) in enumerate(raw_lines):
                    if index == len(raw_lines) - 1:
                        line_amount = refund_amount - allocated
                        tax_amount = max(Decimal("0"), return_tax - allocated_tax)
                    else:
                        line_amount = min(
                            raw_amount.quantize(cents, rounding=ROUND_HALF_UP),
                            max(Decimal("0"), refund_amount - allocated),
                        )
                        allocated += line_amount
                        tax_amount = min(
                            raw_taxes[index].quantize(cents, rounding=ROUND_HALF_UP),
                            max(Decimal("0"), return_tax - allocated_tax),
                        )
                        allocated_tax += tax_amount
                    cost_amount = (returned_qty * Decimal(str(row["buy_price"] or 0))).quantize(cents, rounding=ROUND_HALF_UP)
                    conn.execute(text("""
                        INSERT INTO sale_return_items(
                            sale_return_id, sale_item_id, product_id, product_name,
                            qty, allocated_amount, allocated_tax_amount, cost_amount
                        ) VALUES (
                            :return_id, :sale_item_id, :product_id, :product_name,
                            :qty, :amount, :tax, :cost
                        )
                    """), {
                        "return_id": return_id,
                        "sale_item_id": row["sale_item_id"],
                        "product_id": row["product_id"],
                        "product_name": row["product_name"] or "Unknown item",
                        "qty": str(returned_qty),
                        "amount": str(max(Decimal("0"), line_amount)),
                        "tax": str(max(Decimal("0"), tax_amount)),
                        "cost": str(max(Decimal("0"), cost_amount)),
                    })
