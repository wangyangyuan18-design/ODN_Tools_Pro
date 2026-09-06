# -*- coding: utf-8 -*-
"""Active Link Design compatibility layer over v15."""

from . import link_design_v9 as _v9
from . import link_design_v15 as _v15

# Keep the payload-helper compatibility required by the v12 cable
# synchronization layer.
if not hasattr(_v9, "_fresh_payload"):
    _v9._fresh_payload = _v9._v2._fresh_payload


class LinkDesignDock(_v15.LinkDesignDock):
    """Active Link Design dock with the v14 engine and v15 UI refinements."""

    pass
