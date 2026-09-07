# -*- coding: utf-8 -*-
"""Global lane assignment and continuous corner geometry for cable offset.

The active Link Design offset model treats Pole Edge offsets as ordered lanes:
0, +1, -1, +2, -2, ... .  Lane allocation is global for the whole set of
Links instead of being decided independently edge-by-edge.

Priority rules:
1. Longer continuous Link routes get priority.
2. A higher-priority Link occupies lane 0 whenever that lane is available.
3. Lower-priority Links use the nearest free lane, preferring their previous
   lane so a Link remains continuous.
4. Existing Distribution Cable lanes remain reserved.

Geometry rule:
A Link that keeps the same lane through a Pole Edge corner uses the
intersection of the two offset edge lines (miter/join).  The fixed 0.30 m
control distance is not inserted as a mandatory backtracking point at such a
corner.  Control-distance transitions remain only for actual lane changes.
"""

from math import hypot

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
)

from . import cable_offset_layout as _base


def _edge_key(raw):
    return _base._canonical_edge(raw)


def _edge_length(edge, edge_crs, work_crs):
    a, b = _base._edge_points(edge)
    a = _base._transform_point(a, edge_crs, work_crs)
    b = _base._transform_point(b, edge_crs, work_crs)
    return hypot(b.x() - a.x(), b.y() - a.y())


def _build_existing_slot_cache(distribution_layer, edge_layer, work_crs, spacing):
    index, geometries = _base._build_existing_index(distribution_layer, work_crs)
    cache = {}
    return index, geometries, cache


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
        segments = design.get("segments", []) or []
        route_length = 0.0
        seen_edges = set()

        for segment_index, segment in enumerate(segments):
            raw_edges = segment.get("edge_sequence", []) or []
            parsed_edges = [
                parsed for raw in raw_edges if (parsed := _edge_key(raw))
            ]
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
                hint = _base._route_side_hint(start_point, end_point, a, b)
                use = _base._LayoutUse(
                    design_index,
                    segment_index,
                    edge_index,
                    edge,
                    start_point,
                    end_point,
                    hint,
                )
                if edge_index > 0:
                    use.prev_edge_key = parsed_edges[edge_index - 1]
                pending.append(use)
                if edge not in seen_edges:
                    route_length += hypot(b.x() - a.x(), b.y() - a.y())
                    seen_edges.add(edge)

        routes[design_index] = {
            "route_length": route_length,
            "use_count": sum(
                1 for item in pending if item.design_index == design_index
            ),
        }

    return pending, routes


def _candidate_slots(previous_slot, side_hint, limit=20):
    """Nearest-slot order with continuity first and lane 0 as the global center."""
    if previous_slot is not None:
        yield int(previous_slot)

    yielded = set()
    if previous_slot is not None:
        yielded.add(int(previous_slot))

    yield_order = [0]
    for magnitude in range(1, limit + 1):
        if side_hint >= 0:
            yield_order.extend((magnitude, -magnitude))
        else:
            yield_order.extend((-magnitude, magnitude))

    for slot in yield_order:
        if slot not in yielded:
            yielded.add(slot)
            yield slot


def _make_global_assigner(slot_map, priority_by_design, counters):
    def assign(edge_users, edge_reserved, previous_slots):
        assigned = {}
        counters["edges"] += 1
        counters["overlap_edges"] += int(
            len(edge_users) > 1 or bool(edge_reserved)
        )

        # The final slot for every (design, segment, edge) was computed from
        # the complete route set.  Returning that decision here keeps the old
        # validated writer unchanged.
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

    # Longer directional route first.  Number of covered Pole Edge segments
    # and design index provide deterministic tie-breakers.
    ordered_designs = sorted(
        uses_by_design,
        key=lambda di: (
            -float(routes.get(di, {}).get("route_length", 0.0)),
            -int(routes.get(di, {}).get("use_count", 0)),
            int(di),
        ),
    )
    priority_by_design = {
        di: (rank, di) for rank, di in enumerate(ordered_designs)
    }

    existing_index, existing_geometries, reserved_cache = _build_existing_slot_cache(
        distribution_layer, edge_layer, work_crs, spacing
    )
    occupied_by_edge = {}
    slot_map = {}

    # Process each complete Link in priority order.  This lets the longest
    # route claim lane 0 first, while shorter routes fill holes around it.
    for design_index in ordered_designs:
        route_uses = sorted(
            uses_by_design.get(design_index, []),
            key=lambda item: (item.segment_index, item.edge_index),
        )
        previous = None
        for use in route_uses:
            reserved = _reserved_for_edge(
                use.edge_key,
                edge_crs,
                work_crs,
                spacing,
                existing_index,
                existing_geometries,
                reserved_cache,
            )
            used = set(reserved)
            used.update(occupied_by_edge.get(use.edge_key, set()))

            chosen = None
            for candidate in _candidate_slots(previous, use.side_hint):
                if candidate not in used:
                    chosen = candidate
                    break

            if chosen is None:
                chosen = 0
                while chosen in used:
                    chosen += 1

            key = (use.design_index, use.segment_index, use.edge_index)
            slot_map[key] = int(chosen)
            occupied_by_edge.setdefault(use.edge_key, set()).add(int(chosen))
            previous = int(chosen)

    # Diagnostic summary: only the longest routes are logged by the caller.
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


