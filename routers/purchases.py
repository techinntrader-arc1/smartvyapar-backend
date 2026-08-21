"""Purchases router - Supplier purchase bills + auto stock update"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from database import get_db
from auth import get_current_user, require_admin
import models
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
import threading

# Helper function to convert to Decimal safely
def to_dec(val) -> Decimal:
    if val is None:
        return Decimal('0.0')
    return Decimal(str(val))

router = APIRouter()
PURCHASE_CREATE_LOCK = threading.Lock()


def serialized_purchase_write(func):
    """Serialize purchase writes so bill allocation and commit stay atomic."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        with PURCHASE_CREATE_LOCK:
            return func(*args, **kwargs)
    return wrapper


class PurchaseItemIn(BaseModel):
    product_id: int
    qty: float = Field(..., gt=0)
    price: float = Field(..., ge=0)


class PurchaseCreate(BaseModel):
    supplier_id: Optional[int] = None
    items: List[PurchaseItemIn] = Field(..., min_items=1)
    discount: float = Field(0.0, ge=0)
    tax_amount: float = Field(0.0, ge=0)
    paid_amount: float = Field(0.0, ge=0)
    payment_source: str = "cash_in_hand"   # cash_in_hand | bank | credit
    notes: Optional[str] = ""
    replace_id: Optional[int] = None


def _next_bill_no(db: Session) -> str:
    setting = db.query(models.Setting).filter_by(key="purchase_prefix").first()
    prefix = setting.value if setting else "PUR"
    
    max_id = db.query(func.max(models.Purchase.id)).scalar() or 0
    next_num = max_id + 1
    
    while True:
        bill_no = f"{prefix}-{next_num:05d}"
        exists = db.query(models.Purchase).filter_by(bill_no=bill_no).first()
        if not exists:
            return bill_no
        next_num += 1


@router.get("/next-number")
def get_next_purchase_number(db: Session = Depends(get_db), _=Depends(require_admin)):
    return {"next_bill_no": _next_bill_no(db)}


@router.get("/")
def list_purchases(supplier_id: Optional[int] = None,
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   query: Optional[str] = None,
                   limit: int = 20,
                   offset: int = 0,
                   response: Response = None,
                   db: Session = Depends(get_db), _=Depends(require_admin)):
    q = db.query(models.Purchase).options(joinedload(models.Purchase.supplier))
    if supplier_id:
        q = q.filter(models.Purchase.supplier_id == supplier_id)
    if start_date:
        q = q.filter(func.date(models.Purchase.created_at) >= start_date)
    if end_date:
        q = q.filter(func.date(models.Purchase.created_at) <= end_date)
    
    if query:
        q = q.outerjoin(models.Supplier).filter(
            (models.Purchase.bill_no.ilike(f"%{query}%")) | 
            (models.Supplier.name.ilike(f"%{query}%"))
        )

    total_count = q.count()
    if response:
        response.headers["X-Total-Count"] = str(total_count)
        response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"

    purchases = q.order_by(models.Purchase.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id": p.id, "bill_no": p.bill_no,
            "supplier": p.supplier.name if p.supplier else "Unknown",
            "supplier_balance": p.supplier.due_balance if p.supplier else 0,
            "total": p.total, "paid_amount": p.paid_amount,
            "due": p.total - p.paid_amount, "date": p.created_at,
        }
        for p in purchases
    ]


