# -*- coding: utf-8 -*-
"""Legacy compatibility facade for the canonical Link Design implementation.

The active implementation now lives in ``link_design.py``.  This module is
kept only so older imports do not break during the migration away from the
historical v9-v17 chain.
"""
from .link_design import *  # noqa: F401,F403
