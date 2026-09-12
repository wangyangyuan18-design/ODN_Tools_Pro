# -*- coding: utf-8 -*-
"""Legacy compatibility facade for Link Design v16.

The active Link Design implementation no longer uses v16-specific runtime
logic. The canonical module imports the required v15 UI base directly through
this compatibility boundary while the historical file remains importable for
older integrations.
"""
from .link_design_v15 import LinkDesignDock  # noqa: F401
