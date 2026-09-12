# -*- coding: utf-8 -*-
"""Compatibility entry point for the unified Offset Core.

The previous v5/v8/v9/v10 monkey-patch chain is retired. Link Design keeps
calling this module, while all planning and geometry now live in
``cable_offset_core.py``.
"""
from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsMessageLog, Qgis
from . import cable_offset_core as _core

DEFAULT_SPACING_M = _core.DEFAULT_SPACING_M
DEFAULT_CONTROL_DISTANCE_M = _core.DEFAULT_CONTROL_M
SPACING_KEY = "ODNToolsPro/CableOffsetLayout/spacing_m"
CONTROL_DISTANCE_KEY = "ODNToolsPro/CableOffsetLayout/control_distance_m"
LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message, level=Qgis.Info):
    try: QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception: pass


def get_settings():
    try: spacing=float(QSettings().value(SPACING_KEY,DEFAULT_SPACING_M))
    except Exception: spacing=DEFAULT_SPACING_M
    try: control=float(QSettings().value(CONTROL_DISTANCE_KEY,DEFAULT_CONTROL_DISTANCE_M))
    except Exception: control=DEFAULT_CONTROL_DISTANCE_M
    return max(.01,spacing),max(.01,control)


def save_settings(spacing,control_distance):
    s=QSettings(); s.setValue(SPACING_KEY,max(.01,float(spacing))); s.setValue(CONTROL_DISTANCE_KEY,max(.01,float(control_distance))); s.sync()


def apply_explicit_layout_to_designs(designs,distribution_layer,edge_layer,spacing=DEFAULT_SPACING_M,control_distance_m=DEFAULT_CONTROL_DISTANCE_M):
    spacing=max(.01,float(spacing)); control_distance_m=max(.01,float(control_distance_m))
    _log("========== Offset START ==========")
    _log(f"[engine] OffsetCore; spacing={spacing:.3f}m; same_point_control={control_distance_m:.3f}m; ordinary_corner_control=NOT_USED")
    summary=_core.apply_offset_layout(designs,distribution_layer,edge_layer,spacing,control_distance_m)
    # FAT landing remains a downstream ownership operation. It consumes the
    # final geometry produced by OffsetCore and never drives cable geometry.
    try:
        from . import cable_offset_layout_v6 as _v6
        from . import cable_offset_fat_v2 as _fat
        _v6._target_for_fat=_fat.target_for_fat
        _log("[fat-landing] owning-Link final geometry active")
    except Exception as exc:
        _log(f"[fat-landing] activation warning: {type(exc).__name__}: {exc}",Qgis.Warning)
    _log(f"[result] changed={summary.get('changed_designs',0)}; extra={summary.get('extra_length_m',0):.3f}m; engine=OffsetCore")
    _log("========== Offset END ==========")
    return summary
