"""Products router - Products, Categories, Units CRUD"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import or_
from database import get_db
from auth import get_current_user
import models

from services import backup_service
router = APIRouter()



# ── Schemas ───────────────────────────────────────────────────────────────────
class CategoryCreate(BaseModel):
    name: str
    description: Optional[str] = ""


class UnitCreate(BaseModel):
    name: str


class BrandCreate(BaseModel):
    name: str
    description: Optional[str] = ""


class ProductCreate(BaseModel):
    name: str
    code: Optional[str] = None
    barcode: Optional[str] = None
    category_id: Optional[int] = None
    unit_id: Optional[int] = None
    brand_id: Optional[int] = None
    unit_name: Optional[str] = None
    brand_name: Optional[str] = None
    category_name: Optional[str] = None
    buy_price: float = 0.0
    sell_price: float = 0.0
    employee_price: Optional[float] = None
    tax_pct: float = 0.0
    stock: float = 0.0
    min_stock: float = 5.0
    location: Optional[str] = None
    is_service: bool = False
    image_path: Optional[str] = None

class ProductBulkItem(BaseModel):
    name: str
    code: Optional[str] = None
    barcode: Optional[str] = None
    category_name: Optional[str] = "General"
    unit_name: Optional[str] = "pcs"
    brand_name: Optional[str] = None
    buy_price: float = 0.0
    sell_price: float = 0.0
    employee_price: Optional[float] = None
    stock: float = 0.0
    min_stock: float = 5.0
    location: Optional[str] = None
    is_service: bool = False


# ── Categories ────────────────────────────────────────────────────────────────
@router.get("/categories")
def list_categories(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Category).order_by(models.Category.name).all()


@router.post("/categories")
def create_category(data: CategoryCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = models.Category(**data.dict())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.delete("/categories/{cid}")
def delete_category(cid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    c = db.query(models.Category).filter_by(id=cid).first()
    if not c:
        raise HTTPException(404)
    db.delete(c)
    db.commit()
    return {"success": True}


# ── Units ─────────────────────────────────────────────────────────────────────
@router.get("/units")
def list_units(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Unit).all()


@router.post("/units")
def create_unit(data: UnitCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    u = models.Unit(**data.dict())
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── Brands ────────────────────────────────────────────────────────────────────
@router.get("/brands")
def list_brands(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(models.Brand).order_by(models.Brand.name).all()


@router.post("/brands")
def create_brand(data: BrandCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    b = models.Brand(**data.dict())
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@router.delete("/brands/{bid}")
def delete_brand(bid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    b = db.query(models.Brand).filter_by(id=bid).first()
    if not b:
        raise HTTPException(404)
    db.delete(b)
    db.commit()
    return {"success": True}


# ── Products ──────────────────────────────────────────────────────────────────
@router.get("/products")
def list_products(response: Response,
                  q: Optional[str] = None, category_id: Optional[int] = None,
                  brand_id: Optional[int] = None,
                  low_stock: bool = False, 
                  limit: int = 200, offset: int = 0,
                  db: Session = Depends(get_db),
                  _=Depends(get_current_user)):
    query = db.query(models.Product).filter(models.Product.is_active == True)
    if q:
        q = q.strip()
        # ── EXACT IDENTIFIER PRIORITY ──
        # Check both Barcode and Item Code for an exact match.
        # This fixes cases where items use short internal codes or scanned barcodes 
        # that might conflict with partial matches of longer barcodes.
        exact_match = db.query(models.Product).filter(
            or_(models.Product.barcode == q, models.Product.code == q),
            models.Product.is_active == True
        ).first()

        if exact_match:
            total = 1
            products = [exact_match]
        else:
            # Fallback to standard partial/fuzzy search
            query = query.filter(
                or_(models.Product.name.ilike(f"%{q}%"),
                    models.Product.barcode.ilike(f"%{q}%"),
                    models.Product.code.ilike(f"%{q}%"))
            )
            total = query.count()
            products = query.order_by(models.Product.name.asc()).offset(offset).limit(limit).all()
    else:
        total = query.count()
        products = query.order_by(models.Product.name.asc()).offset(offset).limit(limit).all()

    # Total count for pagination headers
    response.headers["X-Total-Count"] = str(total)
    response.headers["x-total-count"] = str(total)
    result = []
    for p in products:
        result.append({
            "id": p.id, "code": p.code, "name": p.name, "barcode": p.barcode,
            "category": p.category.name if p.category else "",
            "unit": p.unit.name if p.unit else "pcs",
            "brand": p.brand.name if p.brand else "",
            "category_id": p.category_id, "unit_id": p.unit_id, "brand_id": p.brand_id,
            "buy_price": p.buy_price, "sell_price": p.sell_price,
            "employee_price": p.employee_price,
            "tax_pct": p.tax_pct, "stock": p.stock,
            "min_stock": p.min_stock, "location": p.location,
            "is_service": p.is_service,
            "image_path": p.image_path,
        })
    return result


@router.get("/products/{pid}")
def get_product(pid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(models.Product).filter_by(id=pid).first()
    if not p:
        raise HTTPException(status_code=404, detail=f"Product with ID {pid} not found in the vault")
    return {
        "id": p.id, "code": p.code, "name": p.name, "barcode": p.barcode,
        "category_id": p.category_id, "unit_id": p.unit_id, "brand_id": p.brand_id,
        "buy_price": p.buy_price, "sell_price": p.sell_price,
        "employee_price": p.employee_price,
        "tax_pct": p.tax_pct, "stock": p.stock, "min_stock": p.min_stock,
        "location": p.location, "is_service": p.is_service,
        "category": p.category.name if p.category else "",
        "unit": p.unit.name if p.unit else "pcs",
        "brand": p.brand.name if p.brand else "",
    }


@router.post("/products")
def create_product(data: ProductCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    if data.employee_price is not None and data.employee_price < 0:
        raise HTTPException(400, "Employee price cannot be negative")
    if data.barcode:
        existing = db.query(models.Product).filter_by(barcode=data.barcode).first()
        if existing:
            raise HTTPException(400, "Barcode already exists")
    # Map Unit by name if provided
    if data.unit_name:
        u_name = data.unit_name.lower()
        unit = db.query(models.Unit).filter_by(name=u_name).first()
        if not unit:
            unit = models.Unit(name=u_name)
            db.add(unit)
            db.commit()
            db.refresh(unit)
        data.unit_id = unit.id

    # Map Category by name if provided
    if data.category_name:
        c_name = data.category_name.strip()
        cat = db.query(models.Category).filter_by(name=c_name).first()
        if not cat:
            cat = models.Category(name=c_name)
            db.add(cat)
            db.commit()
            db.refresh(cat)
        data.category_id = cat.id

    # Map Brand by name if provided
    if data.brand_name:
        b_name = data.brand_name.strip()
        brand = db.query(models.Brand).filter_by(name=b_name).first()
        if not brand:
            brand = models.Brand(name=b_name)
            db.add(brand)
            db.commit()
            db.refresh(brand)
        data.brand_id = brand.id

    p = models.Product(**data.dict(exclude={'unit_name', 'brand_name', 'category_name'}))
    db.add(p)
    db.commit()
    # Record opening stock movement
    if data.stock > 0:
        sm = models.StockMovement(
            product_id=p.id, movement_type="adjustment",
            qty_change=data.stock, qty_after=data.stock, note="Opening stock"
        )
        db.add(sm)
        db.commit()
    db.refresh(p)
    
    # Trigger background backup
    try:
        from database import get_db_path, get_backup_dir
        backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
    except: pass

    return {
        "id": p.id, "code": p.code, "name": p.name, "barcode": p.barcode,
        "category": p.category.name if p.category else "",
        "unit": p.unit.name if p.unit else "pcs",
        "brand": p.brand.name if p.brand else "",
        "category_id": p.category_id, "unit_id": p.unit_id, "brand_id": p.brand_id,
        "buy_price": p.buy_price, "sell_price": p.sell_price,
        "employee_price": p.employee_price,
        "tax_pct": p.tax_pct, "stock": p.stock,
        "min_stock": p.min_stock, "location": p.location,
        "is_service": p.is_service,
        "image_path": p.image_path,
    }



@router.put("/products/{pid}")
def update_product(pid: int, data: ProductCreate, db: Session = Depends(get_db),
                   _=Depends(get_current_user)):
    if data.employee_price is not None and data.employee_price < 0:
        raise HTTPException(400, "Employee price cannot be negative")
    p = db.query(models.Product).filter_by(id=pid).first()
    if not p:
        raise HTTPException(status_code=404, detail=f"Target Product ID {pid} not found for updating")
    old_stock = p.stock
    # Map Unit by name if provided
    if data.unit_name:
        u_name = data.unit_name.lower()
        unit = db.query(models.Unit).filter_by(name=u_name).first()
        if not unit:
            unit = models.Unit(name=u_name)
            db.add(unit)
            db.commit()
            db.refresh(unit)
        data.unit_id = unit.id

    # Map Category by name if provided
    if data.category_name:
        c_name = data.category_name.strip()
        cat = db.query(models.Category).filter_by(name=c_name).first()
        if not cat:
            cat = models.Category(name=c_name)
            db.add(cat)
            db.commit()
            db.refresh(cat)
        data.category_id = cat.id

    # Map Brand by name if provided
    if data.brand_name:
        b_name = data.brand_name.strip()
        brand = db.query(models.Brand).filter_by(name=b_name).first()
        if not brand:
            brand = models.Brand(name=b_name)
            db.add(brand)
            db.commit()
            db.refresh(brand)
        data.brand_id = brand.id

    for k, v in data.dict(exclude={'unit_name', 'brand_name', 'category_name'}).items():
        setattr(p, k, v)
    db.commit()
    # If stock changed, record adjustment
    if data.stock != old_stock:
        sm = models.StockMovement(
            product_id=p.id, movement_type="adjustment",
            qty_change=data.stock - old_stock, qty_after=data.stock, note="Manual adjustment"
        )
        db.add(sm)
        db.commit()

    # Trigger background backup
    try:
        from database import get_db_path, get_backup_dir
        backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
    except: pass

    return {"success": True}



@router.delete("/products/{pid}")
def delete_product(pid: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(models.Product).filter_by(id=pid).first()
    if not p:
        raise HTTPException(status_code=404, detail=f"Product with ID {pid} not found for deletion")
    p.is_active = False  # soft delete
    db.commit()
    return {"success": True}
@router.post("/products/bulk")
def bulk_create_products(items: List[ProductBulkItem], db: Session = Depends(get_db), _=Depends(get_current_user)):
    count = 0
    # Pre-fetch categories and units to avoid N+1 queries
    cats = {c.name.lower(): c.id for c in db.query(models.Category).all()}
    units = {u.name.lower(): u.id for u in db.query(models.Unit).all()}

    for item in items:
        # Check uniqueness
        if item.barcode:
            if db.query(models.Product).filter_by(barcode=item.barcode).first():
                continue
        if item.code:
            if db.query(models.Product).filter_by(code=item.code).first():
                continue

        # Map Category
        c_name = item.category_name or "General"
        if c_name.lower() not in cats:
            new_cat = models.Category(name=c_name)
            db.add(new_cat)
            db.commit()
            db.refresh(new_cat)
            cats[c_name.lower()] = new_cat.id
        
        # Map Unit
        u_name = item.unit_name or "pcs"
        if u_name.lower() not in units:
            new_unit = models.Unit(name=u_name)
            db.add(new_unit)
            db.commit()
            db.refresh(new_unit)
            units[u_name.lower()] = new_unit.id

        p = models.Product(
            name=item.name, code=item.code, barcode=item.barcode,
            category_id=cats[c_name.lower()], unit_id=units[u_name.lower()],
            buy_price=item.buy_price, sell_price=item.sell_price,
            employee_price=item.employee_price,
            stock=item.stock, min_stock=item.min_stock,
            location=item.location, is_service=item.is_service
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        # Record opening stock movement
        if item.stock > 0:
            sm = models.StockMovement(
                product_id=p.id, movement_type="adjustment",
                qty_change=item.stock, qty_after=item.stock, note="Bulk Import"
            )
            db.add(sm)
            db.commit()
        count += 1
    
    # Trigger background backup
    try:
        from database import get_db_path, get_backup_dir
        backup_service.perform_auto_backup(get_db_path(), get_backup_dir())
    except: pass

    return {"success": True, "count": count}
