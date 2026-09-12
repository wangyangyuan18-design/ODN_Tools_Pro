# -*- coding: utf-8 -*-
"""Canonical Link Design entry point.

Runtime architecture:
    __init__.py -> link_design.py -> link_design_core.py
                                     -> odn_project_routing.py
                                     -> cable_offset_core.py

No versioned Link Design implementation is imported here.
"""

from .link_design_core import (
    LinkDesignDock,
    LinkDesignCore,
    LinkDesignMapTool,
    LinkDesignMapToolV9,
    LinkDesignMapToolV11,
    LinkDesignMapToolV15,
    PlanningOverlay,
    LinkDesignController,
)

# Install the final relative-lane compression policy after the authoritative
# OffsetCore module has been loaded. Corner D/E/F geometry is not modified.
from . import cable_offset_lane_allocator  # noqa: F401,E402

__all__ = [
    "LinkDesignDock",
    "LinkDesignCore",
    "LinkDesignController",
    "LinkDesignMapTool",
    "LinkDesignMapToolV9",
    "LinkDesignMapToolV11",
    "LinkDesignMapToolV15",
    "PlanningOverlay",
]
