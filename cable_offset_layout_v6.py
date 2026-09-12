# -*- coding: utf-8 -*-
"""Temporary compatibility facade; Offset Core is authoritative.

No planning, lane allocation, geometry, FAT landing, or runtime patching is
implemented here. New code must import cable_offset_core directly. This thin
facade remains only because the current Link Design version stack still loads
the historical module name during class construction.
"""
from . import cable_offset_core as _core
DEFAULT_SPACING_M=_core.DEFAULT_SPACING_M
DEFAULT_CONTROL_DISTANCE_M=_core.DEFAULT_CONTROL_M
DEFAULT_FAT_MAX_DISTANCE_M=_core.DEFAULT_FAT_MAX_DISTANCE_M
get_settings=_core.get_settings
save_settings=_core.save_settings
apply_explicit_layout_to_designs=_core.apply_offset_layout
commit_fat_landing_points=_core.commit_fat_landing_points
