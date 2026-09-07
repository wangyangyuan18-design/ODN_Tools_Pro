# -*- coding: utf-8 -*-
"""Global lane assignment and continuous corner geometry for cable offset.

Pole Edge offsets are ordered lanes: 0, +1, -1, +2, -2, ... . Allocation is
global for the complete Link set. The longest directional Link gets highest
priority; each shared Pole Edge is packed from lane 0 outward in that fixed
priority order, so empty higher-priority lanes are filled automatically.

A Link that keeps the same lane through a Pole Edge corner uses the
intersection of the two offset edge lines. The control distance is used only
for genuine lane changes and is never inserted as a forced backtrack at a
same-lane corner.
"""

from math import hypot

from qgis.core import QgsGeometry, QgsPointXY

from . import cable_offset_layout as _base


def _reserved_for_edge(edge_key, edge_crs, work_crs, spacing, existing_index, existing_geometries, cache):
    if edge_key in cache:
        return set(cache[edge_key])
    a, b = _base._edge_points(edge_key)
    a = _base._transform_point(a, edge_crs, work_crs)
    b = _base._transform_point(b, edge_crs, work_crs)
    geom = QgsGeometry.fromPolylineXY([a, b])
    reserved = _base._existing_slot_occupancy(
        geom, spacing, existing_index, existing_geometries
    )
    cache[edge_key] = set(reserved)
    return set(reserved)


def _collect_uses(designs, edge_crs, work_crs):
    pending = []
    routes = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        route_length = 0.0
        seen_edges = set()
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            raw_edges = segment.get("edge_sequence", []) or []
            parsed_edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
            points = segment.get("points", []) or []
            if not parsed_edges or len(points) < 2:
                continue
            nodes = _base._extract_route_graph_nodes(
                segment, work_crs, edge_crs, edge_crs
            )
            if len(nodes) != len(parsed_edges) + 1:
                continue
            start_point = _base._transform_point(
                QgsPointXY(float(points[0][0]), float(points[0][1])),
                edge_crs,
                work_crs,
            )
            end_point = _base._transform_point(
                QgsPointXY(float(points[-1][0]), float(points[-1][1])),
                edge_crs,
                work_crs,
            )
            for edge_index, edge in enumerate(parsed_edges):
                a, b = _base._edge_points(edge)
                a = _base._transform_point(a, edge_crs, work_crs)
                b = _base._transform_point(b, edge_crs, work_crs)
                use = _base._LayoutUse(
                    design_index,
                    segment_index,
                    edge_index,
                    edge,
                    start_point,
                    end_point,
                    _base._route_side_hint(start_point, end_point, a, b),
                )
                if edge_index > 0:
                    use.prev_edge_key = parsed_edges[edge_index - 1]
                pending.append(use)
                if edge not in seen_edges:
                    route_length += hypot(b.x() - a.x(), b.y() - a.y())
                    seen_edges.add(edge)
        routes[design_index] = {
            "route_length": route_length,
            "use_count": sum(1 for item in pending if item.design_index == design_index),
        }
    return pending, routes


def _packed_lane_candidates(side_hint, limit=20):
    """Return lanes in center-out order: 0, nearest side pair, next pair."""
    yield 0
    for magnitude in range(1, limit + 1):
        if side_hint < 0:
            yield -magnitude
            yield magnitude
        else:
            yield magnitude
            yield -magnitude


def _make_global_assigner(slot_map, priority_by_design, counters):
    def assign(edge_users, edge_reserved, previous_slots):
        assigned = {}
        counters["edges"] += 1
        counters["overlap_edges"] += int(len(edge_users) > 1 or bool(edge_reserved))
        for use in sorted(
            edge_users,
            key=lambda item: priority_by_design.get(
                item.design_index, (float("inf"), item.design_index)
            ),
        ):
            key = (use.design_index, use.segment_index, use.edge_index)
            slot = int(slot_map.get(key, 0))
            use.slot = slot
            assigned[use.design_index] = slot
            counters["slots"][slot] = counters["slots"].get(slot, 0) + 1
        return assigned
    return assign


