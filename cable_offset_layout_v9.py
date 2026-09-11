# -*- coding: utf-8 -*-
"""Global lane priority v9 and route-aware continuous corner geometry.

Geometry rules:
- 0.50 m is the physical separation between parallel cable lanes.
- Ordinary Pole Edge corners are built from the offset lanes themselves.
- The 0.30 m control distance is NOT used for ordinary corners or lane
  transitions. It belongs to same-point multi-cable fan-out cases such as
  FDT/BB/CL and FAT return-cable takeoff, which are handled by the endpoint
  writer/landing logic rather than the Pole Edge corner builder.
- A same-lane corner stays continuous and never backtracks through the
  original Pole Edge node.
- Lane placement is based on geometric lane index, not Link number.
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
                if angle >= 25.0:
                    longest_run = max(longest_run, current_run)
                    current_run = 0.0
    longest_run = max(longest_run, current_run)

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
            start_point = _base._transform_point(
                QgsPointXY(float(points[0][0]), float(points[0][1])), edge_crs, work_crs
            )
            end_point = _base._transform_point(
                QgsPointXY(float(points[-1][0]), float(points[-1][1])), edge_crs, work_crs
            )
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
        reserved = _reserved_for_edge(
            edge_key, edge_crs, work_crs, spacing,
            existing_index, existing_geometries, reserved_cache
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

    def assign(edge_users, edge_reserved, previous_slots):
        counters["edges"] += 1
        counters["overlap_edges"] += int(len(edge_users) > 1 or bool(edge_reserved))
        assigned = {}
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

    return assign, {
        "routes": routes,
        "ordered_designs": ordered_designs,
        "slot_map": slot_map,
    }


def make_build_wrapper(original, control_distance_m=0.30):
    """Return the continuous lane builder.

    `control_distance_m` is intentionally accepted for compatibility with the
    existing v5 caller, but it is not used here. Ordinary corners are not
    control-distance transitions; they are pure 0.50 m lane-offset geometry.
    """
    def build(segment, slots_by_edge, spacing, work_crs, source_crs, edge_crs):
        result = _v8.build_continuous_segment_points(
            segment,
            slots_by_edge,
            spacing,
            work_crs,
            source_crs,
            edge_crs,
        )
        return result

    return build
