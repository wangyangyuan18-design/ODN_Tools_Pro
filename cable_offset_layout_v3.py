# -*- coding: utf-8 -*-
"""Explicit cable offset layout with user-selected transition angles.

During layout calculation, Pole Edge identity is treated as geometric rather
than depending on the source feature id. This is important when two route
segments are geometrically coincident but originate from different Pole Edge
features.
"""
from qgis.core import QgsFeature, QgsVectorLayer
from . import cable_offset_layout_v2 as _v2

ALLOWED = (45.0, 60.0, 75.0, 90.0)
DEFAULT = (60.0, 75.0, 90.0)


def _manual_only_layer(distribution_layer, designs):
    layer = QgsVectorLayer(
        f"LineString?crs={distribution_layer.crs().authid()}",
        "ODN Offset Occupancy",
        "memory",
    )
    idx = distribution_layer.fields().indexOf("_ODN_LINK_ID")
    known = {
        str(d.get("_link_id"))
        for d in designs or []
        if d.get("_link_id")
    }
    fs = []
    for feature in distribution_layer.getFeatures():
        if idx >= 0 and feature.attribute(idx) and str(feature.attribute(idx)) in known:
            continue
        clone = QgsFeature()
        clone.setGeometry(feature.geometry())
        fs.append(clone)
    if fs:
        layer.dataProvider().addFeatures(fs)
    layer.updateExtents()
    return layer


def _transition_factory(angles):
    allowed = sorted(
        set(float(x) for x in (angles or DEFAULT) if float(x) in ALLOWED)
    ) or list(DEFAULT)

    def transition(change):
        magnitude = abs(int(change))
        if len(allowed) == 1:
            return allowed[0]
        if magnitude <= 2:
            return allowed[0]
        if magnitude <= 4:
            return allowed[min(1, len(allowed) - 1)]
        return allowed[-1]

    return transition


def _geometry_only_canonical_edge(raw):
    """Normalize an edge for overlap matching without using Pole Edge FID.

    The existing layout engine uses the canonical edge tuple as its grouping
    key. Replacing only the feature id during the calculation means two
    coincident Pole Edge features with identical endpoints are considered the
    same physical route segment, while the original saved edge_sequence is
    never modified.
    """
    edge = _v2._base._canonical_edge(raw)
    if edge is None:
        return None
    return 0, edge[1], edge[2]


def apply_explicit_layout_to_designs(
    designs,
    distribution_layer,
    edge_layer,
    spacing=0.5,
    angles=None,
):
    angles = angles or DEFAULT
    allowed = []
    for x in angles:
        try:
            x = float(x)
        except Exception:
            continue
        if x in ALLOWED and x not in allowed:
            allowed.append(x)
    if not allowed:
        allowed = list(DEFAULT)
    try:
        spacing = max(0.01, float(spacing))
    except Exception:
        spacing = 0.5

    states = [
        (bool(d.get("written")), bool(d.get("needs_resync")))
        for d in designs or []
    ]

    base = _v2._base
    old_transition = base._transition_angle
    old_canonical = base._canonical_edge
    try:
        for d in designs or []:
            d["written"] = False
            d["needs_resync"] = True

        occupancy = _manual_only_layer(distribution_layer, designs)

        # The layout engine historically grouped by (Pole Edge FID + endpoints).
        # For explicit offsetting we intentionally group by physical geometry,
        # so different Pole Edge features with the same endpoints still overlap.
        base._canonical_edge = _geometry_only_canonical_edge
        base._transition_angle = _transition_factory(allowed)
        try:
            summary = _v2.apply_layout_to_designs(
                designs,
                occupancy,
                edge_layer,
                spacing=spacing,
            )
        finally:
            base._canonical_edge = old_canonical
            base._transition_angle = old_transition

        for d in designs or []:
            d["layout"] = {
                "version": 4,
                "spacing_m": round(spacing, 3),
                "angles_deg": [float(x) for x in sorted(allowed)],
            }
            d["written"] = False
            d["needs_resync"] = True

        summary["angles_deg"] = [float(x) for x in sorted(allowed)]
        return summary
    except Exception:
        base._canonical_edge = old_canonical
        base._transition_angle = old_transition
        for d, state in zip(designs or [], states):
            d["written"], d["needs_resync"] = state
        raise
