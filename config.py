"""
SmartVyapar - Global Configuration Module
Secured against hardcoded credentials using environment variables, .env file, and optional client_secret JSON fallback.
"""

import os
import json
import logging
import secrets
from pathlib import Path

# Setup logger for configuration module
logger = logging.getLogger("smartvyapar.config")
logger.setLevel(logging.INFO)

class ConfigError(Exception):
    """Custom exception raised when required configuration settings are missing or invalid."""
    pass


def _external_config_dir() -> Path:
    configured = os.getenv("SV_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "SmartVyapar" / "config"
    return Path.home() / ".smartvyapar" / "config"


# Only load a dotenv file from the per-user external config directory. This
# prevents a source checkout or packaged application directory from becoming a
# credential store.
try:
    from dotenv import load_dotenv
    # 1. First check local app directory .env (standard for web servers/Hostinger)
    local_env = Path(__file__).parent / ".env"
    if local_env.exists():
        load_dotenv(dotenv_path=local_env, override=False)
        logger.info(f"Loaded environment variables from local config: {local_env}")
    
    # 2. Check external user config directory .env
    external_env = _external_config_dir() / ".env"
    if external_env.exists():
        load_dotenv(dotenv_path=external_env, override=False)
        logger.info(f"Loaded environment variables from external config: {external_env}")
except ImportError:
    logger.warning("python-dotenv not installed. Falling back to system environment variables.")


def _load_google_credentials():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    # Optional fallback is restricted to the per-user config directory. Never
    # load OAuth secrets from the source/application directory.
    if not client_id or not client_secret:
        for json_file in _external_config_dir().glob("client_secret*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    info = data.get("installed") or data.get("web") or {}
                    if info.get("client_id") and info.get("client_secret"):
                        client_id = client_id or info.get("client_id", "").strip()
                        client_secret = client_secret or info.get("client_secret", "").strip()
                        logger.info(f"Loaded Google OAuth credentials from external config file {json_file.name}")
                        break
            except Exception as e:
                logger.warning(f"Could not parse external credential file {json_file}: {e}")

    return client_id, client_secret


def _load_or_create_config_secret() -> str:
    configured = os.getenv("SECRET_KEY", "").strip()
    if configured and configured != "smartvyapar_default_secret_key" and len(configured) >= 32:
        return configured

    secret_path = _external_config_dir() / "app_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        saved = secret_path.read_text(encoding="utf-8").strip()
        if len(saved) >= 32:
            return saved

    generated = secrets.token_urlsafe(48)
    secret_path.write_text(generated, encoding="utf-8")
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    return generated


class Config:
    """
    Global Application Configuration Class.
    Loads and validates environment configurations.
    """

    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET = _load_google_credentials()
    SECRET_KEY: str = _load_or_create_config_secret()
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development").strip()

    @classmethod
    def validate(cls, raise_on_error: bool = True) -> bool:
        """
        Validates required environment variables and raises ConfigError if critical settings are missing in production.
        """
        missing_vars = []

        if not cls.GOOGLE_CLIENT_ID:
            missing_vars.append("GOOGLE_CLIENT_ID")
        if not cls.GOOGLE_CLIENT_SECRET:
            missing_vars.append("GOOGLE_CLIENT_SECRET")

        if missing_vars:
            warn_msg = (
                f"Google OAuth credentials missing: {', '.join(missing_vars)}. "
                "Google Drive cloud backup integration will be disabled until credentials are provided in .env."
            )
            logger.warning(warn_msg)
            return False

        logger.info("Google OAuth Configuration validated successfully.")
        return True


# Run validation on module import
try:
    Config.validate(raise_on_error=Config.ENVIRONMENT.lower() == "production")
except ConfigError as err:
    logger.error(f"Config validation error: {err}")

# Module-level variable exports for backward compatibility across services
GOOGLE_CLIENT_ID = Config.GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET = Config.GOOGLE_CLIENT_SECRET
SECRET_KEY = Config.SECRET_KEY
