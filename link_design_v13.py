# -*- coding: utf-8 -*-
"""Link Design v13 compatibility layer over the v14 cable layout engine."""

from . import link_design_v9 as _v9
from . import link_design_v14 as _v14

# v14 uses the same v9/v12 controller stack. Keep the payload-helper
# compatibility required by the v12 cable synchronization layer.
if not hasattr(_v9, "_fresh_payload"):
    _v9._fresh_payload = _v9._v2._fresh_payload


class LinkDesignDock(_v14.LinkDesignDock):
    """Active Link Design dock with automatic overlap cable layout."""

    pass