@router.get("/{purchase_id}")
def get_purchase(purchase_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.query(models.Purchase).filter_by(id=purchase_id).first()
    if not p:
        raise HTTPException(404)
    return {
        "id": p.id, "bill_no": p.bill_no,
        "supplier_id": p.supplier_id,
        "supplier": p.supplier.name if p.supplier else "",
        "subtotal": p.subtotal, "discount": p.discount,
        "tax_amount": p.tax_amount, "total": p.total,
        "paid_amount": p.paid_amount, "payment_source": p.payment_source, "notes": p.notes, "date": p.created_at,
        "items": [
            {"product_id": i.product_id, "product_name": i.product_name,
             "qty": i.qty, "price": i.price, "total": i.total}
            for i in p.items
        ]
    }


@router.post("/")
@serialized_purchase_write
def create_purchase(data: PurchaseCreate, db: Session = Depends(get_db),
                    _=Depends(require_admin)):
    try:
        existing_cash_tx = None
        if data.replace_id:
            # Void the old one first
            old_p = db.query(models.Purchase).filter_by(id=data.replace_id).first()
            if old_p:
                for item in old_p.items:
                    product = db.query(models.Product).filter_by(id=item.product_id).first()
                    if product:
                        product.stock = models.Product.stock - item.qty
                        db.flush()
                        db.refresh(product)
                if old_p.supplier_id:
                    supplier = db.query(models.Supplier).filter_by(id=old_p.supplier_id).first()
                    if supplier:
                        due_dec = to_dec(old_p.total) - to_dec(old_p.paid_amount)
                        new_supplier_bal = to_dec(supplier.due_balance) - due_dec
                        supplier.due_balance = float(new_supplier_bal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
                
                # Retrieve old cash transaction to update it in-place instead of deleting immediately
                existing_cash_tx = db.query(models.CashTransaction).filter_by(
                    reference_type="purchase", reference_id=old_p.id
                ).first()
                
                db.delete(old_p)
                db.flush()

        subtotal_dec = sum(to_dec(i.qty) * to_dec(i.price) for i in data.items)
        total_dec = subtotal_dec + to_dec(data.tax_amount) - to_dec(data.discount)
        if total_dec < 0:
            raise HTTPException(400, "Discount cannot exceed purchase subtotal plus tax")
        if to_dec(data.paid_amount) > total_dec:
            raise HTTPException(400, "Paid amount cannot exceed purchase total")

        discount_factor = Decimal('0.0')
        if subtotal_dec > 0 and data.discount > 0:
            discount_factor = to_dec(data.discount) / subtotal_dec

        subtotal = float(subtotal_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        total = float(total_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

        from datetime import datetime
        purchase = models.Purchase(
            bill_no=_next_bill_no(db),
            supplier_id=data.supplier_id,
            subtotal=subtotal,
            discount=data.discount,
            tax_amount=data.tax_amount,
            total=total,
            paid_amount=data.paid_amount,
            payment_source=data.payment_source or "cash_in_hand",
            notes=data.notes,
            created_at=datetime.now(),
        )
        db.add(purchase)
        db.flush()

        for item in data.items:
            product = db.query(models.Product).filter_by(id=item.product_id).first()
            if not product:
                raise HTTPException(400, f"Product {item.product_id} not found")

            item_total = float((to_dec(item.qty) * to_dec(item.price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            pi = models.PurchaseItem(
                purchase_id=purchase.id,
                product_id=item.product_id,
                product_name=product.name,
                qty=item.qty,
                price=item.price,
                total=item_total,
            )
            db.add(pi)

            # Increase stock atomically
            product.stock = models.Product.stock + item.qty
            db.flush()
            db.refresh(product)
            
            # Update buy price to latest (applying proportional trade discount)
            discounted_price_dec = to_dec(item.price) - (to_dec(item.price) * discount_factor)
            product.buy_price = float(discounted_price_dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            
            sm = models.StockMovement(
                product_id=product.id,
                movement_type="purchase",
                qty_change=item.qty,
                qty_after=product.stock,
                note=f"Purchase {purchase.bill_no}"
            )
            db.add(sm)

        # Update supplier due balance
        if data.supplier_id:
            supplier = db.query(models.Supplier).filter_by(id=data.supplier_id).first()
            if supplier:
                due_diff_dec = to_dec(total) - to_dec(data.paid_amount)
                new_due_bal = to_dec(supplier.due_balance) + due_diff_dec
                supplier.due_balance = float(new_due_bal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

        # Auto-create or update cash book entry if paid from cash_in_hand
        if data.paid_amount > 0 and (data.payment_source or "cash_in_hand") == "cash_in_hand":
            supplier_name = ""
            if data.supplier_id:
                sup = db.query(models.Supplier).filter_by(id=data.supplier_id).first()
                if sup: supplier_name = sup.name
            
            if existing_cash_tx:
                # Update existing cash transaction in-place
                existing_cash_tx.amount = data.paid_amount
                existing_cash_tx.paid_to = supplier_name
                existing_cash_tx.reference_id = purchase.id
                existing_cash_tx.reference_no = purchase.bill_no
                existing_cash_tx.notes = data.notes or ""
                existing_cash_tx = None # marked as updated/handled
            else:
                cash_tx = models.CashTransaction(
                    tx_type="cash_out",
                    cash_out_type="purchase_payment",
                    amount=data.paid_amount,
                    account="cash_in_hand",
                    paid_to=supplier_name,
                    reference_type="purchase",
                    reference_id=purchase.id,
                    reference_no=purchase.bill_no,
                    notes=data.notes or "",
                    created_by="system",
                    created_at=datetime.now(),
                )
                db.add(cash_tx)

        # If we had an existing cash transaction but the new bill is not cash/not paid, delete it
        if existing_cash_tx:
            db.delete(existing_cash_tx)

        try:
            db.commit()
        except IntegrityError as integrity_error:
            db.rollback()
            raise HTTPException(
                409,
                "Purchase number conflict. No data was saved; please submit the purchase again."
            ) from integrity_error

        db.refresh(purchase)
        
        # Trigger background backup
        try:
            from database import get_db_path, get_backup_dir
            from services import backup_service
            backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
        except: pass

        return {"id": purchase.id, "bill_no": purchase.bill_no, "total": purchase.total}
    except Exception as e:
        db.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(500, f"Purchase creation failed: {str(e)}")


@router.delete("/{purchase_id}")
def delete_purchase(purchase_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    try:
        p = db.query(models.Purchase).filter_by(id=purchase_id).first()
        if not p:
            raise HTTPException(404, "Purchase not found")
        
        # 1. Reverse stock
        for item in p.items:
            product = db.query(models.Product).filter_by(id=item.product_id).first()
            if product:
                product.stock = models.Product.stock - item.qty
                db.flush()
                db.refresh(product)
                sm = models.StockMovement(
                    product_id=product.id,
                    movement_type="adjustment",
                    qty_change=-item.qty,
                    qty_after=product.stock,
                    note=f"Void/Return Purchase {p.bill_no}"
                )
                db.add(sm)
                db.flush()
        
        # 2. Reverse supplier balance
        if p.supplier_id:
            supplier = db.query(models.Supplier).filter_by(id=p.supplier_id).first()
            if supplier:
                due_dec = to_dec(p.total) - to_dec(p.paid_amount)
                new_due_bal = to_dec(supplier.due_balance) - due_dec
                supplier.due_balance = float(new_due_bal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        
        # 3. Reverse cash transaction
        cash_tx = db.query(models.CashTransaction).filter_by(reference_type="purchase", reference_id=purchase_id).first()
        if cash_tx:
            db.delete(cash_tx)
        
        # 4. Delete purchase (cascades to items)
        db.delete(p)
        db.commit()
        return {"message": "Purchase record voided and stock successfully reversed."}
    except Exception as e:
        db.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(500, f"Error voiding purchase: {str(e)}")
