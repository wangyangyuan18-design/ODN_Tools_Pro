# -*- coding: utf-8 -*-
"""Global lane priority v9 and route-aware corner transitions.

Rules implemented from the ODN layout discussion:
- Main lane is chosen by directional continuity, not by Link length alone.
- The longest straight run is the strongest priority signal; route length is
  secondary. Turns and reversals reduce priority.
- Shared Pole Edge lanes remain globally consistent for the complete Link set.
- Same-lane corners use a continuous parallel offset join.
- Lane changes are directional: when moving toward the main lane, transition
  can start before the Pole Edge node; when moving outward, transition starts
  after the node. This prevents unnecessary saw-tooth geometry.
- 0.50 m is lane spacing; 0.30 m is only the transition/control distance.
"""

from math import acos, degrees, hypot

from qgis.core import QgsGeometry, QgsPointXY

from . import cable_offset_layout as _base
from . import cable_offset_layout_v8 as _v8


def _unit(a, b):
    return _base._unit(a, b)


def _turn_angle(a, b, c):
    v1 = _unit(a, b)
    v2 = _unit(b, c)
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1]))
    return degrees(acos(dot))


def _route_metrics(design_index, design, edge_crs, work_crs):
    total = 0.0
    longest_run = 0.0
    current_run = 0.0
    turn_sum = 0.0
    turn_count = 0
    reversal_count = 0
    edge_count = 0
    seen = set()

    for segment in design.get("segments", []) or []:
        raw_edges = segment.get("edge_sequence", []) or []
        edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
        if not edges:
            continue
        nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
        if len(nodes) != len(edges) + 1:
            continue
        for i, edge in enumerate(edges):
            a, b = _base._edge_points(edge)
            a = _base._transform_point(a, edge_crs, work_crs)
            b = _base._transform_point(b, edge_crs, work_crs)
            length = hypot(b.x() - a.x(), b.y() - a.y())
            edge_count += 1
            if edge not in seen:
                total += length
                seen.add(edge)
            current_run += length
            if i + 1 < len(edges):
                angle = _turn_angle(nodes[i], nodes[i + 1], nodes[i + 2])
                turn_count += 1
                turn_sum += angle
                if angle >= 135.0:
                    reversal_count += 1
                # A turn breaks the directional main-run. A very small bend is
                # treated as continuation so road-following routes stay strong.
                if angle >= 25.0:
                    longest_run = max(longest_run, current_run)
                    current_run = 0.0
    longest_run = max(longest_run, current_run)

    # Directional continuity is deliberately stronger than total length.
    # This is what lets a shorter, cleaner L2 outrank a longer L1 that turns
    # away and comes back. The route length remains a secondary tie-breaker.
    score = (
        longest_run * 1000.0
        + total * 10.0
        - turn_count * 150.0
        - reversal_count * 500.0
    )
    return {
        "route_length": total,
        "longest_directional_run": longest_run,
        "turn_count": turn_count,
        "turn_sum": turn_sum,
        "reversal_count": reversal_count,
        "edge_count": edge_count,
        "priority_score": score,
    }


def _reserved_for_edge(edge_key, edge_crs, work_crs, spacing, existing_index, existing_geometries, cache):
    if edge_key in cache:
        return set(cache[edge_key])
    a, b = _base._edge_points(edge_key)
    a = _base._transform_point(a, edge_crs, work_crs)
    b = _base._transform_point(b, edge_crs, work_crs)
    reserved = _base._existing_slot_occupancy(
        QgsGeometry.fromPolylineXY([a, b]), spacing, existing_index, existing_geometries
    )
    cache[edge_key] = set(reserved)
    return set(reserved)


