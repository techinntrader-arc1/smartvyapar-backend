"""FastFood Module Router — Categories, Items, Orders, Reports, Modifiers, Recipes, Tables, Riders, KDS, TDS, Shift Close"""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from datetime import datetime, date, timedelta
from database import get_db
from auth import get_current_user
import models

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class FFCategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = "🍽️"
    sort_order: int = 0


class FFItemCreate(BaseModel):
    name: str
    category_id: Optional[int] = None
    price: float = 0.0
    description: Optional[str] = None
    image_path: Optional[str] = None
    is_available: bool = True
    sort_order: int = 0


class FFModifierCreate(BaseModel):
    name: str
    price: float = 0.0
    sort_order: int = 0


class FFTableCreate(BaseModel):
    name: str
    sort_order: int = 0


class FFRiderCreate(BaseModel):
    name: str
    phone: Optional[str] = None


class FFRecipeItem(BaseModel):
    product_id: int
    qty: float


class FFRecipeSave(BaseModel):
    item_id: int
    ingredients: List[FFRecipeItem]


class FFOrderItemIn(BaseModel):
    item_id: Optional[int] = None
    item_name: str
    qty: float = 1.0
    price: float
    total: float
    notes: Optional[str] = None
    modifiers: Optional[List[int]] = []  # List of modifier IDs applied


class FFOrderCreate(BaseModel):
    order_type: str = "parcel"          # parcel | dine_in | takeaway | delivery
    table_no: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    rider_id: Optional[int] = None
    subtotal: float = 0.0
    discount: float = 0.0
    tax_amount: float = 0.0
    total: float = 0.0
    paid_amount: float = 0.0
    payment_method: str = "cash"        # cash | card | credit
    notes: Optional[str] = None
    items: List[FFOrderItemIn] = []


class DayClosingInput(BaseModel):
    date: str
    opening_cash: float
    actual_counted_cash: float
    notes: Optional[str] = None


# ── Helper: next KOT number ───────────────────────────────────────────────────

def get_next_order_no(db: Session) -> str:
    last = db.query(models.FFOrder).order_by(models.FFOrder.id.desc()).first()
    if last and last.order_no:
        try:
            num = int(last.order_no.replace("KOT-", ""))
            return f"KOT-{str(num + 1).zfill(4)}"
        except Exception:
            pass
    return "KOT-0001"


# ── FF Categories ─────────────────────────────────────────────────────────────

@router.get("/fastfood/categories")
def list_ff_categories(db: Session = Depends(get_db), _=Depends(get_current_user)):
    cats = db.query(models.FFCategory).filter_by(is_active=True).order_by(
        models.FFCategory.sort_order, models.FFCategory.name
    ).all()
    return [{
        "id": c.id, "name": c.name, "description": c.description,
        "icon": c.icon, "sort_order": c.sort_order,
        "item_count": db.query(models.FFItem).filter_by(category_id=c.id, is_available=True).count()
    } for c in cats]


@router.post("/fastfood/categories")
def create_ff_category(data: FFCategoryCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    existing = db.query(models.FFCategory).filter_by(name=data.name, is_active=True).first()
    if existing:
        raise HTTPException(400, f"Category '{data.name}' already exists")
    c = models.FFCategory(**data.dict())
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "name": c.name, "icon": c.icon, "description": c.description, "sort_order": c.sort_order}


