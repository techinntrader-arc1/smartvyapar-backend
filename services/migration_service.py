"""
Migration Service - Handles legacy FoxPro DBF data migration to SmartVyapar SQLite database.
Supports stock calculation (Purchases - Sales), catalog import, customer import, dry-run preview,
progress tracking, report export, resume capability, and async execution.
"""

import os
import glob
import struct
import json
import logging
import asyncio
import io
import csv
from typing import Optional, Dict, Any, List, Tuple, Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session
import models

logger = logging.getLogger("smartvyapar.migration")
logger.setLevel(logging.INFO)


# ── DBF Parsing Helper ────────────────────────────────────────────────────────

def read_dbf(filename: str) -> List[Dict[str, str]]:
    """
    Robust pure-python dBase III/IV DBF reader. Reads header structure and non-deleted records.

    Args:
        filename (str): Absolute or relative file path to .DBF file.

    Returns:
        List[Dict[str, str]]: List of record dictionaries.
    """
    if not os.path.exists(filename) or not os.path.isfile(filename):
        logger.warning(f"DBF file not found: {filename}")
        return []
    
    try:
        with open(filename, 'rb') as f:
            f.seek(4)
            num_records_bytes = f.read(4)
            if not num_records_bytes or len(num_records_bytes) < 4:
                return []
            num_records = struct.unpack('<I', num_records_bytes)[0]

            f.seek(8)
            header_bytes = f.read(2)
            if not header_bytes or len(header_bytes) < 2:
                return []
            header_len = struct.unpack('<H', header_bytes)[0]
            
            f.seek(10)
            rec_len_bytes = f.read(2)
            if not rec_len_bytes or len(rec_len_bytes) < 2:
                return []
            record_len = struct.unpack('<H', rec_len_bytes)[0]
            
            f.seek(32)
            fields: List[Tuple[str, str, int]] = []
            while True:
                desc = f.read(32)
                if not desc or desc[0] == 0x0D:
                    break
                name = desc[:11].decode('ascii', errors='ignore').strip('\x00')
                ftype = chr(desc[11])
                flen = desc[16]
                fields.append((name, ftype, flen))
            
            f.seek(header_len)
            records: List[Dict[str, str]] = []
            for _ in range(num_records):
                status = f.read(1)
                if not status:
                    break
                
                row: Dict[str, str] = {}
                for name, ftype, flen in fields:
                    raw = f.read(flen)
                    try:
                        val = raw.decode('cp1252', errors='ignore').strip()
                    except Exception:
                        val = ""
                    row[name] = val
                
                if status != b'*':
                    records.append(row)
                    
            return records
    except Exception as e:
        logger.error(f"Error reading DBF file '{filename}': {e}", exc_info=True)
        return []


def sf(v: Any) -> float:
    """Safe float conversion helper."""
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


# ── Validation & Utility Helpers ──────────────────────────────────────────────

def _validate_migration_input(folder_path: str) -> Tuple[bool, str, Optional[str]]:
    """Validate target migration folder and locate ITEM.DBF."""
    if not folder_path or not os.path.exists(folder_path):
        return False, f"Target folder not found: '{folder_path}'", None

    base_search = [
        os.path.join(folder_path, "SHOP", "ITEM.DBF"),
        os.path.join(folder_path, "DATA", "ITEM.DBF"),
        os.path.join(folder_path, "ITEM.DBF")
    ]
    
    items_path = None
    for p in base_search:
        if os.path.exists(p):
            items_path = p
            break

    if not items_path:
        return False, f"ITEM.DBF not found in '{folder_path}'. Please select folder containing SHOP, DATA, or ITEM.DBF", None

    return True, "Valid folder", items_path


# ── Main Migration Function ───────────────────────────────────────────────────