def make_global_slot_assigner(designs, distribution_layer, edge_layer, spacing, counters):
    edge_crs = edge_layer.crs()
    work_crs = _base._choose_work_crs(edge_layer)
    pending, routes = _collect_uses(designs, edge_crs, work_crs)

    users_by_edge = {}
    uses_by_design = {}
    for use in pending:
        users_by_edge.setdefault(use.edge_key, []).append(use)
        uses_by_design.setdefault(use.design_index, []).append(use)

    # Longer directional coverage wins. This is the global "longest Link gets
    # the main lane" priority, not the post-offset cable length.
    ordered_designs = sorted(
        uses_by_design,
        key=lambda di: (
            -float(routes.get(di, {}).get("route_length", 0.0)),
            -int(routes.get(di, {}).get("use_count", 0)),
            int(di),
        ),
    )
    priority_by_design = {di: (rank, di) for rank, di in enumerate(ordered_designs)}

    existing_index, existing_geometries = _base._build_existing_index(
        distribution_layer, work_crs
    )
    reserved_cache = {}
    slot_map = {}

    # Pack every shared edge independently from the center lane outward, but
    # always using the same global Link priority. Thus a gap in lane 0, +1, -1
    # is filled by the highest-priority user of that edge.
    for edge_key, users in users_by_edge.items():
        reserved = _reserved_for_edge(
            edge_key,
            edge_crs,
            work_crs,
            spacing,
            existing_index,
            existing_geometries,
            reserved_cache,
        )
        used = set(reserved)
        for use in sorted(
            users,
            key=lambda item: priority_by_design.get(
                item.design_index, (float("inf"), item.design_index)
            ),
        ):
            chosen = None
            for candidate in _packed_lane_candidates(use.side_hint):
                if candidate not in used:
                    chosen = candidate
                    break
            if chosen is None:
                magnitude = max([abs(v) for v in used] + [0]) + 1
                chosen = magnitude if use.side_hint >= 0 else -magnitude
                while chosen in used:
                    magnitude += 1
                    chosen = magnitude if use.side_hint >= 0 else -magnitude
            slot_map[(use.design_index, use.segment_index, use.edge_index)] = int(chosen)
            use.slot = int(chosen)
            used.add(int(chosen))

    return _make_global_assigner(slot_map, priority_by_design, counters), {
        "routes": routes,
        "ordered_designs": ordered_designs,
        "slot_map": slot_map,
    }


def _line_intersection(p1, p2, q1, q2):
    rx = p2.x() - p1.x()
    ry = p2.y() - p1.y()
    sx = q2.x() - q1.x()
    sy = q2.y() - q1.y()
    denominator = rx * sy - ry * sx
    scale = max(hypot(rx, ry) * hypot(sx, sy), 1.0)
    if abs(denominator) <= 1e-10 * scale:
        return None
    qpx = q1.x() - p1.x()
    qpy = q1.y() - p1.y()
    t = (qpx * sy - qpy * sx) / denominator
    return QgsPointXY(p1.x() + t * rx, p1.y() + t * ry)


def _corner_join(node, prev_a, prev_b, next_a, next_b, signed_offset, spacing):
    if abs(signed_offset) <= 1e-12:
        return QgsPointXY(node)
    prev_t = _base._unit(prev_a, prev_b)
    next_t = _base._unit(next_a, next_b)
    p1 = _base._offset_point(node, prev_t, signed_offset)
    p2 = _base._offset_point(
        QgsPointXY(prev_a.x() + prev_t[0], prev_a.y() + prev_t[1]),
        prev_t,
        signed_offset,
    )
    q1 = _base._offset_point(node, next_t, signed_offset)
    q2 = _base._offset_point(
        QgsPointXY(next_a.x() + next_t[0], next_a.y() + next_t[1]),
        next_t,
        signed_offset,
    )
    intersection = _line_intersection(p1, p2, q1, q2)
    if intersection is not None:
        distance = hypot(intersection.x() - node.x(), intersection.y() - node.y())
        if distance <= max(3.0 * abs(signed_offset), 5.0 * max(spacing, 0.01)):
            return intersection
    return (
        _base._offset_point(node, prev_t, signed_offset),
        _base._offset_point(node, next_t, signed_offset),
    )


def _append(result, point, eps=1e-7):
    point = QgsPointXY(point)
    if not result or hypot(result[-1].x() - point.x(), result[-1].y() - point.y()) > eps:
        result.append(point)


def _replace_last(result, point):
    point = QgsPointXY(point)
    if result:
        result[-1] = point
    else:
        result.append(point)


