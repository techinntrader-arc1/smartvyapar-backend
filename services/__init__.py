"""
SmartVyapar Services Package
Contains service modules for automated database backups, restoration, FoxPro migration,
Google Drive integration, Firebase synchronization, and data sync management.
"""

__version__ = "1.3.5"
__author__ = "SmartVyapar Team"

from .backup_service import (
    perform_auto_backup,
    perform_manual_backup,
    perform_async_backup,
    list_backups,
    restore_backup,
    restore_async_backup,
    delete_old_backups,
    get_backup_status,
    BackupResult,
)

from .safe_backup_service import (
    trigger_safe_backup,
    force_safe_backup_sync,
    trigger_async_safe_backup,
    force_async_safe_backup,
    get_latest_manifest,
    update_backup_config,
    perform_atomic_backup,
)

from .restore_service import (
    get_preview_stats,
    perform_restore,
    perform_async_restore,
    list_restore_points,
    undo_last_restore,
    save_restore_history,
    get_restore_history,
)

from .migration_service import (
    migrate_foxpro_data,
    migrate_foxpro_data_async,
    read_dbf,
    export_migration_report,
    resume_migration,
)

from .google_drive_service import (
    get_google_config,
    get_flow,
    get_auth_url,
    save_token,
    load_credentials,
    upload_file_to_drive,
    list_files_in_drive,
    download_file_from_drive,
    delete_file_from_drive,
    get_file_metadata,
    check_storage_quota,
    revoke_access,
)

from .firebase_sync import (
    FirebaseSyncService,
)

from .sync_manager import (
    SyncManager,
)

__all__ = [
    # Version metadata
    "__version__",
    "__author__",

    # Backup Service
    "perform_auto_backup",
    "perform_manual_backup",
    "perform_async_backup",
    "list_backups",
    "restore_backup",
    "restore_async_backup",
    "delete_old_backups",
    "get_backup_status",
    "BackupResult",

    # Safe Backup Service
    "trigger_safe_backup",
    "force_safe_backup_sync",
    "trigger_async_safe_backup",
    "force_async_safe_backup",
    "get_latest_manifest",
    "update_backup_config",
    "perform_atomic_backup",

    # Restore Service
    "get_preview_stats",
    "perform_restore",
    "perform_async_restore",
    "list_restore_points",
    "undo_last_restore",
    "save_restore_history",
    "get_restore_history",

    # Migration Service
    "migrate_foxpro_data",
    "migrate_foxpro_data_async",
    "read_dbf",
    "export_migration_report",
    "resume_migration",

    # Google Drive Service
    "get_google_config",
    "get_flow",
    "get_auth_url",
    "save_token",
    "load_credentials",
    "upload_file_to_drive",
    "list_files_in_drive",
    "download_file_from_drive",
    "delete_file_from_drive",
    "get_file_metadata",
    "check_storage_quota",
    "revoke_access",

    # Firebase Sync & Sync Manager
    "FirebaseSyncService",
    "SyncManager",
]
