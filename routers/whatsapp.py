"""
SmartVyapar - WhatsApp Router
Endpoints for WhatsApp message history and dashboard statistics
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import date
from database import get_db
from auth import get_current_user

router = APIRouter()

@router.post("/history")
def log_whatsapp_message(payload: dict, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """
    Records a sent or failed WhatsApp message in history log.
    """
    recipient_phone = payload.get("recipient_phone")
    doc_type = payload.get("doc_type", "message")
    doc_ref = payload.get("doc_ref", "")
    status = payload.get("status", "Delivered")
    message_text = payload.get("message_text", "")
    error_message = payload.get("error_message", "")

    if not recipient_phone:
        raise HTTPException(status_code=400, detail="recipient_phone is required")

    try:
        db.execute(text("""
            INSERT INTO whatsapp_history (recipient_phone, doc_type, doc_ref, status, message_text, error_message)
            VALUES (:recipient_phone, :doc_type, :doc_ref, :status, :message_text, :error_message)
        """), {
            "recipient_phone": str(recipient_phone),
            "doc_type": str(doc_type),
            "doc_ref": str(doc_ref),
            "status": str(status),
            "message_text": str(message_text),
            "error_message": str(error_message)
        })
        db.commit()
        return {"status": "success", "message": "WhatsApp history recorded"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to record history: {str(e)}")

@router.get("/history")
def get_whatsapp_history(limit: int = 50, q: Optional[str] = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """
    Returns WhatsApp message logs.
    """
    try:
        if q:
            query = text("""
                SELECT id, recipient_phone, doc_type, doc_ref, status, message_text, error_message, created_at
                FROM whatsapp_history
                WHERE recipient_phone LIKE :q OR doc_type LIKE :q OR doc_ref LIKE :q OR status LIKE :q
                ORDER BY id DESC LIMIT :limit
            """)
            rows = db.execute(query, {"q": f"%{q}%", "limit": limit}).fetchall()
        else:
            query = text("""
                SELECT id, recipient_phone, doc_type, doc_ref, status, message_text, error_message, created_at
                FROM whatsapp_history
                ORDER BY id DESC LIMIT :limit
            """)
            rows = db.execute(query, {"limit": limit}).fetchall()

        result = []
        for r in rows:
            result.append({
                "id": r[0],
                "recipient_phone": r[1],
                "doc_type": r[2],
                "doc_ref": r[3],
                "status": r[4],
                "message_text": r[5],
                "error_message": r[6],
                "created_at": str(r[7])
            })
        return result
    except Exception as e:
        return []

@router.get("/dashboard-stats")
def get_whatsapp_dashboard_stats(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """
    Returns WhatsApp statistics for the dashboard widget.
    """
    today_str = date.today().isoformat()
    try:
        sent_today = db.execute(text("""
            SELECT COUNT(*) FROM whatsapp_history
            WHERE status = 'Delivered' AND date(created_at) = :today
        """), {"today": today_str}).scalar() or 0

        failed_today = db.execute(text("""
            SELECT COUNT(*) FROM whatsapp_history
            WHERE status = 'Failed' AND date(created_at) = :today
        """), {"today": today_str}).scalar() or 0

        total_sent = db.execute(text("""
            SELECT COUNT(*) FROM whatsapp_history WHERE status = 'Delivered'
        """)).scalar() or 0

        return {
            "sent_today": sent_today,
            "failed_today": failed_today,
            "total_sent": total_sent
        }
    except Exception as e:
        return {
            "sent_today": 0,
            "failed_today": 0,
            "total_sent": 0
        }
