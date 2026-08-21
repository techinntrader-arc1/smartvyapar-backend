"""
Thermal Print Router - Direct background print bridge via off-screen Edge browser.
Handles receipt formatting, Windows printer enumeration, print queue monitoring, and test receipt generation.
"""

import os
import time
import json
import tempfile
import subprocess
import threading
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from pydantic import BaseModel, Field

from auth import get_current_user, require_admin
import models

# Safe import for rate limiting (slowapi)
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address)
except ImportError:
    class DummyLimiter:
        def limit(self, limit_value: str):
            def decorator(func):
                return func
            return decorator
    limiter = DummyLimiter()

logger = logging.getLogger("smartvyapar.thermal")
logger.setLevel(logging.INFO)

router = APIRouter()

BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Track print queue & statistics
PRINT_QUEUE: List[Dict[str, Any]] = []
QUEUE_LOCK = threading.Lock()
TOTAL_PROCESSED_COUNT = 0


# ── Helper Functions ──────────────────────────────────────────────────────────

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _find_browser() -> Optional[str]:
    """Locate Microsoft Edge executable on host system."""
    for p in BROWSER_PATHS:
        if os.path.exists(p):
            return p
    return None


def _list_printers() -> List[Dict[str, Any]]:
    """Enumerate Windows printers via win32print or PowerShell fallback."""
    printers = []
    default_name = ""

    if os.name == "nt":
        try:
            import win32print
            default_name = win32print.GetDefaultPrinter()
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            raw_printers = win32print.EnumPrinters(flags)
            for p in raw_printers:
                name = p[2]
                printers.append({
                    "name": name,
                    "is_default": (name == default_name),
                    "driver": p[1] if len(p) > 1 else ""
                })
            if printers:
                return printers
        except Exception as e:
            logger.debug(f"win32print enumeration fallback: {e}")

        try:
            cmd = ["powershell", "-NoProfile", "-Command", "Get-Printer | Select-Object Name, Type, IsDefault | ConvertTo-Json"]
            kwargs = {"creationflags": CREATE_NO_WINDOW} if CREATE_NO_WINDOW else {}
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, **kwargs)
            if res.returncode == 0 and res.stdout.strip():
                raw_json = json.loads(res.stdout)
                if isinstance(raw_json, dict):
                    raw_json = [raw_json]
                for item in raw_json:
                    p_name = item.get("Name", "Unknown")
                    is_def = bool(item.get("IsDefault", False))
                    printers.append({
                        "name": p_name,
                        "is_default": is_def,
                        "driver": str(item.get("Type", ""))
                    })
                if printers:
                    return printers
        except Exception as e:
            logger.debug(f"PowerShell printer enumeration fallback: {e}")

    return [{"name": "Default Thermal Printer", "is_default": True, "driver": "Generic / Text Only"}]


def _build_print_html(body_html: str, width_mm: int) -> str:
    """Wrap receipt body HTML in responsive thermal CSS styles and auto-print script."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{ size: {width_mm}mm auto; margin: 0; }}
    html, body {{
      margin: 0; padding: 0;
      width: {width_mm}mm;
      background: #fff; color: #000;
      font-family: 'Courier New', Courier, monospace;
      -webkit-print-color-adjust: exact;
    }}
    #wrapper {{ width: 100%; display: inline-block; padding-bottom: 20px; }}
    .spacer {{ height: 80px; }}
  </style>
</head>
<body>
<div id="wrapper">
{body_html}
<div class="spacer"></div>
<br><br><br><br><br><br><br><br><br><br>
</div>
<script>
  window.onload = function() {{
    setTimeout(function() {{
      window.print();
      setTimeout(function() {{ window.close(); }}, 3000);
    }}, 800);
  }};
</script>
</body>
</html>"""


