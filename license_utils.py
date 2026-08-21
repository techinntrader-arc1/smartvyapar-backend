import subprocess
import hashlib
import hmac
import os
import platform
import uuid


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _first_value(output: str, heading: str) -> str:
    values = [line.strip() for line in output.splitlines() if line.strip()]
    return next((value for value in values if value.lower() != heading.lower()), "")

def get_hardware_id():
    """Returns a unique hardware ID for the machine (Baseboard Serial)."""
    try:
        # Motherboard Serial is more stable than MAC or CPUID
        # creationflags=0x08000000 is CREATE_NO_WINDOW to hide the black popup
        output = subprocess.check_output(
            ["wmic", "baseboard", "get", "serialnumber"],
            creationflags=CREATE_NO_WINDOW,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        )
        serial = _first_value(output, "SerialNumber")
        if serial:
            return serial
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        # Fallback 1: PowerShell Get-CimInstance (For Windows 11 where WMIC is deprecated/disabled)
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance -ClassName Win32_BaseBoard).SerialNumber"]
        out = subprocess.check_output(
            cmd,
            creationflags=CREATE_NO_WINDOW,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        ).strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass

    # Fallback 2: Windows MachineGuid is stable and avoids the shared
    # "unknown" identifier previously used when WMI was unavailable.
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if machine_guid:
                    return str(machine_guid)
        except OSError:
            pass

    fallback_material = f"{platform.node()}|{uuid.getnode()}|{platform.machine()}"
    return "SV-" + hashlib.sha256(fallback_material.encode("utf-8")).hexdigest()[:24].upper()

def verify_activation_key(hwid, key):
    """
    Verifies if a key is valid for a given Hardware ID.
    Simple Logic: Hash(HWID + SECRET) should match part of the key.
    """
    SECRET_SALT = "SmartVyaparElite_2026" # Don't share this
    expected = hashlib.sha256(f"{hwid}{SECRET_SALT}".encode()).hexdigest()[:16].upper()
    return hmac.compare_digest(key.strip().upper(), expected)
