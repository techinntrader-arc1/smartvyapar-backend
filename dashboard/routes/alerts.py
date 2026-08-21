"""
Alerts and anomaly detection route handlers.
Provides system alerts, stale data indicators, real-time WebSocket feeds, and management actions.
"""

import logging
from datetime import datetime, date, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from dashboard.services import alert_service
import models

# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger("smartvyapar.alerts")
logger.setLevel(logging.INFO)

router = APIRouter()

# In-memory store for alert state (read/dismissed/acknowledged/resolved)
_alert_states: Dict[str, Dict[str, Any]] = {}


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class AlertItem(BaseModel):
    id: Optional[str] = None
    type: str = Field(..., description="Alert category or type name")
    severity: str = Field("info", description="Severity level: info, warning, critical")
    message: str = Field(..., description="Human-readable alert summary")
    detail: Optional[str] = Field(None, description="Detailed alert context")
    timestamp: Optional[str] = Field(None, description="ISO timestamp of alert occurrence")
    is_read: bool = Field(False, description="Read status of alert")
    status: str = Field("active", description="Alert state: active, acknowledged, resolved, dismissed")


class AlertFeedResponse(BaseModel):
    alerts: List[AlertItem]
    count: int
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0


class StaleIndicatorResponse(BaseModel):
    indicators: List[AlertItem]
    count: int


class AlertStatsResponse(BaseModel):
    total_alerts: int
    critical_count: int
    warning_count: int
    info_count: int
    active_count: int
    read_count: int
    dismissed_count: int


class BulkReadRequest(BaseModel):
    alert_ids: List[str] = Field(..., description="List of alert IDs to mark as read")


class AlertActionResponse(BaseModel):
    success: bool
    alert_id: str
    status: str
    message: str


class HealthCheckResponse(BaseModel):
    status: str
    module: str
    active_connections: int
    timestamp: str


# ── WebSocket Manager ─────────────────────────────────────────────────────────
class AlertConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"[Alerts WS] Client connected. Total active: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"[Alerts WS] Client disconnected. Total active: {len(self.active_connections)}")

    async def broadcast_alert(self, alert_data: Dict[str, Any]):
        for connection in self.active_connections:
            try:
                await connection.send_json(alert_data)
            except Exception as e:
                logger.warning(f"[Alerts WS] Error broadcasting to client: {e}")

ws_manager = AlertConnectionManager()


# ── Helper Functions ──────────────────────────────────────────────────────────
def _generate_alert_id(alert: Dict[str, Any]) -> str:
    """Generates a stable unique ID for an alert item."""
    msg_slug = alert.get("message", "alert")[:20].replace(" ", "_").lower()
    t_type = alert.get("type", "gen")
    return f"{t_type}_{msg_slug}"


def _enrich_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Enriches raw alert dict with stable ID, state tracking, and timestamp."""
    aid = _generate_alert_id(alert)
    state = _alert_states.get(aid, {})
    
    enriched = dict(alert)
    enriched["id"] = aid
    enriched["is_read"] = state.get("is_read", False)
    enriched["status"] = state.get("status", "active")
    if "timestamp" not in enriched or not enriched["timestamp"]:
        enriched["timestamp"] = datetime.now(timezone.utc).isoformat()
    return enriched


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthCheckResponse, summary="Alerts module health check")
def alerts_health():
    """Health check endpoint for the alerts module."""
    return HealthCheckResponse(
        status="ok",
        module="alerts",
        active_connections=len(ws_manager.active_connections),
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@router.get("/feed", response_model=AlertFeedResponse, summary="Get combined alert feed")
def alert_feed(
    severity: Optional[str] = Query(None, description="Filter by severity: critical, warning, info"),
    alert_type: Optional[str] = Query(None, description="Filter by alert type"),
    is_read: Optional[bool] = Query(None, description="Filter by read status"),
    limit: int = Query(100, ge=1, le=1000, description="Pagination limit"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Retrieves the system alert feed including anomalies, inventory stock alerts, and stale data indicators.
    Fully compatible with desktop UI while supporting pagination and filtering.
    """
    try:
        logger.info(f"[Alerts] Fetching alert feed for user '{current_user.username}'")
        raw_alerts = alert_service.get_alert_feed(db)
        raw_stale = alert_service.get_stale_data_indicators(db)

        all_raw = raw_alerts + raw_stale
        enriched = [_enrich_alert(a) for a in all_raw]

        # Apply filtering
        filtered = []
        for item in enriched:
            if item.get("status") == "dismissed":
                continue
            if severity and item.get("severity") != severity.lower():
                continue
            if alert_type and item.get("type") != alert_type:
                continue
            if is_read is not None and item.get("is_read") != is_read:
                continue
            filtered.append(item)

        # Breakdown counts
        critical_c = sum(1 for i in filtered if i.get("severity") == "critical")
        warning_c = sum(1 for i in filtered if i.get("severity") == "warning")
        info_c = sum(1 for i in filtered if i.get("severity") == "info")

        # Apply pagination
        paginated = filtered[offset : offset + limit]

        # Format items to match AlertItem schema
        items = [AlertItem(**item) for item in paginated]

        return AlertFeedResponse(
            alerts=items,
            count=len(filtered),
            critical_count=critical_c,
            warning_count=warning_c,
            info_count=info_c
        )
    except Exception as e:
        logger.error(f"[Alerts Error] Failed to generate alert feed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving alert feed: {str(e)}"
        )


