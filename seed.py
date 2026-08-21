"""Seed script - Insert sample test data"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import SessionLocal, init_db
import models

def seed():
    init_db()
    db = SessionLocal()
    try:
        if db.query(models.Product).count() > 0:
            print("Already seeded. Skipping.")
            return

        # ── Customers ─────────────────────────────────────────────────────────
        customers = [
            ("Ahmed Ali", "0300-1234567", "Shop 5, Main Bazar", 0),
            ("Fatima Khan", "0321-9876543", "House 12, Block B", 2500),
            ("Usman Tariq", "0333-1112223", "Office 3, City Center", 0),
            ("Sara Malik", "0345-5556667", "Flat 8, Gulberg", 500),
            ("Bilal Hussain", "0311-7778889", "Plot 22, DHA", 0),
        ]
        for name, phone, addr, credit in customers:
            db.add(models.Customer(name=name, phone=phone, address=addr, credit_balance=credit))

        # ── Suppliers ─────────────────────────────────────────────────────────
        suppliers = [
            ("Pak Trading Co", "0300-4445556", "Wholesale Market, Lahore"),
            ("City Distributors", "0321-6667778", "Industrial Area, Karachi"),
            ("National Imports", "0333-8889990", "GT Road, Rawalpindi"),
        ]
        for name, phone, addr in suppliers:
            db.add(models.Supplier(name=name, phone=phone, address=addr))

        db.flush()

        # ── Categories & Units ────────────────────────────────────────────────
        categories = ["General", "Electronics", "Food & Beverage"]
        for c in categories:
            if not db.query(models.Category).filter_by(name=c).first():
                db.add(models.Category(name=c))
        
        units = ["pcs", "kg", "box", "liter"]
        for u in units:
            if not db.query(models.Unit).filter_by(name=u).first():
                db.add(models.Unit(name=u))
                
        db.flush()

        # Get category/unit IDs
        general = db.query(models.Category).filter_by(name="General").first()
        electronics = db.query(models.Category).filter_by(name="Electronics").first()
        food = db.query(models.Category).filter_by(name="Food & Beverage").first()
        pcs = db.query(models.Unit).filter_by(name="pcs").first()
        kg = db.query(models.Unit).filter_by(name="kg").first()
        box = db.query(models.Unit).filter_by(name="box").first()

        # ── Products ──────────────────────────────────────────────────────────
        products = [
            ("Surf Excel 1kg",   "867001", food and food.id or 1,   kg and kg.id or 1,   180, 220, 0,  50, 10),
            ("Pepsi 1.5L",       "867002", food and food.id or 1,   pcs and pcs.id or 1, 90,  110, 0,  100, 20),
            ("Lays Chips",       "867003", food and food.id or 1,   pcs and pcs.id or 1, 25,  35,  0,  200, 30),
            ("Ariel 2kg",        "867004", general and general.id or 1, kg and kg.id or 1, 350, 420, 0, 30, 10),
            ("USB Cable Type-C", "867005", electronics and electronics.id or 1, pcs and pcs.id or 1, 150, 250, 17, 40, 5),
            ("Earphones",        "867006", electronics and electronics.id or 1, pcs and pcs.id or 1, 200, 380, 17, 25, 5),
            ("Notepad A4",       "867007", general and general.id or 1, pcs and pcs.id or 1, 30, 55, 0, 150, 20),
            ("Pen Blue",         "867008", general and general.id or 1, box and box.id or 1, 80, 120, 0, 80, 10),
            ("Rice Basmati 5kg", "867009", food and food.id or 1,   pcs and pcs.id or 1, 900, 1100, 0, 20, 5),
            ("Bread",            "867010", food and food.id or 1,   pcs and pcs.id or 1, 55, 70, 0, 60, 10),
            ("Milk 1L",          "867011", food and food.id or 1,   pcs and pcs.id or 1, 150, 185, 0, 40, 10),
            ("Sugar 1kg",        "867012", food and food.id or 1,   kg and kg.id or 1, 140, 165, 0, 70, 15),
            ("Power Bank 10000", "867013", electronics and electronics.id or 1, pcs and pcs.id or 1, 1200, 1800, 17, 15, 3),
            ("Mouse Wireless",   "867014", electronics and electronics.id or 1, pcs and pcs.id or 1, 600, 950, 17, 20, 3),
            ("Colgate Toothpaste","867015",general and general.id or 1, pcs and pcs.id or 1, 110, 150, 0, 60, 10),
            ("Dettol Soap",      "867016", general and general.id or 1, pcs and pcs.id or 1, 75, 110, 0, 80, 15),
            ("Cooking Oil 5L",   "867017", food and food.id or 1,   pcs and pcs.id or 1, 2200, 2600, 0, 15, 5),
            ("Tea 200g",         "867018", food and food.id or 1,   pcs and pcs.id or 1, 280, 340, 0, 35, 10),
            ("Biscuits",         "867019", food and food.id or 1,   pcs and pcs.id or 1, 30, 50, 0, 100, 20),
            ("Shampoo 200ml",    "867020", general and general.id or 1, pcs and pcs.id or 1, 185, 260, 0, 45, 8),
        ]
        for name, barcode, cat_id, unit_id, buy, sell, tax, stock, min_stock in products:
            db.add(models.Product(
                name=name, barcode=barcode, category_id=cat_id, unit_id=unit_id,
                buy_price=buy, sell_price=sell, tax_pct=tax,
                stock=stock, min_stock=min_stock
            ))

        # ── Sample Expenses ───────────────────────────────────────────────────
        from datetime import datetime
        expenses = [
            ("Rent", 15000, "Monthly shop rent"),
            ("Electricity", 3500, "Monthly bill"),
            ("Salary", 18000, "Staff salary"),
            ("Transport", 1200, "Delivery charges"),
            ("Misc", 800, "Miscellaneous"),
        ]
        for cat, amount, note in expenses:
            db.add(models.Expense(category=cat, amount=amount, note=note, date=datetime.now()))

        db.commit()
        print("Seed data inserted successfully!")
        print("   - 5 Customers")
        print("   - 3 Suppliers")
        print("   - 20 Products")
        print("   - 5 Expense records")

    finally:
        db.close()


if __name__ == "__main__":
    seed()
