"""Facade for split pages.py (legacy Excel tooling + new Data Center)."""
from ._pages_import_export_page import import_export_page  # noqa
from ._pages_get_template import get_template  # noqa
from ._pages_preview_import import preview_import  # noqa
from ._pages_execute_import import execute_import  # noqa
from ._pages_full_raw_export import full_raw_export  # noqa
from ._pages_tenant_db_export import tenant_db_export  # noqa
from ._pages_full_raw_import import full_raw_import  # noqa
from ._pages_full_raw_import_history import full_raw_import_history  # noqa
from ._pages_app_upgrade import app_upgrade  # noqa
from ._pages_app_upgrade_start import app_upgrade_start  # noqa
from ._pages_app_upgrade_status import app_upgrade_status  # noqa
from ._pages_export_data import export_data  # noqa
from ._pages_email_file import email_file  # noqa
from ._pages_transfer_import import transfer_import  # noqa
# Data Center (new data path)
from ._pages_dc_export import (  # noqa
    dc_export_page,
    dc_export_json,
    dc_export_xlsx,
    dc_export_db,
)
from ._pages_dc_restore import (  # noqa
    dc_restore_page,
    dc_restore_plan,
    dc_restore_plan_server,
    dc_restore_apply,
    dc_restore_result,
)
from ._pages_dc_db_restore import (  # noqa
    dc_db_restore_page,
    dc_db_restore_apply,
)
from ._pages_dc_legacy import (  # noqa
    dc_legacy_page,
    dc_legacy_plan,
    dc_legacy_apply,
    dc_legacy_convert,
)
from ._pages_dc_history import dc_history_page  # noqa