@router.put("/fastfood/categories/{cid}")
def update_ff_category(cid: int, data: FFCategoryCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.query(models.FFCategory).filter_by(id=cid).first()
    if not c:
        raise HTTPException(404, "Category not found")
    for k, v in data.dict().items():
        setattr(c, k, v)
    db.commit()
    return {"success": True}


@router.delete("/fastfood/categories/{cid}")
def delete_ff_category(cid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.query(models.FFCategory).filter_by(id=cid).first()
    if not c:
        raise HTTPException(404, "Category not found")
    c.is_active = False  # soft delete
    db.commit()
    return {"success": True}


# ── FF Items ──────────────────────────────────────────────────────────────────

@router.get("/fastfood/items")
def list_ff_items(
    category_id: Optional[int] = None,
    available_only: bool = False,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    q = db.query(models.FFItem)
    if category_id:
        q = q.filter_by(category_id=category_id)
    if available_only:
        q = q.filter_by(is_available=True)
    items = q.order_by(models.FFItem.sort_order, models.FFItem.name).all()
    return [{
        "id": i.id, "name": i.name, "price": i.price,
        "description": i.description, "image_path": i.image_path,
        "is_available": i.is_available, "sort_order": i.sort_order,
        "category_id": i.category_id,
        "category": i.category.name if i.category else "",
        "category_icon": i.category.icon if i.category else "🍽️",
    } for i in items]


@router.post("/fastfood/items")
def create_ff_item(data: FFItemCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = models.FFItem(**data.dict())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "id": item.id, "name": item.name, "price": item.price,
        "category_id": item.category_id,
        "category": item.category.name if item.category else "",
        "is_available": item.is_available,
    }


@router.put("/fastfood/items/{iid}")
def update_ff_item(iid: int, data: FFItemCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(models.FFItem).filter_by(id=iid).first()
    if not item:
        raise HTTPException(404, "Item not found")
    for k, v in data.dict().items():
        setattr(item, k, v)
    db.commit()
    return {"success": True}


@router.delete("/fastfood/items/{iid}")
def delete_ff_item(iid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(models.FFItem).filter_by(id=iid).first()
    if not item:
        raise HTTPException(404, "Item not found")
    item.is_available = False  # soft delete (keep history)
    db.commit()
    return {"success": True}


@router.patch("/fastfood/items/{iid}/toggle")
def toggle_ff_item(iid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(models.FFItem).filter_by(id=iid).first()
    if not item:
        raise HTTPException(404, "Item not found")
    item.is_available = not item.is_available
    db.commit()
    return {"id": item.id, "is_available": item.is_available}


# ── FF Modifiers ──────────────────────────────────────────────────────────────

@router.get("/fastfood/modifiers")
def list_ff_modifiers(db: Session = Depends(get_db), _=Depends(get_current_user)):
    mods = db.query(models.FFModifier).filter_by(is_active=True).order_by(models.FFModifier.sort_order).all()
    return [{"id": m.id, "name": m.name, "price": m.price, "sort_order": m.sort_order} for m in mods]


@router.post("/fastfood/modifiers")
def create_ff_modifier(data: FFModifierCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    m = models.FFModifier(**data.dict())
    db.add(m)
    db.commit()
    db.refresh(m)
    return {"id": m.id, "name": m.name, "price": m.price}


@router.put("/fastfood/modifiers/{mid}")
def update_ff_modifier(mid: int, data: FFModifierCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    m = db.query(models.FFModifier).filter_by(id=mid).first()
    if not m: raise HTTPException(404, "Modifier not found")
    for k, v in data.dict().items():
        setattr(m, k, v)
    db.commit()
    return {"success": True}


@router.delete("/fastfood/modifiers/{mid}")
def delete_ff_modifier(mid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    m = db.query(models.FFModifier).filter_by(id=mid).first()
    if not m: raise HTTPException(404, "Modifier not found")
    m.is_active = False
    db.commit()
    return {"success": True}


# ── FF Table Management ───────────────────────────────────────────────────────

@router.get("/fastfood/tables")
def list_ff_tables(db: Session = Depends(get_db), _=Depends(get_current_user)):
    tables = db.query(models.FFTable).filter_by(is_active=True).order_by(models.FFTable.sort_order).all()
    return [{"id": t.id, "name": t.name, "status": t.status} for t in tables]


@router.post("/fastfood/tables")
def create_ff_table(data: FFTableCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    t = models.FFTable(**data.dict())
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "name": t.name}


@router.patch("/fastfood/tables/{tid}/status")
def update_ff_table_status(tid: int, status: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    t = db.query(models.FFTable).filter_by(id=tid).first()
    if not t: raise HTTPException(404, "Table not found")
    t.status = status
    db.commit()
    return {"success": True}


@router.delete("/fastfood/tables/{tid}")
def delete_ff_table(tid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    t = db.query(models.FFTable).filter_by(id=tid).first()
    if not t: raise HTTPException(404, "Table not found")
    t.is_active = False
    db.commit()
    return {"success": True}


# ── FF Rider Management ───────────────────────────────────────────────────────

@router.get("/fastfood/riders")
def list_ff_riders(db: Session = Depends(get_db), _=Depends(get_current_user)):
    riders = db.query(models.FFRider).filter_by(is_active=True).all()
    return [{"id": r.id, "name": r.name, "phone": r.phone, "status": r.status} for r in riders]


@router.post("/fastfood/riders")
def create_ff_rider(data: FFRiderCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    r = models.FFRider(**data.dict())
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "name": r.name}


@router.delete("/fastfood/riders/{rid}")
def delete_ff_rider(rid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    r = db.query(models.FFRider).filter_by(id=rid).first()
    if not r: raise HTTPException(404, "Rider not found")
    r.is_active = False
    db.commit()
    return {"success": True}


# ── FF Recipes & Costing ──────────────────────────────────────────────────────

@router.get("/fastfood/recipes/{item_id}")
def get_item_recipe(item_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    recipe = db.query(models.FFRecipe).filter_by(item_id=item_id).all()
    return [{
        "id": r.id,
        "product_id": r.product_id,
        "product_name": r.product.name if r.product else "Unknown",
        "qty": r.qty,
        "cost": r.product.buy_price if r.product else 0.0
    } for r in recipe]


@router.post("/fastfood/recipes")
def save_item_recipe(data: FFRecipeSave, db: Session = Depends(get_db), _=Depends(get_current_user)):
    # Clear existing
    db.query(models.FFRecipe).filter_by(item_id=data.item_id).delete()
    # Add new
    for ing in data.ingredients:
        r = models.FFRecipe(item_id=data.item_id, product_id=ing.product_id, qty=ing.qty)
        db.add(r)
    db.commit()
    return {"success": True}


@router.get("/fastfood/recipes-costing")
def get_all_recipes_costing(db: Session = Depends(get_db), _=Depends(get_current_user)):
    items = db.query(models.FFItem).filter_by(is_available=True).all()
    result = []
    for item in items:
        recipe = db.query(models.FFRecipe).filter_by(item_id=item.id).all()
        total_cost = 0.0
        for r in recipe:
            cost = r.product.buy_price if r.product else 0.0
            total_cost += cost * r.qty
        
        profit = item.price - total_cost
        margin_pct = (profit / item.price * 100) if item.price > 0 else 0
        
        result.append({
            "item_id": item.id,
            "name": item.name,
            "price": item.price,
            "total_cost": round(total_cost, 2),
            "profit": round(profit, 2),
            "margin_pct": round(margin_pct, 1),
            "ingredients_count": len(recipe)
        })
    return result


# ── FF Orders ─────────────────────────────────────────────────────────────────

@router.get("/fastfood/orders")
def list_ff_orders(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    q = db.query(models.FFOrder)
    if date_from:
        q = q.filter(models.FFOrder.created_at >= date_from)
    if date_to:
        q = q.filter(models.FFOrder.created_at <= date_to + " 23:59:59")
    if status:
        q = q.filter_by(status=status)
    total = q.count()
    orders = q.order_by(models.FFOrder.created_at.desc()).offset(offset).limit(limit).all()
    result = []
    for o in orders:
        result.append({
            "id": o.id, "order_no": o.order_no, "order_type": o.order_type,
            "table_no": o.table_no, "customer_name": o.customer_name,
            "subtotal": o.subtotal, "discount": o.discount,
            "tax_amount": o.tax_amount, "total": o.total,
            "paid_amount": o.paid_amount, "payment_method": o.payment_method,
            "status": o.status, "cashier": o.cashier,
            "notes": o.notes, "rider_name": o.rider.name if o.rider else None,
            "rider_status": o.rider_status,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "item_count": len(o.items),
        })
    return {"total": total, "orders": result}


@router.get("/fastfood/orders/{oid}")
def get_ff_order(oid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    o = db.query(models.FFOrder).filter_by(id=oid).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return {
        "id": o.id, "order_no": o.order_no, "order_type": o.order_type,
        "table_no": o.table_no, "customer_name": o.customer_name,
        "customer_phone": o.customer_phone, "delivery_address": o.delivery_address,
        "subtotal": o.subtotal, "discount": o.discount,
        "tax_amount": o.tax_amount, "total": o.total,
        "paid_amount": o.paid_amount, "payment_method": o.payment_method,
        "status": o.status, "cashier": o.cashier, "notes": o.notes,
        "rider_id": o.rider_id, "rider_name": o.rider.name if o.rider else None,
        "rider_status": o.rider_status, "kot_printed": o.kot_printed,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "items": [{
            "id": i.id, "item_id": i.item_id, "item_name": i.item_name,
            "qty": i.qty, "price": i.price, "total": i.total, "notes": i.notes,
            "modifiers": [{"name": m.name, "price": m.price} for m in i.modifiers]
        } for i in o.items]
    }


@router.post("/fastfood/orders")
def create_ff_order(data: FFOrderCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    order_no = get_next_order_no(db)
    
    # 1. Create order parent
    order = models.FFOrder(
        order_no=order_no,
        order_type=data.order_type,
        table_no=data.table_no,
        customer_name=data.customer_name,
        customer_phone=data.customer_phone,
        delivery_address=data.delivery_address,
        rider_id=data.rider_id,
        subtotal=data.subtotal,
        discount=data.discount,
        tax_amount=data.tax_amount,
        total=data.total,
        paid_amount=data.paid_amount,
        payment_method=data.payment_method,
        notes=data.notes,
        cashier=user.username if hasattr(user, 'username') else "cashier",
        status="pending", # Start in KDS pending status
        rider_status="pending" if data.order_type == "delivery" else None
    )
    
    # Update table status if occupied
    if data.order_type == "dine_in" and data.table_no:
        table = db.query(models.FFTable).filter_by(name=data.table_no).first()
        if table:
            table.status = "occupied"
            
    db.add(order)
    db.commit()
    db.refresh(order)

    # 2. Add order items & process modifiers
    for item_data in data.items:
        oi = models.FFOrderItem(
            order_id=order.id,
            item_id=item_data.item_id,
            item_name=item_data.item_name,
            qty=item_data.qty,
            price=item_data.price,
            total=item_data.total,
            notes=item_data.notes,
        )
        db.add(oi)
        db.commit()
        db.refresh(oi)
        
        # Save modifiers
        for mod_id in item_data.modifiers:
            mod = db.query(models.FFModifier).filter_by(id=mod_id).first()
            if mod:
                oim = models.FFOrderItemModifier(
                    order_item_id=oi.id,
                    modifier_id=mod.id,
                    name=mod.name,
                    price=mod.price
                )
                db.add(oim)
        
        # 3. AUTOMATIC RECIPE-BASED INVENTORY DEDUCTIONS
        if item_data.item_id:
            recipe = db.query(models.FFRecipe).filter_by(item_id=item_data.item_id).all()
            for r in recipe:
                prod = db.query(models.Product).filter_by(id=r.product_id).first()
                if prod:
                    deduct_qty = r.qty * item_data.qty
                    prod.stock -= deduct_qty  # Deduct raw material stock
                    
                    # Log stock movement
                    movement = models.StockMovement(
                        product_id=prod.id,
                        movement_type="sale",
                        qty_change=-deduct_qty,
                        qty_after=prod.stock,
                        note=f"FastFood Order #{order_no} recipe deduction"
                    )
                    db.add(movement)

    db.commit()
    return {
        "id": order.id,
        "order_no": order.order_no,
        "total": order.total,
        "order_type": order.order_type,
        "created_at": order.created_at.isoformat() if order.created_at else None
    }


@router.put("/fastfood/orders/{oid}/status")
def update_ff_order_status(oid: int, status: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    o = db.query(models.FFOrder).filter_by(id=oid).first()
    if not o:
        raise HTTPException(404, "Order not found")
    
    prev_status = o.status
    o.status = status
    
    # If order completed, cancelled or refunded, release table
    if status in ["completed", "cancelled", "refunded"] and o.order_type == "dine_in" and o.table_no:
        table = db.query(models.FFTable).filter_by(name=o.table_no).first()
        if table:
            table.status = "available"
            
    # If cancelled/refunded, restore ingredient stock if previously active
    if status in ["cancelled", "refunded"] and prev_status not in ["cancelled", "refunded"]:
        for item in o.items:
            recipe = db.query(models.FFRecipe).filter_by(item_id=item.item_id).all()
            for r in recipe:
                prod = db.query(models.Product).filter_by(id=r.product_id).first()
                if prod:
                    add_qty = r.qty * item.qty
                    prod.stock += add_qty
                    movement = models.StockMovement(
                        product_id=prod.id,
                        movement_type="refund",
                        qty_change=add_qty,
                        qty_after=prod.stock,
                        note=f"FastFood Order #{o.order_no} cancellation stock restore"
                    )
                    db.add(movement)

    db.commit()
    return {"success": True}


@router.patch("/fastfood/orders/{oid}/assign-rider")
def assign_ff_order_rider(oid: int, rider_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    o = db.query(models.FFOrder).filter_by(id=oid).first()
    if not o: raise HTTPException(404, "Order not found")
    rider = db.query(models.FFRider).filter_by(id=rider_id).first()
    if not rider: raise HTTPException(404, "Rider not found")
    
    o.rider_id = rider_id
    o.rider_status = "dispatched"
    rider.status = "busy"
    db.commit()
    return {"success": True}


@router.patch("/fastfood/orders/{oid}/mark-delivered")
def mark_ff_order_delivered(oid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    o = db.query(models.FFOrder).filter_by(id=oid).first()
    if not o: raise HTTPException(404, "Order not found")
    
    o.rider_status = "delivered"
    o.status = "completed"
    if o.rider_id:
        rider = db.query(models.FFRider).filter_by(id=o.rider_id).first()
        if rider:
            rider.status = "available"
    db.commit()
    return {"success": True}


# ── Kitchen Display System (KDS) & Token Display System (TDS) ─────────────────

@router.get("/fastfood/kds/orders")
def get_kds_orders(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fetch active orders waiting to be prepared in kitchen"""
    from datetime import date, datetime, time
    today_start = datetime.combine(date.today(), time.min)
    orders = db.query(models.FFOrder).filter(
        models.FFOrder.status.in_(["pending", "ready"]),
        models.FFOrder.created_at >= today_start
    ).order_by(models.FFOrder.created_at.asc()).all()
    
    result = []
    for o in orders:
        result.append({
            "id": o.id,
            "order_no": o.order_no,
            "order_type": o.order_type,
            "table_no": o.table_no,
            "customer_name": o.customer_name,
            "status": o.status,
            "notes": o.notes,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "elapsed_seconds": int((datetime.now() - o.created_at).total_seconds()) if o.created_at else 0,
            "items": [{
                "name": i.item_name,
                "qty": i.qty,
                "notes": i.notes,
                "modifiers": [m.name for m in i.modifiers]
            } for i in o.items]
        })
    return result


@router.get("/fastfood/tds/queue")
def get_tds_queue(db: Session = Depends(get_db)):
    """Get preparing vs ready order token lists for large display screen"""
    from datetime import date, datetime, time
    today_start = datetime.combine(date.today(), time.min)
    preparing = db.query(models.FFOrder.order_no).filter(
        models.FFOrder.status == "pending",
        models.FFOrder.created_at >= today_start
    ).order_by(models.FFOrder.id.desc()).limit(15).all()
    ready = db.query(models.FFOrder.order_no).filter(
        models.FFOrder.status == "ready",
        models.FFOrder.created_at >= today_start
    ).order_by(models.FFOrder.id.desc()).limit(10).all()
    
    return {
        "preparing": [r[0] for r in preparing],
        "ready": [r[0] for r in ready]
    }


# ── Shift closing (Day end calculations) ──────────────────────────────────────

@router.get("/fastfood/closing-metrics")
def get_closing_metrics(date_str: str = Query(..., description="YYYY-MM-DD"), db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Calculate shift expected totals for a specific day closing check"""
    orders = db.query(models.FFOrder).filter(
        models.FFOrder.created_at >= date_str + " 00:00:00",
        models.FFOrder.created_at <= date_str + " 23:59:59",
        models.FFOrder.status.in_(["pending", "ready", "completed"])
    ).all()
    
    cash_sales = sum(o.total for o in orders if o.payment_method == "cash")
    card_sales = sum(o.total for o in orders if o.payment_method == "card")
    credit_sales = sum(o.total for o in orders if o.payment_method == "credit")
    
    total_sales = cash_sales + card_sales + credit_sales
    total_orders = len(orders)
    total_discount = sum(o.discount for o in orders)
    
    # Expected drawer cash starts from opening cash (read from day_closings)
    dc = db.query(models.DayClosing).filter_by(date=date_str).first()
    opening_cash = dc.opening_cash if dc else 0.0
    
    return {
        "date": date_str,
        "opening_cash": opening_cash,
        "cash_sales": round(cash_sales, 2),
        "card_sales": round(card_sales, 2),
        "credit_sales": round(credit_sales, 2),
        "total_sales": round(total_sales, 2),
        "total_orders": total_orders,
        "total_discount": round(total_discount, 2)
    }


@router.post("/fastfood/day-closing")
def perform_day_closing(data: DayClosingInput, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Lock the cash drawer shift totals and write DayClosing log"""
    # Fetch expected values
    metrics = get_closing_metrics(data.date, db)
    
    # Calculate difference
    expected_closing = data.opening_cash + metrics["cash_sales"]
    diff = data.actual_counted_cash - expected_closing
    
    dc = db.query(models.DayClosing).filter_by(date=data.date).first()
    if not dc:
        dc = models.DayClosing(date=data.date)
        db.add(dc)
        
    dc.opening_cash = data.opening_cash
    dc.total_cash_in = metrics["cash_sales"]
    dc.total_cash_out = 0.0 # Standard till withdraw
    dc.expected_closing_cash = expected_closing
    dc.actual_counted_cash = data.actual_counted_cash
    dc.difference = diff
    dc.status = "closed"
    dc.notes = data.notes
    dc.closed_by = user.username if hasattr(user, 'username') else "cashier"
    dc.closed_at = datetime.now()
    
    db.commit()
    return {
        "success": True,
        "expected_closing": expected_closing,
        "difference": diff
    }


# ── FF Reports ─────────────────────────────────────────────────────────────────

@router.get("/fastfood/reports/summary")
def ff_reports_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    """Daily/date-range FastFood sales summary"""
    q = db.query(models.FFOrder).filter(models.FFOrder.status.in_(["pending", "ready", "completed", "cancelled"]))
    if date_from:
        q = q.filter(models.FFOrder.created_at >= date_from)
    if date_to:
        q = q.filter(models.FFOrder.created_at <= date_to + " 23:59:59")

    orders = q.all()
    active_orders = [o for o in orders if o.status != "cancelled"]
    total_orders = len(active_orders)
    total_revenue = sum(o.total for o in active_orders)
    total_discount = sum(o.discount for o in active_orders)
    total_items_sold = sum(sum(i.qty for i in o.items) for o in active_orders)

    # Order type breakdown
    by_type = {}
    for o in active_orders:
        by_type[o.order_type] = by_type.get(o.order_type, 0) + o.total

    # Payment method breakdown
    by_payment = {}
    for o in active_orders:
        by_payment[o.payment_method] = by_payment.get(o.payment_method, 0) + o.total

    # Status breakdown
    by_status = {}
    for o in orders:
        by_status[o.status] = by_status.get(o.status, 0) + 1

    # Daily breakdown (last 30 days if no date range)
    daily = {}
    for o in active_orders:
        if o.created_at:
            day = o.created_at.strftime("%Y-%m-%d")
            if day not in daily:
                daily[day] = {"orders": 0, "revenue": 0, "items": 0}
            daily[day]["orders"] += 1
            daily[day]["revenue"] += o.total
            daily[day]["items"] += sum(i.qty for i in o.items)

    return {
        "total_orders": total_orders,
        "total_revenue": round(total_revenue, 2),
        "total_discount": round(total_discount, 2),
        "total_items_sold": total_items_sold,
        "avg_order_value": round(total_revenue / total_orders, 2) if total_orders > 0 else 0,
        "by_order_type": by_type,
        "by_payment_method": by_payment,
        "by_status": by_status,
        "daily_breakdown": [{"date": k, **v} for k, v in sorted(daily.items())],
    }


@router.get("/fastfood/reports/best-sellers")
def ff_best_sellers(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 10,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    """Top selling FastFood items by quantity"""
    q = db.query(
        models.FFOrderItem.item_name,
        func.sum(models.FFOrderItem.qty).label("total_qty"),
        func.sum(models.FFOrderItem.total).label("total_revenue"),
        func.count(models.FFOrderItem.id).label("order_count"),
    ).join(models.FFOrder, models.FFOrderItem.order_id == models.FFOrder.id).filter(
        models.FFOrder.status.in_(["pending", "ready", "completed"])
    )

    if date_from:
        q = q.filter(models.FFOrder.created_at >= date_from)
    if date_to:
        q = q.filter(models.FFOrder.created_at <= date_to + " 23:59:59")

    results = q.group_by(models.FFOrderItem.item_name).order_by(
        func.sum(models.FFOrderItem.qty).desc()
    ).limit(limit).all()

    return [{
        "item_name": r.item_name,
        "total_qty": r.total_qty,
        "total_revenue": round(r.total_revenue, 2),
        "order_count": r.order_count,
    } for r in results]


@router.get("/fastfood/reports/category-breakdown")
def ff_category_breakdown(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    """Revenue breakdown by FastFood category"""
    q = db.query(
        models.FFItem.category_id,
        func.sum(models.FFOrderItem.total).label("revenue"),
        func.sum(models.FFOrderItem.qty).label("qty"),
    ).join(models.FFOrderItem, models.FFItem.id == models.FFOrderItem.item_id).join(
        models.FFOrder, models.FFOrderItem.order_id == models.FFOrder.id
    ).filter(models.FFOrder.status.in_(["pending", "ready", "completed"]))

    if date_from:
        q = q.filter(models.FFOrder.created_at >= date_from)
    if date_to:
        q = q.filter(models.FFOrder.created_at <= date_to + " 23:59:59")

    results = q.group_by(models.FFItem.category_id).all()
    output = []
    for r in results:
        cat = db.query(models.FFCategory).filter_by(id=r.category_id).first()
        output.append({
            "category": cat.name if cat else "Unknown",
            "icon": cat.icon if cat else "🍽️",
            "revenue": round(r.revenue or 0, 2),
            "qty": r.qty or 0,
        })
    return sorted(output, key=lambda x: x["revenue"], reverse=True)


@router.post("/fastfood/items/{iid}/upload-image")
async def upload_ff_item_image(
    iid: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(get_current_user)
):
    import os
    import shutil
    item = db.query(models.FFItem).filter_by(id=iid).first()
    if not item:
        raise HTTPException(404, "Item not found")

    from database import get_static_dir
    static_dir = get_static_dir()
    ff_items_dir = os.path.join(static_dir, "ff_items")
    os.makedirs(ff_items_dir, exist_ok=True)

    # Save with unique name to prevent collisions
    filename = f"item_{iid}_{int(datetime.now().timestamp())}.png"
    file_path = os.path.join(ff_items_dir, filename)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Save relative URL
        item.image_path = f"/static/ff_items/{filename}"
        db.commit()
        return {"success": True, "image_path": item.image_path}
    except Exception as e:
        raise HTTPException(500, f"Image upload failed: {e}")
