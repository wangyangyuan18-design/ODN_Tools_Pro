# -*- coding: utf-8 -*-
"""Retired compatibility facade.

No offset implementation remains here. Active offset processing is provided by
cable_offset_core; this module only prevents historical Link Design imports
from breaking while the Link Design version stack is being retired.
"""
from . import cable_offset_core as _core
DEFAULT_SPACING_M = _core.DEFAULT_SPACING_M
get_settings = _core.get_settings
save_settings = _core.save_settings
apply_explicit_layout_to_designs = _core.apply_offset_layout