def _collect_uses(designs, edge_crs, work_crs):
    pending = []
    routes = {}
    for design_index, design in enumerate(designs or []):
        if design.get("written") and not design.get("needs_resync"):
            continue
        metrics = _route_metrics(design_index, design, edge_crs, work_crs)
        for segment_index, segment in enumerate(design.get("segments", []) or []):
            raw_edges = segment.get("edge_sequence", []) or []
            edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
            points = segment.get("points", []) or []
            if not edges or len(points) < 2:
                continue
            nodes = _base._extract_route_graph_nodes(segment, work_crs, edge_crs, edge_crs)
            if len(nodes) != len(edges) + 1:
                continue
            start_point = _base._transform_point(QgsPointXY(float(points[0][0]), float(points[0][1])), edge_crs, work_crs)
            end_point = _base._transform_point(QgsPointXY(float(points[-1][0]), float(points[-1][1])), edge_crs, work_crs)
            for edge_index, edge in enumerate(edges):
                a, b = _base._edge_points(edge)
                a = _base._transform_point(a, edge_crs, work_crs)
                b = _base._transform_point(b, edge_crs, work_crs)
                use = _base._LayoutUse(
                    design_index, segment_index, edge_index, edge,
                    start_point, end_point,
                    _base._route_side_hint(start_point, end_point, a, b),
                )
                if edge_index > 0:
                    use.prev_edge_key = edges[edge_index - 1]
                pending.append(use)
        routes[design_index] = metrics
    return pending, routes


def _packed_lane_candidates(side_hint, limit=20):
    yield 0
    for magnitude in range(1, limit + 1):
        if side_hint < 0:
            yield -magnitude
            yield magnitude
        else:
            yield magnitude
            yield -magnitude


def make_global_slot_assigner(designs, distribution_layer, edge_layer, spacing, counters):
    edge_crs = edge_layer.crs()
    work_crs = _base._choose_work_crs(edge_layer)
    pending, routes = _collect_uses(designs, edge_crs, work_crs)
    users_by_edge = {}
    for use in pending:
        users_by_edge.setdefault(use.edge_key, []).append(use)

    ordered_designs = sorted(
        routes,
        key=lambda di: (
            -float(routes[di].get("priority_score", 0.0)),
            -float(routes[di].get("longest_directional_run", 0.0)),
            -float(routes[di].get("route_length", 0.0)),
            int(di),
        ),
    )
    priority_by_design = {di: (rank, di) for rank, di in enumerate(ordered_designs)}

    existing_index, existing_geometries = _base._build_existing_index(distribution_layer, work_crs)
    reserved_cache = {}
    slot_map = {}

    for edge_key, users in users_by_edge.items():
        reserved = _reserved_for_edge(edge_key, edge_crs, work_crs, spacing, existing_index, existing_geometries, reserved_cache)
        used = set(reserved)
        for use in sorted(users, key=lambda item: priority_by_design.get(item.design_index, (float("inf"), item.design_index))):
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

    def assign(edge_users, edge_reserved, previous_slots):
        counters["edges"] += 1
        counters["overlap_edges"] += int(len(edge_users) > 1 or bool(edge_reserved))
        assigned = {}
        for use in sorted(edge_users, key=lambda item: priority_by_design.get(item.design_index, (float("inf"), item.design_index))):
            key = (use.design_index, use.segment_index, use.edge_index)
            slot = int(slot_map.get(key, 0))
            use.slot = slot
            assigned[use.design_index] = slot
            counters["slots"][slot] = counters["slots"].get(slot, 0) + 1
        return assigned

    return assign, {
        "routes": routes,
        "ordered_designs": ordered_designs,
        "slot_map": slot_map,
    }


def _append(result, point, eps=1e-7):
    p = QgsPointXY(point)
    if not result or hypot(result[-1].x() - p.x(), result[-1].y() - p.y()) > eps:
        result.append(p)


def _replace_last(result, point):
    p = QgsPointXY(point)
    if result:
        result[-1] = p
    else:
        result.append(p)


def _transition_point(a, b, slot, spacing, control_distance, from_node=True):
    length = hypot(b.x() - a.x(), b.y() - a.y())
    if slot == 0 or length <= 1e-12:
        return QgsPointXY(a if from_node else b)
    run = min(abs(slot) * float(spacing) / max(1e-12, abs(slot) * float(spacing) / max(float(control_distance), 1e-12)), length * 0.45)
    # The expression above simplifies to control_distance; keeping the
    # explicit form documents that control distance is independent of spacing.
    run = min(float(control_distance), length * 0.45)
    center = _base._point_along(a, b, run / length if from_node else 1.0 - run / length)
    return _base._offset_point(center, _base._unit(a, b), slot * spacing)