def _build_continuous_segment_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [parsed for raw in raw_edges if (parsed := _base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [
            _base._transform_point(
                QgsPointXY(float(raw[0]), float(raw[1])), source_crs, work_crs
            )
            for raw in stored if len(raw) >= 2
        ]
    work_stored = [
        _base._transform_point(
            QgsPointXY(float(raw[0]), float(raw[1])), source_crs, work_crs
        )
        for raw in stored if len(raw) >= 2
    ]
    if len(work_stored) < 2:
        return work_stored
    nodes = _base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        return work_stored
    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slot != 0 for slot in slots):
        return work_stored

    result = []
    start_point, end_point = work_stored[0], work_stored[-1]
    first_slot = slots[0]
    first_a, first_b = nodes[0], nodes[1]
    first_len = hypot(first_b.x() - first_a.x(), first_b.y() - first_a.y())
    if first_slot == 0:
        _append(result, start_point)
        _append(result, first_a)
    else:
        _append(result, start_point)
        angle = _base._transition_angle(first_slot)
        run = min(_base._transition_run(abs(first_slot) * spacing, angle), first_len * 0.45)
        fraction = run / first_len if first_len > 1e-12 else 0.0
        center = _base._point_along(first_a, first_b, fraction)
        _append(result, _base._offset_point(center, _base._unit(first_a, first_b), first_slot * spacing))

    for i in range(len(edges)):
        a, b = nodes[i], nodes[i + 1]
        t = _base._unit(a, b)
        slot = slots[i]
        offset_b = _base._offset_point(b, t, slot * spacing)
        if i > 0:
            prev_slot = slots[i - 1]
            prev_a, prev_b = nodes[i - 1], nodes[i]
            prev_t = _base._unit(prev_a, prev_b)
            if prev_slot == slot:
                if slot == 0:
                    _replace_last(result, a)
                else:
                    join = _corner_join(a, prev_a, prev_b, a, b, slot * spacing, spacing)
                    if isinstance(join, tuple):
                        _replace_last(result, join[0])
                        _append(result, join[1])
                    else:
                        _replace_last(result, join)
            elif prev_slot == 0 and slot != 0:
                _append(result, a)
                delta = abs(slot) * spacing
                angle = _base._transition_angle(slot)
                edge_len = hypot(b.x() - a.x(), b.y() - a.y())
                run = min(_base._transition_run(delta, angle), edge_len * 0.45)
                fraction = run / edge_len if edge_len > 1e-12 else 0.0
                p = _base._point_along(a, b, fraction)
                _append(result, _base._offset_point(p, t, slot * spacing))
            elif prev_slot != 0 and slot == 0:
                delta = abs(prev_slot) * spacing
                angle = _base._transition_angle(prev_slot)
                prev_len = hypot(prev_b.x() - prev_a.x(), prev_b.y() - prev_a.y())
                run = min(_base._transition_run(delta, angle), prev_len * 0.45)
                fraction = 1.0 - run / prev_len if prev_len > 1e-12 else 1.0
                p = _base._point_along(prev_a, prev_b, fraction)
                _replace_last(result, _base._offset_point(p, prev_t, prev_slot * spacing))
                _append(result, a)
            else:
                delta = abs(prev_slot - slot) * spacing
                angle = _base._transition_angle(prev_slot - slot)
                prev_len = hypot(prev_b.x() - prev_a.x(), prev_b.y() - prev_a.y())
                next_len = hypot(b.x() - a.x(), b.y() - a.y())
                run_prev = min(_base._transition_run(delta, angle), prev_len * 0.35)
                run_next = min(_base._transition_run(delta, angle), next_len * 0.35)
                frac_prev = 1.0 - run_prev / prev_len if prev_len > 1e-12 else 1.0
                frac_next = run_next / next_len if next_len > 1e-12 else 0.0
                p1 = _base._offset_point(_base._point_along(prev_a, prev_b, frac_prev), prev_t, prev_slot * spacing)
                p2 = _base._offset_point(_base._point_along(a, b, frac_next), t, slot * spacing)
                _replace_last(result, p1)
                _append(result, p2)

        if i < len(edges) - 1:
            _append(result, b if slot == 0 else offset_b)
        elif slot == 0:
            _append(result, b)
            _append(result, end_point)
        else:
            final_len = hypot(b.x() - a.x(), b.y() - a.y())
            angle = _base._transition_angle(slot)
            run = min(_base._transition_run(abs(slot) * spacing, angle), final_len * 0.45)
            fraction = 1.0 - run / final_len if final_len > 1e-12 else 0.5
            exit_center = _base._point_along(a, b, fraction)
            exit_point = _base._offset_point(exit_center, t, slot * spacing)
            _replace_last(result, exit_point)
            _append(result, end_point)

    return result


def make_build_wrapper(original):
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        try:
            return _build_continuous_segment_points(
                segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
            )
        except Exception:
            return original(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs)
    return build
