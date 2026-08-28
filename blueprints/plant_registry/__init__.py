"""Plant & Equipment Register module package.

Registered by the module registry (see ``module.toml``), not by an edit to the
application factory.  ``wire_package`` shares names across the split submodules
the same way the accounts/inventory packs do.
"""
from .models import PlantAsset, PlantAssetMovement  # noqa: F401  (registers metadata)
from ._common import plant_registry_bp, MODULE_CONFIG  # noqa: F401
from .pages import *  # noqa
from .service import PlantRegistryError  # noqa: F401

from utils.pkg_wire import wire_package

wire_package("blueprints.plant_registry")
