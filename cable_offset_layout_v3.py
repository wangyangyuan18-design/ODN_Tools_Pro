# -*- coding: utf-8 -*-
"""Explicit Cable Offset Layout v3.

This wrapper provides the user-facing "offset and write" operation:
- configurable transition angle: 60 / 75 / 90 degrees;
- configurable offset spacing (default 0.5 m);
- existing Distribution Cable belonging to saved Links is excluded from
  occupancy calculations so the new offset result can replace the old
  un-offset result;
- unrelated/manual Distribution Cable remains an obstacle and is preserved;
- all saved Links are considered together because slot allocation must be
  globally consistent on shared Pole Edge segments.
"""

from qgis.core import QgsFeature, QgsVectorLayer

from . import cable_offset_layout_v2 as _v2


def _manual_only_layer(distribution_layer, designs):
    """Build a temporary line layer containing only unrelated/manual DC."""
    layer = QgsVectorLayer(
        f"LineString?crs={distribution_layer.crs().authid()}",
        "ODN Offset Occupancy",
        "memory",
    )
    link_idx = distribution_layer.fields().indexOf("_ODN_LINK_ID")
    known_ids = set()
    if link_idx >= 0:
        for design in designs or []:
            value = design.get("_link_id")
            if value:
                known_ids.add(str(value))

    provider = layer.dataProvider()
    features = []
    for feature in distribution_layer.getFeatures():
        if link_idx >= 0:
            value = feature.attribute(link_idx)
            if value and str(value) in known_ids:
                continue
        clone = QgsFeature()
        clone.setGeometry(feature.geometry())
        features.append(clone)
    if features:
        provider.addFeatures(features)
    layer.updateExtents()
    return layer


def apply_explicit_layout_to_designs(
    designs,
    distribution_layer,
    edge_layer,
    spacing=0.5,
    angle_deg=60.0,
):
    """Apply the selected angle/spacing to all saved Links in-place."""
    try:
        angle_deg = float(angle_deg)
    except (TypeError, ValueError):
        angle_deg = 60.0
    if angle_deg not in (60.0, 75.0, 90.0):
        angle_deg = 60.0

    original_states = []
    try:
        for design in designs or []:
            original_states.append(
                (bool(design.get("written")), bool(design.get("needs_resync")))
            )
            # v2 deliberately skips already-written Links. Explicit offset
            # must be able to recalculate them all before the final write.
            design["written"] = False
            design["needs_resync"] = True

        occupancy_layer = _manual_only_layer(distribution_layer, designs)
        base = _v2._base
        original_transition_angle = base._transition_angle
        base._transition_angle = lambda _slot_change: angle_deg
        try:
            return _v2.apply_layout_to_designs(
                designs,
                occupancy_layer,
                edge_layer,
                spacing=spacing,
            )
        finally:
            base._transition_angle = original_transition_angle
    except Exception:
        for design, state in zip(designs or [], original_states):
            design["written"], design["needs_resync"] = state
        raise
