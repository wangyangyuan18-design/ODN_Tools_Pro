# -*- coding: utf-8 -*-
"""Explicit cable offset layout with user-selected transition angles.

The layout rule is deliberately local to the actually overlapping Pole Edge
sections:
- slot 0 stays on the original Pole Edge;
- a non-zero slot is a parallel route at ``slot * spacing``;
- entering/leaving an offset uses the selected fixed angle;
- changing between non-zero slots also uses a controlled transition;
- sections with no overlap are never carried over from a previous offset.

This module only wraps the existing global occupancy/slot engine.  It replaces
its point builder with a deterministic geometry builder so CRS conversion,
route order, and transition geometry are handled in one place.
"""
from math import hypot

from qgis.core import QgsFeature, QgsVectorLayer, QgsMessageLog, Qgis, QgsPointXY
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
    known = {str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
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
        f"[occupancy] total={all_count}; excluded_current={excluded_count}; "
        f"existing_used={len(fs)}; current_links={len(known)}"
    )
    return layer


def _transition_factory(angles):
    allowed = sorted(set(float(x) for x in (angles or DEFAULT) if float(x) in ALLOWED)) or list(DEFAULT)

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
    """Group by Pole Edge endpoint geometry, not the source feature FID."""
    def canonical(raw):
        edge = original_canonical(raw)
        if edge is None:
            return None
        return 0, edge[1], edge[2]
    return canonical


def _make_logged_assign_slots(original_assign):
    def assign(edge_users, edge_reserved, previous_slots):
        result = original_assign(edge_users, edge_reserved, previous_slots)
        nonzero = [int(v) for v in result.values() if int(v) != 0]
        if edge_reserved or nonzero:
            _log(
                f"[slot] users={len(edge_users)}; reserved={sorted(edge_reserved or set())}; "
                f"nonzero={nonzero}"
            )
        return result
    return assign


def _make_logged_occupancy(original_occupancy):
    def occupancy(edge_geom, spacing, index, geometries):
        reserved = original_occupancy(edge_geom, spacing, index, geometries)
        if reserved:
            _log(
                f"[occupancy-hit] edge_len={edge_geom.length():.2f}m; "
                f"reserved={sorted(reserved)}"
            )
        return reserved
    return occupancy


def _append(points, point, eps=1e-7):
    point = QgsPointXY(point)
    if not points or hypot(points[-1].x() - point.x(), points[-1].y() - point.y()) > eps:
        points.append(point)


def _point_at(a, b, distance):
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(a)
    f = max(0.0, min(1.0, float(distance) / length))
    return QgsPointXY(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f)


def _offset(point, a, b, distance):
    tx = b.x() - a.x()
    ty = b.y() - a.y()
    length = hypot(tx, ty)
    if length <= 1e-12 or abs(distance) <= 1e-12:
        return QgsPointXY(point)
    nx, ny = -ty / length, tx / length
    return QgsPointXY(point.x() + nx * distance, point.y() + ny * distance)


def _transition_run(offset_delta, angle_deg):
    """Run along Pole Edge required by a fixed transition angle."""
    if abs(offset_delta) <= 1e-12:
        return 0.0
    if float(angle_deg) >= 89.5:
        return 0.0
    import math
    return abs(offset_delta) / math.tan(math.radians(float(angle_deg)))


