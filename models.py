"""
SmartVyapar - SQLAlchemy Models (All 14 tables)
"""

from sqlalchemy import (
    Column, Integer, String, Float, Numeric, Boolean, Date, DateTime, Text,
    ForeignKey, Enum, UniqueConstraint, CheckConstraint
)
from decimal import Decimal
from sqlalchemy.orm import relationship, synonym
from sqlalchemy.sql import func
from database import Base


def Money():
    """Fixed-scale monetary type while retaining float compatibility in APIs."""
    return Numeric(18, 2, asdecimal=False)


# ── Users ─────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    full_name = Column(String(100))
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum("admin", "staff"), default="staff")
    permissions = Column(Text, default="billing,sales-list,customers,suppliers,payments,stock-report,license-info,help,labels")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)


# ── Categories ────────────────────────────────────────────────────────────────
class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    products = relationship("Product", back_populates="category")


# ── Units ─────────────────────────────────────────────────────────────────────
class Unit(Base):
    __tablename__ = "units"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)  # pcs, kg, box, liter
    products = relationship("Product", back_populates="unit")


# ── Brands ─────────────────────────────────────────────────────────────────────
class Brand(Base):
    __tablename__ = "brands"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    products = relationship("Product", back_populates="brand")


# ── Customers ─────────────────────────────────────────────────────────────────
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)
    phone = Column(String(20))
    address = Column(Text)
    credit_balance = Column(Money(), default=0.0)  # positive = owes us
    whatsapp_enabled = Column(Boolean, default=True)
    auto_send_invoice = Column(Boolean, default=False)
    auto_send_ledger = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    sales = relationship("Sale", back_populates="customer")
    payments = relationship("Payment", back_populates="customer",
                            primaryjoin="and_(Payment.party_type=='customer', Payment.party_id==Customer.id)",
                            foreign_keys="[Payment.party_id]")


# ── Suppliers ─────────────────────────────────────────────────────────────────
class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)
    phone = Column(String(20))
    address = Column(Text)
    due_balance = Column(Money(), default=0.0)  # we owe them
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    purchases = relationship("Purchase", back_populates="supplier")


# ── Products ──────────────────────────────────────────────────────────────────
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=True)
    name = Column(String(150), nullable=False, index=True)
    barcode = Column(String(50), unique=True, nullable=True, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True, index=True)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=True)
    brand_id = Column(Integer, ForeignKey("brands.id"), nullable=True)
    buy_price = Column(Money(), default=0.0)
    sell_price = Column(Money(), default=0.0)
    employee_price = Column(Numeric(18, 2, asdecimal=True), nullable=True)
    tax_pct = Column(Float, default=0.0)          # tax percentage
    stock = Column(Float, default=0.0, index=True)            # current stock
    min_stock = Column(Float, default=5.0, index=True)        # low stock threshold
    image_path = Column(String(255), nullable=True)
    location = Column(String(100), nullable=True)  # Rack Location
    is_service = Column(Boolean, default=False)   # Non-stock items
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    category = relationship("Category", back_populates="products")
    unit = relationship("Unit", back_populates="products")
    brand = relationship("Brand", back_populates="products")
    sale_items = relationship("SaleItem", back_populates="product")
    purchase_items = relationship("PurchaseItem", back_populates="product")
    stock_movements = relationship("StockMovement", back_populates="product")


