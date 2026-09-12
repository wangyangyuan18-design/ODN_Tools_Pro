# -*- coding: utf-8 -*-
"""Compatibility facade for the retired cable-offset implementation.

All real planning, geometry and FAT landing now lives in cable_offset_core.
This module contains no offset algorithm and exists only while Link Design v17
is being migrated away from the historical import name.
"""
from . import cable_offset_core as _core

DEFAULT_SPACING_M = _core.DEFAULT_SPACING_M
DEFAULT_CONTROL_DISTANCE_M = _core.DEFAULT_CONTROL_M
DEFAULT_FAT_MAX_DISTANCE_M = _core.DEFAULT_FAT_MAX_DISTANCE_M

get_settings = _core.get_settings
save_settings = _core.save_settings
apply_explicit_layout_to_designs = _core.apply_offset_layout
commit_fat_landing_points = _core.commit_fat_landing_points