def migrate_foxpro_data(
    db: Session,
    folder_path: str,
    wipe: bool = False,
    dry_run: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Dict[str, Any]:
    """
    Orchestrates legacy FoxPro DBF migration into SmartVyapar SQLite database.
    Calculates stock balances from stock ledgers (Purchases - Sales), imports product catalog,
    and imports customer accounts.

    Args:
        db (Session): SQLAlchemy database session.
        folder_path (str): Root folder containing FoxPro backup files.
        wipe (bool): If True, clears existing products and customers before migration.
        dry_run (bool): If True, parses and simulates migration without committing DB changes.
        progress_callback (Callable): Optional status progress callback (current, total, message).

    Returns:
        Dict[str, Any]: Migration execution status and detailed log entries.
    """
    logs: List[str] = []
    
    try:
        is_valid, msg, items_path = _validate_migration_input(folder_path)
        if not is_valid or not items_path:
            logger.error(f"Migration input validation failed: {msg}")
            raise ValueError(msg)

        logger.info(f"Starting FoxPro migration from '{items_path}' (wipe={wipe}, dry_run={dry_run})")
        logs.append(f"Located catalog at: {items_path}")

        data_dir = os.path.dirname(items_path)
        parent_dir = os.path.dirname(data_dir)
        stock_dir = os.path.join(parent_dir, "STOCK")
        actual_data_dir = os.path.join(parent_dir, "DATA")

        # 1. Calculate Stock Balances (Purchases - Sales)
        logs.append("Calculating stock balances from ledger files (Purchases - Sales)...")
        purchased: Dict[str, float] = {}
        sold: Dict[str, float] = {}

        p_files = [os.path.join(stock_dir, "SLGR.DBF"), os.path.join(stock_dir, "ZSLGR.DBF")]
        for pf in p_files:
            if os.path.exists(pf):
                recs = read_dbf(pf)
                for r in recs:
                    if r.get('VTYPE', '').strip().upper() == 'IN':
                        code = r.get('ITEM', '').strip()
                        qty = sf(r.get('QTY'))
                        if code and qty > 0:
                            purchased[code] = purchased.get(code, 0.0) + qty
                logs.append(f"Processed purchases from {os.path.basename(pf)}")

        s_files = ["TEST.DBF", "TEST2.DBF", "OSALE3.DBF", "SWSALE.DBF"]
        for sfname in s_files:
            spf = os.path.join(stock_dir, sfname)
            if os.path.exists(spf):
                recs = read_dbf(spf)
                for r in recs:
                    code = r.get('ITEM', '').strip()
                    qty = sf(r.get('QTY'))
                    if code and qty > 0:
                        sold[code] = sold.get(code, 0.0) + qty
                logs.append(f"Processed sales from {sfname}")

        if os.path.exists(actual_data_dir):
            dfiles = glob.glob(os.path.join(actual_data_dir, "*.DBF"))
            for df in dfiles:
                if "ITEM" in os.path.basename(df).upper() or "MACT" in os.path.basename(df).upper():
                    continue
                recs = read_dbf(df)
                for r in recs:
                    code = r.get('ITEM', '').strip()
                    qty = sf(r.get('QTY'))
                    if code and qty > 0:
                        sold[code] = sold.get(code, 0.0) + qty
            logs.append(f"Scanned {len(dfiles)} monthly data files for sales")

        stock_map: Dict[str, float] = {}
        for code in (set(purchased.keys()) | set(sold.keys())):
            bal = round(purchased.get(code, 0.0) - sold.get(code, 0.0), 2)
            if bal > 0:
                stock_map[code] = bal

        # 2. Handle Optional Wipe
        if wipe and not dry_run:
            logs.append("Wiping existing products and customers as requested...")
            db.query(models.Product).delete()
            db.query(models.Customer).delete()
            db.commit()

        # 3. Read Catalog (ITEM.DBF) and Import
        fox_items = read_dbf(items_path)
        total_items = len(fox_items)
        imported_count = 0
        skipped_count = 0

        logs.append(f"Found {total_items} items in catalog DBF")

        if dry_run:
            logs.append(f"DRY RUN COMPLETE: Would process {total_items} catalog products.")
            return {
                "status": "success",
                "dry_run": True,
                "total_items": total_items,
                "calculated_stocks_count": len(stock_map),
                "logs": logs
            }

        conn = db.connection().connection
        cursor = conn.cursor()

        for idx, f_item in enumerate(fox_items, 1):
            raw_name = f_item.get('DESCR', '').strip()
            raw_code = f_item.get('ITEM', '').strip()
            if not raw_name and not raw_code:
                skipped_count += 1
                continue

            name = raw_name or raw_code
            code = raw_code if raw_code else None
            barcode = raw_code if raw_code and len(raw_code) >= 8 else None
            buy_price = sf(f_item.get('PRATE'))
            sell_price = sf(f_item.get('SRATE'))
            stock = stock_map.get(raw_code, 0.0)

            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO products 
                    (name, code, barcode, buy_price, sell_price, stock, is_active, is_service, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 0, CURRENT_TIMESTAMP)
                """, (name, code, barcode, buy_price, sell_price, stock))
                imported_count += 1
            except Exception as e:
                logger.warning(f"Skipped product record '{name}': {e}")
                logs.append(f"Skipped {name}: {str(e)}")

            if progress_callback and idx % 20 == 0:
                progress_callback(idx, total_items, f"Importing product {idx}/{total_items}")

        conn.commit()
        logs.append(f"Catalog migration finished: {imported_count} imported, {skipped_count} skipped.")

        # 4. Import Customer Accounts (MACT.DBF)
        mact_path = os.path.join(data_dir, "MACT.DBF")
        imported_customers = 0
        if os.path.exists(mact_path):
            logs.append("Importing customer accounts from MACT.DBF...")
            fox_mact = read_dbf(mact_path)
            for f_party in fox_mact:
                name = f_party.get('DESCR', '').strip()
                code = f_party.get('CODE', '').strip()
                if not name:
                    continue
                
                existing = db.query(models.Customer).filter_by(phone=code).first()
                if not existing:
                    db.add(models.Customer(
                        name=name,
                        phone=code,
                        credit_balance=sf(f_party.get('OBAL')),
                        is_active=True
                    ))
                    imported_customers += 1
            db.commit()
            logs.append(f"Parties imported successfully: {imported_customers} new customers added.")

        logger.info("FoxPro migration completed successfully.")
        return {
            "status": "success",
            "imported_products": imported_count,
            "imported_customers": imported_customers,
            "logs": logs
        }
    except Exception as e:
        db.rollback()
        logger.error(f"FoxPro migration failed: {e}", exc_info=True)
        logs.append(f"ERROR: Migration failed - {str(e)}")
        return {
            "status": "failed",
            "error": str(e),
            "logs": logs
        }


# ── New Feature Endpoints / Utilities ─────────────────────────────────────────

async def migrate_foxpro_data_async(
    db: Session,
    folder_path: str,
    wipe: bool = False,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Asynchronous wrapper for migration of large FoxPro datasets."""
    try:
        return await asyncio.to_thread(migrate_foxpro_data, db, folder_path, wipe, dry_run)
    except Exception as e:
        logger.error(f"Async migration failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e), "logs": [str(e)]}


def health_check(folder_path: Optional[str] = None) -> Dict[str, Any]:
    """Health check validating migration requirements and dataset presence."""
    try:
        if not folder_path:
            return {
                "status": "ready",
                "message": "Migration service operational. Specify folder_path to test target dataset."
            }
        
        is_valid, msg, items_path = _validate_migration_input(folder_path)
        return {
            "status": "ready" if is_valid else "dataset_not_found",
            "valid": is_valid,
            "message": msg,
            "items_path": items_path
        }
    except Exception as e:
        logger.error(f"Migration health check error: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


def export_migration_report(migration_result: Dict[str, Any], format: str = "csv") -> str:
    """Export migration result logs to CSV or JSON string format."""
    logs = migration_result.get("logs", [])
    if format == "json":
        return json.dumps(migration_result, indent=2)
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Log Entry"])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for log in logs:
        writer.writerow([now_str, log])
    return output.getvalue()


def resume_migration(
    db: Session,
    folder_path: str
) -> Dict[str, Any]:
    """Resume catalog and customer migration without wiping existing data."""
    logger.info(f"Resuming migration for '{folder_path}'")
    return migrate_foxpro_data(db, folder_path, wipe=False, dry_run=False)
