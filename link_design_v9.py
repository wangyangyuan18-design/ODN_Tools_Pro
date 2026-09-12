# -*- coding: utf-8 -*-
"""Compatibility facade for the canonical Link Design runtime.

No implementation remains in this versioned module. A small compatibility
alias is retained for auxiliary Link Design tools that still request the
historical private payload helper.
"""
from .link_design import *  # noqa: F401,F403
from .link_design_core import fresh_payload as _fresh_payload