def _build_continuous_segment_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
    raw_edges = segment.get("edge_sequence", []) or []
    edges = [e for raw in raw_edges if (e := _base._canonical_edge(raw))]
    stored = segment.get("points", []) or []
    if not edges or len(stored) < 2:
        return [_base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs) for p in stored if len(p) >= 2]
    nodes = _base._extract_route_graph_nodes(segment, work_crs, source_crs, edge_crs)
    if len(nodes) != len(edges) + 1:
        raise RuntimeError("v9: route nodes 与 edge_sequence 不一致")
    stored_work = [_base._transform_point(QgsPointXY(float(p[0]), float(p[1])), source_crs, work_crs) for p in stored if len(p) >= 2]
    slots = [int(slots_by_edge.get(i, 0)) for i in range(len(edges))]
    if not any(slots):
        return stored_work

    # v8 supplies the robust same-lane miter/bevel join. v9 replaces only the
    # lane-change timing so D/E/F-type corners are directional instead of
    # symmetric saw-tooth transitions.
    result = []
    start_point, end_point = stored_work[0], stored_work[-1]
    control_distance = 0.30
    first_slot = slots[0]
    first_a, first_b = nodes[0], nodes[1]
    first_len = hypot(first_b.x() - first_a.x(), first_b.y() - first_a.y())
    _append(result, start_point)
    if first_slot == 0:
        _append(result, first_a)
    else:
        run = min(control_distance, first_len * 0.45)
        center = _base._point_along(first_a, first_b, run / first_len if first_len > 1e-12 else 0.0)
        _append(result, _base._offset_point(center, _base._unit(first_a, first_b), first_slot * spacing))

    for i, (a, b) in enumerate(zip(nodes[:-1], nodes[1:])):
        slot = slots[i]
        t = _base._unit(a, b)
        if i > 0:
            prev_slot = slots[i - 1]
            prev_a, prev_b = nodes[i - 1], nodes[i]
            prev_t = _base._unit(prev_a, prev_b)
            if prev_slot == slot:
                if slot == 0:
                    _replace_last(result, a)
                else:
                    join = _v8._same_lane_join(a, prev_a, prev_b, a, b, slot, spacing)
                    if isinstance(join, tuple):
                        _replace_last(result, join[0])
                        _append(result, join[1])
                    else:
                        _replace_last(result, join)
            else:
                # If the new lane is closer to the main lane, start the
                # transition before the node (D-type). If it moves outward,
                # cross the node first and transition on the outgoing edge
                # (E/F-type). This keeps the main route visually dominant.
                prev_mag = abs(prev_slot)
                new_mag = abs(slot)
                if new_mag < prev_mag and prev_slot != 0:
                    run = min(control_distance, hypot(a.x() - prev_a.x(), a.y() - prev_a.y()) * 0.45)
                    center = _base._point_along(prev_a, prev_b, max(0.0, 1.0 - run / max(hypot(prev_b.x()-prev_a.x(), prev_b.y()-prev_a.y()), 1e-12)))
                    p_old = _base._offset_point(center, prev_t, prev_slot * spacing)
                    _replace_last(result, p_old)
                    _append(result, a)
                    run2 = min(control_distance, hypot(b.x()-a.x(), b.y()-a.y()) * 0.45)
                    center2 = _base._point_along(a, b, run2 / max(hypot(b.x()-a.x(), b.y()-a.y()), 1e-12))
                    _append(result, _base._offset_point(center2, t, slot * spacing))
                else:
                    # Outward or cross-side transition: reach the corner on
                    # the old lane, then change lane after the node.
                    _append(result, _base._offset_point(a, prev_t, prev_slot * spacing) if prev_slot else a)
                    if slot:
                        run = min(control_distance, hypot(b.x()-a.x(), b.y()-a.y()) * 0.45)
                        center = _base._point_along(a, b, run / max(hypot(b.x()-a.x(), b.y()-a.y()), 1e-12))
                        _append(result, _base._offset_point(center, t, slot * spacing))
                    else:
                        _append(result, a)

        if i == len(edges) - 1:
            if slot == 0:
                _append(result, b)
            else:
                run = min(control_distance, hypot(b.x()-a.x(), b.y()-a.y()) * 0.45)
                center = _base._point_along(a, b, max(0.0, 1.0-run/max(hypot(b.x()-a.x(), b.y()-a.y()),1e-12)))
                _append(result, _base._offset_point(center, t, slot * spacing))
            _append(result, end_point)
        elif slot == 0:
            _append(result, b)
        else:
            _append(result, _base._offset_point(b, t, slot * spacing))

    return result


def make_build_wrapper(original):
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        return _build_continuous_segment_points(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs)
    return build
