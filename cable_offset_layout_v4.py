# -*- coding: utf-8 -*-
"""Retired compatibility facade.

No offset algorithm is implemented here. Active processing is centralized in
cable_offset_core.
"""
from . import cable_offset_core as _core
DEFAULT_SPACING_M = _core.DEFAULT_SPACING_M
get_settings = _core.get_settings
save_settings = _core.save_settings
apply_explicit_layout_to_designs = _core.apply_offset_layout
