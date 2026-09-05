# -*- coding: utf-8 -*-
"""Minimal wrapper to load the real implementation.

This file intentionally small: it imports the corrected implementation
from site_co_design_impl.py so that the plugin entry point remains
site_co_design.SiteCoDesign as expected by QGIS.
"""
from .site_co_design_impl import SiteCoDesign