# ── Sales ─────────────────────────────────────────────────────────────────────
class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, index=True)
    invoice_no = Column(String(30), unique=True, nullable=False)
    idempotency_key = Column(String(160), unique=True, nullable=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    subtotal = Column(Money(), default=0.0)
    discount = Column(Money(), default=0.0)
    tax_amount = Column(Money(), default=0.0)
    total = Column(Money(), default=0.0)
    paid_amount = Column(Money(), default=0.0)
    amount_tendered = Column(Money(), default=0.0)
    change_returned = Column(Money(), default=0.0)
    # String preserves legacy methods while allowing the employee_credit flow.
    payment_method = Column(String(30), default="cash")
    status = Column(Enum("completed", "held", "returned", "partially_returned"), default="completed")
    cashier = Column(String(50))
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    customer = relationship("Customer", back_populates="sales")
    employee = relationship("Employee", back_populates="sales", foreign_keys=[employee_id])
    items = relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")
    returns = relationship("SaleReturn", back_populates="sale", order_by="SaleReturn.posted_at")


class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_name = Column(String(150))   # snapshot at sale time
    qty = Column(Float, nullable=False)
    price = Column(Money(), nullable=False)
    discount = Column(Money(), default=0.0)
    tax_pct = Column(Float, default=0.0)
    total = Column(Money(), nullable=False)
    returned_qty = Column(Float, default=0.0)
    buy_price = Column(Money(), default=0.0)  # Captured at time of sale for profit accuracy
    sale = relationship("Sale", back_populates="items")
    product = relationship("Product", back_populates="sale_items")
    return_lines = relationship("SaleReturnItem", back_populates="sale_item")


class SaleReturn(Base):
    """Immutable header for one posted full or partial sale return."""
    __tablename__ = "sale_returns"
    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, index=True)
    idempotency_key = Column(String(160), nullable=False, unique=True, index=True)
    return_type = Column(String(20), nullable=False)
    refund_amount = Column(Numeric(18, 2, asdecimal=True), nullable=False)
    payment_method = Column(String(30), nullable=False)
    cash_transaction_id = Column(Integer, ForeignKey("cash_transactions.id"), nullable=True, unique=True)
    created_by = Column(String(50), nullable=False)
    notes = Column(Text)
    posted_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    sale = relationship("Sale", back_populates="returns")
    items = relationship("SaleReturnItem", back_populates="sale_return", order_by="SaleReturnItem.id")
    cash_transaction = relationship("CashTransaction", foreign_keys=[cash_transaction_id])

    __table_args__ = (
        CheckConstraint("refund_amount >= 0", name="ck_sale_return_refund_nonnegative"),
        CheckConstraint("return_type IN ('full', 'partial', 'legacy')", name="ck_sale_return_type"),
    )


class SaleReturnItem(Base):
    """Immutable quantity and allocated-value snapshot for a posted return line."""
    __tablename__ = "sale_return_items"
    id = Column(Integer, primary_key=True, index=True)
    sale_return_id = Column(Integer, ForeignKey("sale_returns.id"), nullable=False, index=True)
    sale_item_id = Column(Integer, ForeignKey("sale_items.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    product_name = Column(String(150), nullable=False)
    qty = Column(Numeric(18, 4, asdecimal=True), nullable=False)
    allocated_amount = Column(Numeric(18, 2, asdecimal=True), nullable=False)
    allocated_tax_amount = Column(Numeric(18, 2, asdecimal=True), nullable=False, default=0)
    cost_amount = Column(Numeric(18, 2, asdecimal=True), nullable=False, default=0)

    sale_return = relationship("SaleReturn", back_populates="items")
    sale_item = relationship("SaleItem", back_populates="return_lines")
    product = relationship("Product")

    __table_args__ = (
        UniqueConstraint("sale_return_id", "sale_item_id", name="uq_sale_return_item_line"),
        CheckConstraint("qty > 0", name="ck_sale_return_item_qty_positive"),
        CheckConstraint("allocated_amount >= 0", name="ck_sale_return_item_amount_nonnegative"),
        CheckConstraint("allocated_tax_amount >= 0", name="ck_sale_return_item_tax_nonnegative"),
        CheckConstraint("cost_amount >= 0", name="ck_sale_return_item_cost_nonnegative"),
    )


# ── Purchases ─────────────────────────────────────────────────────────────────
class Purchase(Base):
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True, index=True)
    bill_no = Column(String(30), unique=True, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    subtotal = Column(Money(), default=0.0)
    discount = Column(Money(), default=0.0)
    tax_amount = Column(Money(), default=0.0)
    total = Column(Money(), default=0.0)
    paid_amount = Column(Money(), default=0.0)
    payment_source = Column(String(30), default="cash_in_hand")  # cash_in_hand / bank / credit
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    supplier = relationship("Supplier", back_populates="purchases")
    items = relationship("PurchaseItem", back_populates="purchase", cascade="all, delete-orphan")


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id = Column(Integer, primary_key=True, index=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_name = Column(String(150))
    qty = Column(Float, nullable=False)
    price = Column(Money(), nullable=False)
    total = Column(Money(), nullable=False)
    purchase = relationship("Purchase", back_populates="items")
    product = relationship("Product", back_populates="purchase_items")


# ── Payments ──────────────────────────────────────────────────────────────────
class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True, index=True)
    party_type = Column(Enum("customer", "supplier"), nullable=False)
    party_id = Column(Integer, nullable=False)
    amount = Column(Money(), nullable=False)
    payment_type = Column(Enum("received", "paid"), nullable=False)
    # Legacy and current clients use additional values such as "online",
    # "online payment", "check", and "bank transfer". A restrictive
    # SQLAlchemy Enum makes the entire payment list fail while deserializing
    # one of those valid historical rows, so keep this compatibility field as
    # a bounded string and normalize new writes in the router.
    method = Column(String(40), default="cash")
    note = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    customer = relationship(
        "Customer",
        primaryjoin="and_(Payment.party_type=='customer', Payment.party_id==Customer.id)",
        foreign_keys=[party_id],
        viewonly=True
    )
    supplier = relationship(
        "Supplier",
        primaryjoin="and_(Payment.party_type=='supplier', Payment.party_id==Supplier.id)",
        foreign_keys=[party_id],
        viewonly=True
    )


# ── Expenses ──────────────────────────────────────────────────────────────────
class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(100), nullable=False)
    amount = Column(Money(), nullable=False)
    note = Column(Text)
    payment_source = Column(String(30), default="cash_in_hand")  # cash_in_hand / bank / other
    date = Column(DateTime, server_default=func.now(), index=True)


# ── Stock Movements ───────────────────────────────────────────────────────────
class StockMovement(Base):
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    movement_type = Column(Enum("sale", "purchase", "adjustment", "return"), nullable=False)
    qty_change = Column(Float, nullable=False)    # positive = in, negative = out
    qty_after = Column(Float, nullable=False)
    note = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), index=True)
    product = relationship("Product", back_populates="stock_movements")


