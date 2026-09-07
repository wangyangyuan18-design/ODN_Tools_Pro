# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QSettings
from . import cable_offset_layout_v3 as _v3

ANGLES=(45.0,60.0,75.0,90.0)
DEFAULT_ANGLES=(60.0,75.0,90.0)
ANGLE_KEY="ODNToolsPro/CableOffsetLayout/angles_deg"
SPACING_KEY="ODNToolsPro/CableOffsetLayout/spacing_m"

def get_settings():
    raw=QSettings().value(ANGLE_KEY, None)
    try: enabled=[float(x) for x in raw] if isinstance(raw,(list,tuple)) else []
    except Exception: enabled=[]
    enabled=sorted(set(x for x in enabled if x in ANGLES)) or list(DEFAULT_ANGLES)
    try: spacing=float(QSettings().value(SPACING_KEY,0.5))
    except Exception: spacing=0.5
    return enabled,max(0.01,spacing)

def save_settings(angles,spacing):
    enabled=sorted(set(float(x) for x in angles if float(x) in ANGLES)) or list(DEFAULT_ANGLES)
    QSettings().setValue(ANGLE_KEY,enabled); QSettings().setValue(SPACING_KEY,max(0.01,float(spacing))); QSettings().sync()

def apply_explicit_layout_to_designs(designs,distribution_layer,edge_layer,spacing=0.5,angles=None):
    return _v3.apply_explicit_layout_to_designs(designs,distribution_layer,edge_layer,spacing=spacing,angles=angles)