def _build_explicit_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
    """Build only local offset geometry; never propagate an old slot blindly.

    ``slots_by_edge`` is already the global occupancy result.  This function
    converts that result into one ordered polyline.  Every slot transition is
    handled at the exact Pole Edge node where the occupancy state changes.
    """
    base = _v2._base
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [e for raw in raw_edges if (e := base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [
            base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs)
            for p in stored if len(p) >= 2
        ]

    work_stored = [
        base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs)
        for p in stored if len(p) >= 2
    ]
    if len(work_stored) < 2:
        return work_stored

    nodes = base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        return work_stored

    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slots):
        return work_stored

    # The endpoint connectors are part of the original Link and must never be
    # translated.  Only the Pole Edge portion is offset.
    start_point = work_stored[0]
    end_point = work_stored[-1]
    result = []
    _append(result, start_point)

    # Enter the first occupied slot.
    first_slot = slots[0]
    first_a, first_b = nodes[0], nodes[1]
    if first_slot == 0:
        _append(result, first_a)
    else:
        angle = base._transition_angle(first_slot)
        run = _transition_run(first_slot * spacing, angle)
        run = min(run, hypot(first_b.x() - first_a.x(), first_b.y() - first_a.y()) * 0.45)
        p = _point_at(first_a, first_b, run)
        _append(result, _offset(p, first_a, first_b, first_slot * spacing))

    for i, slot in enumerate(slots):
        a, b = nodes[i], nodes[i + 1]
        length = hypot(b.x() - a.x(), b.y() - a.y())
        if length <= 1e-12:
            continue
        prev_slot = slots[i - 1] if i > 0 else first_slot

        if i > 0 and prev_slot != slot:
            # The state changes exactly at node ``a``.  Do not carry the old
            # offset into a non-overlapping edge.
            if prev_slot == 0 and slot != 0:
                _append(result, a)
                angle = base._transition_angle(slot)
                run = min(_transition_run(slot * spacing, angle), length * 0.45)
                p = _point_at(a, b, run)
                _append(result, _offset(p, a, b, slot * spacing))
            elif prev_slot != 0 and slot == 0:
                # Finish the previous parallel run at the node, then return to
                # the original Pole Edge using the same fixed-angle rule.
                pa, pb = nodes[i - 1], nodes[i]
                plen = hypot(pb.x() - pa.x(), pb.y() - pa.y())
                angle = base._transition_angle(prev_slot)
                run = min(_transition_run(prev_slot * spacing, angle), plen * 0.45)
                p = _point_at(pa, pb, max(0.0, plen - run))
                _append(result, _offset(p, pa, pb, prev_slot * spacing))
                _append(result, b)
            else:
                # Directly change between two parallel tracks.  The lateral
                # change is only the slot difference, never the absolute slot.
                _append(result, _offset(a, a, b, prev_slot * spacing))
                delta = (slot - prev_slot) * spacing
                angle = base._transition_angle(delta)
                run = min(_transition_run(delta, angle), length * 0.45)
                p = _point_at(a, b, run)
                _append(result, _offset(p, a, b, slot * spacing))

        if slot == 0:
            _append(result, b)
        else:
            _append(result, _offset(b, a, b, slot * spacing))

    # Leave the final parallel track and reconnect to the original endpoint.
    last_slot = slots[-1]
    if last_slot != 0:
        a, b = nodes[-2], nodes[-1]
        length = hypot(b.x() - a.x(), b.y() - a.y())
        angle = base._transition_angle(last_slot)
        run = min(_transition_run(last_slot * spacing, angle), length * 0.45)
        p = _point_at(a, b, max(0.0, length - run))
        _append(result, _offset(p, a, b, last_slot * spacing))
        _append(result, b)

    _append(result, end_point)
    return result


def _make_logged_build_points(original_build):
    # ``original_build`` is intentionally retained as a fallback for malformed
    # legacy segments.  Valid routes use the explicit local-transition builder.
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        slots = [
            int(slots_by_edge.get(i, 0))
            for i in range(len(segment.get("edge_sequence", []) or []))
        ]
        try:
            result = _build_explicit_points(
                segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
            )
        except Exception as exc:
            _log(
                f"[geometry-error] from={segment.get('from', '?')}; "
                f"to={segment.get('to', '?')}; edge_count={len(slots)}; "
                f"type={type(exc).__name__}: {exc}; fallback=legacy"
            )
            result = original_build(
                segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
            )
        if any(slot != 0 for slot in slots):
            _log(
                f"[geometry] from={segment.get('from', '?')}; to={segment.get('to', '?')}; "
                f"offset_slots={slots}; output_points={len(result)}"
            )
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

    states = [(bool(d.get("written")), bool(d.get("needs_resync"))) for d in designs or []]
    base = _v2._base
    old_transition = base._transition_angle
    old_canonical = base._canonical_edge
    old_assign = base._assign_slots
    old_occupancy = base._existing_slot_occupancy
    old_build = base._build_segment_points

    try:
        _log("========== Offset START ==========")
        _log(
            f"[input] designs={len(designs or [])}; DC={distribution_layer.featureCount()}; "
            f"PoleEdge={edge_layer.featureCount()}; spacing={spacing:.3f}m; "
            f"angles={sorted(allowed)}; edge_crs={edge_layer.crs().authid()}; "
            f"dc_crs={distribution_layer.crs().authid()}"
        )

        invalid_designs = 0
        total_segments = 0
        total_edges = 0
        for design in designs or []:
            segs = design.get("segments", []) or []
            total_segments += len(segs)
            total_edges += sum(len(s.get("edge_sequence", []) or []) for s in segs)
            if not segs or any(not (s.get("edge_sequence", []) or []) for s in segs):
                invalid_designs += 1
        _log(
            f"[routes] segments={total_segments}; edge_refs={total_edges}; "
            f"designs_missing_edge_refs={invalid_designs}"
        )

        for d in designs or []:
            d["written"] = False
            d["needs_resync"] = True

        occupancy = _manual_only_layer(distribution_layer, designs)
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

        _log(
            f"[result] changed_designs={summary.get('changed_designs', 0)}; "
            f"changed_indices={summary.get('changed_indices', [])}; "
            f"extra_length_m={summary.get('extra_length_m', 0)}"
        )
        _log("========== Offset END ==========")

        for d in designs or []:
            d["layout"] = {
                "version": 5,
                "spacing_m": round(spacing, 3),
                "angles_deg": [float(x) for x in sorted(allowed)],
                "rule": "local_overlap_parallel_transition",
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