# ── Settings ──────────────────────────────────────────────────────────────────
class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)


# ── Licensing & Activation ───────────────────────────────────────────────────
class License(Base):
    __tablename__ = "license"
    id = Column(Integer, primary_key=True, index=True)
    hardware_id = Column(String(100), unique=True, index=True)
    activation_key = Column(String(100), nullable=True)
    is_activated = Column(Boolean, default=False)
    installation_date = Column(DateTime, server_default=func.now())
    expiry_date = Column(DateTime, nullable=True)
    last_check = Column(DateTime, server_default=func.now())


# ── Cash Transactions ─────────────────────────────────────────────────────────
class CashTransaction(Base):
    __tablename__ = "cash_transactions"
    id = Column(Integer, primary_key=True, index=True)
    tx_type = Column(String(10), nullable=False) # cash_in / cash_out
    cash_in_type = Column(String(50))
    cash_out_type = Column(String(50))
    amount = Column(Money(), nullable=False)
    account = Column(String(30), default="cash_in_hand")
    received_from = Column(String(150))
    paid_to = Column(String(150))
    category = Column(String(100))
    reference_type = Column(String(30))
    reference_id = Column(Integer)
    reference_no = Column(String(50))
    notes = Column(Text)
    created_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now(), index=True)


# ── Day Closing ────────────────────────────────────────────────────────────────
class DayClosing(Base):
    __tablename__ = "day_closings"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String(10), unique=True, nullable=False, index=True)
    opening_cash = Column(Money(), default=0.0)
    total_cash_in = Column(Money(), default=0.0)
    total_cash_out = Column(Money(), default=0.0)
    expected_closing_cash = Column(Money(), default=0.0)
    actual_counted_cash = Column(Money())
    difference = Column(Money())
    status = Column(String(10), default="open") # open / closed
    notes = Column(Text)
    closed_by = Column(String(50))
    closed_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())


# ── External Funds ─────────────────────────────────────────────────────────────
class ExternalFund(Base):
    __tablename__ = "external_funds"
    id = Column(Integer, primary_key=True, index=True)
    fund_type = Column(String(30), nullable=False) # owner_injection, borrowed, friend_loan
    direction = Column(String(5), nullable=False) # in / out
    party_name = Column(String(150), nullable=False)
    amount = Column(Money(), nullable=False)
    cash_tx_id = Column(Integer, ForeignKey("cash_transactions.id"))
    notes = Column(Text)
    created_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now(), index=True)


# ── Audit Log ──────────────────────────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50))
    entity_id = Column(Integer)
    old_value = Column(Text)
    new_value = Column(Text)
    user = Column(String(50))
    ip_note = Column(String(100))
    created_at = Column(DateTime, server_default=func.now(), index=True)


