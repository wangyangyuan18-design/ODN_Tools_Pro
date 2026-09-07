# -*- coding: utf-8 -*-
"""Explicit cable offset layout with user-selected transition angles."""
from qgis.core import QgsFeature, QgsVectorLayer
from . import cable_offset_layout_v2 as _v2

ALLOWED=(45.0,60.0,75.0,90.0)
DEFAULT=(60.0,75.0,90.0)

def _manual_only_layer(distribution_layer, designs):
    layer=QgsVectorLayer(f"LineString?crs={distribution_layer.crs().authid()}","ODN Offset Occupancy","memory")
    idx=distribution_layer.fields().indexOf("_ODN_LINK_ID")
    known={str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
    fs=[]
    for feature in distribution_layer.getFeatures():
        if idx>=0 and feature.attribute(idx) and str(feature.attribute(idx)) in known: continue
        clone=QgsFeature(); clone.setGeometry(feature.geometry()); fs.append(clone)
    if fs: layer.dataProvider().addFeatures(fs)
    layer.updateExtents(); return layer

def _transition_factory(angles):
    allowed=sorted(set(float(x) for x in (angles or DEFAULT) if float(x) in ALLOWED)) or list(DEFAULT)
    def transition(change):
        magnitude=abs(int(change))
        if len(allowed)==1: return allowed[0]
        if magnitude<=2: return allowed[0]
        if magnitude<=4: return allowed[min(1,len(allowed)-1)]
        return allowed[-1]
    return transition

def apply_explicit_layout_to_designs(designs,distribution_layer,edge_layer,spacing=0.5,angles=None):
    angles=angles or DEFAULT
    allowed=[]
    for x in angles:
        try: x=float(x)
        except Exception: continue
        if x in ALLOWED and x not in allowed: allowed.append(x)
    if not allowed: allowed=list(DEFAULT)
    try: spacing=max(0.01,float(spacing))
    except Exception: spacing=0.5
    states=[(bool(d.get("written")),bool(d.get("needs_resync"))) for d in designs or []]
    try:
        for d in designs or []:
            d["written"]=False; d["needs_resync"]=True
        occupancy=_manual_only_layer(distribution_layer,designs)
        base=_v2._base; old=base._transition_angle; base._transition_angle=_transition_factory(allowed)
        try:
            summary=_v2.apply_layout_to_designs(designs,occupancy,edge_layer,spacing=spacing)
        finally:
            base._transition_angle=old
        for d in designs or []:
            d["layout"]={"version":4,"spacing_m":round(spacing,3),"angles_deg":[float(x) for x in sorted(allowed)]}
            d["written"]=False; d["needs_resync"]=True
        summary["angles_deg"]=[float(x) for x in sorted(allowed)]
        return summary
    except Exception:
        for d,state in zip(designs or [],states): d["written"],d["needs_resync"]=state
        raise
