# -*- coding: utf-8 -*-
"""Compatibility entry for the ODN 2.0 planning tool.

The active implementation lives in odn2_planner.py so future ODN standards can
use independent rule engines without mixing their design logic.
"""
from .odn2_planner import Odn2PlannerDialog

# Keep the legacy class name used by the current plugin entry point.
FdtPlannerDialog = Odn2PlannerDialog