def _offset_line_intersection(node, prev_a, prev_b, next_a, next_b, signed_offset):
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
    return _line_intersection(p1, p2, q1, q2)


def _corner_join(node, prev_a, prev_b, next_a, next_b, signed_offset, spacing):
    """Join same-lane offset edges without inserting a backtracking point."""
    if abs(signed_offset) <= 1e-12:
        return QgsPointXY(node)

    intersection = _offset_line_intersection(
        node,
        prev_a,
        prev_b,
        next_a,
        next_b,
        signed_offset,
    )
    if intersection is not None:
        distance = hypot(
            intersection.x() - node.x(),
            intersection.y() - node.y(),
        )
        # Avoid pathological miters on near-parallel or extremely acute turns.
        miter_limit = max(3.0 * abs(signed_offset), 5.0 * max(spacing, 0.01))
        if distance <= miter_limit:
            return intersection

    # Bevel fallback: still stays entirely on the same offset lane and never
    # returns to the original Pole Edge corner.
    prev_t = _base._unit(prev_a, prev_b)
    next_t = _base._unit(next_a, next_b)
    p_prev = _base._offset_point(node, prev_t, signed_offset)
    p_next = _base._offset_point(node, next_t, signed_offset)
    return (p_prev, p_next)


def _append(result, point, eps=1e-7):
    point = QgsPointXY(point)
    if not result or hypot(
        result[-1].x() - point.x(), result[-1].y() - point.y()
    ) > eps:
        result.append(point)


def _replace_last(result, point):
    point = QgsPointXY(point)
    if result:
        result[-1] = point
    else:
        result.append(point)


def _build_continuous_segment_points(
    segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs
):
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [
        parsed for raw in raw_edges if (parsed := _base._canonical_edge(raw))
    ]
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [
            _base._transform_point(
                QgsPointXY(float(raw[0]), float(raw[1])),
                source_crs,
                work_crs,
            )
            for raw in stored
            if len(raw) >= 2
        ]

    work_stored = [
        _base._transform_point(
            QgsPointXY(float(raw[0]), float(raw[1])),
            source_crs,
            work_crs,
        )
        for raw in stored
        if len(raw) >= 2
    ]
    if len(work_stored) < 2:
        return work_stored

    nodes = _base._extract_route_graph_nodes(
        segment, work_crs, source_crs, edge_crs
    )
    if len(nodes) != len(edges) + 1:
        return work_stored

    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slot != 0 for slot in slots):
        return work_stored

    result = []
    start_point = work_stored[0]
    end_point = work_stored[-1]
    first_slot = slots[0]
    first_a, first_b = nodes[0], nodes[1]
    first_len = hypot(first_b.x() - first_a.x(), first_b.y() - first_a.y())

    if first_slot == 0:
        _append(result, start_point)
        _append(result, first_a)
    else:
        _append(result, start_point)
        angle = _base._transition_angle(first_slot)
        run = min(
            _base._transition_run(abs(first_slot) * spacing, angle),
            first_len * 0.45,
        )
        fraction = run / first_len if first_len > 1e-12 else 0.0
        center = _base._point_along(first_a, first_b, fraction)
        _append(
            result,
            _base._offset_point(
                center,
                _base._unit(first_a, first_b),
                first_slot * spacing,
            ),
        )

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
                    join = _corner_join(
                        a,
                        prev_a,
                        prev_b,
                        a,
                        b,
                        slot * spacing,
                        spacing,
                    )
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
                _replace_last(
                    result,
                    _base._offset_point(p, prev_t, prev_slot * spacing),
                )
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
                p1 = _base._offset_point(
                    _base._point_along(prev_a, prev_b, frac_prev),
                    prev_t,
                    prev_slot * spacing,
                )
                p2 = _base._offset_point(
                    _base._point_along(a, b, frac_next),
                    t,
                    slot * spacing,
                )
                _replace_last(result, p1)
                _append(result, p2)

        if i < len(edges) - 1:
            if slots[i] == 0:
                _append(result, b)
            else:
                # The next edge will replace this endpoint with the proper
                # corner join when it has the same lane.
                _append(result, offset_b)
        elif slot == 0:
            _append(result, b)
            _append(result, end_point)
        else:
            final_len = hypot(b.x() - a.x(), b.y() - a.y())
            angle = _base._transition_angle(slot)
            run = min(
                _base._transition_run(abs(slot) * spacing, angle),
                final_len * 0.45,
            )
            fraction = 1.0 - run / final_len if final_len > 1e-12 else 0.5
            exit_center = _base._point_along(a, b, fraction)
            exit_point = _base._offset_point(
                exit_center,
                t,
                slot * spacing,
            )
            _replace_last(result, exit_point)
            _append(result, end_point)

    return result


def make_build_wrapper(original):
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        try:
            return _build_continuous_segment_points(
                segment,
                slots_by_edge,
                spacing,
                work_crs,
                source_crs,
                edge_crs,
            )
        except Exception:
            return original(
                segment,
                slots_by_edge,
                spacing,
                work_crs,
                source_crs,
                edge_crs,
            )

    return build