@router.get("/stale-indicators", response_model=StaleIndicatorResponse, summary="Get stale data indicators")
def stale_indicators(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Retrieves stale data indicators such as un-backed up data or zero sales today."""
    try:
        logger.info(f"[Alerts] Fetching stale indicators for user '{current_user.username}'")
        raw_stale = alert_service.get_stale_data_indicators(db)
        enriched = [_enrich_alert(a) for a in raw_stale]
        items = [AlertItem(**a) for a in enriched if a.get("status") != "dismissed"]
        return StaleIndicatorResponse(indicators=items, count=len(items))
    except Exception as e:
        logger.error(f"[Alerts Error] Failed to get stale indicators: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching stale indicators: {str(e)}"
        )


@router.get("/stats", response_model=AlertStatsResponse, summary="Get alert statistics")
def alert_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Returns aggregated summary metrics for all active system alerts."""
    try:
        raw_alerts = alert_service.get_alert_feed(db)
        raw_stale = alert_service.get_stale_data_indicators(db)
        enriched = [_enrich_alert(a) for a in (raw_alerts + raw_stale)]

        total = len(enriched)
        crit = sum(1 for i in enriched if i.get("severity") == "critical")
        warn = sum(1 for i in enriched if i.get("severity") == "warning")
        info = sum(1 for i in enriched if i.get("severity") == "info")
        active = sum(1 for i in enriched if i.get("status") == "active")
        read = sum(1 for i in enriched if i.get("is_read"))
        dismissed = sum(1 for i in enriched if i.get("status") == "dismissed")

        return AlertStatsResponse(
            total_alerts=total,
            critical_count=crit,
            warning_count=warn,
            info_count=info,
            active_count=active,
            read_count=read,
            dismissed_count=dismissed
        )
    except Exception as e:
        logger.error(f"[Alerts Error] Failed to generate stats: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating alert statistics: {str(e)}"
        )


@router.post("/{alert_id}/read", response_model=AlertActionResponse, summary="Mark alert as read")
def mark_alert_read(
    alert_id: str,
    current_user: models.User = Depends(get_current_user)
):
    """Marks a single alert item as read."""
    state = _alert_states.setdefault(alert_id, {})
    state["is_read"] = True
    logger.info(f"[Alerts] Alert '{alert_id}' marked as read by user '{current_user.username}'")
    return AlertActionResponse(
        success=True,
        alert_id=alert_id,
        status=state.get("status", "active"),
        message="Alert marked as read"
    )


@router.post("/{alert_id}/acknowledge", response_model=AlertActionResponse, summary="Acknowledge alert")
def acknowledge_alert(
    alert_id: str,
    current_user: models.User = Depends(get_current_user)
):
    """Acknowledges an alert item."""
    state = _alert_states.setdefault(alert_id, {})
    state["status"] = "acknowledged"
    state["is_read"] = True
    logger.info(f"[Alerts] Alert '{alert_id}' acknowledged by user '{current_user.username}'")
    return AlertActionResponse(
        success=True,
        alert_id=alert_id,
        status="acknowledged",
        message="Alert acknowledged"
    )


@router.post("/{alert_id}/resolve", response_model=AlertActionResponse, summary="Resolve alert")
def resolve_alert(
    alert_id: str,
    current_user: models.User = Depends(get_current_user)
):
    """Resolves an alert item."""
    state = _alert_states.setdefault(alert_id, {})
    state["status"] = "resolved"
    state["is_read"] = True
    logger.info(f"[Alerts] Alert '{alert_id}' resolved by user '{current_user.username}'")
    return AlertActionResponse(
        success=True,
        alert_id=alert_id,
        status="resolved",
        message="Alert marked as resolved"
    )


@router.post("/{alert_id}/dismiss", response_model=AlertActionResponse, summary="Dismiss alert")
def dismiss_alert(
    alert_id: str,
    current_user: models.User = Depends(get_current_user)
):
    """Dismisses an alert item so it is hidden from the main feed."""
    state = _alert_states.setdefault(alert_id, {})
    state["status"] = "dismissed"
    state["is_read"] = True
    logger.info(f"[Alerts] Alert '{alert_id}' dismissed by user '{current_user.username}'")
    return AlertActionResponse(
        success=True,
        alert_id=alert_id,
        status="dismissed",
        message="Alert dismissed successfully"
    )


@router.post("/bulk/read", response_model=AlertActionResponse, summary="Bulk mark alerts as read")
def bulk_read_alerts(
    body: BulkReadRequest,
    current_user: models.User = Depends(get_current_user)
):
    """Marks multiple alerts as read in bulk."""
    for aid in body.alert_ids:
        state = _alert_states.setdefault(aid, {})
        state["is_read"] = True
    logger.info(f"[Alerts] Bulk read applied to {len(body.alert_ids)} alerts by '{current_user.username}'")
    return AlertActionResponse(
        success=True,
        alert_id="bulk",
        status="active",
        message=f"Marked {len(body.alert_ids)} alerts as read"
    )


# ── WebSocket Route ───────────────────────────────────────────────────────────
@router.websocket("/ws/alerts")
async def websocket_alerts(websocket: WebSocket):
    """WebSocket endpoint for receiving real-time alert events."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep-alive receive loop
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"[Alerts WS] Connection error: {e}")
        ws_manager.disconnect(websocket)
