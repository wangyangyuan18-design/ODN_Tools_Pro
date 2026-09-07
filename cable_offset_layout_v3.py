# -*- coding: utf-8 -*-
"""Explicit cable offset layout with user-selected transition angles.

This wrapper adds diagnostic logging around the existing geometry engine.
"""
from qgis.core import QgsFeature, QgsVectorLayer, QgsMessageLog, Qgis
from . import cable_offset_layout_v2 as _v2

ALLOWED = (45.0, 60.0, 75.0, 90.0)
DEFAULT = (60.0, 75.0, 90.0)
LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, Qgis.Info)
    except Exception:
        pass


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
    all_count = 0
    excluded_count = 0
    for feature in distribution_layer.getFeatures():
        all_count += 1
        if idx >= 0 and feature.attribute(idx) and str(feature.attribute(idx)) in known:
            excluded_count += 1
            continue
        clone = QgsFeature()
        clone.setGeometry(feature.geometry())
        fs.append(clone)
    if fs:
        layer.dataProvider().addFeatures(fs)
    layer.updateExtents()
    _log(
        f"[occupancy] DC总要素={all_count}, 排除本次设计Link={excluded_count}, "
        f"作为既有占用参与计算={len(fs)}, Link IDs={len(known)}"
    )
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


def _make_geometry_only_canonical_edge(original_canonical):
    """Keep edge endpoints as identity while discarding source Pole Edge FID."""
    def canonical(raw):
        edge = original_canonical(raw)
        if edge is None:
            return None
        return 0, edge[1], edge[2]
    return canonical


def _make_logged_assign_slots(original_assign):
    def assign(edge_users, edge_reserved, previous_slots):
        result = original_assign(edge_users, edge_reserved, previous_slots)
        if len(edge_users) > 1 or edge_reserved:
            sample = edge_users[0].edge_key if edge_users else None
            _log(
                f"[slot] edge={sample}; users={len(edge_users)}; reserved={sorted(edge_reserved or set())}; "
                f"assigned={result}; previous={dict(previous_slots)}"
            )
        return result
    return assign


def _make_logged_occupancy(original_occupancy):
    def occupancy(edge_geom, spacing, index, geometries):
        reserved = original_occupancy(edge_geom, spacing, index, geometries)
        if reserved:
            _log(
                f"[existing-occupancy] edge_len={edge_geom.length():.3f}m; "
                f"spacing={spacing:.3f}m; reserved_slots={sorted(reserved)}"
            )
        return reserved
    return occupancy


def _make_logged_build_points(original_build):
    def build(segment, slots_by_edge, spacing, work_crs, source_crs):
        slots = [int(slots_by_edge.get(i, 0)) for i in range(len(segment.get("edge_sequence", []) or []))]
        result = original_build(segment, slots_by_edge, spacing, work_crs, source_crs)
        if any(slot != 0 for slot in slots):
            _log(f"[geometry] nonzero slots={slots}; output_points={len(result)}")
        else:
            _log(f"[geometry] all slots=0; preserve original points; edge_count={len(slots)}")
        return result
    return build


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
    old_assign = base._assign_slots
    old_occupancy = base._existing_slot_occupancy
    old_build = base._build_segment_points

    try:
        _log("========== Offset run START ==========")
        _log(
            f"[input] designs={len(designs or [])}; distribution_features={distribution_layer.featureCount()}; "
            f"pole_edge_features={edge_layer.featureCount()}; spacing={spacing:.3f}m; "
            f"angles={sorted(allowed)}; edge_crs={edge_layer.crs().authid()}; dc_crs={distribution_layer.crs().authid()}"
        )
        for i, design in enumerate(designs or []):
            segs = design.get("segments", []) or []
            total_edges = sum(len(s.get("edge_sequence", []) or []) for s in segs)
            _log(
                f"[design {i}] link_id={design.get('_link_id')}; fdt={design.get('fdt')}; "
                f"link={design.get('link')}; written={design.get('written')}; needs_resync={design.get('needs_resync')}; "
                f"segments={len(segs)}; edge_refs={total_edges}"
            )

        for d in designs or []:
            d["written"] = False
            d["needs_resync"] = True

        occupancy = _manual_only_layer(distribution_layer, designs)

        # For explicit offsetting, group by Pole Edge geometry rather than source FID.
        geometry_canonical = _make_geometry_only_canonical_edge(old_canonical)
        base._canonical_edge = geometry_canonical
        base._transition_angle = _transition_factory(allowed)
        base._assign_slots = _make_logged_assign_slots(old_assign)
        base._existing_slot_occupancy = _make_logged_occupancy(old_occupancy)
        base._build_segment_points = _make_logged_build_points(old_build)

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
            base._assign_slots = old_assign
            base._existing_slot_occupancy = old_occupancy
            base._build_segment_points = old_build

        _log(f"[summary] {summary}")
        _log("========== Offset run END ==========")

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
    except Exception as exc:
        _log(f"[ERROR] {type(exc).__name__}: {exc}")
        base._canonical_edge = old_canonical
        base._transition_angle = old_transition
        base._assign_slots = old_assign
        base._existing_slot_occupancy = old_occupancy
        base._build_segment_points = old_build
        for d, state in zip(designs or [], states):
            d["written"], d["needs_resync"] = state
        raise
