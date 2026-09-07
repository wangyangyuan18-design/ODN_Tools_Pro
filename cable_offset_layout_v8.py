# -*- coding: utf-8 -*-
"""Clean continuous offset geometry for the global lane allocator.

This module is the active geometry builder for the offset writer.

Rules:
- A Link that keeps the same lane across a Pole Edge corner uses one
  continuous offset join (miter, with bevel fallback).
- A same-lane corner does NOT insert the original Pole Edge node as an
  intermediate waypoint and does NOT use the 0.30 m lane-change control run.
- The control distance is used only when the lane number actually changes.
- Geometry errors are raised instead of silently falling back to the legacy
  edge-by-edge builder, because that fallback could recreate the old
  A -> control point -> B -> control point -> C geometry.
"""

from math import hypot

from qgis.core import QgsMessageLog, QgsPointXY, Qgis

from . import cable_offset_layout as _base

LOG_TAG = "ODN_Tools_Pro / Cable Offset"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), LOG_TAG, level)
    except Exception:
        pass


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
    """Construct the geometric join of two offset lines at a corner.

    No original node is emitted. The result is either the line intersection
    (miter join) or two lane points (bevel fallback), both already offset from
    the Pole Edge corner.
    """
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
        # Reject pathological miters on extremely acute turns. The bevel
        # remains on the two correct offset lines and never goes through node.
        if distance <= max(3.0 * abs(d), 5.0 * max(float(spacing), 0.01)):
            return hit

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
    center = _base._point_along(a, b, run / length)
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
    center = _base._point_along(a, b, max(0.0, 1.0 - run / length))
    return _base._offset_point(center, _base._unit(a, b), int(slot) * float(spacing))


def _entry_point(nodes, slots, i, spacing):
    a, b = nodes[i], nodes[i + 1]
    slot = slots[i]
    if i == 0:
        return QgsPointXY(a) if slot == 0 else _transition_entry(a, b, slot, spacing)

    prev_slot = slots[i - 1]
    node = a
    if prev_slot == slot:
        if slot == 0:
            return QgsPointXY(node)
        prev_a, prev_b = nodes[i - 1], nodes[i]
        return _same_lane_join(node, prev_a, prev_b, a, b, slot, spacing)

    # Genuine lane change: no forced stop/backtrack through the node.
    if slot == 0:
        return QgsPointXY(node)
    return _transition_entry(a, b, slot, spacing)


def _exit_point(nodes, slots, i, spacing):
    a, b = nodes[i], nodes[i + 1]
    slot = slots[i]
    if i == len(slots) - 1:
        return QgsPointXY(b) if slot == 0 else _transition_exit(a, b, slot, spacing)

    next_slot = slots[i + 1]
    node = b
    if slot == next_slot:
        if slot == 0:
            return QgsPointXY(node)
        next_a, next_b = nodes[i + 1], nodes[i + 2]
        return _same_lane_join(node, a, b, next_a, next_b, slot, spacing)

    # Genuine lane change: finish the old lane before the node.
    if slot == 0:
        return QgsPointXY(node)
    return _transition_exit(a, b, slot, spacing)


def build_continuous_segment_points(
    segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
):
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

    nodes = _base._extract_route_graph_nodes(
        segment, work_crs, source_crs, edge_crs
    )
    if len(nodes) != len(edges) + 1:
        raise RuntimeError(
            "v8 continuous corner builder: edge_sequence 与 route nodes 数量不一致"
        )

    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]

    # Lane 0 is the authoritative original route. Do not rebuild it and do not
    # add any corner control points.
    if not any(slot != 0 for slot in slots):
        return stored_work

    result = []
    _append(result, stored_work[0])

    for i in range(len(edges)):
        entry = _entry_point(nodes, slots, i, spacing)
        exit_ = _exit_point(nodes, slots, i, spacing)

        # For a same-lane corner, entry == exit (the miter point) or they are
        # the two bevel endpoints. In neither case is the original node added.
        _append(result, entry)
        _append(result, exit_)

        if i > 0 and slots[i - 1] == slots[i] and slots[i] != 0:
            _log(
                f"[corner-v8] edge={i-1}->{i}; slot={slots[i]}; "
                "mode=same_lane_continuous_join; control_distance=NOT_USED"
            )
        elif i > 0 and slots[i - 1] != slots[i]:
            _log(
                f"[corner-v8] edge={i-1}->{i}; "
                f"slot_change={slots[i - 1]}->{slots[i]}; "
                "mode=genuine_lane_change"
            )

    _append(result, stored_work[-1])
    return result


def make_build_wrapper(original):
    """Return the active builder without a silent legacy-geometry fallback."""
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        # Preserve the original builder only for genuinely non-routable or
        # non-offset data; valid offset geometry errors must propagate so v3
        # can abort rather than silently recreating the old corner path.
        return build_continuous_segment_points(
            segment,
            slots_by_edge,
            spacing,
            work_crs,
            source_crs,
            edge_crs,
        )
    return build
