# -*- coding: utf-8 -*-
"""Clean continuous offset geometry for the global lane allocator.

This module deliberately replaces the legacy edge-by-edge corner builder.
For a Link that keeps the same lane through a Pole Edge corner, the output is
built as one continuous offset polyline and uses a miter/bevel join. No
control-distance point is inserted at a same-lane corner and the route never
returns to the original Pole Edge node just to leave it again.
"""

from math import hypot

from qgis.core import QgsPointXY

from . import cable_offset_layout as _base


def _append(points, point, eps=1e-7):
    point = QgsPointXY(point)
    if not points or hypot(points[-1].x() - point.x(), points[-1].y() - point.y()) > eps:
        points.append(point)


def _replace_last(points, point):
    point = QgsPointXY(point)
    if points:
        points[-1] = point
    else:
        points.append(point)


def _intersection(p1, p2, q1, q2):
    rx = p2.x() - p1.x()
    ry = p2.y() - p1.y()
    sx = q2.x() - q1.x()
    sy = q2.y() - q1.y()
    den = rx * sy - ry * sx
    scale = max(hypot(rx, ry) * hypot(sx, sy), 1.0)
    if abs(den) <= 1e-10 * scale:
        return None
    qpx = q1.x() - p1.x()
    qpy = q1.y() - p1.y()
    t = (qpx * sy - qpy * sx) / den
    return QgsPointXY(p1.x() + t * rx, p1.y() + t * ry)


def _same_lane_join(node, prev_a, prev_b, next_a, next_b, slot, spacing):
    if slot == 0:
        return QgsPointXY(node)
    prev_t = _base._unit(prev_a, prev_b)
    next_t = _base._unit(next_a, next_b)
    d = float(slot) * float(spacing)
    p1 = _base._offset_point(node, prev_t, d)
    p2 = _base._offset_point(
        QgsPointXY(prev_a.x() + prev_t[0], prev_a.y() + prev_t[1]),
        prev_t,
        d,
    )
    q1 = _base._offset_point(node, next_t, d)
    q2 = _base._offset_point(
        QgsPointXY(next_a.x() + next_t[0], next_a.y() + next_t[1]),
        next_t,
        d,
    )
    hit = _intersection(p1, p2, q1, q2)
    if hit is not None:
        distance = hypot(hit.x() - node.x(), hit.y() - node.y())
        if distance <= max(3.0 * abs(d), 5.0 * max(float(spacing), 0.01)):
            return hit
    # Bevel fallback: still stays on the offset lanes and never returns to node.
    return (
        _base._offset_point(node, prev_t, d),
        _base._offset_point(node, next_t, d),
    )


def _transition_entry(a, b, slot, spacing, fraction_cap=0.45):
    d = abs(int(slot)) * float(spacing)
    if slot == 0 or d <= 1e-12:
        return QgsPointXY(a)
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(a)
    angle = _base._transition_angle(slot)
    run = min(_base._transition_run(d, angle), length * fraction_cap)
    center = _base._point_along(a, b, run)
    return _base._offset_point(center, _base._unit(a, b), int(slot) * float(spacing))


def _transition_exit(a, b, slot, spacing, fraction_cap=0.45):
    d = abs(int(slot)) * float(spacing)
    if slot == 0 or d <= 1e-12:
        return QgsPointXY(b)
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if length <= 1e-12:
        return QgsPointXY(b)
    angle = _base._transition_angle(slot)
    run = min(_base._transition_run(d, angle), length * fraction_cap)
    center = _base._point_along(a, b, max(0.0, length - run))
    return _base._offset_point(center, _base._unit(a, b), int(slot) * float(spacing))


def _entry_point(nodes, slots, i, start_point, spacing):
    a, b = nodes[i], nodes[i + 1]
    slot = slots[i]
    if i == 0:
        if slot == 0:
            return QgsPointXY(a)
        return _transition_entry(a, b, slot, spacing)

    prev_slot = slots[i - 1]
    node = a
    if prev_slot == slot:
        if slot == 0:
            return QgsPointXY(node)
        prev_a, prev_b = nodes[i - 1], nodes[i]
        return _same_lane_join(node, prev_a, prev_b, a, b, slot, spacing)

    # Genuine lane change: start the new lane after the node.  The preceding
    # edge ends before the node, and the two points form one clean diagonal.
    if slot == 0:
        return QgsPointXY(node)
    return _transition_entry(a, b, slot, spacing)


def _exit_point(nodes, slots, i, end_point, spacing):
    a, b = nodes[i], nodes[i + 1]
    slot = slots[i]
    if i == len(slots) - 1:
        if slot == 0:
            return QgsPointXY(b)
        return _transition_exit(a, b, slot, spacing)

    next_slot = slots[i + 1]
    node = b
    if slot == next_slot:
        if slot == 0:
            return QgsPointXY(node)
        next_a, next_b = nodes[i + 1], nodes[i + 2]
        return _same_lane_join(node, a, b, next_a, next_b, slot, spacing)

    # Genuine lane change: finish the old lane before the node.  Do not append
    # the original node as a separate backtracking waypoint.
    if slot == 0:
        return QgsPointXY(node)
    return _transition_exit(a, b, slot, spacing)


def build_continuous_segment_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [
            _base._transform_point(
                QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs
            )
            for p in stored if len(p) >= 2
        ]

    stored_work = [
        _base._transform_point(
            QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs
        )
        for p in stored if len(p) >= 2
    ]
    if len(stored_work) < 2:
        return stored_work

    nodes = _base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        return stored_work

    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slot != 0 for slot in slots):
        return stored_work

    result = []
    start_point = stored_work[0]
    end_point = stored_work[-1]
    _append(result, start_point)

    for i in range(len(edges)):
        entry = _entry_point(nodes, slots, i, start_point, spacing)
        exit_ = _exit_point(nodes, slots, i, end_point, spacing)

        # Same-lane corner: entry/exit already contain the exact continuous
        # join.  Lane-change corner: entry and exit deliberately live on the
        # two adjacent offset lanes and are connected directly.
        _append(result, entry)
        _append(result, exit_)

    _append(result, end_point)
    return result


def make_build_wrapper(original):
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        try:
            return build_continuous_segment_points(
                segment,
                slots_by_edge,
                spacing,
                work_crs,
                source_crs,
                edge_crs,
            )
        except Exception:
            return original(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs)
    return build
