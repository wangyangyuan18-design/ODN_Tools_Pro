# -*- coding: utf-8 -*-
"""Link Design v13 compatibility fix.

Provides the payload helper expected by the v12 cable synchronization layer.
The implementation lives in link_design_v12; this module only repairs the
module-level compatibility alias and exposes the same Dock class.
"""

from . import link_design_v9 as _v9
from . import link_design_v12 as _v12

# v12 references _v9._fresh_payload. The canonical helper is defined in the
# stable v2 module and imported by v9 as _v2.
if not hasattr(_v9, "_fresh_payload"):
    _v9._fresh_payload = _v9._v2._fresh_payload


class LinkDesignDock(_v12.LinkDesignDock):
    """v13 dock: v12 behavior plus payload-helper compatibility."""

    pass