# ── Employees, Payroll & Employee Ledger ─────────────────────────────────────
# Employee money uses Decimal-backed NUMERIC columns.  The older core tables use
# Money() with float API compatibility, but payroll calculations must never rely
# on binary floating point.
def EmployeeMoney():
    return Numeric(18, 2, asdecimal=True)


class Employee(Base):
    """Employee master record.

    ``current_balance`` follows one explicit rule throughout the module:
    positive = the company owes the employee, negative = the employee owes the
    company.  Every mutation also writes an immutable ledger entry.
    """
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True, index=True)
    employee_code = Column(String(40), unique=True, nullable=False, index=True)
    full_name = Column(String(150), nullable=False, index=True)
    phone = Column(String(30))
    cnic = Column(String(30), unique=True, nullable=True, index=True)
    address = Column(Text)
    designation = Column(String(100))
    joining_date = Column(Date, nullable=True)
    monthly_salary = Column(EmployeeMoney(), nullable=False, default=0)
    opening_balance = Column(EmployeeMoney(), nullable=False, default=0)
    credit_limit = Column(EmployeeMoney(), nullable=False, default=0)
    current_balance = Column(EmployeeMoney(), nullable=False, default=0, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    ledger_entries = relationship("EmployeeLedgerEntry", back_populates="employee")
    payrolls = relationship("EmployeePayroll", back_populates="employee")
    goods_credits = relationship("EmployeeGoodsCredit", back_populates="employee")
    salary_payments = relationship("EmployeeSalaryPayment", back_populates="employee")
    sales = relationship("Sale", back_populates="employee", foreign_keys="Sale.employee_id")

    __table_args__ = (
        CheckConstraint("monthly_salary >= 0", name="ck_employee_salary_nonnegative"),
        CheckConstraint("credit_limit >= 0", name="ck_employee_credit_limit_nonnegative"),
    )


class EmployeeLedgerEntry(Base):
    """Append-only employee ledger.

    ``amount`` is always positive and ``signed_amount`` is the actual balance
    movement.  Posted rows are never deleted or edited; mistakes are corrected
    by a linked REVERSAL row.
    """
    __tablename__ = "employee_ledger_entries"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    transaction_type = Column(String(40), nullable=False, index=True)
    amount = Column(EmployeeMoney(), nullable=False)
    signed_amount = Column(EmployeeMoney(), nullable=False)
    balance_after = Column(EmployeeMoney(), nullable=False)
    reference_no = Column(String(80), nullable=False, index=True)
    description = Column(Text)
    salary_period = Column(String(7), nullable=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=True, index=True)
    payroll_id = Column(Integer, ForeignKey("employee_payrolls.id"), nullable=True, index=True)
    cash_transaction_id = Column(Integer, ForeignKey("cash_transactions.id"), nullable=True)
    source_type = Column(String(40), nullable=True, index=True)
    source_id = Column(Integer, nullable=True)
    idempotency_key = Column(String(160), nullable=True, unique=True, index=True)
    reversal_of_id = Column(Integer, ForeignKey("employee_ledger_entries.id"), nullable=True, unique=True)
    is_reversed = Column(Boolean, nullable=False, default=False, index=True)
    reversal_reason = Column(Text)
    metadata_json = Column(Text)
    created_by = Column(String(50), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    employee = relationship("Employee", back_populates="ledger_entries")
    reversal_of = relationship("EmployeeLedgerEntry", remote_side=[id], foreign_keys=[reversal_of_id])

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_employee_ledger_amount_positive"),
        CheckConstraint("signed_amount != 0", name="ck_employee_ledger_signed_nonzero"),
    )


class EmployeePayroll(Base):
    """One immutable salary accrual per employee and calendar month."""
    __tablename__ = "employee_payrolls"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    salary_period = Column(String(7), nullable=False, index=True)
    gross_salary = Column(EmployeeMoney(), nullable=False, default=0)
    bonus = Column(EmployeeMoney(), nullable=False, default=0)
    overtime = Column(EmployeeMoney(), nullable=False, default=0)
    deductions = Column(EmployeeMoney(), nullable=False, default=0)
    advances = Column(EmployeeMoney(), nullable=False, default=0)
    goods_credit = Column(EmployeeMoney(), nullable=False, default=0)
    employee_repayments = Column(EmployeeMoney(), nullable=False, default=0)
    goods_returns = Column(EmployeeMoney(), nullable=False, default=0)
    carried_debt_offset = Column(EmployeeMoney(), nullable=False, default=0)
    amount_paid = Column(EmployeeMoney(), nullable=False, default=0)
    net_payable = Column(EmployeeMoney(), nullable=False, default=0)
    status = Column(String(20), nullable=False, default="unpaid", index=True)
    accrual_entry_id = Column(Integer, ForeignKey("employee_ledger_entries.id"), nullable=True, unique=True)
    generated_by = Column(String(50), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    employee = relationship("Employee", back_populates="payrolls", foreign_keys=[employee_id])
    payments = relationship("EmployeeSalaryPayment", back_populates="payroll")

    payroll_month = synonym("salary_period")
    net_salary = synonym("net_payable")

    @property
    def remaining_balance(self):
        return float(max(Decimal("0.00"), Decimal(str(self.net_payable or 0)) - Decimal(str(self.amount_paid or 0))))

    __table_args__ = (
        UniqueConstraint("employee_id", "salary_period", name="uq_employee_payroll_period"),
        CheckConstraint("gross_salary >= 0", name="ck_payroll_gross_nonnegative"),
        CheckConstraint("amount_paid >= 0", name="ck_payroll_paid_nonnegative"),
    )


class EmployeeSalaryPayment(Base):
    """Immutable payment/accounting record for a payroll disbursement."""
    __tablename__ = "employee_salary_payments"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    payroll_id = Column(Integer, ForeignKey("employee_payrolls.id"), nullable=False, index=True)
    ledger_entry_id = Column(Integer, ForeignKey("employee_ledger_entries.id"), nullable=False, unique=True)
    cash_transaction_id = Column(Integer, ForeignKey("cash_transactions.id"), nullable=False, unique=True)
    amount = Column(EmployeeMoney(), nullable=False)
    method = Column(String(20), nullable=False)
    account = Column(String(30), nullable=False)
    note = Column(Text)
    idempotency_key = Column(String(160), nullable=True, unique=True, index=True)
    is_reversed = Column(Boolean, nullable=False, default=False, index=True)
    reversed_by_entry_id = Column(Integer, ForeignKey("employee_ledger_entries.id"), nullable=True, unique=True)
    reversal_reason = Column(Text)
    created_by = Column(String(50), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    employee = relationship("Employee", back_populates="salary_payments")
    payroll = relationship("EmployeePayroll", back_populates="payments")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_employee_salary_payment_positive"),
    )


class EmployeeGoodsCredit(Base):
    """Audit snapshot linking a normal stock-reducing sale to an employee."""
    __tablename__ = "employee_goods_credits"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False, unique=True, index=True)
    invoice_no = Column(String(80), nullable=False, index=True)
    total = Column(EmployeeMoney(), nullable=False)
    returned_amount = Column(EmployeeMoney(), nullable=False, default=0)
    price_mode = Column(String(20), nullable=False, default="retail")
    status = Column(String(20), nullable=False, default="active", index=True)
    ledger_entry_id = Column(Integer, ForeignKey("employee_ledger_entries.id"), nullable=False, unique=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    employee = relationship("Employee", back_populates="goods_credits")
    items = relationship("EmployeeGoodsCreditItem", back_populates="goods_credit", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("total > 0", name="ck_employee_goods_total_positive"),
        CheckConstraint("returned_amount >= 0", name="ck_employee_goods_return_nonnegative"),
    )


class EmployeeGoodsCreditItem(Base):
    __tablename__ = "employee_goods_credit_items"
    id = Column(Integer, primary_key=True, index=True)
    goods_credit_id = Column(Integer, ForeignKey("employee_goods_credits.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    product_name = Column(String(150), nullable=False)
    qty = Column(Float, nullable=False)
    price = Column(EmployeeMoney(), nullable=False)
    cost = Column(EmployeeMoney(), nullable=False, default=0)
    total = Column(EmployeeMoney(), nullable=False)
    returned_qty = Column(Float, nullable=False, default=0)

    goods_credit = relationship("EmployeeGoodsCredit", back_populates="items")


# ── FastFood Module ───────────────────────────────────────────────────────────
class FFCategory(Base):
    """FastFood menu categories (e.g., Burgers, Drinks, Pizza)"""
    __tablename__ = "ff_categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(10), nullable=True)         # emoji icon e.g. 🍔
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    items = relationship("FFItem", back_populates="category", cascade="all, delete-orphan")


class FFItem(Base):
    """FastFood individual menu items"""
    __tablename__ = "ff_items"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("ff_categories.id"), nullable=True, index=True)
    price = Column(Money(), default=0.0)
    description = Column(Text, nullable=True)
    image_path = Column(String(255), nullable=True)
    is_available = Column(Boolean, default=True)     # can be toggled (e.g. item out of stock)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    category = relationship("FFCategory", back_populates="items")
    order_items = relationship("FFOrderItem", back_populates="item")


class FFOrder(Base):
    """FastFood order (separate from regular Sale)"""
    __tablename__ = "ff_orders"
    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(30), unique=True, nullable=False, index=True)  # e.g. KOT-0001
    order_type = Column(String(20), default="parcel")  # parcel | dine_in | takeaway | delivery
    table_no = Column(String(20), nullable=True)     # for dine_in
    customer_name = Column(String(100), nullable=True)
    customer_phone = Column(String(20), nullable=True)
    subtotal = Column(Money(), default=0.0)
    discount = Column(Money(), default=0.0)
    tax_amount = Column(Money(), default=0.0)
    total = Column(Money(), default=0.0)
    paid_amount = Column(Money(), default=0.0)
    payment_method = Column(String(20), default="cash")  # cash | card | credit
    status = Column(String(20), default="pending")  # pending | ready | completed | cancelled
    cashier = Column(String(50))
    notes = Column(Text)
    
    # Delivery and KOT tracking
    rider_id = Column(Integer, ForeignKey("ff_riders.id"), nullable=True)
    delivery_address = Column(Text, nullable=True)
    rider_status = Column(String(20), nullable=True) # pending | dispatched | delivered
    kot_printed = Column(Boolean, default=False)
    
    created_at = Column(DateTime, server_default=func.now(), index=True)
    items = relationship("FFOrderItem", back_populates="order", cascade="all, delete-orphan")
    rider = relationship("FFRider", back_populates="orders")


class FFOrderItem(Base):
    """Individual items within a FastFood order"""
    __tablename__ = "ff_order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("ff_orders.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("ff_items.id"), nullable=True)
    item_name = Column(String(150))     # snapshot at order time
    qty = Column(Float, nullable=False, default=1.0)
    price = Column(Money(), nullable=False)
    total = Column(Money(), nullable=False)
    notes = Column(Text, nullable=True) # special instructions e.g. "no onion"
    order = relationship("FFOrder", back_populates="items")
    item = relationship("FFItem", back_populates="order_items")
    modifiers = relationship("FFOrderItemModifier", back_populates="order_item", cascade="all, delete-orphan")


class FFOrderItemModifier(Base):
    """Modifiers selected for a specific order item"""
    __tablename__ = "ff_order_item_modifiers"
    id = Column(Integer, primary_key=True, index=True)
    order_item_id = Column(Integer, ForeignKey("ff_order_items.id"), nullable=False)
    modifier_id = Column(Integer, ForeignKey("ff_modifiers.id"), nullable=False)
    name = Column(String(100), nullable=False)  # snapshot
    price = Column(Money(), nullable=False)       # snapshot
    created_at = Column(DateTime, server_default=func.now())
    
    order_item = relationship("FFOrderItem", back_populates="modifiers")
    modifier = relationship("FFModifier")


class FFTable(Base):
    """FastFood restaurant tables for Dine-In mode"""
    __tablename__ = "ff_tables"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    status = Column(String(20), default="available")  # available | occupied | reserved
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class FFRider(Base):
    """FastFood delivery riders"""
    __tablename__ = "ff_riders"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(20))
    status = Column(String(20), default="available")  # available | busy
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    orders = relationship("FFOrder", back_populates="rider")


class FFModifier(Base):
    """Item modifications / add-ons (e.g. Extra Cheese, No Mayo)"""
    __tablename__ = "ff_modifiers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    price = Column(Money(), default=0.0)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class FFRecipe(Base):
    """FastFood recipe costing: links FF menu items to raw inventory products"""
    __tablename__ = "ff_recipes"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("ff_items.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty = Column(Float, default=1.0)
    created_at = Column(DateTime, server_default=func.now())
    
    item = relationship("FFItem")
    product = relationship("Product")