def _do_print(html: str, width_mm: int, printer_name: str, job_id: str):
    """Execute off-screen Edge browser printing via VBScript runner."""
    global TOTAL_PROCESSED_COUNT
    browser = _find_browser()
    if not browser:
        logger.error("Print job failed: Microsoft Edge browser not found.")
        with QUEUE_LOCK:
            for item in PRINT_QUEUE:
                if item["job_id"] == job_id:
                    item["status"] = "failed"
                    item["error"] = "Browser not found"
        return

    full_html = _build_print_html(html, width_mm)
    
    fd, temp_path = tempfile.mkstemp(suffix=".html")
    vbs_path = temp_path + ".vbs"
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(full_html)

    profile_dir = os.path.join(tempfile.gettempdir(), f"sv_ptr_{int(time.time())}")
    os.makedirs(profile_dir, exist_ok=True)

    edge_cmd = (
        f'"{browser}" '
        f'"file:///{temp_path.replace("\\", "/")}" '
        f'--kiosk-printing '
        f'--no-first-run '
        f'--no-default-browser-check '
        f'--window-position=-10000,-10000 '
        f'--window-size=10,10 '
        f'--user-data-dir="{profile_dir}"'
    )
    
    escaped_cmd = edge_cmd.replace('"', '""')
    vbs_content = f'Set WshShell = CreateObject("WScript.Shell")\nWshShell.Run "{escaped_cmd}", 4, True'
    
    with open(vbs_path, 'w', encoding='utf-8') as f:
        f.write(vbs_content)

    try:
        logger.info(f"Executing thermal print job '{job_id}' (width={width_mm}mm)")
        if os.name == "nt":
            subprocess.run(["wscript.exe", vbs_path], check=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        else:
            logger.info("Thermal direct printing skipped on non-Windows host.")
        with QUEUE_LOCK:
            TOTAL_PROCESSED_COUNT += 1
            for item in PRINT_QUEUE:
                if item["job_id"] == job_id:
                    item["status"] = "completed"
    except Exception as e:
        logger.error(f"Error executing print job '{job_id}': {e}", exc_info=True)
        with QUEUE_LOCK:
            for item in PRINT_QUEUE:
                if item["job_id"] == job_id:
                    item["status"] = "failed"
                    item["error"] = str(e)
    finally:
        def _cleanup():
            time.sleep(10)
            try:
                if os.path.exists(temp_path): os.unlink(temp_path)
                if os.path.exists(vbs_path): os.unlink(vbs_path)
                import shutil
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception as clean_err:
                logger.warning(f"Print job cleanup warning: {clean_err}")

            with QUEUE_LOCK:
                global PRINT_QUEUE
                PRINT_QUEUE = [i for i in PRINT_QUEUE if i["job_id"] != job_id]

        threading.Thread(target=_cleanup, daemon=True).start()


# ── Schemas ──────────────────────────────────────────────────────────────

class PrintRequest(BaseModel):
    html: str = Field(..., min_length=1, description="Raw receipt HTML body string")
    width_mm: int = Field(80, ge=40, le=120, description="Receipt paper width in mm")
    printer_name: Optional[str] = Field("", description="Target printer name")


class PrintResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    queue_id: Optional[str] = None


class PrinterItem(BaseModel):
    name: str
    is_default: bool
    driver: Optional[str] = ""


class PrinterListResponse(BaseModel):
    printers: List[PrinterItem]
    default_printer: Optional[str] = None


class PrintStatusResponse(BaseModel):
    active_jobs: int
    total_processed: int
    queue: List[Dict[str, Any]]


class ThermalHealthCheckResponse(BaseModel):
    status: str
    timestamp: str
    module: str
    browser_available: bool
    browser_path: Optional[str] = None
    endpoints: List[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health", response_model=ThermalHealthCheckResponse)
@router.get("/thermal/health", response_model=ThermalHealthCheckResponse)
@limiter.limit("1200/minute")
def thermal_health(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    """Health check endpoint for thermal printing module."""
    browser_path = _find_browser()
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "thermal",
        "browser_available": browser_path is not None,
        "browser_path": browser_path,
        "endpoints": [
            "/thermal/print",
            "/thermal/printers",
            "/thermal/status",
            "/thermal/test",
            "/health"
        ]
    }


@router.get("/printers", response_model=PrinterListResponse)
@router.get("/thermal/printers", response_model=PrinterListResponse)
@limiter.limit("1200/minute")
def list_printers(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    """List available system printers and identify default printer."""
    try:
        printers = _list_printers()
        default_printer = next((p["name"] for p in printers if p.get("is_default")), None)
        return {
            "printers": printers,
            "default_printer": default_printer
        }
    except Exception as e:
        logger.error(f"Error enumerating printers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list printers: {str(e)}"
        )


@router.get("/status", response_model=PrintStatusResponse)
@router.get("/thermal/status", response_model=PrintStatusResponse)
@limiter.limit("1200/minute")
def get_print_status(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    """Get active print queue status and total processed count."""
    try:
        with QUEUE_LOCK:
            active_jobs = len([i for i in PRINT_QUEUE if i.get("status") == "processing"])
            return {
                "active_jobs": active_jobs,
                "total_processed": TOTAL_PROCESSED_COUNT,
                "queue": list(PRINT_QUEUE)
            }
    except Exception as e:
        logger.error(f"Error fetching print status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get print status: {str(e)}"
        )


@router.post("/print", response_model=PrintResponse)
@router.post("/thermal/print", response_model=PrintResponse)
@limiter.limit("10/minute")
def thermal_print(
    request: Request,
    req: PrintRequest,
    current_user: models.User = Depends(get_current_user)
):
    """Enqueue receipt HTML content for off-screen background thermal printing."""
    try:
        browser = _find_browser()
        if not browser:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Microsoft Edge browser required for printing was not found on host system."
            )

        job_id = f"JOB_{int(time.time() * 1000)}"
        job_info = {
            "job_id": job_id,
            "width_mm": req.width_mm,
            "printer_name": req.printer_name or "Default",
            "status": "processing",
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        with QUEUE_LOCK:
            PRINT_QUEUE.append(job_info)

        t = threading.Thread(
            target=_do_print,
            args=(req.html, req.width_mm, req.printer_name, job_id),
            daemon=True
        )
        t.start()

        return {
            "success": True,
            "message": "Print job queued successfully",
            "queue_id": job_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error queueing print job: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue print job: {str(e)}"
        )


@router.post("/test", response_model=PrintResponse)
@router.post("/thermal/test", response_model=PrintResponse)
@limiter.limit("10/minute")
def print_test_receipt(
    request: Request,
    width_mm: int = Query(80, ge=40, le=120),
    printer_name: Optional[str] = Query(""),
    current_user: models.User = Depends(require_admin)
):
    """Print a sample test receipt to verify printer hardware alignment."""
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sample_html = f"""
        <div style="text-align:center; font-family:monospace; font-size:12px;">
            <h2 style="margin:4px 0;">SMART VYAPAR POS</h2>
            <p style="margin:2px 0;">*** TEST RECEIPT ***</p>
            <p style="margin:2px 0;">Date: {now_str}</p>
            <hr style="border-top:1px dashed #000;">
            <table style="width:100%; font-size:11px; text-align:left;">
                <tr><td>Test Item A</td><td style="text-align:right;">1 x 100.00</td></tr>
                <tr><td>Test Item B</td><td style="text-align:right;">2 x 250.00</td></tr>
            </table>
            <hr style="border-top:1px dashed #000;">
            <p style="font-weight:bold; font-size:14px; margin:4px 0;">TOTAL: PKR 600.00</p>
            <hr style="border-top:1px dashed #000;">
            <p style="margin:4px 0;">Printed by: {current_user.username}</p>
            <p style="margin:4px 0;">Printer OK!</p>
        </div>
        """

        job_id = f"TEST_{int(time.time() * 1000)}"
        job_info = {
            "job_id": job_id,
            "width_mm": width_mm,
            "printer_name": printer_name or "Default",
            "status": "processing",
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        with QUEUE_LOCK:
            PRINT_QUEUE.append(job_info)

        t = threading.Thread(
            target=_do_print,
            args=(sample_html, width_mm, printer_name, job_id),
            daemon=True
        )
        t.start()

        return {
            "success": True,
            "message": "Test receipt sent to printer queue",
            "queue_id": job_id
        }
    except Exception as e:
        logger.error(f"Error printing test receipt: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to print test receipt: {str(e)}"
        )
